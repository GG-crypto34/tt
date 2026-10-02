import json
import logging
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.logging_setup import SafeFormatter
from app.providers.public import (
    ProviderUnavailable,
    PublicTikTokProvider,
    canonical_url,
    parse_html,
    parse_items,
)
from app.source import CascadeSourceIdentifier, explicit_title
from app.telegram import TelegramClient, TelegramRejected, TelegramUncertain


def payload():
    return {
        "id": "1234567890",
        "createTime": int(time.time() - 3600),
        "desc": "Фильм «Бойцовский клуб» #кино",
        "author": {"uniqueId": "demo"},
        "authorStats": {"followerCount": 1200},
        "stats": {"playCount": "18000", "diggCount": 1200, "shareCount": 200, "commentCount": 10},
        "video": {"downloadAddr": "https://v.tiktokcdn.com/video.mp4"},
    }


def html_page():
    return (
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        + json.dumps(
            {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": payload()}}}}
        )
        + "</script>"
    )


def test_hydration_and_search_response_parser():
    videos = parse_html(html_page())
    assert len(videos) == 1 and videos[0].metrics.views == 18000
    assert videos[0].followers == 1200 and videos[0].hashtags == ["кино"]
    response = {"data": [{"item": payload()}, {"item": payload()}]}
    assert len(parse_items(response)) == 1
    assert parse_items({"error": "captcha"}) == []
    assert parse_html('<script id="SIGI_STATE">broken</script>') == []
    with pytest.raises(ValueError):
        canonical_url("http://localhost/metadata")


async def test_public_metadata_seed_fallback_and_cdn_redirect_download(tmp_path):
    requested = []
    data = b"\x00\x00\x00\x18ftypmp42" + b"video" * 100

    def handler(request):
        requested.append(str(request.url))
        if request.url.host == "www.tiktok.com":
            return httpx.Response(200, text=html_page())
        if request.url.path == "/video.mp4":
            return httpx.Response(302, headers={"location": "https://v.tiktokcdn.com/final.mp4"})
        return httpx.Response(200, content=data)

    settings = Settings(
        browser_enabled=False, request_delay_seconds=0, seed_urls_file=tmp_path / "seeds.txt"
    )
    settings.seed_urls_file.write_text(
        "https://www.tiktok.com/@demo/video/1234567890\n", encoding="utf-8"
    )
    provider = PublicTikTokProvider(
        settings, httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        videos = await provider.search("фильм", 3)
        assert videos[0].metrics.views == 18000
        assert len(requested) == 1
        await provider.search("сериал", 3)
        assert len(requested) == 1  # Seed metadata only once per scan.
        destination = tmp_path / "selected.mp4"
        await provider.download(videos[0], destination)
        assert destination.read_bytes() == data and not destination.with_suffix(".part").exists()
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "content", [b"not an mp4", b"\x00ftyp" + b"x" * 1000001], ids=["invalid", "oversize"]
)
async def test_bad_or_oversize_media_is_removed(tmp_path, content):
    def handler(request):
        return (
            httpx.Response(200, text=html_page())
            if request.url.host == "www.tiktok.com"
            else httpx.Response(200, content=content)
        )

    provider = PublicTikTokProvider(
        Settings(max_media_mb=1, browser_enabled=False),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    destination = tmp_path / "selected.mp4"
    try:
        with pytest.raises(ProviderUnavailable):
            await provider.download(parse_items(payload())[0], destination)
        assert not destination.exists() and not destination.with_suffix(".part").exists()
    finally:
        await provider.close()


async def test_browser_failure_falls_back_and_is_reported(tmp_path):
    provider = PublicTikTokProvider(Settings(seed_urls_file=tmp_path / "empty"))
    provider._page_items = AsyncMock(side_effect=ProviderUnavailable("CAPTCHA требуется"))
    try:
        with pytest.raises(ProviderUnavailable, match="CAPTCHA"):
            await provider.search("фильм", 3)
        with pytest.raises(ProviderUnavailable):
            await provider.search("сериал", 3)
        assert provider._page_items.await_count == 1  # Stop search after first blocked request.
    finally:
        await provider.close()


async def test_source_cascade_only_explicit_evidence(system):
    source = await system.identifier.identify(system.provider.videos[0], None)
    assert source.title == "Пример фильма" and source.method == "caption"
    assert explicit_title("Очень крутой фильм! #рек", "caption", 0.7).title is None
    video = system.provider.videos[0]
    video.caption = "Момент из фильма"
    video.subtitles = "Название фильма: Бойцовский клуб"
    assert (await system.identifier.identify(video, None)).method == "subtitles"
    video.subtitles = ""
    system.settings.ocr_enabled = True
    identifier = CascadeSourceIdentifier(system.settings)
    identifier._ocr = AsyncMock(return_value="Фильм «Матрица»")
    assert (
        await identifier.identify(video, system.settings.media_dir / "video.mp4")
    ).title == "Матрица"
    identifier._ocr = AsyncMock(side_effect=RuntimeError("offline"))
    assert (await identifier.identify(video, system.settings.media_dir / "video.mp4")).title is None


async def telegram_with_mock(handler):
    client = TelegramClient("000:fake")
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_real_telegram_transport_payload_and_rate_limit(monkeypatch):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    monkeypatch.setattr("app.telegram.asyncio.sleep", AsyncMock())
    client = await telegram_with_mock(handler)
    try:
        assert await client.send_text(123, "Текст") == 42
        assert len(calls) == 2 and calls[1]["chat_id"] == 123
    finally:
        await client.close()


async def test_real_telegram_does_not_retry_ambiguous_request():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("server might already have accepted message")

    client = await telegram_with_mock(handler)
    try:
        with pytest.raises(TelegramUncertain):
            await client.send_text(1, "Текст")
        assert len(calls) == 1
    finally:
        await client.close()


async def test_real_telegram_explicit_rejection():
    client = await telegram_with_mock(
        lambda request: httpx.Response(
            403, json={"ok": False, "error_code": 403, "description": "blocked"}
        )
    )
    try:
        with pytest.raises(TelegramRejected) as result:
            await client.send_text(1, "Текст")
        assert result.value.code == 403
    finally:
        await client.close()


def test_config_and_logging_secrets(system):
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        Settings().validate()
    settings = Settings(timezone="invalid")
    with pytest.raises(ValueError, match="TIMEZONE"):
        settings.validate(live=False)
    settings = Settings(request_timeout=float("nan"))
    with pytest.raises(ValueError):
        settings.validate(live=False)
    formatter = SafeFormatter([system.settings.user_password, "000:fake"])
    record = logging.LogRecord("test", logging.ERROR, "", 0, "user-secret bot000:fake", (), None)
    output = formatter.format(record)
    assert "user-secret" not in output and "000:fake" not in output
