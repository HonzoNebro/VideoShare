from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html import escape
import logging
from pathlib import Path
import secrets
import shutil
import time
import traceback

from telegram import Update
from telegram import Message
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import TelegramError, TimedOut
from telegram.ext import ContextTypes

from bot.auth import is_group_allowed, is_private_allowed
from bot.cache import CacheEntry, VideoCache
from bot.config import Settings
from bot.downloader import (
    DownloadError,
    DownloadVariant,
    OversizeError,
    VideoDownloader,
    VideoMetadata,
    VideoResult,
    variant_cache_key,
)
from bot.formatting import build_caption, extract_urls, text_mentions_bot
from bot.url_normalizer import normalize_url

LOGGER = logging.getLogger(__name__)
CALLBACK_PREFIX = "vs"


@dataclass(frozen=True)
class SendTarget:
    chat_id: int
    chat_type: str | None
    reply_to_message_id: int | None

    @property
    def type(self) -> str | None:
        return self.chat_type


@dataclass(frozen=True)
class PendingSelection:
    source_url: str
    normalized_url: str
    metadata: VideoMetadata
    target: SendTarget
    created_at: float


class BotServices:
    def __init__(
        self,
        *,
        settings: Settings,
        downloader: VideoDownloader,
        cache: VideoCache,
    ) -> None:
        self.settings = settings
        self.downloader = downloader
        self.cache = cache
        self._job_semaphore: asyncio.Semaphore | None = None
        self._cache_key_locks: dict[str, asyncio.Lock] = {}
        self.pending_selections: dict[str, PendingSelection] = {}
        self.active_jobs = 0
        self.queued_jobs = 0

    @property
    def job_semaphore(self) -> asyncio.Semaphore:
        if self._job_semaphore is None:
            self._job_semaphore = asyncio.Semaphore(self.settings.max_concurrent_jobs)
        return self._job_semaphore

    def cache_key_lock(self, cache_key: str) -> asyncio.Lock:
        lock = self._cache_key_locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._cache_key_locks[cache_key] = lock
        return lock


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = _services(context)
    if not update.effective_message:
        return
    if not _is_update_allowed(update, context, services):
        if _should_warn_unauthorized(update, services):
            await update.effective_message.reply_text("No estas autorizado para usar este bot.")
        return

    await update.effective_message.reply_text(
        "Enviame un enlace publico de video y te lo devolvere listo para compartir."
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat:
        return

    lines = [
        f"user_id: {user.id if user else 'desconocido'}",
        f"chat_id: {chat.id}",
        f"chat_type: {chat.type}",
    ]
    if message.reply_to_message and message.reply_to_message.from_user:
        lines.append(f"reply_user_id: {message.reply_to_message.from_user.id}")

    await message.reply_text("\n".join(lines))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = _services(context)
    message = update.effective_message
    if not message:
        return
    if not _is_command_allowed(update, services):
        if _should_warn_unauthorized(update, services):
            await message.reply_text("No estas autorizado para usar este bot.")
        return

    stats = services.cache.stats()
    lines = [
        f"trabajos_activos: {services.active_jobs}",
        f"trabajos_en_cola: {services.queued_jobs}",
        f"capacidad: {services.settings.max_concurrent_jobs}",
        f"cache_videos: {stats.video_entries}",
        f"cache_aliases: {stats.url_aliases}",
        f"cache_rechazos: {stats.rejected_entries}",
        f"cache_ttl_dias: {stats.ttl_days}",
        f"cache_db: {stats.db_path}",
    ]
    await message.reply_text("\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = _services(context)
    message = update.effective_message
    if not message:
        return

    if not _is_update_allowed(update, context, services):
        if _should_warn_unauthorized(update, services):
            await message.reply_text("No estas autorizado para usar este bot.")
        return

    urls = extract_urls(message.text or message.caption)
    if not urls:
        return

    status = await message.reply_text(_progress_text("Preparando", 1, len(urls), 5))
    for index, url in enumerate(urls, start=1):
        timeout_message = "La operacion ha tardado demasiado y se ha cancelado."
        progress = ProgressEditor(
            status,
            index,
            len(urls),
            edit_interval_seconds=services.settings.progress_update_interval_seconds,
        )
        try:
            if update.effective_chat:
                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action=ChatAction.TYPING,
                )
            await progress.update("Obteniendo informacion", 10, force=True)
            normalized_url = normalize_url(url)
            alias_key = services.cache.get_alias(normalized_url)
            if alias_key:
                rejected = services.cache.get_rejection(alias_key)
                if rejected:
                    await progress.update("Rechazado desde cache", 100, force=True)
                    await message.reply_text(f"{rejected.message}\n\n{url}")
                    continue

            timeout_message = "La extraccion de informacion ha tardado demasiado y se ha cancelado."
            metadata = await services.downloader.extract_metadata(url, progress=progress.threadsafe_update)
            services.cache.put_alias(normalized_url=normalized_url, cache_key=metadata.cache_key)
            rejected = services.cache.get_rejection(metadata.cache_key)
            if rejected:
                await progress.update("Rechazado desde cache", 100, force=True)
                await message.reply_text(f"{rejected.message}\n\n{url}")
                continue
            try:
                _validate_metadata_limits(services.settings, metadata)
            except OversizeError as exc:
                services.cache.put_rejection(
                    cache_key=metadata.cache_key,
                    message=str(exc),
                    **_metadata_cache_fields(metadata),
                )
                await message.reply_text(f"{exc}\n\n{url}")
                continue

            if not update.effective_chat:
                continue
            token = _create_pending_selection(
                services,
                source_url=url,
                normalized_url=normalized_url,
                metadata=metadata,
                target=SendTarget(
                    chat_id=update.effective_chat.id,
                    chat_type=update.effective_chat.type,
                    reply_to_message_id=message.message_id,
                ),
            )
            await progress.update("Esperando seleccion", 100, force=True)
            await message.reply_text(
                _selection_text(metadata, url),
                reply_markup=_kind_keyboard(token),
                disable_web_page_preview=True,
            )
        except asyncio.TimeoutError:
            await message.reply_text(timeout_message)
        except OversizeError as exc:
            await message.reply_text(f"{exc}\n\n{url}")
        except DownloadError as exc:
            await message.reply_text(str(exc))
        except TelegramError as exc:
            if _is_message_not_modified(exc):
                LOGGER.debug("Ignoring Telegram no-op message edit", exc_info=True)
                continue
            LOGGER.exception("Telegram failed while sending %s", url)
            await _notify_admin_error(context, services, update, url, exc, "Error de Telegram")
        except Exception as exc:
            LOGGER.exception("Unexpected error while processing %s", url)
            await _notify_admin_error(context, services, update, url, exc, "Error inesperado")

        if len(urls) > 1 and index < len(urls):
            await _safe_edit_status(status, _progress_text("Siguiente enlace", index + 1, len(urls), 5))

    await _safe_delete_status(status)


async def handle_variant_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = _services(context)
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX:
        return

    token = parts[1]
    action = parts[2]
    pending = services.pending_selections.get(token)
    if not pending:
        await query.answer("La seleccion ha caducado.")
        await _edit_query_message(
            query.message,
            "La seleccion ha caducado. Reenvia el enlace para elegir formato y calidad.",
        )
        return

    await query.answer()
    if action == "kind" and len(parts) == 4:
        kind = parts[3]
        if kind not in {"video", "audio"}:
            return
        await _edit_query_message(
            query.message,
            _quality_text(pending.metadata, kind),
            reply_markup=_quality_keyboard(token, kind),
        )
        return

    if action != "quality" or len(parts) != 5:
        return

    kind = parts[3]
    quality = parts[4]
    try:
        variant = DownloadVariant(kind, quality)
    except ValueError:
        return

    services.pending_selections.pop(token, None)
    await _process_variant_selection(update, context, services, pending, variant)


async def _process_variant_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: BotServices,
    pending: PendingSelection,
    variant: DownloadVariant,
) -> None:
    status = update.effective_message
    if not status:
        return

    result: VideoResult | None = None
    progress = ProgressEditor(
        status,
        1,
        1,
        edit_interval_seconds=services.settings.progress_update_interval_seconds,
    )
    cache_key = variant_cache_key(pending.metadata, variant)
    timeout_message = "La operacion ha tardado demasiado y se ha cancelado."
    try:
        await progress.update("Preparando seleccion", 5, force=True)
        rejected = services.cache.get_rejection(cache_key)
        if rejected:
            await progress.update("Rechazado desde cache", 100, force=True)
            await context.bot.send_message(
                chat_id=pending.target.chat_id,
                text=f"{rejected.message}\n\n{pending.source_url}",
                reply_to_message_id=pending.target.reply_to_message_id,
            )
            return

        async with services.cache_key_lock(cache_key):
            cached = services.cache.get(cache_key)
            if cached:
                await progress.update("Enviando desde cache", 90, force=True)
                try:
                    await _send_cached(context, services, pending.metadata, pending.target, cached)
                    await progress.update("Completado desde cache", 100, force=True)
                    return
                except TelegramError:
                    LOGGER.info("Cached Telegram file_id failed, deleting cache entry", exc_info=True)
                    services.cache.delete(cache_key)

            await _acquire_job_slot(services, progress)
            try:
                if variant.kind == "audio":
                    await context.bot.send_chat_action(
                        chat_id=pending.target.chat_id,
                        action=ChatAction.UPLOAD_DOCUMENT,
                    )
                else:
                    await context.bot.send_chat_action(
                        chat_id=pending.target.chat_id,
                        action=ChatAction.UPLOAD_VIDEO,
                    )
                await progress.update("Procesando", 20, force=True)
                timeout_message = "La descarga o conversion ha tardado demasiado y se ha cancelado."
                result = await services.downloader.fetch_with_metadata(
                    pending.metadata,
                    variant=variant,
                    progress=progress.threadsafe_update,
                )
                await progress.update("Subiendo a Telegram", 85, force=True)
                timeout_message = "La subida a Telegram ha tardado demasiado y se ha cancelado."
                await asyncio.wait_for(
                    _send_result(context, services, result, pending.target),
                    timeout=services.settings.upload_timeout_seconds,
                )
                await progress.update("Completado", 100, force=True)
            finally:
                _release_job_slot(services)
    except asyncio.TimeoutError:
        await context.bot.send_message(
            chat_id=pending.target.chat_id,
            text=timeout_message,
            reply_to_message_id=pending.target.reply_to_message_id,
        )
    except OversizeError as exc:
        services.cache.put_rejection(
            cache_key=cache_key,
            message=str(exc),
            **_metadata_cache_fields(pending.metadata),
        )
        await context.bot.send_message(
            chat_id=pending.target.chat_id,
            text=f"{exc}\n\n{pending.source_url}",
            reply_to_message_id=pending.target.reply_to_message_id,
        )
    except DownloadError as exc:
        await context.bot.send_message(
            chat_id=pending.target.chat_id,
            text=str(exc),
            reply_to_message_id=pending.target.reply_to_message_id,
        )
    except TelegramError as exc:
        if _is_message_not_modified(exc):
            LOGGER.debug("Ignoring Telegram no-op message edit", exc_info=True)
            return
        LOGGER.exception("Telegram failed while sending %s", pending.source_url)
        await _notify_admin_error(
            context,
            services,
            update,
            pending.source_url,
            exc,
            "Error de Telegram",
        )
    except Exception as exc:
        LOGGER.exception("Unexpected error while processing %s", pending.source_url)
        await _notify_admin_error(
            context,
            services,
            update,
            pending.source_url,
            exc,
            "Error inesperado",
        )
    finally:
        if result:
            services.downloader.cleanup_result(result)


