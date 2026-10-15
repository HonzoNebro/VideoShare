from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from telegram.constants import ChatType
from telegram.error import BadRequest

from bot.config import Settings
from bot.downloader import DownloadVariant, ImageResult, ImageVariant, OversizeError, TrimRange, VideoMetadata
from bot.handlers import (
    BotServices,
    PendingSelection,
    PendingTrimRequest,
    SendTarget,
    _album_option_keyboard,
    _caption_for_chat,
    _cache_sent_message,
    _error_report_text,
    _handle_trim_range_message,
    _is_update_allowed,
    _kind_keyboard,
    _process_variant_selection,
    _send_image_result,
    _is_message_not_modified,
    _is_disk_full_error,
    _quality_keyboard,
    _validate_selection_limits,
    _validate_metadata_limits,
    handle_variant_callback,
)


def _settings(
    *,
    caption_mode_private: str = "full",
    caption_mode_group: str = "none",
) -> Settings:
    return Settings(
        telegram_bot_token="token",
        allowed_user_ids=set(),
        allowed_chat_ids=set(),
        download_dir=Path("/tmp/videoshare-test"),
        max_video_duration_seconds=60,
        max_estimated_download_mb=10,
        caption_mode_private=caption_mode_private,
        caption_mode_group=caption_mode_group,
    )


def _metadata() -> VideoMetadata:
    return VideoMetadata(
        video_id="id",
        title="Titulo",
        description="Descripcion",
        source_url="https://example.com/video?x=1&y=2",
        webpage_url="https://example.com/video?x=1&y=2",
        uploader="Canal",
        extractor="Example",
        duration=10,
        estimated_size_bytes=None,
    )


def test_validate_metadata_limits_rejects_long_video() -> None:
    metadata = VideoMetadata(
        video_id="id",
        title=None,
        description=None,
        source_url="https://example.com/video",
        webpage_url="https://example.com/video",
        uploader=None,
        extractor="Example",
        duration=61,
        estimated_size_bytes=None,
    )

    with pytest.raises(OversizeError):
        _validate_metadata_limits(_settings(), metadata)


def test_validate_metadata_limits_rejects_large_estimate() -> None:
    metadata = VideoMetadata(
        video_id="id",
        title=None,
        description=None,
        source_url="https://example.com/video",
        webpage_url="https://example.com/video",
        uploader=None,
        extractor="Example",
        duration=10,
        estimated_size_bytes=11 * 1024 * 1024,
    )

    with pytest.raises(OversizeError):
        _validate_metadata_limits(_settings(), metadata)


def test_validate_selection_limits_allows_valid_trim_from_long_video() -> None:
    metadata = VideoMetadata(
        video_id="id",
        title=None,
        description=None,
        source_url="https://example.com/video",
        webpage_url="https://example.com/video",
        uploader=None,
        extractor="Example",
        duration=600,
        estimated_size_bytes=None,
    )

    _validate_selection_limits(
        _settings(),
        metadata,
        TrimRange(start_seconds=10, end_seconds=20),
    )


def test_validate_selection_limits_rejects_long_trim() -> None:
    metadata = VideoMetadata(
        video_id="id",
        title=None,
        description=None,
        source_url="https://example.com/video",
        webpage_url="https://example.com/video",
        uploader=None,
        extractor="Example",
        duration=600,
        estimated_size_bytes=None,
    )

    with pytest.raises(OversizeError):
        _validate_selection_limits(
            _settings(),
            metadata,
            TrimRange(start_seconds=10, end_seconds=80.01),
        )


def test_caption_for_private_full() -> None:
    services = SimpleNamespace(settings=_settings(caption_mode_private="full"))

    caption = _caption_for_chat(_metadata(), services, SimpleNamespace(type=ChatType.PRIVATE))

    assert caption is not None
    assert "<b>Titulo</b>" in caption
    assert "Canal" in caption
    assert "Descripcion" in caption
    assert "Enlace original" in caption


def test_caption_for_group_none() -> None:
    services = SimpleNamespace(settings=_settings(caption_mode_group="none"))

    caption = _caption_for_chat(_metadata(), services, SimpleNamespace(type=ChatType.GROUP))

    assert caption is None


def test_caption_for_group_link() -> None:
    services = SimpleNamespace(settings=_settings(caption_mode_group="link"))

    caption = _caption_for_chat(_metadata(), services, SimpleNamespace(type=ChatType.SUPERGROUP))

    assert caption == '<a href="https://example.com/video?x=1&amp;y=2">Enlace original</a>'


