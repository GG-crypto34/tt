import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_browser_search import BrowserPage, install_browser

from app.config import Settings
from app.providers.public import PublicTikTokProvider


def batch(ids, *, hours=48, more=True):
    return {
        "status_code": 0,
        "has_more": more,
        "data": [
            {
                "item": {
                    "id": str(vid),
                    "author": {"uniqueId": "cinema"},
                    "createTime": int(time.time() - hours * 3600),
                    "desc": "Фильм #кино",
                    "stats": {"playCount": 21000},
                }
            }
            for vid in ids
        ],
    }


class PagedPage(BrowserPage):
    def __init__(self, batches, *, login=False, captcha_after=None, button=False):
        super().__init__()
        self.batches = batches
        self.index = -1
        self.pending = 0
        self.login = login
        self.captcha_after = captcha_after
        self.button = button
        self.advances = 0
        self.viewport_size = {"width": 1280, "height": 720}
        self.mouse = SimpleNamespace(move=AsyncMock(), wheel=AsyncMock(side_effect=self.advance))
        self.click = AsyncMock(side_effect=self.advance)

    async def advance(self, *args, **kwargs):
        self.advances += 1
        if self.index + 1 < len(self.batches):
            self.pending = self.index + 1

    async def wait_for_timeout(self, milliseconds):
        if milliseconds == 250 and self.pending is not None:
            self.index = self.pending
            self.pending = None
            self.capture(
                SimpleNamespace(
                    url=f"https://www.tiktok.com/api/search/general/full/?cursor={self.index * 12}",
                    status=200,
                    text=AsyncMock(return_value=json.dumps(self.batches[self.index])),
                )
            )
        await asyncio.sleep(0)

    def locator(self, selector):
        text = (
            "Verify to continue CAPTCHA"
            if (self.captcha_after is not None and self.advances >= self.captcha_after)
            else self.text
        )
        visible = (
            self.login
            if "login-modal" in selector
            else self.button
            if "load-more" in selector
            else False
        )
        return SimpleNamespace(
            inner_text=AsyncMock(return_value=text),
            is_visible=AsyncMock(return_value=visible),
            is_enabled=AsyncMock(return_value=True),
            click=self.click,
        )


async def test_scroll_collects_new_batches_and_fresh_video_after_old_first_page(
    monkeypatch, system
):
    page = PagedPage([batch(range(1, 13)), batch(range(13, 25), hours=1, more=False)])
    install_browser(monkeypatch, page)
    system.settings.base_queries = ("фильм",)
    provider = PublicTikTokProvider(system.settings)
    system.provider = provider
    try:
        await system.scan()
        assert page.advances == 1 and page.closed
        assert system.db.state("scan_found") == "24"
        assert system.db.state("scan_rejected_age") == "12"
        assert len(system.ranked(time.time(), [1])) == 12
        assert provider.search_pages == 2
        assert provider.search_reports[0]["fresh"] == 12
        assert provider.search_reports[0]["stop_reason"] == "no_more"
    finally:
        await provider.close()


@pytest.mark.parametrize("mode", ["page_limit", "item_limit", "no_progress"])
async def test_pagination_is_bounded_and_stops_on_duplicate_batches(monkeypatch, mode):
    pages = [batch(range(1, 13)), batch(range(13, 25)), batch(range(25, 37))]
    if mode == "no_progress":
        pages[1] = batch(range(1, 13))
    page = PagedPage(pages)
    install_browser(monkeypatch, page)
    settings = Settings(
        request_delay_seconds=0,
        search_page_wait_seconds=0.02,
        search_max_pages=2 if mode == "page_limit" else 5,
    )
    provider = PublicTikTokProvider(settings)
    try:
        results = await provider.search("фильм", 20 if mode == "item_limit" else 100)
        assert page.advances == 1 and page.closed
        assert len(results) == (12 if mode == "no_progress" else 20 if mode == "item_limit" else 24)
        assert provider.search_reports[0]["stop_reason"] == mode
        assert provider.search_pages == 2
    finally:
        await provider.close()


async def test_visible_load_more_button_collects_next_batch(monkeypatch):
    page = PagedPage([batch([1]), batch([2], more=False)], button=True)
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings(request_delay_seconds=0))
    try:
        assert len(await provider.search("фильм", 100)) == 2
        page.click.assert_awaited_once()
        page.mouse.wheel.assert_not_awaited()
    finally:
        await provider.close()


