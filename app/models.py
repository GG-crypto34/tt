from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Metrics:
    views: int = 0
    likes: int = 0
    shares: int = 0
    comments: int = 0


@dataclass
class Video:
    id: str
    url: str
    published_at: float
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    author: str = ""
    followers: int | None = None
    metrics: Metrics = field(default_factory=Metrics)
    subtitles: str = ""
    subtitle_tracks: list[dict[str, str]] = field(default_factory=list)
    download_url: str = ""
    provider: str = "public"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Video":
        return cls(**{**data, "metrics": Metrics(**data["metrics"])})


@dataclass
class Source:
    title: str | None = None
    confidence: float = 0
    evidence: str = ""
    method: str = "unknown"
    year: str | None = None
    type: str | None = None


class TikTokProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[Video]: ...
    async def metadata(self, url: str) -> Video: ...
    async def download(self, video: Video, destination: Path) -> None: ...
    async def close(self) -> None: ...


class SourceIdentifier(Protocol):
    async def identify(self, video: Video, media: Path | None) -> Source: ...
