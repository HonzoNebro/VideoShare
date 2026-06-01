from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


def _parse_int_set(raw: str | None) -> set[int]:
    if not raw:
        return set()

    values: set[int] = set()
    for item in raw.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        try:
            values.add(int(stripped))
        except ValueError as exc:
            raise ValueError(f"Expected comma-separated integer ids, got {stripped!r}") from exc
    return values


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    return int(raw.strip())


def _parse_caption_mode(raw: str | None, default: str) -> str:
    mode = (raw or "").strip().lower()
    if mode in {"full", "link", "none"}:
        return mode
    return default


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: set[int]
    allowed_chat_ids: set[int]
    download_dir: Path
    max_upload_mb: int = 1024
    max_description_chars: int = 650
    metadata_timeout_seconds: int = 60
    download_timeout_seconds: int = 600
    upload_timeout_seconds: int = 300
    cache_db_path: Path = Path("/tmp/videoshare-bot/cache.sqlite3")
    cache_ttl_days: int = 30
    max_concurrent_jobs: int = 1
    progress_update_interval_seconds: float = 10.0
    max_video_duration_seconds: int = 1200
    max_estimated_download_mb: int = 1024
    max_video_height: int = 480
    caption_mode_private: str = "full"
    caption_mode_group: str = "none"
    error_report_user_id: int | None = None
    quiet_unauthorized: bool = False
    log_level: str = "INFO"
    http_log_level: str = "WARNING"
    telegram_api_base_url: str | None = None
    telegram_api_base_file_url: str | None = None
    telegram_local_mode: bool = False
    ytdlp_cookies_file: Path | None = None
    ytdlp_js_runtimes: str | None = None
    ytdlp_remote_components: str | None = None

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def load_settings(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file, override=True)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    download_dir = Path(os.getenv("DOWNLOAD_DIR", "/tmp/videoshare-bot")).expanduser()
    cache_db_raw = os.getenv("CACHE_DB_PATH", "").strip()

    return Settings(
        telegram_bot_token=token,
        allowed_user_ids=_parse_int_set(os.getenv("ALLOWED_USER_IDS")),
        allowed_chat_ids=_parse_int_set(os.getenv("ALLOWED_CHAT_IDS")),
        download_dir=download_dir,
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "1024")),
        max_description_chars=int(os.getenv("MAX_DESCRIPTION_CHARS", "650")),
        metadata_timeout_seconds=int(os.getenv("METADATA_TIMEOUT_SECONDS", "60")),
        download_timeout_seconds=int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "600")),
        upload_timeout_seconds=int(os.getenv("UPLOAD_TIMEOUT_SECONDS", "300")),
        cache_db_path=Path(cache_db_raw).expanduser() if cache_db_raw else download_dir / "cache.sqlite3",
        cache_ttl_days=int(os.getenv("CACHE_TTL_DAYS", "30")),
        max_concurrent_jobs=max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1"))),
        progress_update_interval_seconds=max(
            1.0,
            float(os.getenv("PROGRESS_UPDATE_INTERVAL_SECONDS", "10")),
        ),
        max_video_duration_seconds=int(os.getenv("MAX_VIDEO_DURATION_SECONDS", "1200")),
        max_estimated_download_mb=int(os.getenv("MAX_ESTIMATED_DOWNLOAD_MB", "1024")),
        max_video_height=max(144, int(os.getenv("MAX_VIDEO_HEIGHT", "480"))),
        caption_mode_private=_parse_caption_mode(os.getenv("CAPTION_MODE_PRIVATE"), "full"),
        caption_mode_group=_parse_caption_mode(os.getenv("CAPTION_MODE_GROUP"), "none"),
        error_report_user_id=_parse_optional_int(os.getenv("ERROR_REPORT_USER_ID")),
        quiet_unauthorized=_parse_bool(os.getenv("QUIET_UNAUTHORIZED"), default=False),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        http_log_level=os.getenv("HTTP_LOG_LEVEL", "WARNING").upper(),
        telegram_api_base_url=os.getenv("TELEGRAM_API_BASE_URL", "").strip() or None,
        telegram_api_base_file_url=os.getenv("TELEGRAM_API_BASE_FILE_URL", "").strip() or None,
        telegram_local_mode=_parse_bool(os.getenv("TELEGRAM_LOCAL_MODE"), default=False),
        ytdlp_cookies_file=(
            Path(os.getenv("YTDLP_COOKIES_FILE", "")).expanduser()
            if os.getenv("YTDLP_COOKIES_FILE", "").strip()
            else None
        ),
        ytdlp_js_runtimes=os.getenv("YTDLP_JS_RUNTIMES", "").strip() or None,
        ytdlp_remote_components=os.getenv("YTDLP_REMOTE_COMPONENTS", "").strip() or None,
    )