def test_caption_none_does_not_drop_cache_metadata() -> None:
    services = SimpleNamespace(settings=_settings(caption_mode_group="none"))
    metadata = _metadata()

    caption = _caption_for_chat(metadata, services, SimpleNamespace(type=ChatType.GROUP))

    assert caption is None
    assert metadata.title == "Titulo"
    assert metadata.description == "Descripcion"
    assert metadata.webpage_url == "https://example.com/video?x=1&y=2"


def test_message_not_modified_is_benign_telegram_error() -> None:
    exc = BadRequest(
        "Message is not modified: specified new message content and reply markup "
        "are exactly the same as a current content and reply markup of the message"
    )

    assert _is_message_not_modified(exc)


def test_disk_full_sqlite_error_is_recognized() -> None:
    assert _is_disk_full_error(sqlite3.OperationalError("database or disk is full"))
    assert not _is_disk_full_error(sqlite3.OperationalError("database is locked"))


def test_error_report_text_contains_operational_context() -> None:
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100, type=ChatType.GROUP),
        effective_user=SimpleNamespace(id=42),
    )

    report = _error_report_text(
        update,
        "https://example.com/video",
        RuntimeError("broken config"),
        "Error inesperado",
    )

    assert "Error operativo en VideoShare" in report
    assert "tipo: Error inesperado" in report
    assert "url: https://example.com/video" in report
    assert "chat_id: -100" in report
    assert "user_id: 42" in report
    assert "RuntimeError: broken config" in report


def test_variant_keyboards_use_short_callback_data() -> None:
    kind_keyboard = _kind_keyboard("abc123")
    quality_keyboard = _quality_keyboard("abc123", "audio")

    kind_buttons = kind_keyboard.inline_keyboard[0]
    quality_buttons = quality_keyboard.inline_keyboard[0]

    assert [button.text for button in kind_buttons] == ["Video", "Audio"]
    assert [button.callback_data for button in kind_buttons] == [
        "vs:abc123:kind:video",
        "vs:abc123:kind:audio",
    ]
    assert [button.text for button in quality_buttons] == ["Alta", "Media", "Baja"]
    assert [button.callback_data for button in quality_buttons] == [
        "vs:abc123:quality:audio:high",
        "vs:abc123:quality:audio:medium",
        "vs:abc123:quality:audio:low",
    ]
    assert all(len(str(button.callback_data)) <= 64 for button in kind_buttons + quality_buttons)

    assert _quality_keyboard("abc123", "video").inline_keyboard[1][0].text == "Recortar"

    trimmed_keyboard = _quality_keyboard(
        "abc123",
        "video",
        TrimRange(start_seconds=83.16, end_seconds=130),
    )
    trim_buttons = trimmed_keyboard.inline_keyboard[1]

    assert [button.text for button in trim_buttons] == ["Cambiar recorte", "Quitar recorte"]
    assert [button.callback_data for button in trim_buttons] == [
        "vs:abc123:trim:video",
        "vs:abc123:untrim:video",
    ]


def test_album_keyboard_only_offers_first_or_all() -> None:
    keyboard = _album_option_keyboard("abc123")
    buttons = [row[0] for row in keyboard.inline_keyboard]

    assert [button.text for button in buttons] == ["Primera", "Todas"]
    assert [button.callback_data for button in buttons] == [
        "vs:abc123:album:1",
        "vs:abc123:album:-1",
    ]
    assert all(len(str(button.callback_data)) <= 64 for button in buttons)


def test_cache_sent_message_stores_variant_cache_key(tmp_path) -> None:
    from bot.cache import VideoCache

    services = SimpleNamespace(cache=VideoCache(tmp_path / "cache.sqlite3"))
    sent = SimpleNamespace(audio=SimpleNamespace(file_id="audio-file", file_unique_id="unique"))

    _cache_sent_message(services, _metadata(), DownloadVariant("audio", "low"), sent)

    entry = services.cache.get("example:id:audio-low")

    assert entry is not None
    assert entry.media_type == "audio"
    assert entry.file_id == "audio-file"


async def test_send_image_result_uses_send_photo_for_single_image(tmp_path) -> None:
    from bot.cache import VideoCache

    image_path = tmp_path / "one.jpg"
    image_path.write_bytes(b"image")
    calls: list[dict[str, object]] = []

    async def send_photo(**kwargs: object):
        calls.append(kwargs)
        return SimpleNamespace(photo=[SimpleNamespace(file_id="photo-file", file_unique_id="unique")])

    services = SimpleNamespace(
        settings=_settings(caption_mode_private="none"),
        cache=VideoCache(tmp_path / "cache.sqlite3"),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_photo=send_photo))
    result = ImageResult([image_path], _metadata(), ImageVariant("medium", 1))

    await _send_image_result(
        context,
        services,
        result,
        SendTarget(chat_id=123, chat_type=ChatType.PRIVATE, reply_to_message_id=99),
    )

    assert calls[0]["chat_id"] == 123
    assert calls[0]["reply_to_message_id"] == 99
    assert services.cache.get("example:id:image-medium-count1").media_type == "photo"  # type: ignore[union-attr]


