import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.providers.public import ProviderUnavailable, PublicTikTokProvider


class BrowserPage:
    def __init__(self, text="Результаты поиска"):
        self.text = text
        self.visited = []
        self.polls = 0
        self.capture = None
        self.closed = False

    def on(self, event, callback):
        assert event == "response"
        self.capture = callback

    async def goto(self, url, **kwargs):
        self.visited.append(url)

    async def wait_for_timeout(self, milliseconds):
        if milliseconds == 250:
            self.polls += 1
            if self.polls == 1:
                self.capture(
                    SimpleNamespace(
                        url="https://www.tiktok.com/api/search/suggest/guide/",
                        status=200,
                        text=AsyncMock(return_value='{"status_code":0}'),
                    )
                )
            if self.polls == 3:
                payload = {
                    "data": [
                        {
                            "item": {
                                "id": "12345",
                                "createTime": int(time.time() - 3600),
                                "author": {"uniqueId": "cinema"},
                                "desc": "Фильм #кино",
                                "stats": {"playCount": 20000},
                            }
                        }
                    ]
                }
                self.capture(
                    SimpleNamespace(
                        url="https://www.tiktok.com/api/search/general/full/",
                        status=200,
                        text=AsyncMock(return_value=json.dumps(payload)),
                    )
                )

    async def content(self):
        return "<html></html>"

    def locator(self, selector):
        return SimpleNamespace(inner_text=AsyncMock(return_value=self.text))

    async def close(self):
        self.closed = True


def install_browser(monkeypatch, page):
    context = SimpleNamespace(route=AsyncMock(), new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    runtime = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock()
    )
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=runtime)),
    )


async def test_public_session_warms_up_and_waits_for_real_results(monkeypatch, tmp_path):
    page = BrowserPage()
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings(seed_urls_file=tmp_path / "missing"))
    try:
        results = await provider.search("фильм", 15)
        assert len(results) == 1 and results[0].metrics.views == 20000
        assert page.visited[0] == "https://www.tiktok.com/"
        assert page.visited[1].startswith("https://www.tiktok.com/search?q=")
        assert page.polls == 3 and page.closed
        assert "доступен" in provider.health
    finally:
        await provider.close()


async def test_homepage_captcha_stops_navigation(monkeypatch):
    page = BrowserPage(text="Verify to continue CAPTCHA")
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings())
    try:
        with pytest.raises(ProviderUnavailable, match="обход отключён"):
            await provider._page_items("https://www.tiktok.com/search?q=test")
        assert page.visited == ["https://www.tiktok.com/"] and page.closed
    finally:
        await provider.close()


async def test_search_health_is_partial_when_only_hashtags_fail(tmp_path):
    provider = PublicTikTokProvider(Settings(seed_urls_file=tmp_path / "missing"))
    from app.models import Video

    provider._page_items = AsyncMock(
        side_effect=[
            [Video("1", "https://www.tiktok.com/@demo/video/1", time.time())],
            ProviderUnavailable("Нет результатов хэштега"),
        ]
    )
    try:
        await provider.search("фильм", 3)
        with pytest.raises(ProviderUnavailable):
            await provider.search("#фильм", 3)
        assert "доступен: запросы" in provider.health
        assert "недоступны: хэштеги" in provider.health
    finally:
        await provider.close()
