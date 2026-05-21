from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 2

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    handle TEXT,
    source_url TEXT,
    uploads_url TEXT,
    title TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    first_synced_at TEXT,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS library_channels (
    library_channel_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    channel_id TEXT REFERENCES channels(channel_id) ON DELETE SET NULL,
    handle TEXT,
    title TEXT,
    selected INTEGER NOT NULL DEFAULT 1,
    import_source TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_url)
);

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT REFERENCES channels(channel_id) ON DELETE SET NULL,
    title TEXT,
    description TEXT,
    duration_seconds INTEGER,
    published_at TEXT,
    live_status TEXT,
    thumbnail_url TEXT,
    metadata_hash TEXT,
    ingest_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    title, description,
    content='videos',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts(rowid, title, description)
    VALUES (new.rowid, new.title, new.description);
END;

CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, title, description)
    VALUES ('delete', old.rowid, old.title, old.description);
    INSERT INTO videos_fts(rowid, title, description)
    VALUES (new.rowid, new.title, new.description);
END;

CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, title, description)
    VALUES ('delete', old.rowid, old.title, old.description);
END;

CREATE TABLE IF NOT EXISTS transcript_versions (
    transcript_version_id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    language TEXT,
    is_generated INTEGER NOT NULL DEFAULT 0,
    raw_path TEXT NOT NULL,
    normalized_path TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    segment_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    transcript_version_id TEXT NOT NULL REFERENCES transcript_versions(transcript_version_id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    channel_id TEXT REFERENCES channels(channel_id) ON DELETE SET NULL,
    sequence INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER,
    text_hash TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(transcript_version_id, sequence)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    artifact_status TEXT NOT NULL DEFAULT 'pending',
    index_status TEXT NOT NULL DEFAULT 'pending',
    embedded_at TEXT,
    PRIMARY KEY (chunk_id, provider, model, dimension)
);

CREATE TABLE IF NOT EXISTS transcript_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    status TEXT NOT NULL,
    error_class TEXT,
    error TEXT,
    retryable INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_kind TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lock_owner TEXT,
    retry_after TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_library_channels_selected ON library_channels(selected, title);
CREATE INDEX IF NOT EXISTS idx_library_channels_channel_id ON library_channels(channel_id);
CREATE INDEX IF NOT EXISTS idx_transcript_versions_video_id ON transcript_versions(video_id);
CREATE INDEX IF NOT EXISTS idx_transcript_versions_active ON transcript_versions(video_id, active);
CREATE INDEX IF NOT EXISTS idx_chunks_video_time ON chunks(video_id, start_ms, end_ms);
CREATE INDEX IF NOT EXISTS idx_transcript_attempts_video_id ON transcript_attempts(video_id);
CREATE INDEX IF NOT EXISTS idx_transcript_attempts_status ON transcript_attempts(status, retryable);
CREATE INDEX IF NOT EXISTS idx_jobs_status_retry ON jobs(status, retry_after);

INSERT OR IGNORE INTO library_channels(
    library_channel_id, source, source_url, channel_id, handle, title, selected, import_source
)
SELECT
    'catalog:' || channel_id,
    'youtube:channel:' || channel_id,
    COALESCE(source_url, 'https://www.youtube.com/channel/' || channel_id),
    channel_id,
    handle,
    title,
    1,
    'catalog'
FROM channels
WHERE channel_id IS NOT NULL;
"""


def connect_catalog(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def bootstrap_catalog(db_path: Path) -> None:
    with connect_catalog(db_path) as connection:
        had_videos_fts = _sqlite_object_exists(connection, "table", "videos_fts")
        connection.executescript(SCHEMA_SQL)
        has_version = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
        if has_version is None or not had_videos_fts or _videos_fts_needs_rebuild(connection):
            connection.execute("INSERT INTO videos_fts(videos_fts) VALUES('rebuild')")
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
            (SCHEMA_VERSION,),
        )


def _sqlite_object_exists(connection: sqlite3.Connection, object_type: str, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        (object_type, name),
    ).fetchone()
    return row is not None


def _videos_fts_needs_rebuild(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT title, description
        FROM videos
        WHERE COALESCE(title, description) IS NOT NULL
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return False
    token_source = row["title"] or row["description"] or ""
    token = next((part for part in token_source.replace("-", " ").split() if part.isalnum()), "")
    if not token:
        return False
    try:
        match = connection.execute(
            "SELECT rowid FROM videos_fts WHERE videos_fts MATCH ? LIMIT 1",
            (token,),
        ).fetchone()
    except sqlite3.OperationalError:
        return True
    return match is None


def fts5_available() -> bool:
    try:
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE test_fts USING fts5(text)")
    except sqlite3.OperationalError:
        return False
    return True


def catalog_tables(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    with connect_catalog(db_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
    return {row["name"] for row in rows}


def catalog_is_initialized(db_path: Path) -> bool:
    required_tables = {
        "schema_migrations",
        "channels",
        "library_channels",
        "videos",
        "videos_fts",
        "transcript_versions",
        "chunks",
        "chunks_fts",
        "embeddings",
        "transcript_attempts",
        "jobs",
    }
    return required_tables.issubset(catalog_tables(db_path))