async def test_send_image_result_uses_media_group_for_album(tmp_path) -> None:
    from bot.cache import VideoCache

    paths = []
    for index in range(2):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"image")
        paths.append(path)
    media_groups: list[dict[str, object]] = []

    async def send_media_group(**kwargs: object):
        media_groups.append(kwargs)
        return [
            SimpleNamespace(photo=[SimpleNamespace(file_id="photo-1", file_unique_id="unique-1")]),
            SimpleNamespace(photo=[SimpleNamespace(file_id="photo-2", file_unique_id="unique-2")]),
        ]

    services = SimpleNamespace(
        settings=_settings(caption_mode_private="full"),
        cache=VideoCache(tmp_path / "cache.sqlite3"),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_media_group=send_media_group))
    result = ImageResult(paths, _metadata(), ImageVariant("medium", -1))

    await _send_image_result(
        context,
        services,
        result,
        SendTarget(chat_id=123, chat_type=ChatType.PRIVATE, reply_to_message_id=99),
    )

    media = media_groups[0]["media"]
    assert len(media) == 2
    assert media[0].caption is not None
    assert media[1].caption is None
    entry = services.cache.get("example:id:image-medium-countall")
    assert entry is not None
    assert services.cache.album_file_ids(entry) == ["photo-1", "photo-2"]


async def test_callback_with_expired_token_warns_user(monkeypatch) -> None:
    answers: list[str] = []
    edits: list[str] = []

    async def fake_answer(text: str | None = None) -> None:
        if text:
            answers.append(text)

    async def fake_edit(message: object, text: str, **kwargs: object) -> None:
        edits.append(text)

    monkeypatch.setattr("bot.handlers._edit_query_message", fake_edit)
    services = BotServices(
        settings=_settings(),
        downloader=SimpleNamespace(),
        cache=SimpleNamespace(),
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))
    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data="vs:missing:quality:video:high",
            message=SimpleNamespace(),
            answer=fake_answer,
        )
    )

    await handle_variant_callback(update, context)

    assert answers == ["La seleccion ha caducado."]
    assert edits == ["La seleccion ha caducado. Reenvia el enlace para elegir formato y calidad."]


async def test_trim_range_message_updates_pending_selection() -> None:
    replies: list[dict[str, object]] = []
    edits: list[dict[str, object]] = []

    async def reply_text(text: str, **kwargs: object) -> None:
        replies.append({"text": text, **kwargs})

    async def edit_message_text(**kwargs: object) -> None:
        edits.append(kwargs)

    services = BotServices(
        settings=_settings(),
        downloader=SimpleNamespace(),
        cache=SimpleNamespace(),
    )
    services.pending_selections["abc123"] = PendingSelection(
        source_url="https://example.com/video",
        normalized_url="https://example.com/video",
        metadata=_metadata(),
        target=SendTarget(chat_id=123, chat_type=ChatType.PRIVATE, reply_to_message_id=1),
        created_at=1,
    )
    services.pending_trim_requests[(123, 42)] = PendingTrimRequest(
        token="abc123",
        kind="audio",
        message_id=77,
        created_at=9999999999,
    )
    update = SimpleNamespace(
        effective_message=SimpleNamespace(text="00:01.00-00:02.00", reply_text=reply_text),
        effective_chat=SimpleNamespace(id=123),
        effective_user=SimpleNamespace(id=42),
    )
    context = SimpleNamespace(bot=SimpleNamespace(edit_message_text=edit_message_text))

    handled = await _handle_trim_range_message(update, context, services)

    assert handled is True
    assert services.pending_selections["abc123"].trim_range == TrimRange(
        start_seconds=1,
        end_seconds=2,
    )
    assert services.pending_trim_requests == {}
    assert replies == []
    assert edits[0]["chat_id"] == 123
    assert edits[0]["message_id"] == 77
    assert "Recorte: 00:01.00-00:02.00" in edits[0]["text"]