async def _send_result(
    context: ContextTypes.DEFAULT_TYPE,
    services: BotServices,
    result: VideoResult,
    target: SendTarget,
) -> None:
    caption = _caption_for_target(result.metadata, services, target)
    parse_mode = ParseMode.HTML if caption else None

    if result.variant.kind == "audio":
        with result.file_path.open("rb") as audio:
            try:
                sent = await context.bot.send_audio(
                    chat_id=target.chat_id,
                    audio=audio,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_to_message_id=target.reply_to_message_id,
                )
                _cache_sent_message(services, result.metadata, result.variant, sent)
                return
            except TimedOut:
                LOGGER.info("send_audio timed out while waiting for Telegram response", exc_info=True)
                raise
            except TelegramError:
                LOGGER.info("send_audio failed, trying send_document", exc_info=True)

    with result.file_path.open("rb") as video:
        try:
            sent = await context.bot.send_video(
                chat_id=target.chat_id,
                video=video,
                caption=caption,
                parse_mode=parse_mode,
                supports_streaming=True,
                reply_to_message_id=target.reply_to_message_id,
            )
            _cache_sent_message(services, result.metadata, result.variant, sent)
            return
        except TimedOut:
            LOGGER.info("send_video timed out while waiting for Telegram response", exc_info=True)
            raise
        except TelegramError:
            LOGGER.info("send_video failed, trying send_document", exc_info=True)

    if result.file_path.stat().st_size > services.settings.max_upload_bytes:
        media_label = "audio" if result.variant.kind == "audio" else "video"
        raise OversizeError(f"El {media_label} es demasiado grande para Telegram.")

    with result.file_path.open("rb") as document:
        sent = await context.bot.send_document(
            chat_id=target.chat_id,
            document=document,
            caption=caption,
            parse_mode=parse_mode,
            reply_to_message_id=target.reply_to_message_id,
        )
        _cache_sent_message(services, result.metadata, result.variant, sent)


