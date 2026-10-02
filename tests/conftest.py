import time

import pytest

from app.config import Settings
from app.db import Database
from app.providers.fake import FakeTikTokProvider
from app.services import TrendService
from app.source import CascadeSourceIdentifier
from app.telegram import FakeMessenger


@pytest.fixture
def system(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        media_dir=tmp_path / "media",
        log_dir=tmp_path / "logs",
        request_delay_seconds=0,
        base_queries=("фильм", "#кино"),
        ocr_enabled=False,
        user_password="user-secret",
        admin_password="admin-secret",
    )
    db = Database(settings.data_dir / "bot.sqlite3")
    provider, messenger = FakeTikTokProvider(), FakeMessenger()
    service = TrendService(settings, db, provider, messenger, CascadeSourceIdentifier(settings))
    db.authorize(1, "first", time.time())
    db.authorize(2, "second", time.time())
    yield service
    db.close()
