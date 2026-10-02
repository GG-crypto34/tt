import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.process_lock import ProcessLock
from app.providers.public import ProviderUnavailable, PublicTikTokProvider, parse_items
from app.ranking import language_relevance
from app.telegram import TelegramClient


def test_process_lock_excludes_second_process(tmp_path):
    first = ProcessLock(tmp_path / "process.lock")
    try:
        with pytest.raises(RuntimeError, match="Другой экземпляр"):
            ProcessLock(tmp_path / "process.lock")
    finally:
        first.close()
    second = ProcessLock(tmp_path / "process.lock")
    second.close()


async def test_subtitles_are_public_bounded_and_help_language(system):
    item = {
        "id": "123",
        "createTime": int(time.time()),
        "desc": "Movie scene",
        "author": {"uniqueId": "test"},
        "stats": {"playCount": 10000},
        "video": {
            "subtitleInfos": [
                {"Url": "https://v.tiktokcdn.com/subtitle.json", "LanguageCodeName": "ru-RU"}
            ]
        },
    }
    video = parse_items(item)[0]
    assert language_relevance(video) >= 0.45
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"utterances": [{"text": "Название фильма: Матрица"}]}
            )
        )
    )
    provider = PublicTikTokProvider(Settings(), client)
    try:
        assert await provider.get_subtitles(video) == "Название фильма: Матрица"
    finally:
        await provider.close()


async def test_ttl_does_not_extend_for_unrelated_fallback_seed(system):
    now = time.time()
    qid = system.db.propose("#другойзапрос", now - 23 * 3600, 7)
    assert system.db.decide_query(qid, 1, True, now - 23 * 3600, 24)
    expiry = system.db.rows("SELECT expires_at FROM dynamic_queries WHERE id=?", (qid,))[0][0]
    await system.scan(now)
    assert (
        system.db.rows("SELECT expires_at FROM dynamic_queries WHERE id=?", (qid,))[0][0] == expiry
    )


async def test_telegram_real_video_multipart(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 10}})

    client = TelegramClient("0:fake")
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video-content")
    try:
        assert await client.send_video(1, path, "caption") == 10
        assert requests[0].headers["content-type"].startswith("multipart/form-data")
        assert b"video-content" in requests[0].content and b"caption" in requests[0].content
        assert await client.send_video(1, path, "x" * 1200) == 10
        assert len(requests) == 3
        assert requests[-1].url.path.endswith("sendMessage")
    finally:
        await client.close()


async def test_provider_outage_preserves_bot_and_previous_data(system):
    now = time.time()
    await system.scan(now - 900)
    system.provider.search = AsyncMock(side_effect=ProviderUnavailable("offline"))
    system.provider.metadata = AsyncMock(side_effect=ProviderUnavailable("offline"))
    await system.scan(now)
    assert len(system.db.rows("SELECT * FROM videos")) == 5
    assert len(system.db.rows("SELECT * FROM metric_snapshots")) == 5
    assert "ошибок" in system.db.state("provider_health")


async def test_candidate_capacity_and_no_media_during_scan(system):
    system.settings.max_candidates = 3
    await system.scan()
    assert len(system.db.rows("SELECT * FROM videos")) == 3
    assert system.provider.downloaded == []
    assert not list(system.settings.media_dir.iterdir())


async def test_stop_during_scan_prevents_later_work(system):
    original = system.provider.search

    async def stopping(query, limit):
        system.db.set_state("global_enabled", "0")
        return await original(query, limit)

    system.provider.search = stopping
    await system.scan()
    assert not system.db.rows("SELECT * FROM videos")


async def test_scan_total_timeout_releases_locks_and_preserves_bot(system):
    import asyncio

    system.settings.scan_timeout_seconds = 0.01

    async def never(query, limit):
        await asyncio.sleep(10)
        return []

    system.provider.search = never
    await system.scan()
    assert "timeout" in system.db.state("provider_health")
    assert "превысил" in system.db.state("provider_health")
    assert not system.scan_lock.locked() and not system.work_lock.locked()
    assert system.db.user(1)


def test_cleanup_leaves_unrelated_files(system):
    import os

    now = time.time()
    path = system.settings.media_dir / "notes.txt"
    path.write_text("keep", encoding="utf-8")
    os.utime(path, (now - 86400, now - 86400))
    system.cleanup(now)
    assert path.exists()
