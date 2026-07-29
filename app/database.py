from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_scan_at TEXT,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL,
    name TEXT NOT NULL,
    extension TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'other',
    mime_type TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    inode INTEGER NOT NULL DEFAULT 0,
    quick_hash TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    width INTEGER,
    height INTEGER,
    duration REAL,
    captured_at TEXT,
    latitude REAL,
    longitude REAL,
    perceptual_hash TEXT NOT NULL DEFAULT '',
    extracted_text TEXT NOT NULL DEFAULT '',
    ai_caption TEXT NOT NULL DEFAULT '',
    manual_caption TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    metadata_status TEXT NOT NULL DEFAULT 'ready',
    vision_status TEXT NOT NULL DEFAULT 'pending',
    transcription_status TEXT NOT NULL DEFAULT 'pending',
    embedding_status TEXT NOT NULL DEFAULT 'pending',
    vision_error TEXT NOT NULL DEFAULT '',
    transcription_error TEXT NOT NULL DEFAULT '',
    embedding_error TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    next_retry_at TEXT,
    terminal_error INTEGER NOT NULL DEFAULT 0,
    last_error_fingerprint TEXT NOT NULL DEFAULT '',
    scan_token TEXT NOT NULL DEFAULT '',
    indexed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_library ON files(library_id);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime_ns DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    work_total INTEGER NOT NULL DEFAULT 0,
    work_done INTEGER NOT NULL DEFAULT 0,
    heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority DESC, id);

