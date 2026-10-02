import asyncio
import html
import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx

from app.config import Settings
from app.models import Metrics, Video
from app.network import retry

log = logging.getLogger(__name__)
VIDEO_URL = re.compile(r"https://www\.tiktok\.com/@[\w.\-]+/video/\d+")


class ProviderUnavailable(RuntimeError):
    pass


class ProviderAccessBlocked(ProviderUnavailable):
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
        self._success_kinds: set[str] = set()
        self._failure_reasons: dict[str, str] = {}
        self.health = "Ещё не проверен"
        self.search_reports: list[dict] = []

    @property
    def search_pages(self) -> int:
        return sum(report["pages"] for report in self.search_reports)

    @property
    def search_limited(self) -> bool:
        return any(
            report["stop_reason"]
            in ("login_required", "captcha", "no_progress", "timeout", "page_error")
            for report in self.search_reports
        )

    @property
    def depth_health(self) -> str:
        reasons = [report["stop_reason"] for report in self.search_reports]
        parts = [f"Страниц: {self.search_pages}"]
        labels = {
            "login_required": "для продолжения требуется вход",
            "captcha": "проверка доступа",
            "no_progress": "прокрутка не дала новых видео",
            "timeout": "достигнут timeout",
            "page_error": "ошибка страницы",
            "page_limit": "достигнут лимит страниц",
            "item_limit": "достигнут лимит видео",
        }
        for reason, label in labels.items():
            count = reasons.count(reason)
            if count:
                parts.append(f"{label}: {count} запросов")
        return "; ".join(parts)

    @property
    def search_available(self) -> bool:
        return bool(self._success_kinds) and not self._search_failed

    async def begin_scan(self) -> None:
        self._search_failed = False
        self._success_kinds.clear()
        self._failure_reasons.clear()
        self.search_reports.clear()

    async def end_scan(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._runtime:
            await self._runtime.stop()
        self._runtime = self._browser = self._context = None

    async def _page_items(self, url: str, *, limit: int | None = None) -> list[Video]:
        from playwright.async_api import Error as BrowserError
        from playwright.async_api import async_playwright

        async with self._lock:
            fresh_session = self._browser is None
            if not self._browser:
                self._runtime = await async_playwright().start()
                self._browser = await self._runtime.chromium.launch(headless=True)
                options = {"locale": "ru-RU"}
                if self.settings.tiktok_storage_state:
                    options["storage_state"] = self.settings.tiktok_storage_state
                self._context = await self._browser.new_context(**options)
                await self._context.route("**/*", self._route)
            page = await self._context.new_page()
            responses = deque()
            found: dict[str, Video] = {}
            report = {
                "query": parse_qs(urlparse(url).query).get("q", [""])[0],
                "pages": 0,
                "collected": 0,
                "fresh": 0,
                "stop_reason": "cancelled",
            }
            has_more = None
            complete = False
            response_pages = 0

            def capture(response):
                if (
                    any(
                        path in response.url
                        for path in (
                            "/api/search/",
                            "/api/challenge/item_list/",
                            "/api/item/detail/",
                        )
                    )
                    and urlparse(response.url).hostname == "www.tiktok.com"
                    and response.status == 200
                    and len(responses) < 32
                ):
                    responses.append(response)

            async def read_responses():
                nonlocal complete, has_more, response_pages
                while responses:
                    response = responses.popleft()
                    payload = await response.text()
                    if len(payload) > 4_000_000:
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        log.debug("provider_response_not_json")
                        continue
                    items = parse_items(data)
                    is_batch = (
                        urlparse(response.url).path.rstrip("/")
                        in ("/api/search/general/full", "/api/search/item/full")
                        and isinstance(data, dict)
                        and data.get("status_code") == 0
                        and any(
                            isinstance(data.get(key), list)
                            for key in ("data", "item_list", "itemList")
                        )
                    )
                    if is_batch:
                        complete = True
                        response_pages += 1
                        report["pages"] = response_pages
                        if data.get("has_more") in (False, True, 0, 1):
                            has_more = bool(data["has_more"])
                    for video in items:
                        found[video.id] = video

            page.on("response", capture)
            try:
                budget = (
                    self.settings.search_query_timeout if limit else self.settings.request_timeout
                )
                async with asyncio.timeout(budget):
                    if fresh_session:
                        # Visit the public entry page before navigating to search, as a browser
                        # normally does. No injected cookies, signatures or login state.
                        await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")
                        await page.wait_for_timeout(1500)
                        await self._check_access(page)
                    await page.goto(url, wait_until="domcontentloaded")
                    found.update({v.id: v for v in parse_html(await page.content())})
                    deadline = asyncio.get_running_loop().time() + self.settings.search_wait_seconds
                    while True:
                        await self._check_access(page)
                        await read_responses()
                        if found or complete:
                            break
                        if asyncio.get_running_loop().time() >= deadline:
                            raise ProviderUnavailable(
                                "Публичная страница не отдала metadata/search JSON после ожидания"
                            )
                        # Wait for actual page responses instead of a single short fixed sleep.
                        await page.wait_for_timeout(250)
                    if limit is None:
                        return list(found.values())
                    report["pages"] = max(1, report["pages"])
                    if not found and has_more is not True:
                        report["stop_reason"] = "empty"
                        return []
                    while True:
                        if len(found) >= limit:
                            report["stop_reason"] = "item_limit"
                            break
                        if has_more is False:
                            report["stop_reason"] = "no_more"
                            break
                        if report["pages"] >= self.settings.search_max_pages:
                            report["stop_reason"] = "page_limit"
                            break
                        # Let the page render before interacting. Only visible controls and
                        # ordinary wheel scrolling are used; login dialogs are not dismissed.
                        await page.wait_for_timeout(
                            max(250, self.settings.request_delay_seconds * 1000)
                        )
                        await self._check_access(page)
                        if await self._login_required(page):
                            report["stop_reason"] = "login_required"
                            break
                        before = len(found)
                        load_more = page.locator('[data-e2e="search-load-more"]')
                        if await load_more.is_visible() and await load_more.is_enabled():
                            await load_more.click(timeout=2000)
                        else:
                            viewport = page.viewport_size or {"width": 1280, "height": 720}
                            await page.mouse.move(viewport["width"] * 0.7, viewport["height"] * 0.6)
                            await page.mouse.wheel(0, 1800)
                        deadline = (
                            asyncio.get_running_loop().time()
                            + self.settings.search_page_wait_seconds
                        )
                        while asyncio.get_running_loop().time() < deadline:
                            await page.wait_for_timeout(250)
                            await self._check_access(page)
                            await read_responses()
                            if len(found) > before or has_more is False:
                                break
                            if await self._login_required(page):
                                report["stop_reason"] = "login_required"
                                break
                        if report["stop_reason"] == "login_required":
                            break
                        if len(found) == before:
                            report["stop_reason"] = (
                                "no_more" if has_more is False else "no_progress"
                            )
                            break
                    return list(found.values())
            except ProviderAccessBlocked:
                report["stop_reason"] = "captcha"
                self._search_failed = True
                if limit is not None and found:
                    return list(found.values())
                raise
            except (TimeoutError, BrowserError) as error:
                report["stop_reason"] = (
                    "timeout" if isinstance(error, TimeoutError) else "page_error"
                )
                if limit is not None and found:
                    log.warning("provider_pagination_partial type=%s", type(error).__name__)
                    return list(found.values())
                raise
            except ProviderUnavailable:
                report["stop_reason"] = "unavailable"
                raise
            finally:
                if limit is not None:
                    report["collected"] = len(found)
                    report["fresh"] = sum(
                        0
                        <= time.time() - v.published_at
                        <= self.settings.max_video_age_hours * 3600
                        for v in found.values()
                    )
                    self.search_reports.append(report)
                    log.info("provider_search_depth %s", report)
                await page.close()

    async def _login_required(self, page) -> bool:
        if await page.locator('[data-e2e="login-modal"]').is_visible():
            return True
        body = (await page.locator("body").inner_text())[:10000].lower()
        return any(
            text in body
            for text in (
                "log in to see more search results",
                "войдите, чтобы увидеть больше результатов",
            )
        )

    async def _check_access(self, page) -> None:
        body = (await page.locator("body").inner_text())[:10000].lower()
        if any(
            t in body
            for t in (
                "verify to continue",
                "captcha",
                "проверку безопасности",
                "drag the slider",
                "передвиньте ползунок",
                "access denied",
            )
        ):
            raise ProviderAccessBlocked("TikTok требует проверку: обход отключён")

    def _update_health(self) -> None:
        labels = {"search": "запросы", "tag": "хэштеги"}
        if self._success_kinds:
            available = ", ".join(labels[k] for k in sorted(self._success_kinds))
            self.health = f"Автоматический поиск доступен: {available}; анонимная выдача ограничена"
            if self.settings.tiktok_storage_state:
                self.health = (
                    f"Автоматический поиск доступен: {available}; используется ваша сессия"
                )
            if any(r["stop_reason"] == "login_required" for r in self.search_reports):
                self.health = (
                    f"Поиск доступен: {available}; продолжение выдачи требует входа в TikTok"
                )
            if self._failure_reasons:
                failed = ", ".join(labels[k] for k in sorted(self._failure_reasons))
                self.health += f"; не выполнена часть запросов: {failed}"
        elif self._failure_reasons:
            reason = next(iter(self._failure_reasons.values()))
            self.health = f"Автоматический поиск недоступен: {reason}; повтор на следующем цикле"
        if self._search_failed:
            self.health = "TikTok остановил поиск проверкой доступа; повтор на следующем цикле"

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

    async def search(self, query: str, limit: int) -> list[Video]:
        kind = "tag" if query.startswith("#") else "search"
        if not self.settings.browser_enabled:
            self.health = "Автоматический поиск отключён: требуется BROWSER_ENABLED=true"
            raise ProviderUnavailable(self.health)
        if self._search_failed:
            raise ProviderAccessBlocked(self.health)
        # Hashtags use the ordinary public search box too. The /tag page currently
        # fails anonymously and is not necessary for searching a literal hashtag.
        try:
            found = await self._page_items(
                f"https://www.tiktok.com/search?q={quote(query)}", limit=limit
            )
            self._success_kinds.add(kind)
            self._failure_reasons.pop(kind, None)
            self._update_health()
            return sorted(found, key=lambda video: video.published_at, reverse=True)[:limit]
        except Exception as error:
            if isinstance(error, ProviderAccessBlocked):
                self._search_failed = True
            reason = str(error) if isinstance(error, ProviderUnavailable) else type(error).__name__
            self._failure_reasons[kind] = reason
            log.warning("provider_search_unavailable type=%s", type(error).__name__)
            self._update_health()
            if self._search_failed:
                raise ProviderAccessBlocked(self.health) from error
            raise ProviderUnavailable(self.health) from error

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
