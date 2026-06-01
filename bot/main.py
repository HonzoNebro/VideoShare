from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.cache import VideoCache
from bot.config import load_settings
from bot.downloader import VideoDownloader
from bot.handlers import (
    BotServices,
    cleanup_download_dir,
    handle_message,
    handle_variant_callback,
    id_command,
    start,
    status_command,
)


class TokenRedactionFilter(logging.Filter):
    TOKEN_RE = re.compile(r"/bot[^/\s]+/")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.TOKEN_RE.sub("/bot<redacted>/", str(record.msg))
        if record.args:
            record.args = tuple(
                self.TOKEN_RE.sub("/bot<redacted>/", str(arg)) for arg in record.args
            )
        return True


def _install_log_redaction() -> None:
    redaction_filter = TokenRedactionFilter()
    logging.getLogger().addFilter(redaction_filter)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redaction_filter)

    old_factory = logging.getLogRecordFactory()

    def record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        record.msg = TokenRedactionFilter.TOKEN_RE.sub("/bot<redacted>/", str(record.msg))
        if record.args:
            record.args = tuple(
                TokenRedactionFilter.TOKEN_RE.sub("/bot<redacted>/", str(arg))
                for arg in record.args
            )
        return record

    logging.setLogRecordFactory(record_factory)


def build_application() -> Application:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _install_log_redaction()
    for logger_name in ("httpx", "httpcore", "telegram", "telegram.ext"):
        logging.getLogger(logger_name).setLevel(
            getattr(logging, settings.http_log_level, logging.WARNING)
        )

    cleanup_download_dir(settings.download_dir)
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
    cache = VideoCache(settings.cache_db_path, ttl_days=settings.cache_ttl_days)

    builder = Application.builder().token(settings.telegram_bot_token)
    builder = builder.connect_timeout(settings.telegram_connect_timeout_seconds)
    builder = builder.read_timeout(settings.telegram_read_timeout_seconds)
    builder = builder.write_timeout(settings.telegram_write_timeout_seconds)
    builder = builder.pool_timeout(settings.telegram_pool_timeout_seconds)
    if settings.telegram_api_base_url:
        builder = builder.base_url(settings.telegram_api_base_url)
    if settings.telegram_api_base_file_url:
        builder = builder.base_file_url(settings.telegram_api_base_file_url)
    if settings.telegram_local_mode:
        builder = builder.local_mode(True)
    application = builder.build()
    application.bot_data["services"] = BotServices(
        settings=settings,
        downloader=downloader,
        cache=cache,
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(handle_variant_callback, pattern=r"^vs:"))
    application.add_handler(MessageHandler(filters.TEXT | filters.CaptionRegex(r"https?://"), handle_message))
    return application


def main() -> None:
    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
