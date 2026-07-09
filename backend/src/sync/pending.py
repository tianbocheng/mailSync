from __future__ import annotations

import imaplib
import json
import sqlite3
from datetime import datetime
from typing import Optional, Sequence

from common.clock import now_iso
from common.errors import SyncError
from domain.message import SourceMessage, message_subject
from imap.protocol import response_int, search_uids, select_mailbox
from sync.labels import normalize_folder_labels
from sync.target_identity import target_identity_values


def _selected_mailbox_uidnext(target: imaplib.IMAP4) -> int:
    try:
        return response_int(target, "UIDNEXT")
    except SyncError:
        return max(search_uids(target), default=0) + 1


def begin_pending_operation(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    account_label: str,
    folder_labels: Sequence[str],
    old_target_uid: Optional[int],
    location_folder_label: Optional[str] = None,
    target_email: str = "",
    target_service_address: str = "",
    sync_source: str | None = None,
) -> None:
    normalized_labels = normalize_folder_labels(folder_labels)
    primary_folder_label = (
        location_folder_label
        or (normalized_labels[0] if normalized_labels else "")
    )
    if primary_folder_label and primary_folder_label not in normalized_labels:
        raise SyncError(
            f"Location label {primary_folder_label} is not in desired labels"
        )
    _, target_uidvalidity = select_mailbox(target, target_folder, readonly=False)
    target_uid_before = max(0, _selected_mailbox_uidnext(target) - 1)
    timestamp = now_iso()
    db.execute(
        """
        INSERT INTO pending_operations (
            source_email, content_hash, message_id, subject, source_folder,
            source_uidvalidity, source_uid, source_flags, internal_date,
            raw_message, account_label, folder_label, folder_labels_json,
            old_target_uid, target_folder, target_email, target_service_address,
            last_sync_source, target_uidvalidity, target_uid_before, new_target_uid,
            status, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                  'pending', NULL, ?, ?)
        ON CONFLICT(source_email, content_hash) DO UPDATE SET
            message_id=excluded.message_id,
            subject=excluded.subject,
            source_folder=excluded.source_folder,
            source_uidvalidity=excluded.source_uidvalidity,
            source_uid=excluded.source_uid,
            source_flags=excluded.source_flags,
            internal_date=excluded.internal_date,
            raw_message=excluded.raw_message,
            account_label=excluded.account_label,
            folder_label=excluded.folder_label,
            folder_labels_json=excluded.folder_labels_json,
            old_target_uid=excluded.old_target_uid,
            target_folder=excluded.target_folder,
            target_email=excluded.target_email,
            target_service_address=excluded.target_service_address,
            last_sync_source=excluded.last_sync_source,
            target_uidvalidity=excluded.target_uidvalidity,
            target_uid_before=excluded.target_uid_before,
            new_target_uid=NULL,
            status='pending',
            last_error=NULL,
            updated_at=excluded.updated_at
        """,
        (
            message.source_email,
            message.content_hash,
            message.message_id,
            message_subject(message.raw),
            message.folder,
            message.uidvalidity,
            message.uid,
            json.dumps(message.system_flags),
            message.internal_date.isoformat() if message.internal_date else None,
            message.raw,
            account_label,
            primary_folder_label,
            json.dumps(normalized_labels, ensure_ascii=False),
            old_target_uid,
            target_folder,
            *target_identity_values(target_email, target_service_address),
            sync_source,
            target_uidvalidity,
            target_uid_before,
            timestamp,
            timestamp,
        ),
    )
    db.commit()


def update_pending_target_uid(
    db: sqlite3.Connection,
    message: SourceMessage,
    target_uid: int,
) -> None:
    db.execute(
        """
        UPDATE pending_operations
        SET new_target_uid=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            target_uid,
            now_iso(),
            message.source_email,
            message.content_hash,
        ),
    )
    db.commit()


def quarantine_pending_operation(
    db: sqlite3.Connection,
    message: SourceMessage,
    target_uid: int,
    error: str,
) -> None:
    db.execute(
        """
        UPDATE pending_operations
        SET new_target_uid=?, status='quarantined', last_error=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            target_uid,
            error,
            now_iso(),
            message.source_email,
            message.content_hash,
        ),
    )
    db.commit()


def pending_row_message(row: sqlite3.Row) -> SourceMessage:
    raw = bytes(row["raw_message"])
    return SourceMessage(
        source_email=row["source_email"],
        folder=row["source_folder"],
        uidvalidity=int(row["source_uidvalidity"]),
        uid=int(row["source_uid"]),
        raw=raw,
        content_hash=row["content_hash"],
        message_id=row["message_id"],
        internal_date=(
            datetime.fromisoformat(row["internal_date"])
            if row["internal_date"]
            else None
        ),
        system_flags=tuple(json.loads(row["source_flags"])),
    )
