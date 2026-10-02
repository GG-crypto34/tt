"""Save the user's own ordinary browser login; login/CAPTCHA are performed by the user."""

import argparse
import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


def login(destination: Path) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=False)
        try:
            context = browser.new_context(locale="ru-RU")
            page = context.new_page()
            page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
            input(
                "Войдите в свой TikTok-аккаунт в открывшемся браузере. "
                "После успешного входа нажмите Enter здесь. "
                "Пароль и проверки вводятся только в браузере: "
            )
            fd, temporary = tempfile.mkstemp(
                prefix=".tiktok-session-", suffix=".tmp", dir=destination.parent
            )
            os.close(fd)
            path = Path(temporary)
            try:
                context.storage_state(path=str(path))
                path.replace(destination)
            finally:
                path.unlink(missing_ok=True)
            print(f"Сессия сохранена: {destination}")
            print("Укажите этот файл в TIKTOK_STORAGE_STATE на VPS. Не публикуйте файл сессии.")
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Сохранить собственную TikTok-сессию для бота")
    parser.add_argument("--output", type=Path, default=Path("data/tiktok-session.json"))
    args = parser.parse_args()
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        str(Path(__file__).resolve().parents[1] / ".tools" / "ms-playwright"),
    )
    login(args.output)
