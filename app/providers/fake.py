import time
from pathlib import Path

from app.models import Metrics, Video


class FakeTikTokProvider:
    """Deterministic offline transport; fake media never sent to real Telegram."""

    def __init__(self):
        self.round = 0
        self.downloaded: list[str] = []
        self.health = "FAKE: offline"
        self.videos = [
            Video(
                id=str(i),
                url=f"https://www.tiktok.com/@demo/video/{i}",
                published_at=time.time() - 3 * 3600,
                caption="Фильм «Пример фильма» #кино #новыйфильм",
                hashtags=["кино", "новыйфильм"],
                author="demo",
                followers=1000,
                metrics=Metrics(15000 + i * 10000, 1000 + i * 300, 200, 100),
                provider="fake",
            )
            for i in range(1, 6)
        ]

    async def begin_scan(self) -> None:
        self.round += 1
        for video in self.videos:
            video.metrics.views += int(video.id) * 1000
            video.metrics.likes += int(video.id) * 100

    async def end_scan(self) -> None:
        pass

    async def search(self, query: str, limit: int) -> list[Video]:
        return self.videos[:limit]

    async def metadata(self, url: str) -> Video:
        return next(v for v in self.videos if v.url == url)

    async def download(self, video: Video, destination: Path) -> None:
        self.downloaded.append(video.id)
        destination.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"fake-video" * 10)

    async def close(self) -> None:
        pass
