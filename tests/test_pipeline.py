import asyncio
import os
import time
from unittest.mock import AsyncMock

from app.models import Source
from app.telegram import TelegramRejected, TelegramUncertain


async def test_end_to_end_two_scans_top_and_per_user_deliveries(system):
    now = time.time()
    await system.scan(now - 900)
    await system.scan(now)
    assert len(system.db.rows("SELECT * FROM videos")) == 5
    assert len(system.db.rows("SELECT * FROM metric_snapshots")) == 10
    ranked = system.ranked(now, [1])
    assert len(ranked) == 5 and all(g.measured for _, g, _ in ranked)
    assert [s for _, _, s in ranked] == sorted([s for _, _, s in ranked], reverse=True)
    expected = [v.id for v, _, _ in ranked[:3]]
    await system.broadcast(1, now)
    assert system.provider.downloaded == expected
    assert len(system.db.rows("SELECT * FROM deliveries WHERE user_id=1")) == 3
    assert not system.db.rows("SELECT * FROM deliveries WHERE user_id=2")
    await system.broadcast(now=now)
    assert len(system.db.rows("SELECT * FROM deliveries WHERE user_id=2")) == 3
    assert len(system.db.rows("SELECT * FROM deliveries WHERE user_id=1")) == 3
    assert system.provider.downloaded == expected
    assert system.db.reserve_delivery(1, expected[0], now, "manual") is False


async def test_personal_pause_and_global_stop(system):
    now = time.time()
    await system.scan(now)
    system.db.pause(1, True)
    await system.broadcast(now=now)
    assert not system.db.rows("SELECT * FROM deliveries WHERE user_id=1")
    assert len(system.db.rows("SELECT * FROM deliveries WHERE user_id=2")) == 3
    system.db.pause(1, False)
    system.db.set_state("global_enabled", "0")
    before = system.provider.round
    await system.scan()
    await system.broadcast(1)
    assert system.provider.round == before
    assert not system.db.rows("SELECT * FROM deliveries WHERE user_id=1")
    system.db.set_state("global_enabled", "1")
    await system.broadcast(1, now)
    assert len(system.db.rows("SELECT * FROM deliveries WHERE user_id=1")) == 3


async def test_download_and_source_failures_still_deliver_links(system):
    await system.scan()
    system.provider.download = AsyncMock(side_effect=RuntimeError("offline"))
    system.identifier.identify = AsyncMock(side_effect=RuntimeError("ocr offline"))
    await system.broadcast(1)
    messages = [m for m in system.messenger.messages if "🔗" in m["text"]]
    assert len(messages) == 3 and all("path" not in m for m in messages)
    assert all("не удалось определить" in m["text"] for m in messages)


async def test_ambiguous_delivery_keeps_reservation(system):
    await system.scan()
    system.messenger.send_video = AsyncMock(side_effect=TelegramUncertain("timeout"))
    await system.broadcast(1)
    pending = system.db.rows("SELECT * FROM deliveries WHERE status='pending'")
    assert len(pending) == 3
    calls = system.messenger.send_video.await_count
    await system.broadcast(1)
    assert system.messenger.send_video.await_count == calls + 2  # only the remaining unseen two


async def test_definite_telegram_failure_can_retry_and_bad_video_has_link_fallback(system):
    await system.scan()
    system.messenger.send_video = AsyncMock(side_effect=TelegramRejected(400))
    await system.broadcast(1)
    assert len(system.db.rows("SELECT * FROM deliveries WHERE status='sent'")) == 3
    assert all(
        "отклонён Telegram" in m["text"] for m in system.messenger.messages if "🔗" in m["text"]
    )


async def test_cleanup_ttl_preserves_auth_pause_global_state(system):
    now = time.time()
    await system.scan(now - 900)
    await system.scan(now)
    await system.broadcast(1, now)
    system.db.pause(1, True)
    system.db.set_state("global_enabled", "0")
    paths = list(system.settings.media_dir.glob("*.mp4"))
    assert len(paths) == 3
    system.cleanup(now + 5 * 3600)
    assert all(p.exists() for p in paths)
    for path in paths:
        os.utime(path, (now - 6 * 3600, now - 6 * 3600))
    orphan = system.settings.media_dir / "orphan.part"
    orphan.write_bytes(b"unfinished")
    os.utime(orphan, (now - 3600, now - 3600))
    system.cleanup(now + 1)
    assert not any(p.exists() for p in paths) and not orphan.exists()
    assert system.db.user(1)["mailing_enabled"] == 0 and not system.db.enabled
    system.cleanup(now + 49 * 3600)
    assert not system.db.rows("SELECT * FROM metric_snapshots")
    assert len(system.db.rows("SELECT * FROM deliveries")) == 3
    system.cleanup(now + 31 * 86400)
    assert not system.db.rows("SELECT * FROM deliveries") and system.db.user(1)


async def test_active_media_not_removed_and_stale_counters_not_delivered(system):
    now = time.time()
    await system.scan(now - 3601)
    assert not system.ranked(now, [1])
    path = (system.settings.media_dir / "active.mp4").resolve()
    path.write_bytes(b"media")
    os.utime(path, (now - 7 * 3600, now - 7 * 3600))
    system.active_media.add(path)
    system.cleanup(now)
    assert path.exists()


async def test_no_scan_overlap(system):
    entered, finish = asyncio.Event(), asyncio.Event()
    original = system.provider.search

    async def slow(query, limit):
        entered.set()
        await finish.wait()
        return await original(query, limit)

    system.provider.search = slow
    task = asyncio.create_task(system.scan())
    await entered.wait()
    await system.scan()
    assert system.provider.round == 1
    finish.set()
    await task


async def test_source_result_is_cached(system):
    await system.scan()
    system.identifier.identify = AsyncMock(return_value=Source("Фильм", 0.75, "caption", "caption"))
    await system.broadcast(1)
    await system.broadcast(2)
    assert system.identifier.identify.await_count == 3