async def test_empty_first_batch_with_more_results_does_not_end_search(monkeypatch):
    page = PagedPage([batch([]), batch([1], hours=1, more=False)])
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings(request_delay_seconds=0))
    try:
        results = await provider.search("фильм", 100)
        assert len(results) == 1 and results[0].id == "1"
        assert provider.search_pages == 2 and page.advances == 1
    finally:
        await provider.close()


async def test_login_gate_is_reported_and_not_dismissed(monkeypatch):
    page = PagedPage([batch(range(1, 13))], login=True)
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings(request_delay_seconds=0))
    try:
        assert len(await provider.search("фильм", 100)) == 12
        assert provider.search_reports[0]["stop_reason"] == "login_required"
        assert "требуется вход" in provider.depth_health
        assert page.advances == 0 and page.closed and provider.search_available
        page.click.assert_not_awaited()
    finally:
        await provider.close()


async def test_later_captcha_preserves_collected_items_and_stops_further_search(monkeypatch):
    page = PagedPage([batch(range(1, 13)), batch(range(13, 25))], captcha_after=1)
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings(request_delay_seconds=0))
    try:
        assert len(await provider.search("фильм", 100)) == 12
        assert provider.search_reports[0]["stop_reason"] == "captcha"
        assert not provider.search_available and page.closed
        from app.providers.public import ProviderAccessBlocked

        with pytest.raises(ProviderAccessBlocked):
            await provider.search("сериал", 100)
        assert len(page.visited) == 2
    finally:
        await provider.close()


async def test_timeout_keeps_first_page_and_marks_partial_search(monkeypatch):
    page = PagedPage([batch(range(1, 13))])
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(
        Settings(request_delay_seconds=0, search_query_timeout=0.02, search_page_wait_seconds=1)
    )
    try:
        assert len(await provider.search("фильм", 100)) == 12
        assert provider.search_reports[0]["stop_reason"] == "timeout" and page.closed
    finally:
        await provider.close()


async def test_own_session_is_passed_to_browser_context(monkeypatch, tmp_path):
    path = tmp_path / "session.json"
    path.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    page = PagedPage([batch([1], more=False)])
    browser = install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings(tiktok_storage_state=str(path)))
    try:
        await provider.search("фильм", 100)
        browser.new_context.assert_awaited_once_with(locale="ru-RU", storage_state=str(path))
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "options",
    [
        {"search_max_pages": 11},
        {"search_limit": 501},
        {"search_page_wait_seconds": 0},
        {"search_query_timeout": 1},
        {"tiktok_storage_state": "missing-session-file.json"},
    ],
)
def test_invalid_depth_settings_are_rejected(options):
    with pytest.raises(ValueError):
        Settings(**options).validate(live=False)


async def test_item_list_responses_count_towards_page_budget(monkeypatch):
    page = PagedPage([batch([1]), batch([2]), batch([3])])
    original_capture = page.on

    def on(event, callback):
        def item_list_response(response):
            old_text = response.text

            async def text():
                data = json.loads(await old_text())
                items = data.pop("data")
                return json.dumps({**data, "item_list": items})

            response.url = "https://www.tiktok.com/api/search/item/full"
            response.text = text
            callback(response)

        original_capture(event, item_list_response)

    page.on = on
    install_browser(monkeypatch, page)
    provider = PublicTikTokProvider(Settings(search_max_pages=2, request_delay_seconds=0))
    try:
        assert len(await provider.search("фильм", 100)) == 2
        assert provider.search_pages == 2 and page.advances == 1
        assert provider.search_reports[0]["stop_reason"] == "page_limit"
    finally:
        await provider.close()


async def test_scan_budget_saves_candidates_before_skipping_remaining_queries(system):
    system.settings.scan_timeout_seconds = 1
    system.provider.search = AsyncMock(return_value=system.provider.videos)
    await system.scan()
    assert system.provider.search.await_count == 1
    assert len(system.ranked(time.time(), [1])) == 5
    assert "бюджету времени" in system.db.state("search_depth")
    assert system.db.state("discovery_limited") == "1"
