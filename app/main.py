import argparse
import asyncio
import logging
import signal
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from app.bot import BotController
from app.config import Settings
from app.db import Database
from app.logging_setup import configure_logging
from app.process_lock import ProcessLock
from app.providers.fake import FakeTikTokProvider
from app.providers.public import PublicTikTokProvider
from app.scheduler import Scheduler, next_broadcast
from app.services import TrendService
from app.source import CascadeSourceIdentifier
from app.telegram import FakeMessenger, TelegramClient

log = logging.getLogger(__name__)


async def smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="movie-trend-smoke-") as directory:
        root = Path(directory)
        settings = Settings(
            data_dir=root / "data",
            media_dir=root / "media",
            log_dir=root / "logs",
            request_delay_seconds=0,
            ocr_enabled=False,
            base_queries=("фильм",),
        )
        db = Database(settings.data_dir / "bot.sqlite3")
        provider, messenger = FakeTikTokProvider(), FakeMessenger()
        service = TrendService(settings, db, provider, messenger, CascadeSourceIdentifier(settings))
        db.authorize(1, "offline", time.time())
        try:
            await service.scan(time.time() - 900)
            await service.scan()
            await service.broadcast()
            sent = [m for m in messenger.messages if "path" in m]
            assert len(sent) == 3
            assert len(provider.downloaded) == 3
            assert all(g.measured for _, g, _ in service.ranked(time.time(), [2]))
            assert len(db.rows("SELECT * FROM deliveries WHERE status='sent'")) == 3
            assert next_broadcast(datetime.now(UTC)).hour % 3 == 0
            service.cleanup(time.time() + 6 * 3600 + 1)
            assert not list(settings.media_dir.glob("*.mp4"))
            print("SMOKE OK: scan -> snapshots -> measured growth -> TOP-3 -> delivery -> cleanup")
        finally:
            await provider.close()
            db.close()


async def run(settings: Settings) -> None:
    configure_logging(settings)
    lock = ProcessLock(settings.data_dir / "process.lock")
    db = Database(settings.data_dir / "bot.sqlite3")
    provider = PublicTikTokProvider(settings)
    client = TelegramClient(settings.telegram_bot_token, settings.request_timeout)
    service = TrendService(settings, db, provider, client, CascadeSourceIdentifier(settings))
    tasks = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        service.cleanup(time.time())
        me = await client.call("getMe", {})
        await client.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": command, "description": description}
                    for command, description in (
                        ("start", "Авторизация"),
                        ("status", "Состояние"),
                        ("top", "Текущий TOP-3"),
                        ("pause", "Личная пауза"),
                        ("resume", "Включить личную рассылку"),
                        ("queries", "Поисковые запросы"),
                        ("about", "Описание и источники"),
                        ("logout", "Выйти"),
                        ("stop_service", "Глобальная остановка"),
                        ("start_service", "Глобальный запуск"),
                    )
                ]
            },
        )
        log.info("startup bot=@%s global_enabled=%s", me["username"], db.enabled)
        tasks = [
            asyncio.create_task(BotController(service, client).run()),
            asyncio.create_task(Scheduler(service).run()),
            asyncio.create_task(stop.wait()),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await provider.close()
        await client.close()
        db.close()
        lock.close()
        log.info("shutdown")


def main() -> None:
    parser = argparse.ArgumentParser(description="Movie Trend Bot")
    parser.add_argument("--env", default=".env")
    parser.add_argument(
        "--smoke", action="store_true", help="Полностью offline-проверка без секретов"
    )
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    try:
        if args.smoke:
            asyncio.run(smoke())
        else:
            settings = Settings.load(args.env)
            if args.check_config:
                print("Конфигурация корректна (секреты скрыты)")
            else:
                asyncio.run(run(settings))
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"Ошибка запуска: {error}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
