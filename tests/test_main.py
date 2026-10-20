from bot.config import Settings
from bot.main import build_application


def test_media_upload_uses_configured_write_timeout(tmp_path, monkeypatch) -> None:
    settings = Settings(
        telegram_bot_token="123456:example",
        allowed_user_ids=set(),
        allowed_chat_ids=set(),
        download_dir=tmp_path / "downloads",
        cache_db_path=tmp_path / "cache.sqlite3",
        telegram_write_timeout_seconds=456,
    )
    monkeypatch.setattr("bot.main.load_settings", lambda: settings)

    application = build_application()

    assert application.bot.request._media_write_timeout == 456


def test_application_registers_help_command(tmp_path, monkeypatch) -> None:
    settings = Settings(
        telegram_bot_token="123456:example",
        allowed_user_ids=set(),
        allowed_chat_ids=set(),
        download_dir=tmp_path / "downloads",
        cache_db_path=tmp_path / "cache.sqlite3",
    )
    monkeypatch.setattr("bot.main.load_settings", lambda: settings)

    application = build_application()

    commands = {
        command
        for handler in application.handlers[0]
        if hasattr(handler, "commands")
        for command in handler.commands
    }
    assert {"start", "help", "id", "status"} <= commands
