import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.providers.public import ProviderAccessBlocked, ProviderUnavailable, PublicTikTokProvider


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
                    "status_code": 0,
                    "has_more": False,
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
                    ],
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
    return browser


async def test_public_session_warms_up_and_waits_for_real_results(monkeypatch):
    page = BrowserPage()
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings())
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


async def test_valid_empty_search_is_available_instead_of_an_outage(monkeypatch):
    page = BrowserPage()

    async def empty_response(milliseconds):
        if milliseconds == 250:
            page.capture(
                SimpleNamespace(
                    url="https://www.tiktok.com/api/search/general/full/",
                    status=200,
                    text=AsyncMock(return_value='{"status_code":0,"data":[]}'),
                )
            )

    page.wait_for_timeout = empty_response
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings())
    try:
        assert await provider.search("нет результатов", 3) == []
        assert provider.search_available and page.closed
    finally:
        await provider.close()


async def test_search_health_is_partial_when_only_hashtags_fail():
    provider = PublicTikTokProvider(Settings())
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
        assert "часть запросов: хэштеги" in provider.health
    finally:
        await provider.close()


async def test_hashtags_use_ordinary_automatic_search():
    provider = PublicTikTokProvider(Settings())
    from app.models import Video

    video = Video("1", "https://www.tiktok.com/@demo/video/1", time.time())
    provider._page_items = AsyncMock(return_value=[video])
    try:
        assert await provider.search("#кино", 3) == [video]
        assert await provider.search("#сериал", 3) == [video]
        urls = [call.args[0] for call in provider._page_items.await_args_list]
        assert len(urls) == 2 and all("/search?q=%23" in url for url in urls)
        assert provider.search_available
    finally:
        await provider.close()


async def test_one_query_timeout_does_not_disable_other_queries():
    provider = PublicTikTokProvider(Settings())
    from app.models import Video

    video = Video("1", "https://www.tiktok.com/@demo/video/1", time.time())
    provider._page_items = AsyncMock(side_effect=[TimeoutError(), [video]])
    try:
        with pytest.raises(ProviderUnavailable):
            await provider.search("фильм", 3)
        assert await provider.search("сериал", 3) == [video]
        assert provider._page_items.await_count == 2 and provider.search_available
    finally:
        await provider.close()


async def test_access_block_stops_all_searches_until_next_scan():
    provider = PublicTikTokProvider(Settings())
    provider._page_items = AsyncMock(side_effect=ProviderAccessBlocked("CAPTCHA"))
    try:
        for query in ("фильм", "#кино", "сериал"):
            with pytest.raises(ProviderAccessBlocked):
                await provider.search(query, 3)
        assert provider._page_items.await_count == 1 and not provider.search_available
        await provider.begin_scan()
        with pytest.raises(ProviderAccessBlocked):
            await provider.search("фильм", 3)
        assert provider._page_items.await_count == 2
    finally:
        await provider.close()
