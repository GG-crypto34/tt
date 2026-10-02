import json
import logging
import re
from logging.handlers import RotatingFileHandler

from app.config import Settings


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"bot\d+:[\w-]+", "bot[REDACTED]", message)
        return json.dumps(
            {
                "time": self.formatTime(record),
                "level": record.levelname,
                "logger": record.name,
                "event": message,
            },
            ensure_ascii=False,
        )


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = SafeFormatter(
        [
            settings.telegram_bot_token,
            settings.user_password,
            settings.admin_password,
            settings.tmdb_api_key,
        ]
    )
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            settings.log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        ),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=settings.log_level, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
