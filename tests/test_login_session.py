import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.login_tiktok import login


@pytest.mark.parametrize("fail", [False, True])
def test_session_export_is_atomic_and_closes_browser(monkeypatch, tmp_path, fail):
    destination = tmp_path / "session.json"
    destination.write_text("previous-session", encoding="utf-8")
    browser = MagicMock()
    context = browser.new_context.return_value

    def save(path):
        if fail:
            raise RuntimeError("save failed")
        Path(path).write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    context.storage_state.side_effect = save
    manager = MagicMock()
    manager.__enter__.return_value.chromium.launch.return_value = browser
    monkeypatch.setattr("scripts.login_tiktok.sync_playwright", lambda: manager)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    if fail:
        with pytest.raises(RuntimeError, match="save failed"):
            login(destination)
        assert destination.read_text(encoding="utf-8") == "previous-session"
    else:
        login(destination)
        assert json.loads(destination.read_text(encoding="utf-8")) == {"cookies": [], "origins": []}
        if os.name == "posix":
            assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".tiktok-session-*.tmp"))
    browser.close.assert_called_once()
    context.new_page.return_value.goto.assert_called_once_with(
        "https://www.tiktok.com/login", wait_until="domcontentloaded"
    )
