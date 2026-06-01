#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [ ! -d ".venv" ]; then
  echo "No existe .venv. Ejecuta primero: bash scripts/run.sh --install-only"
  exit 1
fi

. .venv/bin/activate
pip install --upgrade "yt-dlp[default]"

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files videoshare-bot.service >/dev/null 2>&1; then
  sudo systemctl restart videoshare-bot
  echo "yt-dlp actualizado y servicio reiniciado."
else
  echo "yt-dlp actualizado. Reinicia el bot manualmente si esta ejecutandose."
fi
