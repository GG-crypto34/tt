"""Public live probe, no Telegram token or password needed."""

import argparse
import asyncio
import json
import logging
import tempfile
import time
from pathlib import Path

from app.config import Settings
from app.providers.public import PublicTikTokProvider
from app.ranking import eligible


async def probe(query: str, url: str | None, download: bool = False, limit: int = 3) -> None:
    settings = Settings.load(live=False)
    provider = PublicTikTokProvider(settings)
    try:
        videos = [await provider.metadata(url)] if url else await provider.search(query, limit)
        downloaded = None
        if download and videos:
            with tempfile.TemporaryDirectory(prefix="movie-trend-probe-") as directory:
                path = Path(directory) / "probe.mp4"
                await provider.download(videos[0], path)
                downloaded = path.stat().st_size
        print(
            json.dumps(
                {
                    "health": provider.health,
                    "search_depth": provider.depth_health,
                    "search_reports": provider.search_reports,
                    "downloaded_bytes": downloaded,
                    "eligible_count": sum(
                        eligible(video, time.time(), settings) for video in videos
                    ),
                    "videos": [
                        {
                            "id": v.id,
                            "url": v.url,
                            "published_at": v.published_at,
                            "age_hours": round((time.time() - v.published_at) / 3600, 2),
                            "views": v.metrics.views,
                            "has_download_url": bool(v.download_url),
                        }
                        for v in videos
                    ],
                },
                ensure_ascii=True,
            )
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "health": provider.health,
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
                ensure_ascii=True,
            )
        )
        raise SystemExit(2) from None
    finally:
        await provider.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="фильм")
    parser.add_argument("--url")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument(
        "--download", action="store_true", help="Проверить один MP4 и сразу удалить"
    )
    args = parser.parse_args()
    asyncio.run(probe(args.query, args.url, args.download, args.limit))
