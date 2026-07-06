from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from common.errors import SyncError


DB_SCHEMA_VERSION = 1


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    current_schema_version = int(
        connection.execute("PRAGMA user_version").fetchone()[0]
    )
    if current_schema_version > DB_SCHEMA_VERSION:
        connection.close()
        raise SyncError(
            f"State database schema version {current_schema_version} is newer "
            f"than this synchronizer supports ({DB_SCHEMA_VERSION})"
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            source_email TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            normalized_hash TEXT,
            target_normalized_hash TEXT,
            message_id TEXT,
            source_folder TEXT NOT NULL,
            source_uidvalidity INTEGER NOT NULL,
            source_uid INTEGER NOT NULL,
            source_flags TEXT NOT NULL,
            internal_date TEXT,
            source_date_verified INTEGER NOT NULL DEFAULT 0,
            target_date_header_verified INTEGER NOT NULL DEFAULT 0,
            target_uidvalidity INTEGER NOT NULL,
            target_uid INTEGER NOT NULL,
            account_label TEXT NOT NULL,
            folder_label TEXT NOT NULL,
            folder_labels_json TEXT,
            last_seen_run TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_email, content_hash)
        );
        DROP INDEX IF EXISTS messages_source_location;
        CREATE INDEX IF NOT EXISTS messages_source_location
        ON messages(source_email, source_folder, source_uidvalidity, source_uid);
        CREATE TABLE IF NOT EXISTS pending_operations (
            source_email TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            message_id TEXT,
            source_folder TEXT NOT NULL,
            source_uidvalidity INTEGER NOT NULL,
            source_uid INTEGER NOT NULL,
            source_flags TEXT NOT NULL,
            internal_date TEXT,
            raw_message BLOB NOT NULL,
            account_label TEXT NOT NULL,
            folder_label TEXT NOT NULL,
            folder_labels_json TEXT,
            old_target_uid INTEGER,
            target_uidvalidity INTEGER NOT NULL,
            target_uid_before INTEGER NOT NULL,
            new_target_uid INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_email, content_hash)
        );
        CREATE TABLE IF NOT EXISTS message_locations (
            source_email TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_folder TEXT NOT NULL,
            source_uidvalidity INTEGER NOT NULL,
            source_uid INTEGER NOT NULL,
            folder_label TEXT NOT NULL,
            last_seen_run TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (
                source_email, source_folder, source_uidvalidity, source_uid
            ),
            FOREIGN KEY (source_email, content_hash)
            REFERENCES messages(source_email, content_hash)
            ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS message_locations_content
        ON message_locations(source_email, content_hash);
        """
    )
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "message_id" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
        connection.commit()
    if "normalized_hash" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN normalized_hash TEXT")
    if "target_normalized_hash" not in columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN target_normalized_hash TEXT"
        )
    if "folder_labels_json" not in columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN folder_labels_json TEXT"
        )
    if "source_date_verified" not in columns:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN "
            "source_date_verified INTEGER NOT NULL DEFAULT 0"
        )
    if "target_date_header_verified" not in columns:
        # Existing non-QQ copies need no header migration. Existing QQ and
        # Foxmail copies are audited once by repair-dates.
        connection.execute(
            "ALTER TABLE messages ADD COLUMN "
            "target_date_header_verified INTEGER NOT NULL DEFAULT 1"
        )
        connection.execute(
            """
            UPDATE messages SET target_date_header_verified=0
            WHERE lower(source_email) LIKE '%@qq.com'
               OR lower(source_email) LIKE '%@foxmail.com'
            """
        )
    pending_columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(pending_operations)"
        ).fetchall()
    }
    if "status" not in pending_columns:
        connection.execute(
            "ALTER TABLE pending_operations "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
        )
    if "last_error" not in pending_columns:
        connection.execute(
            "ALTER TABLE pending_operations ADD COLUMN last_error TEXT"
        )
    if "folder_labels_json" not in pending_columns:
        connection.execute(
            "ALTER TABLE pending_operations ADD COLUMN folder_labels_json TEXT"
        )
    for row in connection.execute(
        """
        SELECT source_email, content_hash, folder_label
        FROM messages
        WHERE folder_labels_json IS NULL
        """
    ).fetchall():
        connection.execute(
            """
            UPDATE messages SET folder_labels_json=?
            WHERE source_email=? AND content_hash=?
            """,
            (
                json.dumps([row["folder_label"]], ensure_ascii=False),
                row["source_email"],
                row["content_hash"],
            ),
        )
    for row in connection.execute(
        """
        SELECT source_email, content_hash, folder_label
        FROM pending_operations
        WHERE folder_labels_json IS NULL
        """
    ).fetchall():
        connection.execute(
            """
            UPDATE pending_operations SET folder_labels_json=?
            WHERE source_email=? AND content_hash=?
            """,
            (
                json.dumps([row["folder_label"]], ensure_ascii=False),
                row["source_email"],
                row["content_hash"],
            ),
        )
    connection.execute(
        """
        INSERT OR IGNORE INTO message_locations (
            source_email, content_hash, source_folder, source_uidvalidity,
            source_uid, folder_label, last_seen_run, updated_at
        )
        SELECT source_email, content_hash, source_folder, source_uidvalidity,
               source_uid, folder_label, last_seen_run, updated_at
        FROM messages
        WHERE source_folder <> '__deleted__'
        """
    )
    connection.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION}")
    connection.commit()
    return connection


@contextmanager
def process_lock(state_path: Path):
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError(
                f"Another synchronizer is already running ({lock_path})"
            ) from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
