import asyncio
import logging
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class TelegramRejected(RuntimeError):
    """API explicitly rejected request, so retry/fallback cannot duplicate it."""

    def __init__(self, code: int):
        self.code = code
        super().__init__(f"Telegram отклонил запрос: код {code}")


class TelegramUncertain(RuntimeError):
    """Server may have accepted the message: do not resend automatically."""


class Messenger(Protocol):
    async def send_text(self, user_id: int, text: str, markup: dict | None = None) -> int: ...
    async def send_video(self, user_id: int, path: Path, caption: str) -> int: ...
    async def edit_text(self, user_id: int, message_id: int, text: str) -> None: ...


class TelegramClient:
    def __init__(self, token: str, timeout: float = 30):
        self._base = f"https://api.telegram.org/bot{token}/"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=45, write=120))

    async def call(self, method: str, payload: dict, files: dict | None = None):
        import json

        for attempt in range(3):
            try:
                if files:
                    for _, value in files.items():
                        value[1].seek(0)
                    data = {
                        k: json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)
                        for k, v in payload.items()
                    }
                    response = await self.client.post(self._base + method, data=data, files=files)
                else:
                    response = await self.client.post(self._base + method, json=payload)
                body = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise TelegramUncertain(type(error).__name__) from None
            if body.get("ok"):
                return body["result"]
            code = int(body.get("error_code", response.status_code))
            if code == 429 and attempt < 2:
                delay = min(30, max(1, body.get("parameters", {}).get("retry_after", 2)))
                await asyncio.sleep(delay)
                continue
            if code >= 500:
                raise TelegramUncertain(f"HTTP {code}")
            raise TelegramRejected(code)
        raise TelegramRejected(429)

    async def send_text(self, user_id: int, text: str, markup: dict | None = None) -> int:
        result = await self.call(
            "sendMessage",
            {
                "chat_id": user_id,
                "text": text[:4000],
                **({"reply_markup": markup} if markup else {}),
            },
        )
        return result["message_id"]

    async def send_video(self, user_id: int, path: Path, caption: str) -> int:
        long_caption = len(caption) > 1024
        video_caption = (
            caption
            if not long_caption
            else caption[:850] + "\nПолная статистика — следующим сообщением."
        )
        with path.open("rb") as media:
            result = await self.call(
                "sendVideo",
                {"chat_id": user_id, "caption": video_caption, "supports_streaming": True},
                files={"video": (path.name, media, "video/mp4")},
            )
        if long_caption:
            try:
                await self.send_text(user_id, caption)
            except (TelegramRejected, TelegramUncertain):
                # The MP4 is already accepted; keep its delivery successful and never resend it.
                log.warning("supplementary_caption_failed user=%d", user_id)
        return result["message_id"]

    async def edit_text(self, user_id: int, message_id: int, text: str) -> None:
        await self.call(
            "editMessageText",
            {
                "chat_id": user_id,
                "message_id": message_id,
                "text": text[:4000],
                "reply_markup": {"inline_keyboard": []},
            },
        )

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def close(self) -> None:
        await self.client.aclose()


class FakeMessenger:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_text(self, user_id: int, text: str, markup: dict | None = None) -> int:
        self.messages.append({"user_id": user_id, "text": text, "markup": markup})
        return len(self.messages)

    async def send_video(self, user_id: int, path: Path, caption: str) -> int:
        self.messages.append({"user_id": user_id, "path": str(path), "text": caption})
        return len(self.messages)

    async def edit_text(self, user_id: int, message_id: int, text: str) -> None:
        self.messages[message_id - 1]["text"] = text
        self.messages[message_id - 1]["markup"] = None

    async def answer_callback(self, callback_id: str, text: str) -> None:
        pass

    async def close(self) -> None:
        pass
