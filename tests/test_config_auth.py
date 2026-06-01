from pathlib import Path
from types import SimpleNamespace

from bot.auth import is_allowed, is_group_allowed, is_private_allowed
from bot.config import Settings, load_settings


def _settings(
    allowed_user_ids: set[int] | None = None,
    allowed_chat_ids: set[int] | None = None,
) -> Settings:
    return Settings(
        telegram_bot_token="token",
        allowed_user_ids=allowed_user_ids or set(),
        allowed_chat_ids=allowed_chat_ids or set(),
        download_dir=Path("/tmp/videoshare-test"),
    )


def test_is_allowed_accepts_everyone_when_no_allowlist() -> None:
    assert is_allowed(_settings(), SimpleNamespace(id=1), SimpleNamespace(id=-1))


def test_is_allowed_accepts_configured_user_or_chat() -> None:
    assert is_allowed(_settings(allowed_user_ids={10}), SimpleNamespace(id=10), SimpleNamespace(id=-1))
    assert is_allowed(_settings(allowed_chat_ids={-20}), SimpleNamespace(id=1), SimpleNamespace(id=-20))


def test_is_allowed_rejects_unlisted_user_and_chat() -> None:
    assert not is_allowed(_settings({10}, {-20}), SimpleNamespace(id=1), SimpleNamespace(id=-1))


def test_private_allowed_only_checks_user_allowlist() -> None:
    settings = _settings(allowed_user_ids={10}, allowed_chat_ids={-20})

    assert is_private_allowed(settings, SimpleNamespace(id=10))
    assert not is_private_allowed(settings, SimpleNamespace(id=1))


def test_group_allowed_only_checks_chat_allowlist() -> None:
    settings = _settings(allowed_user_ids={10}, allowed_chat_ids={-20})

    assert is_group_allowed(settings, SimpleNamespace(id=-20))
    assert not is_group_allowed(settings, SimpleNamespace(id=-1))


def test_load_settings_parses_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=abc",
                "ALLOWED_USER_IDS=1, 2",
                "ALLOWED_CHAT_IDS=-100",
                f"DOWNLOAD_DIR={tmp_path}",
                "MAX_UPLOAD_MB=49",
                "METADATA_TIMEOUT_SECONDS=11",
                "DOWNLOAD_TIMEOUT_SECONDS=22",
                "UPLOAD_TIMEOUT_SECONDS=33",
                "MAX_CONCURRENT_JOBS=2",
                "PROGRESS_UPDATE_INTERVAL_SECONDS=12.5",
                "MAX_VIDEO_DURATION_SECONDS=44",
                "MAX_ESTIMATED_DOWNLOAD_MB=55",
                "MAX_VIDEO_HEIGHT=720",
                "CAPTION_MODE_PRIVATE=link",
                "CAPTION_MODE_GROUP=full",
                "ERROR_REPORT_USER_ID=99",
                "HTTP_LOG_LEVEL=ERROR",
                "TELEGRAM_API_BASE_URL=http://127.0.0.1:8081/bot",
                "TELEGRAM_API_BASE_FILE_URL=http://127.0.0.1:8081/file/bot",
                "TELEGRAM_LOCAL_MODE=true",
                f"YTDLP_COOKIES_FILE={tmp_path / 'cookies.txt'}",
                "YTDLP_JS_RUNTIMES=deno",
                "YTDLP_REMOTE_COMPONENTS=ejs:npm",
                "QUIET_UNAUTHORIZED=true",
            ]
        )
    )

    settings = load_settings(env_file)

    assert settings.telegram_bot_token == "abc"
    assert settings.allowed_user_ids == {1, 2}
    assert settings.allowed_chat_ids == {-100}
    assert settings.download_dir == tmp_path
    assert settings.cache_db_path == tmp_path / "cache.sqlite3"
    assert settings.max_upload_mb == 49
    assert settings.metadata_timeout_seconds == 11
    assert settings.download_timeout_seconds == 22
    assert settings.upload_timeout_seconds == 33
    assert settings.max_concurrent_jobs == 2
    assert settings.progress_update_interval_seconds == 12.5
    assert settings.max_video_duration_seconds == 44
    assert settings.max_estimated_download_mb == 55
    assert settings.max_video_height == 720
    assert settings.caption_mode_private == "link"
    assert settings.caption_mode_group == "full"
    assert settings.error_report_user_id == 99
    assert settings.http_log_level == "ERROR"
    assert settings.telegram_api_base_url == "http://127.0.0.1:8081/bot"
    assert settings.telegram_api_base_file_url == "http://127.0.0.1:8081/file/bot"
    assert settings.telegram_local_mode is True
    assert settings.ytdlp_cookies_file == tmp_path / "cookies.txt"
    assert settings.ytdlp_js_runtimes == "deno"
    assert settings.ytdlp_remote_components == "ejs:npm"
    assert settings.quiet_unauthorized is True


def test_load_settings_caption_modes_default_and_invalid(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=abc",
                f"DOWNLOAD_DIR={tmp_path}",
                "CAPTION_MODE_PRIVATE=bad",
                "CAPTION_MODE_GROUP=",
            ]
        )
    )

    settings = load_settings(env_file)

    assert settings.caption_mode_private == "full"
    assert settings.caption_mode_group == "none"


def test_load_settings_uses_large_file_defaults(tmp_path: Path, monkeypatch) -> None:
    for name in (
        "MAX_UPLOAD_MB",
        "MAX_ESTIMATED_DOWNLOAD_MB",
        "MAX_VIDEO_DURATION_SECONDS",
        "TELEGRAM_API_BASE_URL",
        "TELEGRAM_API_BASE_FILE_URL",
        "TELEGRAM_LOCAL_MODE",
    ):
        monkeypatch.delenv(name, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=abc",
                f"DOWNLOAD_DIR={tmp_path}",
            ]
        )
    )

    settings = load_settings(env_file)

    assert settings.max_upload_mb == 1024
    assert settings.max_estimated_download_mb == 1024
    assert settings.max_video_duration_seconds == 1200
    assert settings.telegram_api_base_url is None
    assert settings.telegram_api_base_file_url is None
    assert settings.telegram_local_mode is False