def test_group_trim_reply_is_allowed_without_bot_mention() -> None:
    services = BotServices(
        settings=_settings(),
        downloader=SimpleNamespace(),
        cache=SimpleNamespace(),
    )
    services.pending_trim_requests[(-100, 42)] = PendingTrimRequest(
        token="abc123",
        kind="video",
        message_id=77,
        created_at=9999999999,
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100, type=ChatType.GROUP),
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(text="0:22.5-0:24.5", caption=None),
    )
    context = SimpleNamespace(bot=SimpleNamespace(username="compartirvideosbot"))

    assert _is_update_allowed(update, context, services)


async def test_send_cached_audio_uses_send_audio() -> None:
    from bot.cache import CacheEntry
    from bot.handlers import _send_cached

    calls: list[dict[str, object]] = []

    async def send_audio(**kwargs: object) -> None:
        calls.append(kwargs)

    context = SimpleNamespace(bot=SimpleNamespace(send_audio=send_audio))
    services = SimpleNamespace(settings=_settings(caption_mode_private="none"))
    target = SendTarget(chat_id=123, chat_type=ChatType.PRIVATE, reply_to_message_id=99)
    entry = CacheEntry(
        cache_key="example:id:audio-low",
        media_type="audio",
        file_id="audio-file",
        file_unique_id=None,
        title=None,
        description=None,
        webpage_url=None,
        uploader=None,
        created_at=1,
        last_used_at=1,
    )

    await _send_cached(context, services, _metadata(), target, entry)

    assert calls == [{
        "chat_id": 123,
        "audio": "audio-file",
        "caption": None,
        "parse_mode": None,
        "reply_to_message_id": 99,
    }]


async def test_send_cached_photo_album_uses_file_ids(tmp_path) -> None:
    from bot.handlers import _send_cached
    from bot.cache import VideoCache

    media_groups: list[dict[str, object]] = []

    async def send_media_group(**kwargs: object) -> None:
        media_groups.append(kwargs)

    cache = VideoCache(tmp_path / "cache.sqlite3")
    cache.put_album(
        cache_key="example:id:image-medium-countall",
        file_ids=["photo-1", "photo-2"],
    )
    entry = cache.get("example:id:image-medium-countall")
    assert entry is not None

    context = SimpleNamespace(bot=SimpleNamespace(send_media_group=send_media_group))
    services = SimpleNamespace(settings=_settings(caption_mode_private="none"), cache=cache)

    await _send_cached(
        context,
        services,
        _metadata(),
        SendTarget(chat_id=123, chat_type=ChatType.PRIVATE, reply_to_message_id=99),
        entry,
    )

    assert media_groups[0]["chat_id"] == 123
    assert media_groups[0]["reply_to_message_id"] == 99
    assert [item.media for item in media_groups[0]["media"]] == ["photo-1", "photo-2"]


async def test_successful_cached_selection_deletes_progress_message(monkeypatch) -> None:
    from bot.cache import CacheEntry

    edits: list[str] = []
    sends: list[object] = []
    deletes: list[object] = []

    class FakeCache:
        def get_rejection(self, cache_key: str) -> None:
            return None

        def get(self, cache_key: str) -> CacheEntry:
            return CacheEntry(
                cache_key=cache_key,
                media_type="video",
                file_id="video-file",
                file_unique_id=None,
                title=None,
                description=None,
                webpage_url=None,
                uploader=None,
                created_at=1,
                last_used_at=1,
            )

        def delete(self, cache_key: str) -> None:
            raise AssertionError("cache should not be deleted")

    async def fake_send_cached(context, services, metadata, target, cached) -> None:
        sends.append(cached)

    async def fake_delete_status(status) -> None:
        deletes.append(status)

    async def edit_text(text: str) -> None:
        edits.append(text)

    monkeypatch.setattr("bot.handlers._send_cached", fake_send_cached)
    monkeypatch.setattr("bot.handlers._safe_delete_status", fake_delete_status)
    services = BotServices(
        settings=_settings(),
        downloader=SimpleNamespace(),
        cache=FakeCache(),
    )
    status = SimpleNamespace(edit_text=edit_text)
    pending = PendingSelection(
        source_url="https://example.com/video",
        normalized_url="https://example.com/video",
        metadata=_metadata(),
        target=SendTarget(chat_id=123, chat_type=ChatType.PRIVATE, reply_to_message_id=1),
        created_at=1,
    )
    update = SimpleNamespace(effective_message=status)
    context = SimpleNamespace()

    await _process_variant_selection(
        update,
        context,
        services,
        pending,
        DownloadVariant("video", "low"),
    )

    assert sends
    assert deletes == [status]
    assert not any("Completado" in edit for edit in edits)
