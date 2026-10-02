import asyncio
import html
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

from app.config import Settings
from app.models import Metrics, Video
from app.network import retry

log = logging.getLogger(__name__)
VIDEO_URL = re.compile(r"https://www\.tiktok\.com/@[\w.\-]+/video/\d+")


class ProviderUnavailable(RuntimeError):
    pass


def canonical_url(url: str) -> str:
    match = VIDEO_URL.fullmatch(url.split("?")[0].rstrip("/"))
    if not match:
        raise ValueError("Нужна полная публичная https://www.tiktok.com/@author/video/ID ссылка")
    return match[0]


def parse_items(data: object) -> list[Video]:
    """Read ordinary page hydration or page-generated search JSON, without private APIs."""
    stack = [data]
    found: dict[str, Video] = {}
    visited = 0
    while stack and visited < 20000:
        obj = stack.pop()
        visited += 1
        if isinstance(obj, list):
            stack.extend(obj)
            continue
        if not isinstance(obj, dict):
            continue
        stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
        if not (obj.get("id") and obj.get("createTime") and obj.get("stats") is not None):
            continue
        try:
            author = obj.get("author") or {}
            username = author.get("uniqueId", "") if isinstance(author, dict) else str(author)
            vid = str(obj["id"])
            if not vid.isdigit() or not username:
                continue
            stats = obj.get("statsV2") or obj["stats"]
            metrics = Metrics(
                *(
                    min(2**63 - 1, max(0, int(stats.get(k, 0))))
                    for k in ("playCount", "diggCount", "shareCount", "commentCount")
                )
            )
            caption = str(obj.get("desc", ""))[:4000]
            hashtags = list(dict.fromkeys(re.findall(r"#([\w]+)", caption)))
            for tag in obj.get("challenges") or []:
                if isinstance(tag, dict) and tag.get("title"):
                    hashtags.append(tag["title"])
            media = obj.get("video") or {}
            tracks = [
                {"url": t["Url"], "language": t.get("LanguageCodeName", "")}
                for t in media.get("subtitleInfos", [])
                if isinstance(t, dict) and t.get("Url")
            ]
            address = media.get("downloadAddr") or media.get("playAddr") or ""
            if isinstance(address, dict):
                address = (address.get("urlList") or [""])[0]
            follower_count = (obj.get("authorStats") or {}).get("followerCount")
            found[vid] = Video(
                id=vid,
                url=f"https://www.tiktok.com/@{username}/video/{vid}",
                published_at=float(obj["createTime"]),
                caption=caption,
                hashtags=list(dict.fromkeys(hashtags)),
                author=username,
                followers=int(follower_count) if follower_count is not None else None,
                metrics=metrics,
                subtitle_tracks=tracks[:3],
                download_url=address if isinstance(address, str) else "",
            )
        except (ValueError, TypeError, AttributeError):
            log.debug("provider_item_invalid")
    return list(found.values())


def parse_html(content: str) -> list[Video]:
    videos: dict[str, Video] = {}
    for payload in re.findall(
        r'<script[^>]+id=["\'](?:__UNIVERSAL_DATA_FOR_REHYDRATION__|SIGI_STATE)["\'][^>]*>'
        r"(.*?)</script>",
        content,
        re.S,
    ):
        try:
            for video in parse_items(json.loads(html.unescape(payload))):
                videos[video.id] = video
        except json.JSONDecodeError:
            log.debug("provider_hydration_invalid")
    return list(videos.values())


class PublicTikTokProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.request_timeout, follow_redirects=False
        )
        self._runtime = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()
        self._search_failed = False
        self._failed_kinds: set[str] = set()
        self._seed_cache: list[Video] | None = None
        self.health = "Ещё не проверен"

    async def begin_scan(self) -> None:
        self._search_failed = False
        self._failed_kinds.clear()
        self._seed_cache = None

    async def end_scan(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._runtime:
            await self._runtime.stop()
        self._runtime = self._browser = self._context = None

    async def _page_items(self, url: str) -> list[Video]:
        from playwright.async_api import async_playwright

        async with self._lock:
            if not self._browser:
                self._runtime = await async_playwright().start()
                self._browser = await self._runtime.chromium.launch(headless=True)
                self._context = await self._browser.new_context(locale="ru-RU")
                await self._context.route("**/*", self._route)
            page = await self._context.new_page()
            responses = []

            def capture(response):
                if (
                    "/api/" in response.url
                    and any(kind in response.url for kind in ("search", "item_list", "detail"))
                    and response.status == 200
                    and len(responses) < 5
                ):
                    responses.append(response)

            page.on("response", capture)
            try:
                async with asyncio.timeout(self.settings.request_timeout):
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2500)
                    body = (await page.locator("body").inner_text())[:10000].lower()
                    if any(
                        t in body
                        for t in (
                            "verify to continue",
                            "captcha",
                            "проверку безопасности",
                            "drag the slider",
                            "access denied",
                        )
                    ):
                        raise ProviderUnavailable("TikTok требует проверку: обход отключён")
                    found = {v.id: v for v in parse_html(await page.content())}
                    for response in responses:
                        payload = await response.text()
                        if len(payload) <= 4_000_000:
                            try:
                                data = json.loads(payload)
                            except json.JSONDecodeError:
                                log.debug("provider_response_not_json")
                                continue
                            for video in parse_items(data):
                                found[video.id] = video
                    if not found:
                        raise ProviderUnavailable(
                            "Публичная страница не отдала metadata/search JSON"
                        )
                    return list(found.values())
            finally:
                await page.close()

    async def _route(self, route) -> None:
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    async def metadata(self, url: str) -> Video:
        url = canonical_url(url)

        async def fetch():
            response = await self.client.get(url)
            response.raise_for_status()
            return response.text

        try:
            found = parse_html(await retry(fetch))
        except httpx.HTTPError as error:
            log.warning("provider_metadata_http_failure type=%s", type(error).__name__)
            found = []
        if not found and self.settings.browser_enabled:
            found = await self._page_items(url)
        wanted = url.rsplit("/", 1)[-1]
        for video in found:
            if video.id == wanted:
                return video
        raise ProviderUnavailable("Публичные metadata видео недоступны")

    async def _seeds(self) -> list[Video]:
        if self._seed_cache is not None:
            return self._seed_cache
        self._seed_cache = []
        path = self.settings.seed_urls_file
        if not path.exists():
            return []
        urls = list(
            dict.fromkeys(
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            )
        )
        for url in urls[: self.settings.max_candidates]:
            try:
                self._seed_cache.append(await self.metadata(url))
            except Exception as error:
                log.warning("seed_metadata_failure type=%s", type(error).__name__)
            await asyncio.sleep(self.settings.request_delay_seconds)
        return self._seed_cache

    async def search(self, query: str, limit: int) -> list[Video]:
        found = []
        kind = "tag" if query.startswith("#") else "search"
        if (
            self.settings.browser_enabled
            and not self._search_failed
            and kind not in self._failed_kinds
        ):
            url = (
                f"https://www.tiktok.com/tag/{quote(query[1:])}"
                if query.startswith("#")
                else f"https://www.tiktok.com/search?q={quote(query)}"
            )
            try:
                found = await self._page_items(url)
                self.health = "Публичный поиск доступен"
            except Exception as error:
                self._failed_kinds.add(kind)
                if isinstance(error, ProviderUnavailable) and "обход отключён" in str(error):
                    self._search_failed = True
                reason = (
                    str(error) if isinstance(error, ProviderUnavailable) else type(error).__name__
                )
                self.health = f"Поиск недоступен: {reason}; резерв: seed_urls.txt"
                log.warning("provider_search_unavailable type=%s", type(error).__name__)
        elif not self.settings.browser_enabled:
            self.health = "Поиск отключён; резерв: seed_urls.txt"
        seeds = await self._seeds()
        if not found and not seeds:
            raise ProviderUnavailable(self.health + "; резервных результатов нет")
        # Seed URLs are monitored regardless of the search query, deduplicated by scanner.
        return list({v.id: v for v in found[:limit] + seeds}.values())

    async def download(self, video: Video, destination: Path) -> None:
        # Refresh expiring CDN URL only for selected TOP videos.
        fresh = await self.metadata(video.url)
        address = fresh.download_url
        temporary = destination.with_suffix(".part")

        async def transfer():
            size = 0
            current_url = address
            for _ in range(5):
                host = urlparse(current_url).hostname or ""
                if urlparse(current_url).scheme != "https" or not any(
                    host == suffix or host.endswith("." + suffix)
                    for suffix in ("tiktok.com", "tiktokcdn.com", "tiktokv.com", "byteoversea.com")
                ):
                    raise ProviderUnavailable("Не найден публичный HTTPS TikTok CDN MP4 URL")
                async with self.client.stream(
                    "GET", current_url, headers={"Referer": video.url}, follow_redirects=False
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        current_url = urljoin(current_url, response.headers.get("location", ""))
                        continue
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        async for chunk in response.aiter_bytes(65536):
                            size += len(chunk)
                            if size > self.settings.max_media_mb * 1_000_000:
                                raise ProviderUnavailable("MP4 превышает лимит Telegram")
                            output.write(chunk)
                    break
            else:
                raise ProviderUnavailable("Слишком много CDN перенаправлений")
            with temporary.open("rb") as source:
                header = source.read(32)
            if size < 32 or b"ftyp" not in header:
                raise ProviderUnavailable("CDN вернул файл, который не является MP4")
            temporary.replace(destination)

        try:
            async with asyncio.timeout(self.settings.download_timeout):
                await retry(transfer)
        finally:
            temporary.unlink(missing_ok=True)

    async def get_subtitles(self, video: Video) -> str:
        texts = []
        for track in sorted(
            video.subtitle_tracks, key=lambda t: not t["language"].startswith("ru")
        )[:2]:
            url = track["url"]
            host = urlparse(url).hostname or ""
            if urlparse(url).scheme != "https" or not any(
                host == suffix or host.endswith("." + suffix)
                for suffix in ("tiktok.com", "tiktokcdn.com", "tiktokv.com", "byteoversea.com")
            ):
                continue

            async def fetch(subtitle_url: str = url):
                content = bytearray()
                async with self.client.stream("GET", subtitle_url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(65536):
                        content.extend(chunk)
                        if len(content) > 512000:
                            raise ProviderUnavailable("Слишком большой файл субтитров")
                return content.decode("utf-8", errors="replace")

            async with asyncio.timeout(self.settings.request_timeout):
                text = await retry(fetch)
            try:
                data = json.loads(text)
                text = "\n".join(line.get("text", "") for line in data.get("utterances", []))
            except (json.JSONDecodeError, AttributeError):
                pass
            texts.append(text[:20000])
        return "\n".join(texts)

    async def close(self) -> None:
        await self.end_scan()
        await self.client.aclose()
