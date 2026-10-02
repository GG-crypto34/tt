import asyncio
import hmac
import logging
import time
from datetime import UTC, datetime

from app.scheduler import next_broadcast
from app.telegram import TelegramRejected, TelegramUncertain

log = logging.getLogger(__name__)
HELP = (
    "/status — состояние\n/top — текущие TOP-3\n/pause — личная пауза\n"
    "/resume — возобновить рассылку\n/queries — запросы\n/logout — выйти\n"
    "/about — описание и источники данных\n"
    "/stop_service и /start_service — глобальное управление с отдельным паролем"
)


class BotController:
    def __init__(self, service, client):
        self.service, self.client = service, client
        self.pending: dict[int, tuple[str, float]] = {}
        self.top_tasks: dict[int, asyncio.Task] = {}
        self.top_last: dict[int, float] = {}

    async def _reply(self, uid: int, text: str) -> None:
        await self.client.send_text(uid, text)

    async def handle(self, update: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        db, settings = self.service.db, self.service.settings
        callback = update.get("callback_query")
        if callback:
            uid = callback.get("from", {}).get("id")
            if not uid or not db.user(uid):
                await self.client.answer_callback(callback["id"], "Сначала выполните /start")
                return
            parts = callback.get("data", "").split(":")
            if (
                len(parts) == 3
                and parts[0] == "q"
                and parts[1].isdigit()
                and parts[2] in ("yes", "no")
            ):
                won = await self.service.decide_query(int(parts[1]), uid, parts[2] == "yes")
                await self.client.answer_callback(
                    callback["id"],
                    "Решение принято" if won else "Решение уже принято или предложение устарело",
                )
            return
        message = update.get("message", {})
        if message.get("chat", {}).get("type") != "private":
            return
        uid = message.get("from", {}).get("id")
        if uid is None:
            return
        text = message.get("text", "")
        if not text:
            return
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command:
            self.pending.pop(uid, None)
        if command == "/start":
            if db.user(uid):
                await self._reply(uid, "Вы авторизованы.\n" + HELP)
            else:
                self.pending[uid] = ("user", now + 120)
                await self._reply(uid, "Введите общий пользовательский пароль в течение 2 минут.")
            return
        if command == "/status":
            user = db.user(uid)
            if not user:
                await self._reply(uid, "Вы не авторизованы. Выполните /start.")
                return
            state = (
                "🟢 сервис работает" if db.enabled else "🔴 поиск и рассылка глобально остановлены"
            )
            last = float(db.state("last_scan_at", "0"))
            tz = settings.timezone
            from zoneinfo import ZoneInfo

            formatted = (
                datetime.fromtimestamp(last, ZoneInfo(tz)).strftime("%d.%m %H:%M")
                if last
                else "ещё не было"
            )
            await self._reply(
                uid,
                f"Вы авторизованы.\n{state}\n"
                f"Личная рассылка: {'включена' if user['mailing_enabled'] else 'пауза'}\n"
                f"Последний scan: {formatted}\n"
                f"Актуальных кандидатов: {len(self.service.ranked(now, [uid]))}\n"
                f"Следующая рассылка: "
                f"{next_broadcast(datetime.fromtimestamp(now, UTC), tz):%d.%m %H:%M} ({tz})\n"
                f"TikTok: {db.state('provider_health', 'ещё не проверен')}",
            )
            return
        if command == "/logout":
            db.logout(uid)
            await self._reply(uid, "Авторизация удалена.")
            return
        pending = self.pending.get(uid)
        if not command and pending:
            if now > pending[1]:
                self.pending.pop(uid, None)
                await self._reply(uid, "Время ввода истекло. Повторите команду.")
                return
            if db.auth_blocked(uid, now):
                await self._reply(uid, "Слишком много попыток. Подождите 15 минут.")
                return
            purpose = pending[0]
            password = settings.user_password if purpose == "user" else settings.admin_password
            valid = hmac.compare_digest(text.encode("utf-8"), password.encode("utf-8"))
            # Best effort removal; secrets never enter logs or DB.
            if hasattr(self.client, "call"):
                try:
                    await self.client.call(
                        "deleteMessage", {"chat_id": uid, "message_id": message["message_id"]}
                    )
                except (TelegramRejected, TelegramUncertain):
                    log.info("password_message_delete_unavailable")
            if not valid:
                db.auth_failure(uid, now)
                await self._reply(uid, "Неверный пароль.")
                return
            self.pending.pop(uid, None)
            if purpose == "user":
                db.authorize(uid, message.get("from", {}).get("username", ""), now)
                await self._reply(uid, "Авторизация выполнена.\n" + HELP)
            else:
                if not db.user(uid):
                    await self._reply(uid, "Сначала авторизуйтесь через /start.")
                    return
                db.conn.execute("DELETE FROM auth_attempts WHERE user_id=?", (uid,))
                db.set_state("global_enabled", "1" if purpose == "start_service" else "0")
                log.info("service_state_changed enabled=%s user=%d", db.enabled, uid)
                await self._reply(
                    uid,
                    "Поиск и рассылка включены."
                    if db.enabled
                    else "Поиск и рассылка глобально остановлены. Бот продолжает отвечать.",
                )
            return
        if not db.user(uid):
            await self._reply(uid, "Выполните /start для авторизации.")
            return
        if command in ("/stop_service", "/start_service"):
            if len(text.split()) > 1:
                await self._reply(uid, "Отправьте команду без аргументов, затем пароль отдельно.")
                return
            self.pending[uid] = (command[1:], now + 120)
            await self._reply(uid, "Введите отдельный ADMIN_PASSWORD в течение 2 минут.")
        elif command in ("/pause", "/resume"):
            db.pause(uid, command == "/pause")
            await self._reply(
                uid,
                "Личная рассылка приостановлена."
                if command == "/pause"
                else "Личная рассылка включена.",
            )
        elif command == "/queries":
            active = db.active_queries(now)
            await self._reply(
                uid,
                "Базовые:\n"
                + "\n".join(settings.base_queries)
                + "\n\nВременные (TTL):\n"
                + ("\n".join(active) or "нет"),
            )
        elif command == "/about":
            text = "Movie Trend Bot: публичные TikTok-данные, локальное OCR и оценка роста."
            if settings.tmdb_api_key:
                text += (
                    "\nКаталог названий: TMDB — https://www.themoviedb.org\n"
                    "This product uses the TMDB API but is not endorsed or certified by TMDB.\n"
                    "Логотип и атрибуция: https://www.themoviedb.org/about/logos-attribution"
                )
            await self._reply(uid, text)
        elif command == "/top":
            if not db.enabled:
                await self._reply(uid, "Поиск и рассылка глобально остановлены.")
            elif (
                uid in self.top_tasks and not self.top_tasks[uid].done()
            ) or now - self.top_last.get(uid, 0) < 60:
                await self._reply(
                    uid, "Подборка уже готовится или недавно запрошена. Подождите минуту."
                )
            else:
                self.top_last[uid] = now
                await self._reply(uid, "Готовлю текущую подборку.")
                self.top_tasks[uid] = asyncio.create_task(self._top(uid))
        else:
            await self._reply(uid, HELP)

    async def _top(self, uid: int) -> None:
        try:
            await self.service.broadcast(uid)
        except Exception:
            log.exception("manual_top_failed user=%d", uid)

    async def run(self) -> None:
        db = self.service.db
        try:
            while True:
                try:
                    offset = int(db.state("telegram_offset", "0"))
                    updates = await self.client.call(
                        "getUpdates",
                        {
                            "offset": offset,
                            "timeout": 25,
                            "allowed_updates": ["message", "callback_query"],
                        },
                    )
                    for update in updates:
                        # Commit before processing: a crash cannot replay password/admin operations.
                        db.set_state("telegram_offset", str(update["update_id"] + 1))
                        try:
                            await self.handle(update)
                        except Exception:
                            log.exception("telegram_update_failed")
                except TelegramRejected as error:
                    if error.code in (401, 409):
                        raise RuntimeError(
                            "Проверьте Telegram token, webhook и второй экземпляр бота"
                        ) from None
                    log.warning("telegram_poll_rejected code=%d", error.code)
                    await asyncio.sleep(5)
                except TelegramUncertain:
                    log.warning("telegram_poll_unavailable")
                    await asyncio.sleep(5)
        finally:
            for task in self.top_tasks.values():
                task.cancel()
            await asyncio.gather(*self.top_tasks.values(), return_exceptions=True)
