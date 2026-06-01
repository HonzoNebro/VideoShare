#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/videoshare-bot}"
SERVICE_USER="${SERVICE_USER:-$USER}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 no esta instalado."
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg no esta instalado. Ejecuta: sudo apt install -y ffmpeg"
  exit 1
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "Creado $APP_DIR/.env. Editalo antes de arrancar el bot."
fi

if [ -f "$APP_DIR/systemd/videoshare-bot.service" ]; then
  sed "s/^User=.*/User=$SERVICE_USER/" "$APP_DIR/systemd/videoshare-bot.service" \
    | sudo tee /etc/systemd/system/videoshare-bot.service >/dev/null
  sudo systemctl daemon-reload
  echo "Servicio systemd instalado. Arranca con: sudo systemctl enable --now videoshare-bot"
fi

echo "Instalacion completada."