async def _send_cached(
    context: ContextTypes.DEFAULT_TYPE,
    services: BotServices,
    metadata: VideoMetadata,
    target: SendTarget,
    entry: CacheEntry,
) -> None:
    caption = _caption_for_target(metadata, services, target)
    parse_mode = ParseMode.HTML if caption else None
    if entry.media_type == "audio":
        await context.bot.send_audio(
            chat_id=target.chat_id,
            audio=entry.file_id,
            caption=caption,
            parse_mode=parse_mode,
            reply_to_message_id=target.reply_to_message_id,
        )
        return

    if entry.media_type == "document":
        await context.bot.send_document(
            chat_id=target.chat_id,
            document=entry.file_id,
            caption=caption,
            parse_mode=parse_mode,
            reply_to_message_id=target.reply_to_message_id,
        )
        return

    await context.bot.send_video(
        chat_id=target.chat_id,
        video=entry.file_id,
        caption=caption,
        parse_mode=parse_mode,
        supports_streaming=True,
        reply_to_message_id=target.reply_to_message_id,
    )


def cleanup_download_dir(download_dir: Path) -> None:
    download_dir.mkdir(parents=True, exist_ok=True)
    for child in download_dir.iterdir():
        if child.is_dir() and child.name.startswith("video-"):
            shutil.rmtree(child, ignore_errors=True)


