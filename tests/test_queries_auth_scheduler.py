import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.bot import BotController
from app.db import Database
from app.scheduler import Scheduler, due_slot, next_broadcast


def message(text, uid=1):
    return {
        "message": {
            "message_id": 1,
            "from": {"id": uid, "username": "test"},
            "chat": {"type": "private", "id": uid},
            "text": text,
        }
    }


def test_query_first_decision_ttl_extension_cooldown_and_old_buttons(system):
    db, now = system.db, time.time()
    qid = db.propose("#новыйфильм", now, 7)
    assert qid
    assert not db.decide_query(qid, 99, True, now, 24)
    assert db.decide_query(qid, 1, True, now, 24)
    assert not db.decide_query(qid, 2, False, now, 24)
    assert db.active_queries(now) == ["#новыйфильм"]
    db.query_success("#новыйфильм", now + 23 * 3600, 24)
    assert db.active_queries(now + 25 * 3600)
    db.expire_queries(now + 48 * 3600)
    assert not db.active_queries(now + 48 * 3600)
    rejected = db.propose("#другойфильм", now, 7)
    db.decide_query(rejected, 2, False, now, 24)
    assert db.propose("#другойфильм", now + 86400, 7) is None
    new_id = db.propose("#другойфильм", now + 8 * 86400, 7)
    assert new_id != rejected
    assert not db.decide_query(rejected, 1, True, now + 8 * 86400, 24)


async def test_suggestions_unique_videos_inline_decision_and_edit(system):
    await system.scan()
    proposals = [m for m in system.messenger.messages if m.get("markup")]
    assert len(proposals) == 2  # one proposal, both authorized users
    await system.scan()
    assert len([m for m in system.messenger.messages if m.get("markup")]) == 2
    qid = system.db.rows("SELECT id FROM dynamic_queries")[0][0]
    assert await system.decide_query(qid, 1, True)
    assert not await system.decide_query(qid, 2, False)
    assert all(m["markup"] is None for m in proposals)


async def test_password_rate_limit_ttl_pause_resume_and_persistent_auth(system):
    bot = BotController(system, system.messenger)
    now = time.time()
    await bot.handle(message("/start", 3), now)
    await bot.handle(message("user-secret", 3), now + 1)
    assert system.db.user(3)
    await bot.handle(message("/pause", 3), now + 2)
    assert not system.db.user(3)["mailing_enabled"]
    await bot.handle(message("/resume", 3), now + 3)
    assert system.db.user(3)["mailing_enabled"]
    await bot.handle(message("/start", 4), now)
    for _ in range(5):
        await bot.handle(message("wrong", 4), now + 1)
    await bot.handle(message("user-secret", 4), now + 2)
    assert not system.db.user(4)
    assert not system.db.auth_blocked(4, now + 901)
    await bot.handle(message("/start", 5), now)
    await bot.handle(message("user-secret", 5), now + 121)
    assert not system.db.user(5)
    second = Database(system.settings.data_dir / "bot.sqlite3")
    try:
        assert second.user(3)
    finally:
        second.close()


async def test_admin_challenge_and_persistent_global_state(system):
    bot = BotController(system, system.messenger)
    now = time.time()
    await bot.handle(message("/stop_service admin-secret"), now)
    assert system.db.enabled
    await bot.handle(message("/stop_service"), now + 1)
    await bot.handle(message("user-secret"), now + 2)
    assert system.db.enabled
    await bot.handle(message("admin-secret"), now + 3)
    assert not system.db.enabled
    second = Database(system.settings.data_dir / "bot.sqlite3")
    assert not second.enabled
    second.close()
    await bot.handle(message("/status"), now + 4)
    assert "глобально остановлены" in system.messenger.messages[-1]["text"]
    await bot.handle(message("/start_service"), now + 5)
    await bot.handle(message("admin-secret"), now + 6)
    assert system.db.enabled
    await bot.handle(message("/logout"), now + 7)
    assert not system.db.user(1)


@pytest.mark.parametrize("utc_hour,next_msk", [(5, 9), (6, 12), (7, 12), (20, 0), (21, 3)])
def test_fixed_moscow_schedule(utc_hour, next_msk):
    now = datetime(2026, 10, 2, utc_hour, 0, tzinfo=UTC)
    assert next_broadcast(now).hour == next_msk
    assert next_broadcast(now) > now
    assert due_slot(now) is not None if (utc_hour + 3) % 3 == 0 else due_slot(now) is None


async def test_scheduler_no_double_slot_restart_no_catchup_and_stop(system):
    system.scan = AsyncMock()
    system.broadcast = AsyncMock()
    system.cleanup = lambda now: None
    now = datetime(2026, 10, 2, 6, 0, tzinfo=UTC)  # 09:00 MSK
    scheduler = Scheduler(system)
    system.db.set_state("last_scan_attempt", str(now.timestamp()))
    await scheduler.tick(now)
    await scheduler.tasks["broadcast"]
    await Scheduler(system).tick(now)
    assert system.broadcast.await_count == 1
    await scheduler.tick(datetime(2026, 10, 2, 7, 0, tzinfo=UTC))
    if "scan" in scheduler.tasks:
        await scheduler.tasks["scan"]
    assert system.broadcast.await_count == 1
    system.db.set_state("global_enabled", "0")
    await scheduler.tick(datetime(2026, 10, 2, 9, 0, tzinfo=UTC))
    assert system.broadcast.await_count == 1