CREATE TABLE IF NOT EXISTS content_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    start_offset INTEGER,
    end_offset INTEGER,
    start_time REAL,
    end_time REAL,
    source_label TEXT NOT NULL DEFAULT '',
    embedding_json TEXT,
    UNIQUE(file_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON content_chunks(file_id);

CREATE TABLE IF NOT EXISTS similarity_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS similarity_group_files (
    group_id INTEGER NOT NULL REFERENCES similarity_groups(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    distance INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(group_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_similarity_files ON similarity_group_files(file_id);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    enabled INTEGER NOT NULL DEFAULT 1,
    password_setup_required INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_token ON user_sessions(token_hash);

CREATE TABLE IF NOT EXISTS library_permissions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, library_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS file_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    query TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, file_id, query)
);

CREATE INDEX IF NOT EXISTS idx_feedback_file ON file_feedback(file_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS favorites (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, file_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS file_tags (
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(tag_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_file_tags_file ON file_tags(file_id);

CREATE TABLE IF NOT EXISTS smart_albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    query TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    filters_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    sources_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages ON conversation_messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    is_named INTEGER NOT NULL DEFAULT 0,
    face_count INTEGER NOT NULL DEFAULT 0,
    cover_face_id INTEGER,
    hidden INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    face_index INTEGER NOT NULL,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    width REAL NOT NULL,
    height REAL NOT NULL,
    confidence REAL NOT NULL,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(file_id, face_index)
);

CREATE INDEX IF NOT EXISTS idx_faces_file ON faces(file_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);

CREATE TABLE IF NOT EXISTS face_scans (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    mtime_ns INTEGER NOT NULL,
    face_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    scanned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_named INTEGER NOT NULL DEFAULT 0,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    radius_m REAL NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    cover_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS place_files (
    place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    distance_m REAL NOT NULL DEFAULT 0,
    PRIMARY KEY(place_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_place_files_file ON place_files(file_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_named INTEGER NOT NULL DEFAULT 0,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    file_count INTEGER NOT NULL DEFAULT 0,
    cover_file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_files (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    PRIMARY KEY(event_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_event_files_file ON event_files(file_id);

CREATE TABLE IF NOT EXISTS trash_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_file_id INTEGER NOT NULL,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    original_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    recycle_path TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    file_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'trashed',
    actor TEXT NOT NULL DEFAULT '',
    trashed_at TEXT NOT NULL,
    restored_at TEXT,
    purged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_trash_status_time ON trash_items(status, trashed_at DESC);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#7c8cff',
    status TEXT NOT NULL DEFAULT 'active',
    owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at DESC);

CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'viewer',
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id, project_id);

CREATE TABLE IF NOT EXISTS project_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES project_folders(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, parent_id, name)
);

CREATE INDEX IF NOT EXISTS idx_project_folders_parent ON project_folders(project_id, parent_id, sort_order, name);

CREATE TABLE IF NOT EXISTS project_statuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    color TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_terminal INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, key)
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    folder_id INTEGER REFERENCES project_folders(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    rating INTEGER NOT NULL DEFAULT 0,
    assignee_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    cover_version_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_project_folder ON assets(project_id, folder_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS asset_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    version_number INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'other',
    size INTEGER NOT NULL DEFAULT 0,
    duration REAL,
    width INTEGER,
    height INTEGER,
    proxy_status TEXT NOT NULL DEFAULT 'not_requested',
    proxy_path TEXT NOT NULL DEFAULT '',
    poster_path TEXT NOT NULL DEFAULT '',
    filmstrip_path TEXT NOT NULL DEFAULT '',
    waveform_path TEXT NOT NULL DEFAULT '',
    proxy_error TEXT NOT NULL DEFAULT '',
    look_status TEXT NOT NULL DEFAULT 'not_requested',
    look_path TEXT NOT NULL DEFAULT '',
    look_name TEXT NOT NULL DEFAULT '',
    look_error TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_asset_versions_asset ON asset_versions(asset_id, version_number DESC);

CREATE TABLE IF NOT EXISTS review_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS review_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    version_id INTEGER REFERENCES asset_versions(id) ON DELETE CASCADE,
    review_session_id INTEGER REFERENCES review_sessions(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    guest_name TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    comment_type TEXT NOT NULL DEFAULT 'text',
    time_start REAL,
    time_end REAL,
    x REAL,
    y REAL,
    drawing_json TEXT NOT NULL DEFAULT '[]',
    visibility TEXT NOT NULL DEFAULT 'team',
    resolved INTEGER NOT NULL DEFAULT 0,
    resolved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_comments_asset ON review_comments(asset_id, version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_comments_unresolved ON review_comments(asset_id, resolved, created_at);

CREATE TABLE IF NOT EXISTS comment_attachments (
    comment_id INTEGER NOT NULL REFERENCES review_comments(id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(comment_id, name)
);

CREATE TABLE IF NOT EXISTS share_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    asset_id INTEGER REFERENCES assets(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    access_code_hash TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    can_download INTEGER NOT NULL DEFAULT 0,
    can_comment INTEGER NOT NULL DEFAULT 1,
    can_view_versions INTEGER NOT NULL DEFAULT 0,
    watermark_text TEXT NOT NULL DEFAULT '',
    brand_name TEXT NOT NULL DEFAULT 'NAS AI Space',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    last_access_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_share_links_project ON share_links(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    read_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at, id DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._write_lock = threading.RLock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            with self.connect() as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    yield connection
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(files)").fetchall()}
            migrations = {
                "scan_token": "ALTER TABLE files ADD COLUMN scan_token TEXT NOT NULL DEFAULT ''",
                "captured_at": "ALTER TABLE files ADD COLUMN captured_at TEXT",
                "latitude": "ALTER TABLE files ADD COLUMN latitude REAL",
                "longitude": "ALTER TABLE files ADD COLUMN longitude REAL",
                "perceptual_hash": "ALTER TABLE files ADD COLUMN perceptual_hash TEXT NOT NULL DEFAULT ''",
                "content_hash": "ALTER TABLE files ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''",
                "manual_caption": "ALTER TABLE files ADD COLUMN manual_caption TEXT NOT NULL DEFAULT ''",
                "metadata_status": "ALTER TABLE files ADD COLUMN metadata_status TEXT NOT NULL DEFAULT 'ready'",
                "vision_status": "ALTER TABLE files ADD COLUMN vision_status TEXT NOT NULL DEFAULT 'pending'",
                "transcription_status": "ALTER TABLE files ADD COLUMN transcription_status TEXT NOT NULL DEFAULT 'pending'",
                "embedding_status": "ALTER TABLE files ADD COLUMN embedding_status TEXT NOT NULL DEFAULT 'pending'",
                "vision_error": "ALTER TABLE files ADD COLUMN vision_error TEXT NOT NULL DEFAULT ''",
                "transcription_error": "ALTER TABLE files ADD COLUMN transcription_error TEXT NOT NULL DEFAULT ''",
                "embedding_error": "ALTER TABLE files ADD COLUMN embedding_error TEXT NOT NULL DEFAULT ''",
                "retry_count": "ALTER TABLE files ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
                "last_attempt_at": "ALTER TABLE files ADD COLUMN last_attempt_at TEXT",
                "next_retry_at": "ALTER TABLE files ADD COLUMN next_retry_at TEXT",
                "terminal_error": "ALTER TABLE files ADD COLUMN terminal_error INTEGER NOT NULL DEFAULT 0",
                "last_error_fingerprint": "ALTER TABLE files ADD COLUMN last_error_fingerprint TEXT NOT NULL DEFAULT ''",
            }
            stage_migration = any(column not in columns for column in (
                "metadata_status", "vision_status", "transcription_status", "embedding_status"
            ))
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            if "user_id" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
            for column, statement in {
                "work_total": "ALTER TABLE tasks ADD COLUMN work_total INTEGER NOT NULL DEFAULT 0",
                "work_done": "ALTER TABLE tasks ADD COLUMN work_done INTEGER NOT NULL DEFAULT 0",
                "heartbeat_at": "ALTER TABLE tasks ADD COLUMN heartbeat_at TEXT",
            }.items():
                if column not in task_columns:
                    connection.execute(statement)
            user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
            if "password_setup_required" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN password_setup_required INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """INSERT OR IGNORE INTO project_members(project_id, user_id, role, created_at)
                   SELECT id, owner_id, 'owner', created_at FROM projects WHERE owner_id IS NOT NULL"""
            )
            connection.execute(
                """UPDATE project_members SET role = 'owner' WHERE EXISTS (
                       SELECT 1 FROM projects p
                       WHERE p.id = project_members.project_id AND p.owner_id = project_members.user_id
                   )"""
            )
            connection.execute(
                """UPDATE project_members SET role = 'manager'
                   WHERE role = 'owner' AND NOT EXISTS (
                       SELECT 1 FROM projects p
                       WHERE p.id = project_members.project_id AND p.owner_id = project_members.user_id
                   )"""
            )
            version_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(asset_versions)").fetchall()
            }
            for column, statement in {
                "look_status": (
                    "ALTER TABLE asset_versions ADD COLUMN look_status "
                    "TEXT NOT NULL DEFAULT 'not_requested'"
                ),
                "look_path": "ALTER TABLE asset_versions ADD COLUMN look_path TEXT NOT NULL DEFAULT ''",
                "look_name": "ALTER TABLE asset_versions ADD COLUMN look_name TEXT NOT NULL DEFAULT ''",
                "look_error": "ALTER TABLE asset_versions ADD COLUMN look_error TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in version_columns:
                    connection.execute(statement)
            chunk_columns = {row[1] for row in connection.execute("PRAGMA table_info(content_chunks)").fetchall()}
            if "source_label" not in chunk_columns:
                connection.execute("ALTER TABLE content_chunks ADD COLUMN source_label TEXT NOT NULL DEFAULT ''")
            people_columns = {row[1] for row in connection.execute("PRAGMA table_info(people)").fetchall()}
            if "hidden" not in people_columns:
                connection.execute("ALTER TABLE people ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            event_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)").fetchall()}
            if "hidden" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            if stage_migration:
                self._backfill_index_stages(connection)
            self._normalize_pending_stages(connection)
            current_columns = {row[1] for row in connection.execute("PRAGMA table_info(files)").fetchall()}
            if {"kind", "transcription_status", "extracted_text"}.issubset(current_columns):
                connection.execute(
                    """UPDATE files SET transcription_status = 'not_applicable'
                       WHERE kind = 'video' AND transcription_status = 'ready'
                       AND TRIM(extracted_text) = ''"""
                )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_files_captured ON files(captured_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_files_quick_hash ON files(quick_hash, size)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files(content_hash, size)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_files_perceptual_hash ON files(perceptual_hash)")
            self._create_fts(connection)
            connection.commit()
        os.chmod(self.path, 0o600)

    @staticmethod
    def _backfill_index_stages(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """SELECT f.*, COUNT(c.id) AS chunk_count,
               SUM(CASE WHEN c.embedding_json IS NOT NULL THEN 1 ELSE 0 END) AS embedding_count
               FROM files f LEFT JOIN content_chunks c ON c.file_id = f.id GROUP BY f.id"""
        ).fetchall()
        for row in rows:
            keys = set(row.keys())
            value = lambda name, default="": row[name] if name in keys else default
            try:
                metadata = json.loads(value("metadata_json", "{}") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            errors = [str(value) for value in metadata.get("ai_errors", [])]
            vision_error = next((value for value in errors if value.startswith("vision:")), "")
            transcription_error = next((value for value in errors if value.startswith("transcription:")), "")
            embedding_error = next((value for value in errors if value.startswith("embedding:")), "")
            kind = str(value("kind", "other"))
            caption = str(value("manual_caption") or value("ai_caption") or "")
            text = str(value("extracted_text") or "")
            media = metadata if isinstance(metadata, dict) else {}
            audio_codec = str(media.get("audio_codec") or media.get("metadata", {}).get("audio_codec") or "")
            vision_status = (
                "ready" if caption else "error" if vision_error else "missing"
            ) if kind in {"image", "video"} else "not_applicable"
            transcription_required = kind == "audio" or (kind == "video" and bool(audio_codec or transcription_error or text))
            transcription_status = (
                "ready" if text else "error" if transcription_error else "missing"
            ) if transcription_required else "not_applicable"
            if int(row["embedding_count"] or 0):
                embedding_status = "ready"
            elif int(row["chunk_count"] or 0):
                embedding_status = "error" if embedding_error else "missing"
            elif kind in {"image", "video", "audio", "document"}:
                embedding_status = "blocked"
            else:
                embedding_status = "not_applicable"
            status = str(value("status", "pending"))
            if status == "ready" and any(value in {"error", "missing", "blocked"} for value in (
                vision_status, transcription_status, embedding_status
            )):
                status = "partial"
            connection.execute(
                """UPDATE files SET metadata_status = 'ready', vision_status = ?, transcription_status = ?,
                   embedding_status = ?, vision_error = ?, transcription_error = ?, embedding_error = ?, status = ?
                   WHERE id = ?""",
                (
                    vision_status, transcription_status, embedding_status,
                    vision_error[:2000], transcription_error[:2000], embedding_error[:2000],
                    status, row["id"],
                ),
            )

    @staticmethod
    def _normalize_pending_stages(connection: sqlite3.Connection) -> None:
        connection.execute(
            """UPDATE files SET metadata_status = 'ready',
               vision_status = CASE WHEN kind IN ('image', 'video') THEN 'pending' ELSE 'not_applicable' END,
               transcription_status = CASE WHEN kind IN ('video', 'audio') THEN 'pending' ELSE 'not_applicable' END,
               embedding_status = CASE
                 WHEN kind IN ('image', 'video', 'audio', 'document') THEN 'pending'
                 ELSE 'not_applicable' END,
               vision_error = '', transcription_error = '', embedding_error = '',
               retry_count = 0, last_attempt_at = NULL, next_retry_at = NULL,
               terminal_error = 0, last_error_fingerprint = ''
               WHERE status = 'pending'"""
        )

    @staticmethod
    def _create_fts(connection: sqlite3.Connection) -> None:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5("
                "name, path, content, caption, tokenize='trigram')"
            )
        except sqlite3.OperationalError:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5("
                "name, path, content, caption, tokenize='unicode61')"
            )

    def fetchone(self, query: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
            return dict(row) if row else None

    def fetchall(self, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]

    def execute(self, query: str, params: Iterable[Any] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(query, tuple(params))
            return int(cursor.lastrowid or 0)

    def create_library(self, name: str, path: str) -> dict[str, Any]:
        now = utc_now()
        library_id = self.execute(
            "INSERT INTO libraries(name, path, created_at) VALUES (?, ?, ?)",
            (name, path, now),
        )
        return self.fetchone("SELECT * FROM libraries WHERE id = ?", (library_id,)) or {}

    def list_libraries(self) -> list[dict[str, Any]]:
        return self.fetchall("SELECT * FROM libraries ORDER BY name COLLATE NOCASE")

    def get_library(self, library_id: int) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM libraries WHERE id = ?", (library_id,))

    def update_library_stats(self, library_id: int) -> None:
        with self.transaction() as connection:
            stats = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes FROM files WHERE library_id = ?",
                (library_id,),
            ).fetchone()
            connection.execute(
                "UPDATE libraries SET file_count = ?, total_bytes = ?, last_scan_at = ? WHERE id = ?",
                (stats["count"], stats["bytes"], utc_now(), library_id),
            )

    def upsert_file(self, values: dict[str, Any]) -> tuple[int, bool]:
        return self.upsert_files([values])[0]

    def upsert_files(self, values_list: list[dict[str, Any]]) -> list[tuple[int, bool]]:
        if not values_list:
            return []
        now = utc_now()
        results: list[tuple[int, bool]] = []
        with self.transaction() as connection:
            for values in values_list:
                existing = connection.execute(
                    "SELECT id, size, mtime_ns FROM files WHERE path = ?",
                    (values["path"],),
                ).fetchone()
                changed = not existing or existing["size"] != values["size"] or existing["mtime_ns"] != values["mtime_ns"]
                if existing and changed:
                    connection.execute(
                        """UPDATE files SET library_id = ?, relative_path = ?, name = ?, extension = ?, kind = ?,
                           mime_type = ?, size = ?, mtime_ns = ?, inode = ?, status = 'pending', error = '',
                           quick_hash = '', content_hash = '', perceptual_hash = '', metadata_status = 'ready',
                           vision_status = 'pending', transcription_status = 'pending', embedding_status = 'pending',
                           vision_error = '', transcription_error = '', embedding_error = '',
                           retry_count = 0, last_attempt_at = NULL, next_retry_at = NULL,
                           terminal_error = 0, last_error_fingerprint = '',
                           scan_token = ?, updated_at = ? WHERE id = ?""",
                        (
                            values["library_id"], values["relative_path"], values["name"], values["extension"],
                            values["kind"], values["mime_type"], values["size"], values["mtime_ns"], values["inode"],
                            values.get("scan_token", ""), now, existing["id"],
                        ),
                    )
                    connection.execute("DELETE FROM similarity_group_files WHERE file_id = ?", (existing["id"],))
                    results.append((int(existing["id"]), True))
                elif existing:
                    connection.execute(
                        "UPDATE files SET library_id = ?, relative_path = ?, scan_token = ? WHERE id = ?",
                        (values["library_id"], values["relative_path"], values.get("scan_token", ""), existing["id"]),
                    )
                    results.append((int(existing["id"]), False))
                else:
                    cursor = connection.execute(
                        """INSERT INTO files(library_id, path, relative_path, name, extension, kind, mime_type,
                           size, mtime_ns, inode, scan_token, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            values["library_id"], values["path"], values["relative_path"], values["name"], values["extension"],
                            values["kind"], values["mime_type"], values["size"], values["mtime_ns"], values["inode"],
                            values.get("scan_token", ""), now, now,
                        ),
                    )
                    results.append((int(cursor.lastrowid), True))
            connection.execute(
                """UPDATE similarity_groups SET member_count = (
                   SELECT COUNT(*) FROM similarity_group_files WHERE group_id = similarity_groups.id)"""
            )
            connection.execute("DELETE FROM similarity_groups WHERE member_count < 2")
        return results

    def mark_files_seen(self, file_ids: list[int], scan_token: str) -> None:
        if not file_ids:
            return
        with self.transaction() as connection:
            connection.executemany(
                "UPDATE files SET scan_token = ? WHERE id = ?",
                [(scan_token, file_id) for file_id in file_ids],
            )

    def mark_missing_files(self, library_id: int, scan_token: str) -> list[int]:
        existing = self.fetchall(
            "SELECT id FROM files WHERE library_id = ? AND scan_token != ?",
            (library_id, scan_token),
        )
        missing_ids = [row["id"] for row in existing]
        if not missing_ids:
            return []
        with self.transaction() as connection:
            for start in range(0, len(missing_ids), 500):
                batch = missing_ids[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                connection.execute(f"DELETE FROM files_fts WHERE rowid IN ({placeholders})", batch)
                connection.execute(f"DELETE FROM files WHERE id IN ({placeholders})", batch)
            connection.execute(
                """UPDATE similarity_groups SET member_count = (
                   SELECT COUNT(*) FROM similarity_group_files WHERE group_id = similarity_groups.id)"""
            )
            connection.execute("DELETE FROM similarity_groups WHERE member_count < 2")
        return [int(file_id) for file_id in missing_ids]

    def pending_file_ids(
        self,
        library_id: int | None = None,
        limit: int | None = None,
        kind: str = "",
        order: str = "balanced",
    ) -> list[int]:
        clauses = ["status = 'pending'"]
        params: list[Any] = []
        if library_id is not None:
            clauses.append("library_id = ?")
            params.append(library_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        ordering = {
            "newest": "mtime_ns DESC, id",
            "oldest": "mtime_ns, id",
            "smallest": "size, id",
        }.get(
            order,
            """CASE kind
               WHEN 'document' THEN 0
               WHEN 'image' THEN 1
               WHEN 'audio' THEN 2
               WHEN 'video' THEN 3
               ELSE 4 END, size, id""",
        )
        query = "SELECT id FROM files WHERE " + " AND ".join(clauses) + f" ORDER BY {ordering}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self.fetchall(query, params)
        return [int(row["id"]) for row in rows]

    def pending_summary(self) -> dict[str, Any]:
        total = self.fetchone(
            """SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes
               FROM files WHERE status = 'pending'"""
        ) or {"count": 0, "bytes": 0}
        kinds = self.fetchall(
            """SELECT kind, COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes
               FROM files WHERE status = 'pending' GROUP BY kind ORDER BY count DESC"""
        )
        libraries = self.fetchall(
            """SELECT l.id, l.name, COUNT(f.id) AS count, COALESCE(SUM(f.size), 0) AS bytes
               FROM libraries l LEFT JOIN files f ON f.library_id = l.id AND f.status = 'pending'
               GROUP BY l.id ORDER BY l.name COLLATE NOCASE"""
        )
        return {"total": int(total["count"]), "bytes": int(total["bytes"]), "kinds": kinds, "libraries": libraries}

    def caption_upgrade_file_ids(self, limit: int = 50) -> list[int]:
        try:
            rows = self.fetchall(
                """SELECT id FROM files WHERE kind = 'image' AND status IN ('ready', 'partial') AND ai_caption != ''
                   AND COALESCE(json_extract(metadata_json, '$.caption_version'), 0) < 3
                   ORDER BY mtime_ns DESC, id LIMIT ?""",
                (max(1, int(limit)),),
            )
        except sqlite3.OperationalError:
            rows = self.fetchall(
                """SELECT id FROM files WHERE kind = 'image' AND status = 'ready' AND ai_caption != ''
                   ORDER BY mtime_ns DESC, id LIMIT ?""",
                (max(1, int(limit)),),
            )
        return [int(row["id"]) for row in rows]

    def caption_upgrade_count(self) -> int:
        try:
            row = self.fetchone(
                """SELECT COUNT(*) AS count FROM files WHERE kind = 'image' AND status IN ('ready', 'partial')
                   AND ai_caption != '' AND COALESCE(json_extract(metadata_json, '$.caption_version'), 0) < 3"""
            )
        except sqlite3.OperationalError:
            row = self.fetchone(
                "SELECT COUNT(*) AS count FROM files WHERE kind = 'image' AND status = 'ready' AND ai_caption != ''"
            )
        return int(row["count"] if row else 0)

    def repair_file_ids(self, limit: int = 50) -> list[int]:
        now = utc_now()
        rows = self.fetchall(
            """SELECT id FROM files WHERE status IN ('partial', 'error')
               AND terminal_error = 0 AND (next_retry_at IS NULL OR next_retry_at <= ?)
               ORDER BY CASE
                 WHEN manual_caption != '' THEN 0
                 WHEN kind = 'document' THEN 1
                 WHEN kind = 'image' THEN 2
                 WHEN kind = 'audio' THEN 3
                 WHEN kind = 'video' THEN 4
                 ELSE 5 END, size, id LIMIT ?""",
            (now, max(1, int(limit))),
        )
        return [int(row["id"]) for row in rows]

    def repair_count(self) -> int:
        now = utc_now()
        row = self.fetchone(
            """SELECT COUNT(*) AS count FROM files WHERE status IN ('partial', 'error')
               AND terminal_error = 0 AND (next_retry_at IS NULL OR next_retry_at <= ?)""",
            (now,),
        )
        return int(row["count"] if row else 0)

    def retry_waiting_count(self) -> int:
        row = self.fetchone(
            """SELECT COUNT(*) AS count FROM files
               WHERE status IN ('partial', 'error') AND terminal_error = 0
               AND next_retry_at IS NOT NULL AND next_retry_at > ?""",
            (utc_now(),),
        )
        return int(row["count"] if row else 0)

    def terminal_failure_count(self) -> int:
        row = self.fetchone(
            "SELECT COUNT(*) AS count FROM files WHERE status IN ('partial', 'error') AND terminal_error = 1"
        )
        return int(row["count"] if row else 0)

    def index_stage_summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "repairable": self.repair_count(),
            "retry_waiting": self.retry_waiting_count(),
            "terminal_failures": self.terminal_failure_count(),
        }
        for column, name in (
            ("vision_status", "vision"),
            ("transcription_status", "transcription"),
            ("embedding_status", "embedding"),
        ):
            rows = self.fetchall(f"SELECT {column} AS status, COUNT(*) AS count FROM files GROUP BY {column}")
            result[name] = {str(row["status"]): int(row["count"]) for row in rows}
        return result

    def get_file(self, file_id: int) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM files WHERE id = ?", (file_id,))

    @staticmethod
    def _retry_state(
        retry_count: int,
        previous_fingerprint: str,
        error: str,
        max_attempts: int,
        base_seconds: int,
    ) -> tuple[int, str | None, int, str]:
        normalized = re.sub(r"[/\\][^\s'\"\]]*(?:cache|tmp)[/\\][^\s'\"\]]+", "<temporary>", error)
        fingerprint = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()
        attempts = retry_count + 1
        terminal = int(attempts >= max(1, max_attempts))
        if terminal:
            next_retry = None
        else:
            delay = min(7 * 86400, max(30, base_seconds) * (2 ** max(0, attempts - 1)))
            next_retry = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
        return attempts, next_retry, terminal, fingerprint

    def finish_file_index(
        self,
        file_id: int,
        result: dict[str, Any],
        chunks: list[dict[str, Any]],
        retry_max_attempts: int = 3,
        retry_base_seconds: int = 300,
    ) -> str:
        now = utc_now()
        stages = result.get("stages", {})
        metadata = result.get("metadata", {})
        errors = [str(value) for value in metadata.get("ai_errors", [])]

        def stage_value(name: str, prefixes: tuple[str, ...], fallback: str) -> tuple[str, str]:
            item = stages.get(name, {}) if isinstance(stages, dict) else {}
            status = str(item.get("status") or fallback)
            error = str(item.get("error") or next(
                (value for value in errors if value.startswith(prefixes)), ""
            ))
            return status, error

        vision_fallback = "ready" if result.get("caption") else "not_applicable"
        transcription_fallback = "ready" if result.get("text") else "not_applicable"
        embedding_fallback = "ready" if any(chunk.get("embedding") for chunk in chunks) else (
            "missing" if chunks else "not_applicable"
        )
        vision_status, vision_error = stage_value("vision", ("vision:",), vision_fallback)
        transcription_status, transcription_error = stage_value(
            "transcription", ("transcription:",), transcription_fallback
        )
        embedding_status, embedding_error = stage_value("embedding", ("embedding:",), embedding_fallback)
        final_status = "partial" if any(
            value in {"error", "missing", "blocked"}
            for value in (vision_status, transcription_status, embedding_status)
        ) else "ready"
        error_summary = "；".join(filter(None, (vision_error, transcription_error, embedding_error)))[:2000]
        with self.transaction() as connection:
            previous = connection.execute(
                "SELECT retry_count, last_error_fingerprint FROM files WHERE id = ?",
                (file_id,),
            ).fetchone()
            if final_status == "ready":
                retry_count, next_retry_at, terminal_error, fingerprint = 0, None, 0, ""
            else:
                retry_count, next_retry_at, terminal_error, fingerprint = self._retry_state(
                    int(previous["retry_count"] or 0) if previous else 0,
                    str(previous["last_error_fingerprint"] or "") if previous else "",
                    error_summary or final_status,
                    retry_max_attempts,
                    retry_base_seconds,
                )
            connection.execute(
                """UPDATE files SET width = ?, height = ?, duration = ?, captured_at = ?, latitude = ?, longitude = ?,
                   quick_hash = ?, perceptual_hash = COALESCE(NULLIF(?, ''), perceptual_hash), extracted_text = ?,
                   ai_caption = ?, metadata_json = ?, status = ?, error = ?, metadata_status = 'ready',
                   vision_status = ?, transcription_status = ?, embedding_status = ?,
                   vision_error = ?, transcription_error = ?, embedding_error = ?,
                   retry_count = ?, last_attempt_at = ?, next_retry_at = ?,
                   terminal_error = ?, last_error_fingerprint = ?, indexed_at = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    result.get("width"), result.get("height"), result.get("duration"), result.get("captured_at"),
                    result.get("latitude"), result.get("longitude"), result.get("quick_hash", ""),
                    result.get("perceptual_hash", ""), result.get("text", ""), result.get("caption", ""),
                    json.dumps(metadata, ensure_ascii=False), final_status, error_summary,
                    vision_status, transcription_status, embedding_status,
                    vision_error[:2000], transcription_error[:2000], embedding_error[:2000],
                    retry_count, now, next_retry_at, terminal_error, fingerprint,
                    now, now, file_id,
                ),
            )
            connection.execute("DELETE FROM content_chunks WHERE file_id = ?", (file_id,))
            for index, chunk in enumerate(chunks):
                connection.execute(
                    """INSERT INTO content_chunks(file_id, chunk_index, content, start_offset, end_offset,
                       start_time, end_time, source_label, embedding_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        file_id, index, chunk.get("content", ""), chunk.get("start_offset"), chunk.get("end_offset"),
                        chunk.get("start_time"), chunk.get("end_time"), chunk.get("source_label", ""),
                        json.dumps(chunk["embedding"]) if chunk.get("embedding") else None,
                    ),
                )
            row = connection.execute("SELECT name, relative_path, extracted_text, ai_caption FROM files WHERE id = ?", (file_id,)).fetchone()
            connection.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
            connection.execute(
                "INSERT INTO files_fts(rowid, name, path, content, caption) VALUES (?, ?, ?, ?, ?)",
                (file_id, row["name"], row["relative_path"], row["extracted_text"], row["ai_caption"]),
            )
        return final_status

    def fail_file_index(
        self,
        file_id: int,
        error: str,
        retry_max_attempts: int = 3,
        retry_base_seconds: int = 300,
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            previous = connection.execute(
                "SELECT retry_count, last_error_fingerprint FROM files WHERE id = ?",
                (file_id,),
            ).fetchone()
            retry_count, next_retry_at, terminal_error, fingerprint = self._retry_state(
                int(previous["retry_count"] or 0) if previous else 0,
                str(previous["last_error_fingerprint"] or "") if previous else "",
                error,
                retry_max_attempts,
                retry_base_seconds,
            )
            connection.execute(
                """UPDATE files SET status = 'error', error = ?, retry_count = ?, last_attempt_at = ?,
                   next_retry_at = ?, terminal_error = ?, last_error_fingerprint = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    error[:2000], retry_count, now, next_retry_at, terminal_error,
                    fingerprint, now, file_id,
                ),
            )

    def reset_file_retry(self, file_id: int, pending: bool = False) -> None:
        status_sql = "status = 'pending', error = ''," if pending else ""
        self.execute(
            f"""UPDATE files SET {status_sql} retry_count = 0, last_attempt_at = NULL,
                next_retry_at = NULL, terminal_error = 0, last_error_fingerprint = '', updated_at = ?
                WHERE id = ?""",
            (utc_now(), file_id),
        )

    def create_task(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int = 0,
        user_id: int | None = None,
    ) -> int:
        return self.execute(
            "INSERT INTO tasks(type, priority, payload_json, created_at, user_id) VALUES (?, ?, ?, ?, ?)",
            (task_type, priority, json.dumps(payload, ensure_ascii=False), utc_now(), user_id),
        )

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return row

    def list_tasks(self, limit: int = 100, user_id: int | None = None) -> list[dict[str, Any]]:
        if user_id is None:
            rows = self.fetchall("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.fetchall("SELECT * FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return rows

    def recover_tasks(self) -> list[int]:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE tasks SET status = 'cancelled', message = '已取消', finished_at = ?
                   WHERE status IN ('pending', 'running') AND cancel_requested = 1""",
                (utc_now(),),
            )
            connection.execute(
                """UPDATE tasks SET status = 'pending', progress = 0, message = '服务重启后恢复',
                   started_at = NULL, work_done = 0, work_total = 0, heartbeat_at = ?
                   WHERE status = 'running' AND cancel_requested = 0""",
                (utc_now(),),
            )
            rows = connection.execute(
                "SELECT id FROM tasks WHERE status = 'pending' AND cancel_requested = 0 ORDER BY priority DESC, id"
            ).fetchall()
            return [int(row["id"]) for row in rows]

    def start_task(self, task_id: int) -> None:
        now = utc_now()
        self.execute(
            """UPDATE tasks SET status = 'running', started_at = ?, message = '', progress = 0,
               work_done = 0, work_total = 0, heartbeat_at = ? WHERE id = ?""",
            (now, now, task_id),
        )

    def update_task(
        self,
        task_id: int,
        progress: float,
        message: str,
        work_done: int | None = None,
        work_total: int | None = None,
    ) -> None:
        now = utc_now()
        if work_done is None and work_total is None:
            self.execute(
                "UPDATE tasks SET progress = ?, message = ?, heartbeat_at = ? WHERE id = ?",
                (max(0.0, min(1.0, progress)), message[:1000], now, task_id),
            )
            return
        self.execute(
            """UPDATE tasks SET progress = ?, message = ?, work_done = COALESCE(?, work_done),
               work_total = COALESCE(?, work_total), heartbeat_at = ? WHERE id = ?""",
            (
                max(0.0, min(1.0, progress)), message[:1000],
                work_done, work_total, now, task_id,
            ),
        )

    def finish_task(self, task_id: int, message: str = "完成") -> None:
        now = utc_now()
        self.execute(
            """UPDATE tasks SET status = 'completed', progress = 1, message = ?, finished_at = ?,
               heartbeat_at = ?, work_done = CASE WHEN work_total > 0 THEN work_total ELSE work_done END
               WHERE id = ?""",
            (message[:1000], now, now, task_id),
        )

    def mark_task_cancelled(self, task_id: int) -> None:
        now = utc_now()
        self.execute(
            """UPDATE tasks SET status = 'cancelled', message = '已取消', finished_at = ?,
               heartbeat_at = ? WHERE id = ?""",
            (now, now, task_id),
        )

    def fail_task(self, task_id: int, error: str) -> None:
        now = utc_now()
        self.execute(
            "UPDATE tasks SET status = 'failed', error = ?, finished_at = ?, heartbeat_at = ? WHERE id = ?",
            (error[:4000], now, now, task_id),
        )

    def cancel_task(self, task_id: int) -> None:
        self.execute("UPDATE tasks SET cancel_requested = 1 WHERE id = ?", (task_id,))

    def reset_task(self, task_id: int) -> None:
        self.execute(
            """UPDATE tasks SET status = 'pending', progress = 0, message = '等待重试', error = '',
               cancel_requested = 0, started_at = NULL, finished_at = NULL,
               work_total = 0, work_done = 0, heartbeat_at = ? WHERE id = ?""",
            (utc_now(), task_id),
        )

    def is_task_cancelled(self, task_id: int) -> bool:
        row = self.fetchone("SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,))
        return bool(row and row["cancel_requested"])

    def active_task(self, task_type: str) -> dict[str, Any] | None:
        row = self.fetchone(
            """SELECT * FROM tasks WHERE type = ? AND status IN ('pending', 'running')
               ORDER BY priority DESC, id LIMIT 1""",
            (task_type,),
        )
        if row:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return row

    def active_index_task(self) -> dict[str, Any] | None:
        row = self.fetchone(
            """SELECT * FROM tasks
               WHERE type IN ('index_pending', 'repair_index', 'index_files', 'upgrade_captions')
               AND status IN ('pending', 'running')
               ORDER BY priority DESC, id LIMIT 1"""
        )
        if row:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        return row

    def active_task_for_library(self, task_type: str, library_id: int) -> dict[str, Any] | None:
        rows = self.fetchall(
            """SELECT * FROM tasks WHERE type = ? AND status IN ('pending', 'running')
               ORDER BY priority DESC, id""",
            (task_type,),
        )
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if int(payload.get("library_id") or 0) == int(library_id):
                row["payload"] = payload
                row.pop("payload_json", None)
                return row
        return None

    def active_task_count(self) -> int:
        row = self.fetchone("SELECT COUNT(*) AS count FROM tasks WHERE status IN ('pending', 'running')")
        return int(row["count"] if row else 0)

    def prune_tasks(self, retain_count: int = 2000, retain_days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, retain_days))).isoformat(timespec="seconds")
        retained = max(100, retain_count)
        with self.transaction() as connection:
            old = connection.execute(
                """DELETE FROM tasks WHERE status NOT IN ('pending', 'running')
                   AND COALESCE(finished_at, created_at) < ?""",
                (cutoff,),
            ).rowcount
            overflow = connection.execute(
                """DELETE FROM tasks WHERE status NOT IN ('pending', 'running') AND id NOT IN (
                   SELECT id FROM tasks WHERE status NOT IN ('pending', 'running')
                   ORDER BY id DESC LIMIT ?)""",
                (retained,),
            ).rowcount
        return max(0, int(old or 0)) + max(0, int(overflow or 0))

    def index_runtime_summary(self) -> dict[str, Any]:
        rows = self.fetchall(
            """SELECT work_done, started_at, finished_at FROM tasks
               WHERE type IN ('index_pending', 'repair_index') AND status = 'completed'
               AND work_done > 0 AND started_at IS NOT NULL AND finished_at IS NOT NULL
               ORDER BY id DESC LIMIT 20"""
        )
        total_items = 0
        total_seconds = 0.0
        for row in rows:
            try:
                started = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
                finished = datetime.fromisoformat(str(row["finished_at"]).replace("Z", "+00:00"))
                duration = max(0.0, (finished - started).total_seconds())
            except (TypeError, ValueError):
                continue
            if duration <= 0:
                continue
            total_items += int(row["work_done"] or 0)
            total_seconds += duration
        rate = total_items / total_seconds if total_items and total_seconds else 0.0
        pending = int(self.pending_summary()["total"])
        repairable = self.repair_count()
        retry_waiting = self.retry_waiting_count()
        remaining = pending + repairable + retry_waiting
        return {
            "sample_batches": len(rows),
            "sample_items": total_items,
            "items_per_minute": round(rate * 60, 2) if rate else 0.0,
            "remaining_items": remaining,
            "eta_seconds": round(remaining / rate) if rate and remaining else 0 if not remaining else None,
        }

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.fetchone("SELECT value_json FROM app_settings WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return default

    def set_setting(self, key: str, value: Any) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO app_settings(key, value_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
                (key, json.dumps(value, ensure_ascii=False), now),
            )

    def update_file_hashes(self, values: list[tuple[str, str, int]]) -> None:
        if not values:
            return
        with self.transaction() as connection:
            connection.executemany(
                "UPDATE files SET quick_hash = COALESCE(NULLIF(?, ''), quick_hash), perceptual_hash = COALESCE(NULLIF(?, ''), perceptual_hash) WHERE id = ?",
                values,
            )

    def update_content_hashes(self, values: list[tuple[str, int]]) -> None:
        if not values:
            return
        with self.transaction() as connection:
            connection.executemany(
                "UPDATE files SET content_hash = ? WHERE id = ?",
                values,
            )

    def replace_similarity_groups(self, groups: list[list[tuple[int, int]]]) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM similarity_group_files")
            connection.execute("DELETE FROM similarity_groups")
            now = utc_now()
            for members in groups:
                cursor = connection.execute(
                    "INSERT INTO similarity_groups(member_count, created_at) VALUES (?, ?)",
                    (len(members), now),
                )
                connection.executemany(
                    "INSERT INTO similarity_group_files(group_id, file_id, distance) VALUES (?, ?, ?)",
                    [(int(cursor.lastrowid), file_id, distance) for file_id, distance in members],
                )

    def replace_places(self, groups: list[dict[str, Any]]) -> None:
        now = utc_now()
        keys = [str(group["key"]) for group in groups]
        with self.transaction() as connection:
            connection.execute("DELETE FROM place_files")
            for group in groups:
                connection.execute(
                    """INSERT INTO places(
                       place_key, name, latitude, longitude, radius_m, file_count, cover_file_id, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(place_key) DO UPDATE SET
                       name = CASE WHEN places.is_named = 1 THEN places.name ELSE excluded.name END,
                       latitude = excluded.latitude, longitude = excluded.longitude, radius_m = excluded.radius_m,
                       file_count = excluded.file_count, cover_file_id = excluded.cover_file_id,
                       updated_at = excluded.updated_at""",
                    (
                        group["key"], group["name"], group["latitude"], group["longitude"], group["radius_m"],
                        len(group["members"]), group["cover_file_id"], now, now,
                    ),
                )
                place_id = connection.execute(
                    "SELECT id FROM places WHERE place_key = ?", (group["key"],)
                ).fetchone()["id"]
                connection.executemany(
                    "INSERT INTO place_files(place_id, file_id, distance_m) VALUES (?, ?, ?)",
                    [(place_id, int(file_id), float(distance)) for file_id, distance in group["members"]],
                )
            if keys:
                placeholders = ",".join("?" for _ in keys)
                connection.execute(
                    f"DELETE FROM places WHERE is_named = 0 AND place_key NOT IN ({placeholders})",
                    keys,
                )
            else:
                connection.execute("DELETE FROM places WHERE is_named = 0")
            connection.execute(
                "DELETE FROM places WHERE id NOT IN (SELECT DISTINCT place_id FROM place_files) AND is_named = 0"
            )

    def replace_events(self, groups: list[dict[str, Any]]) -> None:
        now = utc_now()
        keys = [str(group["key"]) for group in groups]
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM event_files WHERE event_id IN (SELECT id FROM events WHERE is_named = 0)"
            )
            connection.execute("DELETE FROM events WHERE is_named = 0")
            for group in groups:
                connection.execute(
                    """INSERT INTO events(
                       event_key, name, start_at, end_at, latitude, longitude, file_count,
                       cover_file_id, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(event_key) DO UPDATE SET
                       name = CASE WHEN events.is_named = 1 THEN events.name ELSE excluded.name END,
                       start_at = excluded.start_at, end_at = excluded.end_at,
                       latitude = excluded.latitude, longitude = excluded.longitude,
                       file_count = excluded.file_count, cover_file_id = excluded.cover_file_id,
                       updated_at = excluded.updated_at""",
                    (
                        group["key"], group["name"], group["start_at"], group["end_at"],
                        group.get("latitude"), group.get("longitude"), len(group["members"]),
                        group["cover_file_id"], now, now,
                    ),
                )
                event_id = connection.execute(
                    "SELECT id FROM events WHERE event_key = ?", (group["key"],)
                ).fetchone()["id"]
                connection.executemany(
                    "INSERT INTO event_files(event_id, file_id) VALUES (?, ?)",
                    [(event_id, int(file_id)) for file_id in group["members"]],
                )
            connection.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT DISTINCT event_id FROM event_files) AND is_named = 0"
            )

    def create_trash_item(self, file: dict[str, Any], recycle_path: str, actor: str) -> int:
        return self.execute(
            """INSERT INTO trash_items(
               original_file_id, library_id, original_path, relative_path, recycle_path, name,
               size, content_hash, file_json, actor, trashed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                file["id"], file["library_id"], file["path"], file["relative_path"], recycle_path,
                file["name"], file["size"], file.get("content_hash", ""),
                json.dumps(file, ensure_ascii=False), actor, utc_now(),
            ),
        )

    def trash_files(self, records: list[tuple[dict[str, Any], str]], actor: str) -> list[int]:
        item_ids: list[int] = []
        with self.transaction() as connection:
            for file, recycle_path in records:
                cursor = connection.execute(
                    """INSERT INTO trash_items(
                       original_file_id, library_id, original_path, relative_path, recycle_path, name,
                       size, content_hash, file_json, actor, trashed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        file["id"], file["library_id"], file["path"], file["relative_path"], recycle_path,
                        file["name"], file["size"], file.get("content_hash", ""),
                        json.dumps(file, ensure_ascii=False), actor, utc_now(),
                    ),
                )
                item_ids.append(int(cursor.lastrowid))
                connection.execute("DELETE FROM files_fts WHERE rowid = ?", (file["id"],))
                connection.execute("DELETE FROM files WHERE id = ?", (file["id"],))
        return item_ids

    def delete_file_record(self, file_id: int) -> None:
        with self.transaction() as connection:
            connection.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
            connection.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def list_trash(self, limit: int = 200, status: str = "trashed") -> list[dict[str, Any]]:
        return self.fetchall(
            "SELECT * FROM trash_items WHERE status = ? ORDER BY trashed_at DESC LIMIT ?",
            (status, limit),
        )

    def get_trash_item(self, item_id: int) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM trash_items WHERE id = ?", (item_id,))

    def mark_trash_restored(self, item_id: int) -> None:
        self.execute(
            "UPDATE trash_items SET status = 'restored', restored_at = ? WHERE id = ? AND status = 'trashed'",
            (utc_now(), item_id),
        )

    def mark_trash_purged(self, item_id: int) -> None:
        self.execute(
            "UPDATE trash_items SET status = 'purged', purged_at = ? WHERE id = ? AND status = 'trashed'",
            (utc_now(), item_id),
        )

    @staticmethod
    def _library_clause(library_ids: list[int] | None, column: str = "library_id") -> tuple[str, list[Any]]:
        if library_ids is None:
            return "", []
        if not library_ids:
            return " AND 0", []
        placeholders = ",".join("?" for _ in library_ids)
        return f" AND {column} IN ({placeholders})", list(library_ids)

    def duplicate_groups(
        self,
        limit: int = 50,
        offset: int = 0,
        library_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        permission_sql, permission_params = self._library_clause(library_ids)
        summaries = self.fetchall(
            """SELECT content_hash, size, COUNT(*) AS member_count, (COUNT(*) - 1) * size AS reclaimable_bytes
               FROM files WHERE content_hash != ''""" + permission_sql + """ GROUP BY content_hash, size HAVING COUNT(*) > 1
               ORDER BY reclaimable_bytes DESC LIMIT ? OFFSET ?""",
            [*permission_params, limit, offset],
        )
        total = self.fetchone(
            """SELECT COUNT(*) AS count, COALESCE(SUM(reclaimable_bytes), 0) AS reclaimable_bytes FROM (
               SELECT (COUNT(*) - 1) * size AS reclaimable_bytes FROM files WHERE content_hash != ''
               """ + permission_sql + """ GROUP BY content_hash, size HAVING COUNT(*) > 1)""",
            permission_params,
        ) or {"count": 0, "reclaimable_bytes": 0}
        groups = []
        for summary in summaries:
            members = self.fetchall(
                "SELECT * FROM files WHERE content_hash = ? AND size = ?" + permission_sql + " ORDER BY mtime_ns",
                [summary["content_hash"], summary["size"], *permission_params],
            )
            groups.append({**summary, "items": members})
        return {"total": total["count"], "reclaimable_bytes": total["reclaimable_bytes"], "groups": groups}

    def similarity_groups(
        self,
        limit: int = 50,
        offset: int = 0,
        library_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        permission_sql, permission_params = self._library_clause(library_ids, "f.library_id")
        summaries = self.fetchall(
            """SELECT sg.id, COUNT(*) AS member_count, sg.created_at FROM similarity_groups sg
               JOIN similarity_group_files sgf ON sgf.group_id = sg.id
               JOIN files f ON f.id = sgf.file_id WHERE 1=1""" + permission_sql + """
               GROUP BY sg.id HAVING COUNT(*) > 1 ORDER BY member_count DESC, sg.id LIMIT ? OFFSET ?""",
            [*permission_params, limit, offset],
        )
        total = self.fetchone(
            """SELECT COUNT(*) AS count FROM (
               SELECT sg.id FROM similarity_groups sg
               JOIN similarity_group_files sgf ON sgf.group_id = sg.id
               JOIN files f ON f.id = sgf.file_id WHERE 1=1""" + permission_sql + """
               GROUP BY sg.id HAVING COUNT(*) > 1)""",
            permission_params,
        ) or {"count": 0}
        groups = []
        for summary in summaries:
            members = self.fetchall(
                """SELECT f.*, sgf.distance FROM similarity_group_files sgf
                   JOIN files f ON f.id = sgf.file_id WHERE sgf.group_id = ?""" + permission_sql + """
                   ORDER BY sgf.distance, f.mtime_ns""",
                [summary["id"], *permission_params],
            )
            groups.append({**summary, "items": members})
        return {"total": total["count"], "groups": groups}

    def dashboard(self, library_ids: list[int] | None = None, user_id: int | None = None) -> dict[str, Any]:
        permission_sql, permission_params = self._library_clause(library_ids)
        files = self.fetchone(
            """SELECT COUNT(*) AS total, COALESCE(SUM(size), 0) AS bytes,
               COALESCE(SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END), 0) AS ready,
               COALESCE(SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END), 0) AS partial,
               COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending,
               COALESCE(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END), 0) AS errors FROM files WHERE 1=1""" + permission_sql,
            permission_params,
        ) or {}
        chunk_permission_sql, chunk_permission_params = self._library_clause(library_ids, "f.library_id")
        semantic = self.fetchone(
            """SELECT COUNT(DISTINCT c.file_id) AS files FROM content_chunks c
               JOIN files f ON f.id = c.file_id WHERE c.embedding_json IS NOT NULL""" + chunk_permission_sql,
            chunk_permission_params,
        ) or {"files": 0}
        content = self.fetchone(
            """SELECT COUNT(DISTINCT c.file_id) AS files FROM content_chunks c
               JOIN files f ON f.id = c.file_id WHERE c.content != ''""" + chunk_permission_sql,
            chunk_permission_params,
        ) or {"files": 0}
        files["semantic_ready"] = int(semantic["files"])
        files["content_ready"] = int(content["files"])
        files["repairable"] = self.repair_count()
        files["retry_waiting"] = self.retry_waiting_count()
        files["terminal_failures"] = self.terminal_failure_count()
        kinds = self.fetchall(
            "SELECT kind, COUNT(*) AS count FROM files WHERE 1=1" + permission_sql + " GROUP BY kind ORDER BY count DESC",
            permission_params,
        )
        if user_id is None:
            active_tasks = self.fetchone("SELECT COUNT(*) AS count FROM tasks WHERE status IN ('pending', 'running')") or {"count": 0}
        else:
            active_tasks = self.fetchone(
                "SELECT COUNT(*) AS count FROM tasks WHERE status IN ('pending', 'running') AND user_id = ?",
                (user_id,),
            ) or {"count": 0}
        return {"files": files, "kinds": kinds, "active_tasks": active_tasks["count"]}

    def set_manual_caption(self, file_id: int, caption: str) -> None:
        self.execute(
            """UPDATE files SET manual_caption = ?, status = 'pending', error = '',
               vision_status = 'pending', embedding_status = 'pending',
               vision_error = '', embedding_error = '', retry_count = 0, last_attempt_at = NULL,
               next_retry_at = NULL, terminal_error = 0, last_error_fingerprint = '',
               updated_at = ? WHERE id = ?""",
            (caption.strip(), utc_now(), file_id),
        )

    def save_feedback(
        self,
        user_id: int | None,
        file_id: int,
        query: str,
        verdict: str,
        note: str = "",
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute(
                """SELECT id FROM file_feedback WHERE
                   ((user_id = ?) OR (user_id IS NULL AND ? IS NULL)) AND file_id = ? AND query = ?""",
                (user_id, user_id, file_id, query.strip()),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE file_feedback SET verdict = ?, note = ?, updated_at = ? WHERE id = ?",
                    (verdict, note[:1000], now, existing["id"]),
                )
            else:
                connection.execute(
                    """INSERT INTO file_feedback(user_id, file_id, query, verdict, note, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, file_id, query.strip(), verdict, note[:1000], now, now),
                )

    def feedback_counts(self) -> dict[str, int]:
        rows = self.fetchall("SELECT verdict, COUNT(*) AS count FROM file_feedback GROUP BY verdict")
        return {str(row["verdict"]): int(row["count"]) for row in rows}

    def set_favorite(self, user_id: int, file_id: int, enabled: bool) -> None:
        if enabled:
            self.execute(
                "INSERT OR IGNORE INTO favorites(user_id, file_id, created_at) VALUES (?, ?, ?)",
                (user_id, file_id, utc_now()),
            )
        else:
            self.execute("DELETE FROM favorites WHERE user_id = ? AND file_id = ?", (user_id, file_id))

    def is_favorite(self, user_id: int, file_id: int) -> bool:
        return bool(self.fetchone("SELECT 1 AS value FROM favorites WHERE user_id = ? AND file_id = ?", (user_id, file_id)))

    def list_tags(self, user_id: int) -> list[dict[str, Any]]:
        return self.fetchall(
            """SELECT t.id, t.name, COUNT(ft.file_id) AS file_count FROM tags t
               LEFT JOIN file_tags ft ON ft.tag_id = t.id WHERE t.user_id = ?
               GROUP BY t.id ORDER BY t.name COLLATE NOCASE""",
            (user_id,),
        )

    def file_tag_names(self, user_id: int, file_id: int) -> list[str]:
        rows = self.fetchall(
            """SELECT t.name FROM tags t JOIN file_tags ft ON ft.tag_id = t.id
               WHERE t.user_id = ? AND ft.file_id = ? ORDER BY t.name COLLATE NOCASE""",
            (user_id, file_id),
        )
        return [str(row["name"]) for row in rows]

    def set_file_tags(self, user_id: int, file_id: int, names: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(name.strip()[:50] for name in names if name.strip()))[:20]
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """DELETE FROM file_tags WHERE file_id = ? AND tag_id IN
                   (SELECT id FROM tags WHERE user_id = ?)""",
                (file_id, user_id),
            )
            for name in normalized:
                connection.execute(
                    "INSERT OR IGNORE INTO tags(user_id, name, created_at) VALUES (?, ?, ?)",
                    (user_id, name, now),
                )
                tag_id = connection.execute(
                    "SELECT id FROM tags WHERE user_id = ? AND name = ? COLLATE NOCASE",
                    (user_id, name),
                ).fetchone()["id"]
                connection.execute(
                    "INSERT OR IGNORE INTO file_tags(tag_id, file_id, created_at) VALUES (?, ?, ?)",
                    (tag_id, file_id, now),
                )
            connection.execute(
                "DELETE FROM tags WHERE user_id = ? AND id NOT IN (SELECT DISTINCT tag_id FROM file_tags)",
                (user_id,),
            )
        return normalized

    def create_smart_album(
        self,
        user_id: int,
        name: str,
        query: str,
        kind: str,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        album_id = self.execute(
            """INSERT INTO smart_albums(user_id, name, query, kind, filters_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name.strip(), query.strip(), kind, json.dumps(filters, ensure_ascii=False), now, now),
        )
        return self.get_smart_album(album_id, user_id) or {}

    def get_smart_album(self, album_id: int, user_id: int) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM smart_albums WHERE id = ? AND user_id = ?", (album_id, user_id))
        if row:
            row["filters"] = json.loads(row.pop("filters_json") or "{}")
        return row

    def list_smart_albums(self, user_id: int) -> list[dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM smart_albums WHERE user_id = ? ORDER BY updated_at DESC", (user_id,))
        for row in rows:
            row["filters"] = json.loads(row.pop("filters_json") or "{}")
        return rows

    def delete_smart_album(self, album_id: int, user_id: int) -> None:
        self.execute("DELETE FROM smart_albums WHERE id = ? AND user_id = ?", (album_id, user_id))

    def create_conversation(self, user_id: int, title: str) -> dict[str, Any]:
        now = utc_now()
        conversation_id = self.execute(
            "INSERT INTO conversations(user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, title.strip()[:100] or "新对话", now, now),
        )
        return self.fetchone("SELECT * FROM conversations WHERE id = ?", (conversation_id,)) or {}

    def list_conversations(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        return self.fetchall(
            """SELECT c.*, COUNT(m.id) AS message_count FROM conversations c
               LEFT JOIN conversation_messages m ON m.conversation_id = c.id
               WHERE c.user_id = ? GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?""",
            (user_id, max(1, int(limit))),
        )

    def conversation(self, conversation_id: int, user_id: int) -> dict[str, Any] | None:
        conversation = self.fetchone(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
        if not conversation:
            return None
        messages = self.fetchall(
            """SELECT id, role, content, sources_json, created_at FROM conversation_messages
               WHERE conversation_id = ? ORDER BY id""",
            (conversation_id,),
        )
        for message in messages:
            message["sources"] = json.loads(message.pop("sources_json") or "[]")
        conversation["messages"] = messages
        return conversation

    def add_conversation_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        sources: list[dict[str, Any]] | None = None,
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO conversation_messages(conversation_id, role, content, sources_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (conversation_id, role, content, json.dumps(sources or [], ensure_ascii=False), now),
            )
            connection.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))

    def delete_conversation(self, conversation_id: int, user_id: int) -> None:
        self.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id))

    def user_count(self) -> int:
        row = self.fetchone("SELECT COUNT(*) AS count FROM users")
        return int(row["count"] if row else 0)

    def bootstrap_required(self) -> bool:
        row = self.fetchone(
            """SELECT CASE WHEN COUNT(*) = 0 OR
               SUM(CASE WHEN password_setup_required = 1 THEN 1 ELSE 0 END) > 0
               THEN 1 ELSE 0 END AS required FROM users"""
        )
        return bool(row and row["required"])

    def create_user(
        self,
        username: str,
        display_name: str,
        password_hash: str,
        role: str,
        library_ids: list[int],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO users(username, display_name, password_hash, role, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (username, display_name, password_hash, role, now, now),
            )
            user_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO library_permissions(user_id, library_id, created_at) VALUES (?, ?, ?)",
                [(user_id, int(library_id), now) for library_id in sorted(set(library_ids))],
            )
        return self.get_user(user_id) or {}

    def complete_initial_setup(
        self,
        username: str,
        display_name: str,
        password_hash: str,
    ) -> dict[str, Any]:
        now = utc_now()
        try:
            with self.transaction() as connection:
                user_count = int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                if not user_count:
                    cursor = connection.execute(
                        """INSERT INTO users(
                               username, display_name, password_hash, role, password_setup_required,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, 'owner', 0, ?, ?)""",
                        (username, display_name, password_hash, now, now),
                    )
                    user_id = int(cursor.lastrowid)
                else:
                    pending = connection.execute(
                        """SELECT id FROM users WHERE password_setup_required = 1
                           AND role IN ('owner', 'admin') ORDER BY id LIMIT 1"""
                    ).fetchone()
                    if not pending:
                        raise ValueError("管理员账号已经初始化")
                    user_id = int(pending["id"])
                    connection.execute(
                        """UPDATE users SET username = ?, display_name = ?, password_hash = ?,
                           role = 'owner', enabled = 1, password_setup_required = 0, updated_at = ?
                           WHERE id = ?""",
                        (username, display_name, password_hash, now, user_id),
                    )
                    connection.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint" in str(exc):
                raise ValueError("用户名已存在") from exc
            raise
        return self.get_user(user_id) or {}

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        row = self.fetchone(
            "SELECT id, username, display_name, role, enabled, created_at, updated_at, last_login_at FROM users WHERE id = ?",
            (user_id,),
        )
        if row:
            row["library_ids"] = [
                int(item["library_id"])
                for item in self.fetchall("SELECT library_id FROM library_permissions WHERE user_id = ? ORDER BY library_id", (user_id,))
            ]
        return row

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.fetchall(
            "SELECT id, username, display_name, role, enabled, created_at, updated_at, last_login_at FROM users ORDER BY username"
        )
        permissions = self.fetchall("SELECT user_id, library_id FROM library_permissions ORDER BY library_id")
        by_user: dict[int, list[int]] = {}
        for item in permissions:
            by_user.setdefault(int(item["user_id"]), []).append(int(item["library_id"]))
        for row in rows:
            row["library_ids"] = by_user.get(int(row["id"]), [])
        return rows

    def set_user(
        self,
        user_id: int,
        display_name: str,
        role: str,
        enabled: bool,
        library_ids: list[int],
        password_hash: str = "",
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            if password_hash:
                connection.execute(
                    "UPDATE users SET display_name = ?, role = ?, enabled = ?, password_hash = ?, updated_at = ? WHERE id = ?",
                    (display_name, role, int(enabled), password_hash, now, user_id),
                )
            else:
                connection.execute(
                    "UPDATE users SET display_name = ?, role = ?, enabled = ?, updated_at = ? WHERE id = ?",
                    (display_name, role, int(enabled), now, user_id),
                )
            connection.execute("DELETE FROM library_permissions WHERE user_id = ?", (user_id,))
            connection.executemany(
                "INSERT INTO library_permissions(user_id, library_id, created_at) VALUES (?, ?, ?)",
                [(user_id, int(library_id), now) for library_id in sorted(set(library_ids))],
            )
            if not enabled:
                connection.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))

    def user_credentials(self, username: str) -> dict[str, Any] | None:
        return self.fetchone("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))

    def create_session(self, user_id: int, token_hash: str, expires_at: str) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO user_sessions(user_id, token_hash, expires_at, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, token_hash, expires_at, now, now),
            )
            connection.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))

    def resolve_session(self, token_hash: str) -> dict[str, Any] | None:
        now = utc_now()
        row = self.fetchone(
            """SELECT u.id, u.username, u.display_name, u.role, s.id AS session_id
               FROM user_sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND s.expires_at > ? AND u.enabled = 1
               AND u.password_setup_required = 0""",
            (token_hash, now),
        )
        if row:
            self.execute("UPDATE user_sessions SET last_seen_at = ? WHERE id = ?", (now, row["session_id"]))
            row["library_ids"] = [
                int(item["library_id"])
                for item in self.fetchall("SELECT library_id FROM library_permissions WHERE user_id = ?", (row["id"],))
            ]
        return row

    def delete_session(self, token_hash: str) -> None:
        self.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,))

    def audit(
        self,
        action: str,
        actor: str,
        user_id: int | None = None,
        target_type: str = "",
        target_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            """INSERT INTO audit_log(user_id, actor, action, target_type, target_id, detail_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, actor, action, target_type, target_id, json.dumps(detail or {}, ensure_ascii=False), utc_now()),
        )

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json") or "{}")
        return rows

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(destination.parent, 0o700)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.unlink(missing_ok=True)
        try:
            with self.connect() as source:
                target = sqlite3.connect(temporary)
                try:
                    source.backup(target)
                finally:
                    target.close()
            os.chmod(temporary, 0o600)
            self.verify_backup(temporary)
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            marker = destination.with_suffix(destination.suffix + ".verified")
            marker.write_text(
                json.dumps(
                    {
                        "quick_check": "ok",
                        "bytes": destination.stat().st_size,
                        "verified_at": utc_now(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.chmod(marker, 0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def verify_backup(path: Path) -> str:
        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
            result = str(row[0] if row else "unknown")
            if result != "ok":
                raise RuntimeError(f"SQLite backup quick_check failed: {result}")
            if connection.execute("SELECT 1 FROM pragma_foreign_key_check LIMIT 1").fetchone():
                raise RuntimeError("SQLite backup contains foreign key errors")
            return result
        finally:
            connection.close()

    def quick_check(self) -> str:
        with self.connect() as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
            return str(row[0] if row else "unknown")

    def probe(self) -> str:
        try:
            with self.connect() as connection:
                row = connection.execute("SELECT 1").fetchone()
            return "ok" if row and row[0] == 1 else "error"
        except sqlite3.Error as exc:
            return f"{type(exc).__name__}: {exc}"


db: Database | None = None


def configure_database(path: Path) -> Database:
    global db
    db = Database(path)
    db.initialize()
    return db