def _services(context: ContextTypes.DEFAULT_TYPE) -> BotServices:
    return context.application.bot_data["services"]


async def _acquire_job_slot(services: BotServices, progress: ProgressEditor) -> None:
    if services.job_semaphore.locked():
        services.queued_jobs += 1
        await progress.update("En cola", 0, force=True)
        try:
            await services.job_semaphore.acquire()
        finally:
            services.queued_jobs = max(0, services.queued_jobs - 1)
    else:
        await services.job_semaphore.acquire()
    services.active_jobs += 1


def _release_job_slot(services: BotServices) -> None:
    services.active_jobs = max(0, services.active_jobs - 1)
    services.job_semaphore.release()


def _create_pending_selection(
    services: BotServices,
    *,
    source_url: str,
    normalized_url: str,
    metadata: VideoMetadata,
    target: SendTarget,
) -> str:
    _prune_pending_selections(services)
    token = secrets.token_urlsafe(6)
    services.pending_selections[token] = PendingSelection(
        source_url=source_url,
        normalized_url=normalized_url,
        metadata=metadata,
        target=target,
        created_at=time.time(),
    )
    return token


def _prune_pending_selections(services: BotServices, ttl_seconds: int = 3600) -> None:
    cutoff = time.time() - ttl_seconds
    expired = [
        token
        for token, pending in services.pending_selections.items()
        if pending.created_at < cutoff
    ]
    for token in expired:
        services.pending_selections.pop(token, None)


def _kind_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Video", callback_data=f"{CALLBACK_PREFIX}:{token}:kind:video"),
        InlineKeyboardButton("Audio", callback_data=f"{CALLBACK_PREFIX}:{token}:kind:audio"),
    ]])


