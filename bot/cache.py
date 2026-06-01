from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time


@dataclass(frozen=True)
class CacheEntry:
    cache_key: str
    media_type: str
    file_id: str
    file_unique_id: str | None
    title: str | None
    description: str | None
    webpage_url: str | None
    uploader: str | None
    created_at: int
    last_used_at: int


@dataclass(frozen=True)
class RejectedEntry:
    cache_key: str
    message: str
    title: str | None
    description: str | None
    webpage_url: str | None
    uploader: str | None
    created_at: int
    last_used_at: int


@dataclass(frozen=True)
class CacheStats:
    video_entries: int
    url_aliases: int
    rejected_entries: int
    db_path: Path
    ttl_days: int


class VideoCache:
    def __init__(self, db_path: Path, ttl_days: int = 30) -> None:
        self.db_path = db_path
        self.ttl_days = ttl_days
        self.ttl_seconds = ttl_days * 24 * 60 * 60
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def get(self, cache_key: str) -> CacheEntry | None:
        self.prune_expired()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    cache_key, media_type, file_id, file_unique_id,
                    title, description, webpage_url, uploader,
                    created_at, last_used_at
                FROM video_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
            if not row:
                return None

            now = int(time.time())
            conn.execute(
                "UPDATE video_cache SET last_used_at = ? WHERE cache_key = ?",
                (now, cache_key),
            )
            return CacheEntry(*row)

    def put(
        self,
        *,
        cache_key: str,
        media_type: str,
        file_id: str,
        file_unique_id: str | None,
        title: str | None = None,
        description: str | None = None,
        webpage_url: str | None = None,
        uploader: str | None = None,
    ) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO video_cache (
                    cache_key, media_type, file_id, file_unique_id,
                    title, description, webpage_url, uploader,
                    created_at, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    media_type = excluded.media_type,
                    file_id = excluded.file_id,
                    file_unique_id = excluded.file_unique_id,
                    title = excluded.title,
                    description = excluded.description,
                    webpage_url = excluded.webpage_url,
                    uploader = excluded.uploader,
                    created_at = excluded.created_at,
                    last_used_at = excluded.last_used_at
                """,
                (
                    cache_key,
                    media_type,
                    file_id,
                    file_unique_id,
                    title,
                    description,
                    webpage_url,
                    uploader,
                    now,
                    now,
                ),
            )

    def delete(self, cache_key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM video_cache WHERE cache_key = ?", (cache_key,))
            conn.execute("DELETE FROM rejected_cache WHERE cache_key = ?", (cache_key,))
            conn.execute("DELETE FROM url_aliases WHERE cache_key = ?", (cache_key,))

    def get_rejection(self, cache_key: str) -> RejectedEntry | None:
        self.prune_expired()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    cache_key, message, title, description, webpage_url, uploader,
                    created_at, last_used_at
                FROM rejected_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
            if not row:
                return None

            now = int(time.time())
            conn.execute(
                "UPDATE rejected_cache SET last_used_at = ? WHERE cache_key = ?",
                (now, cache_key),
            )
            return RejectedEntry(*row)

    def put_rejection(
        self,
        *,
        cache_key: str,
        message: str,
        title: str | None = None,
        description: str | None = None,
        webpage_url: str | None = None,
        uploader: str | None = None,
    ) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rejected_cache (
                    cache_key, message, title, description, webpage_url, uploader,
                    created_at, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    message = excluded.message,
                    title = excluded.title,
                    description = excluded.description,
                    webpage_url = excluded.webpage_url,
                    uploader = excluded.uploader,
                    created_at = excluded.created_at,
                    last_used_at = excluded.last_used_at
                """,
                (
                    cache_key,
                    message,
                    title,
                    description,
                    webpage_url,
                    uploader,
                    now,
                    now,
                ),
            )

    def get_alias(self, normalized_url: str) -> str | None:
        self.prune_expired()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cache_key
                FROM url_aliases
                WHERE normalized_url = ?
                """,
                (normalized_url,),
            ).fetchone()
            if not row:
                return None

            now = int(time.time())
            conn.execute(
                "UPDATE url_aliases SET last_used_at = ? WHERE normalized_url = ?",
                (now, normalized_url),
            )
            return str(row[0])

    def put_alias(self, *, normalized_url: str, cache_key: str) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO url_aliases (normalized_url, cache_key, created_at, last_used_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(normalized_url) DO UPDATE SET
                    cache_key = excluded.cache_key,
                    last_used_at = excluded.last_used_at
                """,
                (normalized_url, cache_key, now, now),
            )

    def stats(self) -> CacheStats:
        self.prune_expired()
        with self._connect() as conn:
            video_entries = conn.execute("SELECT COUNT(*) FROM video_cache").fetchone()[0]
            url_aliases = conn.execute("SELECT COUNT(*) FROM url_aliases").fetchone()[0]
            rejected_entries = conn.execute("SELECT COUNT(*) FROM rejected_cache").fetchone()[0]
        return CacheStats(
            video_entries=int(video_entries),
            url_aliases=int(url_aliases),
            rejected_entries=int(rejected_entries),
            db_path=self.db_path,
            ttl_days=self.ttl_days,
        )

    def prune_expired(self) -> None:
        cutoff = int(time.time()) - self.ttl_seconds
        with self._connect() as conn:
            conn.execute("DELETE FROM video_cache WHERE last_used_at < ?", (cutoff,))
            conn.execute("DELETE FROM rejected_cache WHERE last_used_at < ?", (cutoff,))
            conn.execute("DELETE FROM url_aliases WHERE last_used_at < ?", (cutoff,))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS video_cache (
                    cache_key TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT,
                    title TEXT,
                    description TEXT,
                    webpage_url TEXT,
                    uploader TEXT,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER NOT NULL
                )
                """
            )
            self._ensure_column(conn, "video_cache", "title", "TEXT")
            self._ensure_column(conn, "video_cache", "description", "TEXT")
            self._ensure_column(conn, "video_cache", "webpage_url", "TEXT")
            self._ensure_column(conn, "video_cache", "uploader", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rejected_cache (
                    cache_key TEXT PRIMARY KEY,
                    message TEXT NOT NULL,
                    title TEXT,
                    description TEXT,
                    webpage_url TEXT,
                    uploader TEXT,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS url_aliases (
                    normalized_url TEXT PRIMARY KEY,
                    cache_key TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_url_aliases_cache_key ON url_aliases(cache_key)"
            )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
