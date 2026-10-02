import asyncio
import logging
import re
import tempfile
from pathlib import Path

import httpx

from app.config import Settings
from app.models import Source, Video
from app.network import retry

log = logging.getLogger(__name__)
TITLE_PATTERNS = (
    re.compile(r'(?:фильм|сериал|название|источник)\s*[:\-–]?\s*[«"“](.{2,100}?)[»"”]', re.I),
    re.compile(r"(?:название\s+(?:фильма|сериала)|фильм|сериал)\s*:\s*([^\n#.!?]{2,100})", re.I),
)


def explicit_title(text: str, method: str, confidence: float) -> Source:
    for pattern in TITLE_PATTERNS:
        match = pattern.search(text)
        if match:
            title = match[1].strip()
            if title.lower() not in ("смотри в профиле", "в комментариях", "неизвестно"):
                return Source(title, confidence, match[0][:200], method)
    return Source()


async def run_command(*args: str, timeout: float = 20) -> str:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError(f"{args[0]} завершился с кодом {process.returncode}")
    return stdout.decode("utf-8", errors="replace")[:20000]


class CascadeSourceIdentifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()

    async def _ocr(self, media: Path) -> str:
        texts = []
        with tempfile.TemporaryDirectory(prefix="ocr-", dir=self.settings.media_dir) as directory:
            for seconds in (2, 8, 16):
                frame = Path(directory) / f"{seconds}.png"
                await run_command(
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-threads",
                    "1",
                    "-ss",
                    str(seconds),
                    "-i",
                    str(media),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-1",
                    "-threads",
                    "1",
                    str(frame),
                )
                if frame.exists():
                    texts.append(
                        await run_command(
                            "tesseract", str(frame), "stdout", "-l", "rus+eng", "--psm", "6"
                        )
                    )
        return "\n".join(texts)

    async def _verify(self, source: Source) -> Source:
        if not self.settings.tmdb_api_key or not source.title:
            return source
        async with httpx.AsyncClient(timeout=self.settings.request_timeout) as client:

            async def fetch():
                response = await client.get(
                    "https://api.themoviedb.org/3/search/multi",
                    params={
                        "api_key": self.settings.tmdb_api_key,
                        "query": source.title,
                        "language": "ru-RU",
                        "include_adult": "false",
                    },
                )
                response.raise_for_status()
                return response.json()

            data = await retry(fetch)

        def normalize(text: str) -> str:
            return re.sub(r"[\W_]", "", text.lower())

        matches = [
            r
            for r in data.get("results", [])
            if r.get("media_type") in ("movie", "tv")
            and any(
                normalize(r.get(k, "")) == normalize(source.title)
                for k in ("title", "name", "original_title", "original_name")
            )
        ]
        if len(matches) == 1:
            result = matches[0]
            source.title = result.get("title") or result.get("name")
            source.year = (result.get("release_date") or result.get("first_air_date") or "")[:4]
            source.type = result["media_type"]
            source.method += "+tmdb"
            # Catalog match verifies the title exists, not that the scene belongs to it.
            source.confidence = min(0.8, source.confidence + 0.05)
        return source

    async def identify(self, video: Video, media: Path | None) -> Source:
        async with self._lock:
            async with asyncio.timeout(self.settings.source_timeout):
                result = explicit_title(video.caption, "caption", 0.75)
                if not result.title:
                    result = explicit_title(video.subtitles, "subtitles", 0.7)
                if not result.title and self.settings.ocr_enabled and media:
                    try:
                        text = await self._ocr(media)
                        result = explicit_title(text, "ocr", 0.65)
                    except Exception as error:
                        log.warning("source_ocr_failed type=%s", type(error).__name__)
                try:
                    result = await self._verify(result)
                except Exception as error:
                    log.warning("source_verification_failed type=%s", type(error).__name__)
                log.info(
                    "source_identified method=%s confidence=%.2f", result.method, result.confidence
                )
                return result