def _quality_keyboard(token: str, kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Alta", callback_data=f"{CALLBACK_PREFIX}:{token}:quality:{kind}:high"),
        InlineKeyboardButton("Media", callback_data=f"{CALLBACK_PREFIX}:{token}:quality:{kind}:medium"),
        InlineKeyboardButton("Baja", callback_data=f"{CALLBACK_PREFIX}:{token}:quality:{kind}:low"),
    ]])


def _selection_text(metadata: VideoMetadata, source_url: str) -> str:
    title = metadata.title or "Enlace detectado"
    return f"{title}\n\nElige formato para procesar este enlace:\n{source_url}"


def _quality_text(metadata: VideoMetadata, kind: str) -> str:
    title = metadata.title or "Enlace detectado"
    label = "video" if kind == "video" else "audio"
    return f"{title}\n\nElige calidad de {label}:"


async def _edit_query_message(
    message: object,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if not isinstance(message, Message):
        return
    try:
        await message.edit_text(
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except TelegramError:
        LOGGER.debug("Could not edit callback message", exc_info=True)


def _caption_for_chat(metadata: VideoMetadata, services: BotServices, chat: object) -> str | None:
    chat_type = getattr(chat, "type", None)
    mode = (
        services.settings.caption_mode_private
        if chat_type == ChatType.PRIVATE
        else services.settings.caption_mode_group
    )
    if mode not in {"full", "link", "none"}:
        mode = "full" if chat_type == ChatType.PRIVATE else "none"

    if mode == "none":
        return None

    source_url = metadata.webpage_url or metadata.source_url
    if mode == "link":
        return f'<a href="{escape(source_url, quote=True)}">Enlace original</a>'

    return build_caption(
        title=metadata.title,
        description=metadata.description,
        source_url=source_url,
        uploader=metadata.uploader,
        max_description_chars=services.settings.max_description_chars,
    )


def _caption_for_target(
    metadata: VideoMetadata,
    services: BotServices,
    target: SendTarget,
) -> str | None:
    return _caption_for_chat(metadata, services, target)


def _metadata_from_cache_entry(source_url: str, entry: CacheEntry) -> VideoMetadata:
    return VideoMetadata(
        video_id=None,
        title=entry.title,
        description=entry.description,
        source_url=source_url,
        webpage_url=entry.webpage_url or source_url,
        uploader=entry.uploader,
        extractor=None,
        duration=None,
        estimated_size_bytes=None,
    )


def _validate_metadata_limits(settings: Settings, metadata: VideoMetadata) -> None:
    if metadata.duration and metadata.duration > settings.max_video_duration_seconds:
        raise OversizeError(
            f"El video dura demasiado ({int(metadata.duration)}s). "
            f"Limite configurado: {settings.max_video_duration_seconds}s."
        )

    if (
        metadata.estimated_size_bytes
        and metadata.estimated_size_bytes > settings.max_estimated_download_mb * 1024 * 1024
    ):
        raise OversizeError(
            "El video parece demasiado grande para procesarlo en la Raspberry. "
            f"Limite estimado: {settings.max_estimated_download_mb} MB."
        )


def _cache_sent_message(
    services: BotServices,
    metadata: VideoMetadata,
    variant: DownloadVariant,
    sent_message: object,
) -> None:
    cache_key = variant_cache_key(metadata, variant)
    audio = getattr(sent_message, "audio", None)
    if audio and getattr(audio, "file_id", None):
        services.cache.put(
            cache_key=cache_key,
            media_type="audio",
            file_id=audio.file_id,
            file_unique_id=getattr(audio, "file_unique_id", None),
            **_metadata_cache_fields(metadata),
        )
        return

    video = getattr(sent_message, "video", None)
    if video and getattr(video, "file_id", None):
        services.cache.put(
            cache_key=cache_key,
            media_type="video",
            file_id=video.file_id,
            file_unique_id=getattr(video, "file_unique_id", None),
            **_metadata_cache_fields(metadata),
        )
        return

    document = getattr(sent_message, "document", None)
    if document and getattr(document, "file_id", None):
        services.cache.put(
            cache_key=cache_key,
            media_type="document",
            file_id=document.file_id,
            file_unique_id=getattr(document, "file_unique_id", None),
            **_metadata_cache_fields(metadata),
        )


def _metadata_cache_fields(metadata: VideoMetadata) -> dict[str, str | None]:
    return {
        "title": metadata.title,
        "description": metadata.description,
        "webpage_url": metadata.webpage_url,
        "uploader": metadata.uploader,
    }


class ProgressEditor:
    def __init__(
        self,
        message: Message,
        current: int,
        total: int,
        *,
        edit_interval_seconds: float,
    ) -> None:
        self.message = message
        self.current = current
        self.total = total
        self.edit_interval_seconds = edit_interval_seconds
        self.loop = asyncio.get_running_loop()
        self.last_edit = 0.0
        self.last_text = ""

    async def update(self, label: str, percent: int, *, force: bool = False) -> None:
        now = time.monotonic()
        text = _progress_text(label, self.current, self.total, percent)
        if not force and (text == self.last_text or now - self.last_edit < self.edit_interval_seconds):
            return

        self.last_edit = now
        self.last_text = text
        try:
            await self.message.edit_text(text)
        except TelegramError:
            LOGGER.debug("Could not edit progress message", exc_info=True)

    def threadsafe_update(self, label: str, percent: int) -> None:
        self.loop.call_soon_threadsafe(
            lambda: self.loop.create_task(self.update(label, percent))
        )


def _is_update_allowed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    services: BotServices,
) -> bool:
    chat = update.effective_chat
    message = update.effective_message
    if not chat:
        return False

    if chat.type == ChatType.PRIVATE:
        return is_private_allowed(services.settings, update.effective_user)

    if not is_group_allowed(services.settings, chat):
        return False

    bot_username = context.bot.username
    text = message.text or message.caption if message else None
    return text_mentions_bot(text, bot_username)


def _is_command_allowed(update: Update, services: BotServices) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    if chat.type == ChatType.PRIVATE:
        return is_private_allowed(services.settings, update.effective_user)
    return is_group_allowed(services.settings, chat)


def _should_warn_unauthorized(update: Update, services: BotServices) -> bool:
    if services.settings.quiet_unauthorized:
        return False
    chat = update.effective_chat
    if not chat:
        return False
    return chat.type == ChatType.PRIVATE


def _progress_text(label: str, current: int, total: int, percent: int) -> str:
    width = 10
    filled = max(0, min(width, round(percent / 100 * width)))
    bar = "#" * filled + "-" * (width - filled)
    prefix = f"{current}/{total} " if total > 1 else ""
    return f"{prefix}{label}\n[{bar}] {percent}%"


async def _safe_delete_status(status: Message) -> None:
    try:
        await status.delete()
    except TelegramError:
        LOGGER.debug("Could not delete progress message", exc_info=True)


async def _safe_edit_status(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except TelegramError:
        LOGGER.debug("Could not edit progress message", exc_info=True)


def _is_message_not_modified(exc: TelegramError) -> bool:
    return "message is not modified" in str(exc).lower()


async def _notify_admin_error(
    context: ContextTypes.DEFAULT_TYPE,
    services: BotServices,
    update: Update,
    url: str,
    exc: BaseException,
    label: str,
) -> None:
    admin_user_id = services.settings.error_report_user_id
    if not admin_user_id:
        return

    try:
        await context.bot.send_message(
            chat_id=admin_user_id,
            text=_error_report_text(update, url, exc, label),
            disable_web_page_preview=True,
        )
    except TelegramError:
        LOGGER.exception("Could not send error report to admin user %s", admin_user_id)


def _error_report_text(update: Update, url: str, exc: BaseException, label: str) -> str:
    chat = update.effective_chat
    user = update.effective_user
    lines = [
        "Error operativo en VideoShare",
        f"tipo: {label}",
        f"url: {url}",
        f"chat_id: {chat.id if chat else 'desconocido'}",
        f"chat_type: {chat.type if chat else 'desconocido'}",
        f"user_id: {user.id if user else 'desconocido'}",
        f"excepcion: {exc.__class__.__name__}: {exc}",
        "",
        "traceback:",
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8)).strip(),
    ]
    return _truncate_text("\n".join(lines), 3900)


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 15].rstrip() + "\n...[truncado]"
