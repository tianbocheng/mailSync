from __future__ import annotations

import imaplib
import sqlite3
from datetime import datetime
from typing import Dict, Optional

from common.errors import SyncError
from domain.message import SourceMessage
from imap.protocol import select_mailbox
from imap.target import (
    TargetContentVerificationError,
    append_target_copy,
    delete_target_copy,
    ensure_target_delete_will_be_safe,
    find_untracked_target_by_message_id,
)
from sync.labels import normalize_folder_labels, row_folder_labels
from sync.pending import (
    begin_pending_operation,
    quarantine_pending_operation,
    update_pending_target_uid,
)
from sync.records import upsert_message_location, upsert_state


def purge_single_location_message(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    existing: sqlite3.Row,
) -> bool:
    """Remove an already-synced message when its sole source location is skipped."""
    location_count = db.execute(
        """
        SELECT COUNT(*) FROM message_locations
        WHERE source_email=? AND content_hash=?
        """,
        (existing["source_email"], existing["content_hash"]),
    ).fetchone()[0]
    if location_count != 1:
        return False
    delete_target_copy(target, target_folder, int(existing["target_uid"]))
    db.execute(
        """
        DELETE FROM message_locations
        WHERE source_email=? AND content_hash=?
        """,
        (existing["source_email"], existing["content_hash"]),
    )
    db.execute(
        """
        DELETE FROM messages
        WHERE source_email=? AND content_hash=?
        """,
        (existing["source_email"], existing["content_hash"]),
    )
    db.commit()
    return True


def sync_message(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    account_label_name: str,
    folder_label_name: str,
    labels_by_name: Dict[str, str],
    run_id: str,
    force_rebuild: bool = False,
    commit_state: bool = True,
) -> str:
    try:
        account_label_id = labels_by_name[account_label_name]
        folder_label_id = labels_by_name[folder_label_name]
    except KeyError as exc:
        raise SyncError(f"Undefined Synology label: {exc.args[0]}") from exc

    quarantined = db.execute(
        """
        SELECT 1 FROM pending_operations
        WHERE source_email=? AND content_hash=? AND status='quarantined'
        """,
        (message.source_email, message.content_hash),
    ).fetchone()
    if quarantined is not None:
        return "quarantined"

    existing = db.execute(
        "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
        (message.source_email, message.content_hash),
    ).fetchone()
    old_target_uid: Optional[int] = None
    desired_folder_labels = (folder_label_name,)
    if existing is None:
        prior_target_uid = find_untracked_target_by_message_id(
            db,
            target,
            target_folder,
            message.message_id,
        )
        if prior_target_uid is None:
            action = "created"
        else:
            old_target_uid = prior_target_uid
            action = "existing_rebuilt"
    else:
        existing_folder_labels = row_folder_labels(existing)
        stored_internal_date = (
            datetime.fromisoformat(existing["internal_date"])
            if existing["internal_date"]
            else None
        )
        if (
            existing["source_folder"] == "__deleted__"
            or existing["account_label"] != account_label_name
        ):
            old_target_uid = int(existing["target_uid"])
            action = "rebuilt"
        elif force_rebuild or stored_internal_date != message.internal_date:
            # Some IMAP servers omit INTERNALDATE. Older versions then APPENDed
            # with no date, causing the target to assign the current year.
            old_target_uid = int(existing["target_uid"])
            desired_folder_labels = existing_folder_labels
            action = "date_rebuilt"
        elif folder_label_name not in existing_folder_labels:
            target_uid = int(existing["target_uid"])
            select_mailbox(target, target_folder, readonly=False)
            typ, response = target.uid(
                "store",
                str(target_uid),
                "+FLAGS.SILENT",
                f"({folder_label_id})",
            )
            if typ != "OK":
                raise SyncError(
                    f"Failed adding {folder_label_id} to target UID "
                    f"{target_uid}: {response!r}"
                )
            desired_folder_labels = normalize_folder_labels(
                (*existing_folder_labels, folder_label_name)
            )
            upsert_state(
                db,
                message,
                int(existing["target_uidvalidity"]),
                target_uid,
                account_label_name,
                desired_folder_labels,
                run_id,
            )
            upsert_message_location(
                db,
                message,
                folder_label_name,
                run_id,
            )
            if commit_state:
                db.commit()
            return "folder_label_added"
        else:
            upsert_state(
                db,
                message,
                int(existing["target_uidvalidity"]),
                int(existing["target_uid"]),
                account_label_name,
                existing_folder_labels,
                run_id,
            )
            upsert_message_location(
                db,
                message,
                folder_label_name,
                run_id,
            )
            if commit_state:
                db.commit()
            return "source_location_added"

    label_ids = [
        account_label_id,
        *(labels_by_name[name] for name in desired_folder_labels),
    ]
    if old_target_uid is not None:
        select_mailbox(target, target_folder, readonly=False)
        ensure_target_delete_will_be_safe(target, old_target_uid)
    begin_pending_operation(
        db,
        target,
        target_folder,
        message,
        account_label_name,
        desired_folder_labels,
        old_target_uid,
        folder_label_name,
    )
    try:
        target_uidvalidity, target_uid = append_target_copy(
            target,
            target_folder,
            message,
            label_ids,
        )
    except TargetContentVerificationError as exc:
        quarantine_pending_operation(
            db,
            message,
            exc.uid,
            str(exc),
        )
        raise
    update_pending_target_uid(db, message, target_uid)
    if old_target_uid is not None:
        delete_target_copy(target, target_folder, old_target_uid)
    upsert_state(
        db,
        message,
        target_uidvalidity,
        target_uid,
        account_label_name,
        desired_folder_labels,
        run_id,
    )
    upsert_message_location(
        db,
        message,
        folder_label_name,
        run_id,
    )
    db.execute(
        """
        DELETE FROM pending_operations
        WHERE source_email=? AND content_hash=?
        """,
        (message.source_email, message.content_hash),
    )
    db.commit()
    return action

