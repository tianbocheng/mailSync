from __future__ import annotations

import json
import sqlite3
from typing import Optional, Sequence

from common.clock import now_iso
from common.errors import SyncError
from domain.message import (
    SourceMessage,
    content_hash_matches,
    source_normalized_hash as cached_source_normalized_hash,
    target_normalized_hash,
)
from sync.labels import normalize_folder_labels


def upsert_state(
    db: sqlite3.Connection,
    message: SourceMessage,
    target_uidvalidity: int,
    target_uid: int,
    account_label: str,
    folder_labels: Sequence[str],
    run_id: str,
) -> None:
    normalized_labels = normalize_folder_labels(folder_labels)
    if not normalized_labels:
        raise SyncError("At least one folder label is required")
    primary_folder_label = normalized_labels[0]
    existing = db.execute(
        """
        SELECT normalized_hash FROM messages
        WHERE source_email=? AND content_hash=?
        """,
        (message.source_email, message.content_hash),
    ).fetchone()
    if content_hash_matches(message.raw, message.content_hash):
        source_normalized = cached_source_normalized_hash(message)
    elif existing is not None and existing["normalized_hash"]:
        # Reconciliation may reconstruct SourceMessage from an already
        # normalized target copy rather than the original source bytes.
        source_normalized = existing["normalized_hash"]
    else:
        source_normalized = None
    db.execute(
        """
        INSERT INTO messages (
            source_email, content_hash, normalized_hash, target_normalized_hash,
            message_id, source_folder,
            source_uidvalidity, source_uid, source_flags, internal_date,
            source_date_verified, target_date_header_verified,
            target_uidvalidity, target_uid, account_label, folder_label,
            folder_labels_json, last_seen_run, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_email, content_hash) DO UPDATE SET
            normalized_hash=excluded.normalized_hash,
            target_normalized_hash=excluded.target_normalized_hash,
            message_id=excluded.message_id,
            source_folder=excluded.source_folder,
            source_uidvalidity=excluded.source_uidvalidity,
            source_uid=excluded.source_uid,
            source_flags=excluded.source_flags,
            internal_date=excluded.internal_date,
            source_date_verified=1,
            target_date_header_verified=CASE
                WHEN excluded.target_uid<>messages.target_uid THEN 1
                ELSE messages.target_date_header_verified
            END,
            target_uidvalidity=excluded.target_uidvalidity,
            target_uid=excluded.target_uid,
            account_label=excluded.account_label,
            folder_label=excluded.folder_label,
            folder_labels_json=excluded.folder_labels_json,
            last_seen_run=excluded.last_seen_run,
            updated_at=excluded.updated_at
        """,
        (
            message.source_email,
            message.content_hash,
            source_normalized,
            target_normalized_hash(message),
            message.message_id,
            message.folder,
            message.uidvalidity,
            message.uid,
            json.dumps(message.system_flags),
            message.internal_date.isoformat() if message.internal_date else None,
            target_uidvalidity,
            target_uid,
            account_label,
            primary_folder_label,
            json.dumps(normalized_labels, ensure_ascii=False),
            run_id,
            now_iso(),
        ),
    )


def upsert_message_location(
    db: sqlite3.Connection,
    message: SourceMessage,
    folder_label: str,
    run_id: str,
) -> None:
    db.execute(
        """
        INSERT INTO message_locations (
            source_email, content_hash, source_folder, source_uidvalidity,
            source_uid, folder_label, last_seen_run, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (
            source_email, source_folder, source_uidvalidity, source_uid
        ) DO UPDATE SET
            content_hash=excluded.content_hash,
            folder_label=excluded.folder_label,
            last_seen_run=excluded.last_seen_run,
            updated_at=excluded.updated_at
        """,
        (
            message.source_email,
            message.content_hash,
            message.folder,
            message.uidvalidity,
            message.uid,
            folder_label,
            run_id,
            now_iso(),
        ),
    )
    db.execute(
        """
        UPDATE messages SET last_seen_run=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            run_id,
            now_iso(),
            message.source_email,
            message.content_hash,
        ),
    )


def find_by_location(
    db: sqlite3.Connection,
    source_email: str,
    folder: str,
    uidvalidity: int,
    uid: int,
) -> Optional[sqlite3.Row]:
    return db.execute(
        """
        SELECT m.*, l.folder_label AS location_folder_label
        FROM message_locations l
        JOIN messages m
          ON m.source_email=l.source_email
         AND m.content_hash=l.content_hash
        WHERE l.source_email=? AND l.source_folder=?
          AND l.source_uidvalidity=? AND l.source_uid=?
        """,
        (source_email, folder, uidvalidity, uid),
    ).fetchone()
