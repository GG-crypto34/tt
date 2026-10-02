import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.bot import BotController
from app.providers.public import ProviderUnavailable
from app.scheduler import Scheduler


def message(text, uid=1):
    return {"message": {"from": {"id": uid}, "chat": {"type": "private"}, "text": text}}


async def test_manual_scan_reports_results_on_personal_pause_and_updates_schedule(system):
    bot = BotController(system, system.messenger)
    system.db.pause(1, True)
    now = time.time()
    await bot.handle(message("/scan"), now)
    assert "Запускаю поиск" in system.messenger.messages[-1]["text"]
    await bot.scan_task
    result = system.messenger.messages[-1]["text"]
    assert "Скан завершён" in result and "Найдено в выдаче: 5" in result
    assert "кандидатов для вас: 5" in result and "/top" in result
    assert system.provider.downloaded == []
    system.scan = AsyncMock(wraps=system.scan)
    system.broadcast = AsyncMock()
    scheduler = Scheduler(system)
    await scheduler.tick(datetime.fromtimestamp(now + 60, UTC))
    if "broadcast" in scheduler.tasks:
        await scheduler.tasks["broadcast"]
    assert "scan" not in scheduler.tasks
    await bot.handle(message("/scan", 2), now + 2)
    assert "Подождите минуту" in system.messenger.messages[-1]["text"]
    assert system.scan.await_count == 0


async def test_manual_scan_requires_auth_and_respects_global_stop(system):
    bot = BotController(system, system.messenger)
    system.scan = AsyncMock(wraps=system.scan)
    await bot.handle(message("/scan", 99))
    assert "/start" in system.messenger.messages[-1]["text"]
    system.db.set_state("global_enabled", "0")
    await bot.handle(message("/scan"))
    assert "глобально остановлены" in system.messenger.messages[-1]["text"]
    assert bot.scan_task is None and system.scan.await_count == 0


async def test_manual_and_scheduled_scans_do_not_overlap_or_block_commands(system):
    bot = BotController(system, system.messenger)
    started, release = asyncio.Event(), asyncio.Event()
    original = system.provider.search

    async def search(query, limit):
        started.set()
        await release.wait()
        return await original(query, limit)

    system.provider.search = AsyncMock(side_effect=search)
    await bot.handle(message("/scan"))
    await asyncio.wait_for(started.wait(), 2)
    try:
        await bot.handle(message("/scan", 2))
        assert "Скан уже выполняется" in system.messenger.messages[-1]["text"]
        assert await system.scan() is False
        await bot.handle(message("/status", 2))
        assert "Вы авторизованы" in system.messenger.messages[-1]["text"]
    finally:
        release.set()
        await bot.scan_task
    assert system.provider.round == 1
    assert len(system.db.rows("SELECT * FROM metric_snapshots")) == 5
    # An automatic scan is also visible to the command, without creating another task.
    await system.scan_lock.acquire()
    try:
        await bot.handle(message("/scan", 2), time.time() + 61)
        assert "Скан уже выполняется" in system.messenger.messages[-1]["text"]
    finally:
        system.scan_lock.release()


@pytest.mark.parametrize("failure", ["outage", "timeout"])
async def test_manual_scan_reports_provider_failure_or_timeout(system, failure):
    bot = BotController(system, system.messenger)
    if failure == "outage":
        system.provider.search = AsyncMock(side_effect=ProviderUnavailable("offline"))
    else:
        system.settings.scan_timeout_seconds = 0.01

        async def slow_search(*args):
            await asyncio.sleep(10)

        system.provider.search = AsyncMock(side_effect=slow_search)
    await bot.handle(message("/scan"))
    await bot.scan_task
    text = system.messenger.messages[-1]["text"]
    assert "Скан завершён с ошибкой поиска" in text
    assert "TikTok:" in text
    if failure == "timeout":
        assert "timeout" in text


async def test_manual_scan_is_cancelled_with_polling_on_shutdown(system):
    bot = BotController(system, system.messenger)
    started = asyncio.Event()

    async def search(*args):
        started.set()
        await asyncio.Event().wait()

    system.provider.search = AsyncMock(side_effect=search)
    system.provider.end_scan = AsyncMock()
    system.messenger.call = AsyncMock(side_effect=RuntimeError("shutdown"))
    await bot.handle(message("/scan"))
    await asyncio.wait_for(started.wait(), 2)
    with pytest.raises(RuntimeError, match="shutdown"):
        await bot.run()
    assert bot.scan_task.cancelled() and not system.scan_lock.locked()
    system.provider.end_scan.assert_awaited_once()
