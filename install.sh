#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]] || [[ ! -f /etc/os-release ]]; then
  echo "Поддерживается Ubuntu 24.04 Linux." >&2
  exit 1
fi
source /etc/os-release
if [[ "$ID" != "ubuntu" || "$VERSION_ID" != "24.04" ]]; then
  echo "Этот установщик предназначен для Ubuntu 24.04." >&2
  exit 1
fi
if [[ "$EUID" -eq 0 ]]; then
  echo "Запустите ./install.sh от обычного пользователя с sudo." >&2
  exit 1
fi
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$project_dir"
if [[ "$project_dir" =~ [[:space:]] ]] || [[ "$project_dir" == *%* ]]; then
  echo "Для systemd разместите проект в пути без пробелов и %." >&2
  exit 1
fi
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg tesseract-ocr \
  tesseract-ocr-rus tesseract-ocr-eng
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
export PLAYWRIGHT_BROWSERS_PATH="$project_dir/.tools/ms-playwright"
.venv/bin/python -m playwright install-deps chromium
.venv/bin/python -m playwright install chromium --only-shell
mkdir -p data media logs
chmod 700 data media logs
if [[ ! -e .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi
service_file="$(mktemp)"
trap 'rm -f -- "$service_file"' EXIT
sed -e "s|@PROJECT_DIR@|$project_dir|g" -e "s|@SERVICE_USER@|$(id -un)|g" \
  systemd/movie-trend-bot.service > "$service_file"
sudo install -m 644 "$service_file" /etc/systemd/system/movie-trend-bot.service
sudo systemctl daemon-reload
echo "Установлено. Заполните .env (nano .env), затем:"
echo "  .venv/bin/python -m app.main --check-config"
echo "  .venv/bin/python -m app.main --smoke"
echo "  sudo systemctl enable --now movie-trend-bot"
