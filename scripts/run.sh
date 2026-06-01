#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

INSTALL=false
INSTALL_ONLY=false
case "${1:-}" in
  --install)
    INSTALL=true
    ;;
  --install-only)
    INSTALL=true
    INSTALL_ONLY=true
    ;;
  "")
    ;;
  *)
    echo "Uso: $0 [--install|--install-only]"
    exit 2
    ;;
esac

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  INSTALL=true
fi

. .venv/bin/activate
if [ "$INSTALL" = true ]; then
  pip install -r requirements.txt
fi

if [ "$INSTALL_ONLY" = true ]; then
  exit 0
fi

python -m bot.main
