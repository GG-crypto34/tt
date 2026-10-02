import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_QUERIES = (
    "фильм",
    "фильмы",
    "сериал",
    "сериалы",
    "кино",
    "нарезка",
    "нарезка из фильма",
    "момент из фильма",
    "момент из сериала",
    "лучшие моменты",
    "#фильм",
    "#фильмы",
    "#сериал",
    "#сериалы",
    "#кино",
    "#нарезка",
)


@dataclass
class Settings:
    telegram_bot_token: str = ""
    user_password: str = ""
    admin_password: str = ""
    timezone: str = "Europe/Moscow"
    scan_interval_minutes: int = 15
    scan_timeout_seconds: float = 600
    min_views: int = 10000
    max_video_age_hours: float = 24
    media_retention_hours: float = 6
    metrics_retention_hours: float = 48
    delivery_retention_days: int = 30
    data_dir: Path = Path("data")
    media_dir: Path = Path("media")
    log_dir: Path = Path("logs")
    seed_urls_file: Path = Path("data/seed_urls.txt")
    request_timeout: float = 30
    download_timeout: float = 120
    source_timeout: float = 90
    max_media_mb: int = 49
    max_candidates: int = 200
    search_limit: int = 15
    request_delay_seconds: float = 2
    query_threshold: int = 3
    query_ttl_hours: float = 24
    query_cooldown_days: int = 7
    language_threshold: float = 0.45
    browser_enabled: bool = True
    ocr_enabled: bool = True
    tmdb_api_key: str = ""
    log_level: str = "INFO"
    base_queries: tuple[str, ...] = BASE_QUERIES
    ranking_weights: dict[str, float] = field(
        default_factory=lambda: {
            "views": 0.4,
            "likes": 0.2,
            "shares": 0.15,
            "comments": 0.1,
            "freshness": 0.1,
            "breakout": 0.05,
        }
    )

    def validate(self, live: bool = True) -> None:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("TIMEZONE должен быть корректной IANA timezone") from None
        for key in self.__dataclass_fields__:
            value = getattr(self, key)
            if isinstance(value, (float, int)) and not math.isfinite(value):
                raise ValueError(f"{key.upper()} должен быть конечным числом")
        if live:
            for key in ("telegram_bot_token", "user_password", "admin_password"):
                if not getattr(self, key).strip():
                    raise ValueError(f"Заполните {key.upper()} в .env")
            if self.user_password == self.admin_password:
                raise ValueError("USER_PASSWORD и ADMIN_PASSWORD должны различаться")
        for key in (
            "scan_interval_minutes",
            "scan_timeout_seconds",
            "max_video_age_hours",
            "media_retention_hours",
            "metrics_retention_hours",
            "delivery_retention_days",
            "request_timeout",
            "download_timeout",
            "source_timeout",
            "max_media_mb",
            "max_candidates",
            "search_limit",
            "query_threshold",
            "query_ttl_hours",
            "query_cooldown_days",
        ):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key.upper()} должен быть > 0")
        if self.min_views < 0 or self.request_delay_seconds < 0:
            raise ValueError("MIN_VIEWS и REQUEST_DELAY_SECONDS должны быть >= 0")
        if self.media_retention_hours > 6 or self.max_media_mb > 49:
            raise ValueError("MEDIA_RETENTION_HOURS <= 6 и MAX_MEDIA_MB <= 49")
        if not 0 <= self.language_threshold <= 1:
            raise ValueError("LANGUAGE_THRESHOLD должен быть от 0 до 1")
        expected = {"views", "likes", "shares", "comments", "freshness", "breakout"}
        if (
            not isinstance(self.ranking_weights, dict)
            or set(self.ranking_weights) != expected
            or any(
                not isinstance(v, (float, int)) or not math.isfinite(v) or v < 0
                for v in self.ranking_weights.values()
            )
            or sum(self.ranking_weights.values()) <= 0
        ):
            raise ValueError("Неверные RANKING_WEIGHTS")
        if not self.base_queries or any(
            not isinstance(q, str) or not q.strip() for q in self.base_queries
        ):
            raise ValueError("BASE_QUERIES_JSON должен содержать непустые запросы")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("LOG_LEVEL должен быть DEBUG/INFO/WARNING/ERROR/CRITICAL")
        media = self.media_dir.resolve()
        cwd = Path.cwd().resolve()
        if (
            media == cwd
            or media in cwd.parents
            or any(path.resolve().is_relative_to(media) for path in (self.data_dir, self.log_dir))
        ):
            raise ValueError("MEDIA_DIR должен быть отдельной директорией для временных файлов")

    @classmethod
    def load(cls, env_file: str = ".env", live: bool = True) -> "Settings":
        load_dotenv(env_file, override=False)
        settings = cls()
        for key in settings.__dataclass_fields__:
            env = os.environ.get(key.upper())
            if env is None:
                continue
            current = getattr(settings, key)
            if isinstance(current, bool):
                if env.lower() not in ("true", "false", "1", "0"):
                    raise ValueError(f"{key.upper()} должен быть true/false")
                value = env.lower() in ("true", "1")
            elif isinstance(current, Path):
                value = Path(env)
            elif isinstance(current, (int, float)):
                value = type(current)(env)
            elif isinstance(current, dict):
                value = json.loads(env)
            elif isinstance(current, tuple):
                value = tuple(json.loads(env))
            else:
                value = env
            setattr(settings, key, value)
        if "BASE_QUERIES_JSON" in os.environ:
            settings.base_queries = tuple(json.loads(os.environ["BASE_QUERIES_JSON"]))
        settings.validate(live)
        return settings
