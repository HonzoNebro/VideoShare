import sqlite3
import time

from bot.cache import VideoCache


def test_cache_put_get_and_delete(tmp_path) -> None:
    cache = VideoCache(tmp_path / "cache.sqlite3", ttl_days=30)

    cache.put(
        cache_key="youtube:abc",
        media_type="video",
        file_id="file-id",
        file_unique_id="unique-id",
        title="Title",
        description="Description",
        webpage_url="https://example.com/video",
        uploader="Uploader",
    )

    entry = cache.get("youtube:abc")

    assert entry is not None
    assert entry.media_type == "video"
    assert entry.file_id == "file-id"
    assert entry.file_unique_id == "unique-id"
    assert entry.title == "Title"
    assert entry.description == "Description"
    assert entry.webpage_url == "https://example.com/video"
    assert entry.uploader == "Uploader"

    cache.delete("youtube:abc")

    assert cache.get("youtube:abc") is None


def test_cache_prunes_expired_entries(tmp_path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = VideoCache(db_path, ttl_days=30)
    expired = int(time.time()) - 31 * 24 * 60 * 60

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO video_cache (
                cache_key, media_type, file_id, file_unique_id,
                title, description, webpage_url, uploader,
                created_at, last_used_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("youtube:old", "video", "file-id", None, None, None, None, None, expired, expired),
        )

    assert cache.get("youtube:old") is None


def test_cache_url_alias_put_get_and_stats(tmp_path) -> None:
    cache = VideoCache(tmp_path / "cache.sqlite3", ttl_days=30)

    cache.put(
        cache_key="youtube:abc",
        media_type="video",
        file_id="file-id",
        file_unique_id=None,
    )
    cache.put_alias(
        normalized_url="https://www.youtube.com/watch?v=abc",
        cache_key="youtube:abc",
    )

    assert cache.get_alias("https://www.youtube.com/watch?v=abc") == "youtube:abc"

    stats = cache.stats()

    assert stats.video_entries == 1
    assert stats.url_aliases == 1
    assert stats.ttl_days == 30


def test_cache_stores_variants_as_independent_entries(tmp_path) -> None:
    cache = VideoCache(tmp_path / "cache.sqlite3", ttl_days=30)

    cache.put_alias(
        normalized_url="https://www.youtube.com/watch?v=abc",
        cache_key="youtube:abc",
    )
    cache.put(
        cache_key="youtube:abc:video-high",
        media_type="video",
        file_id="video-file-id",
        file_unique_id=None,
    )
    cache.put(
        cache_key="youtube:abc:audio-low",
        media_type="audio",
        file_id="audio-file-id",
        file_unique_id=None,
    )

    assert cache.get_alias("https://www.youtube.com/watch?v=abc") == "youtube:abc"
    assert cache.get("youtube:abc:video-high").file_id == "video-file-id"  # type: ignore[union-attr]
    assert cache.get("youtube:abc:audio-low").file_id == "audio-file-id"  # type: ignore[union-attr]

    cache.delete("youtube:abc:video-high")

    assert cache.get("youtube:abc:video-high") is None
    assert cache.get("youtube:abc:audio-low") is not None
    assert cache.get_alias("https://www.youtube.com/watch?v=abc") == "youtube:abc"


def test_cache_prunes_expired_aliases(tmp_path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = VideoCache(db_path, ttl_days=30)
    expired = int(time.time()) - 31 * 24 * 60 * 60

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO url_aliases (normalized_url, cache_key, created_at, last_used_at)
            VALUES (?, ?, ?, ?)
            """,
            ("https://example.com/old", "url:old", expired, expired),
        )

    assert cache.get_alias("https://example.com/old") is None


def test_cache_rejection_put_get_and_delete(tmp_path) -> None:
    cache = VideoCache(tmp_path / "cache.sqlite3", ttl_days=30)

    cache.put_rejection(
        cache_key="youtube:too-long",
        message="El video dura demasiado.",
        title="Title",
        description="Description",
        webpage_url="https://example.com/video",
        uploader="Uploader",
    )

    entry = cache.get_rejection("youtube:too-long")

    assert entry is not None
    assert entry.message == "El video dura demasiado."
    assert entry.title == "Title"
    assert entry.description == "Description"
    assert entry.webpage_url == "https://example.com/video"
    assert entry.uploader == "Uploader"

    stats = cache.stats()
    assert stats.rejected_entries == 1

    cache.delete("youtube:too-long")

    assert cache.get_rejection("youtube:too-long") is None


def test_cache_prunes_expired_rejections(tmp_path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = VideoCache(db_path, ttl_days=30)
    expired = int(time.time()) - 31 * 24 * 60 * 60

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO rejected_cache (
                cache_key, message, title, description, webpage_url, uploader,
                created_at, last_used_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("youtube:old", "old rejection", None, None, None, None, expired, expired),
        )

    assert cache.get_rejection("youtube:old") is None


def test_cache_migrates_old_video_cache_schema(tmp_path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE video_cache (
                cache_key TEXT PRIMARY KEY,
                media_type TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL
            )
            """
        )

    cache = VideoCache(db_path, ttl_days=30)
    cache.put(
        cache_key="youtube:new",
        media_type="video",
        file_id="file-id",
        file_unique_id=None,
        title="Migrated",
    )

    entry = cache.get("youtube:new")

    assert entry is not None
    assert entry.title == "Migrated"
