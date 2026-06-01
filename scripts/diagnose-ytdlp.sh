#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [ ! -d ".venv" ]; then
  echo "No existe .venv. Ejecuta primero: bash scripts/run.sh --install-only"
  exit 1
fi

. .venv/bin/activate

echo "python: $(python --version)"
echo "yt-dlp: $(python -c 'import yt_dlp; print(yt_dlp.version.__version__)')"
echo "node: $(command -v node || true) $(node --version 2>/dev/null || true)"
echo "cookies: ${YTDLP_COOKIES_FILE:-<no exportada en shell; se leera de .env>}"

python - <<'PY'
from bot.config import load_settings
from bot.downloader import VideoDownloader

settings = load_settings()
downloader = VideoDownloader(
    download_dir=settings.download_dir,
    max_upload_bytes=settings.max_upload_bytes,
    timeout_seconds=settings.download_timeout_seconds,
    metadata_timeout_seconds=settings.metadata_timeout_seconds,
    cookies_file=settings.ytdlp_cookies_file,
    js_runtimes=settings.ytdlp_js_runtimes,
    remote_components=settings.ytdlp_remote_components,
    max_video_height=settings.max_video_height,
)
opts = downloader._base_ydl_opts()
safe = {
    key: ("<set>" if key == "cookiefile" else value)
    for key, value in opts.items()
    if key not in {"logger"}
}
print(f"ytdlp_opts: {safe}")
PY
