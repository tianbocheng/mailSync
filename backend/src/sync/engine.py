from __future__ import annotations

import imaplib
import sqlite3
from datetime import datetime
from typing import Dict, Optional, Sequence

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
from sync.labels import (
    configured_label_ids,
    configured_label_names,
    normalize_folder_labels,
    row_folder_labels,
)
from sync.pending import (
    begin_pending_operation,
    quarantine_pending_operation,
    update_pending_target_uid,
)
from sync.records import upsert_message_location, upsert_state
from sync.target_identity import row_matches_target_identity, target_identity_filter


def purge_single_location_message(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    existing: sqlite3.Row,
    target_email: str = "",
    target_service_address: str = "",
) -> bool:
    """Remove an already-synced message when its sole source location is skipped."""
    if not row_matches_target_identity(
        existing,
        target_email,
        target_service_address,
    ):
        return False
    stored_target_folder = _stored_target_folder(existing) or target_folder
    location_count = db.execute(
        """
        SELECT COUNT(*) FROM message_locations
        WHERE source_email=? AND content_hash=?
        """,
        (existing["source_email"], existing["content_hash"]),
    ).fetchone()[0]
    if location_count != 1:
        return False
    delete_target_copy(target, stored_target_folder, int(existing["target_uid"]))
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
    folder_label_name: str | Sequence[str],
    labels_by_name: Dict[str, str],
    run_id: str,
    force_rebuild: bool = False,
    commit_state: bool = True,
    target_email: str = "",
    target_service_address: str = "",
    sync_source: str | None = None,
) -> str:
    account_label_name = str(account_label_name or "").strip()
    if account_label_name not in labels_by_name:
        account_label_name = ""
    configured_folder_labels = configured_label_names(
        labels_by_name,
        _label_names(folder_label_name),
    )
    primary_folder_label = configured_folder_labels[0] if configured_folder_labels else ""

    target_filter, target_params = target_identity_filter(
        "pending_operations",
        target_email,
        target_service_address,
    )
    quarantined = db.execute(
        f"""
        SELECT 1 FROM pending_operations
        WHERE source_email=? AND content_hash=? AND status='quarantined'
          {target_filter}
        """,
        (
            message.source_email,
            message.content_hash,
            *target_params,
        ),
    ).fetchone()
    if quarantined is not None:
        return "quarantined"

    stored_existing = db.execute(
        "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
        (message.source_email, message.content_hash),
    ).fetchone()
    existing = (
        stored_existing
        if (
            _has_target_copy(stored_existing)
            and row_matches_target_identity(
                stored_existing,
                target_email,
                target_service_address,
            )
        )
        else None
    )
    existing_target_folder = (
        _stored_target_folder(existing) if existing is not None else None
    )
    copy_target_folder = existing_target_folder or target_folder
    old_target_uid: Optional[int] = None
    desired_folder_labels = configured_folder_labels
    if existing is None:
        prior_target_uid = find_untracked_target_by_message_id(
            db,
            target,
            target_folder,
            message.message_id,
            target_email,
            target_service_address,
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
        if existing["source_folder"] == "__deleted__":
            old_target_uid = int(existing["target_uid"])
            action = "rebuilt"
        elif force_rebuild or stored_internal_date != message.internal_date:
            # Some IMAP servers omit INTERNALDATE. Older versions then APPENDed
            # with no date, causing the target to assign the current year.
            old_target_uid = int(existing["target_uid"])
            desired_folder_labels = configured_label_names(
                labels_by_name,
                existing_folder_labels,
            )
            action = "date_rebuilt"
        else:
            missing_account_label = (
                bool(account_label_name)
                and existing["account_label"] != account_label_name
            )
            missing_folder_labels = [
                name
                for name in configured_folder_labels
                if name not in existing_folder_labels
            ]
            if missing_account_label or missing_folder_labels:
                target_uid = int(existing["target_uid"])
                missing_label_ids = configured_label_ids(
                    labels_by_name,
                    (
                        *((account_label_name,) if missing_account_label else ()),
                        *missing_folder_labels,
                    ),
                )
                select_mailbox(target, copy_target_folder, readonly=False)
                if missing_label_ids:
                    typ, response = target.uid(
                        "store",
                        str(target_uid),
                        "+FLAGS.SILENT",
                        f"({' '.join(missing_label_ids)})",
                    )
                    if typ != "OK":
                        raise SyncError(
                            f"Failed adding labels to target UID "
                            f"{target_uid}: {response!r}"
                        )
                desired_folder_labels = normalize_folder_labels(
                    (*existing_folder_labels, *configured_folder_labels)
                )
                upsert_state(
                    db,
                    message,
                    int(existing["target_uidvalidity"]),
                    target_uid,
                    account_label_name,
                    desired_folder_labels,
                    run_id,
                    sync_action="folder_label_added",
                    sync_source=sync_source,
                    target_folder=copy_target_folder,
                    target_email=target_email,
                    target_service_address=target_service_address,
                )
                upsert_message_location(
                    db,
                    message,
                    primary_folder_label,
                    run_id,
                )
                if commit_state:
                    db.commit()
                return "folder_label_added"
            upsert_state(
                db,
                message,
                int(existing["target_uidvalidity"]),
                int(existing["target_uid"]),
                account_label_name,
                existing_folder_labels,
                run_id,
                sync_action="source_location_added",
                sync_source=sync_source,
                target_folder=copy_target_folder,
                target_email=target_email,
                target_service_address=target_service_address,
            )
            upsert_message_location(
                db,
                message,
                primary_folder_label,
                run_id,
            )
            if commit_state:
                db.commit()
            return "source_location_added"

    label_ids = configured_label_ids(
        labels_by_name,
        (account_label_name, *desired_folder_labels),
    )
    if old_target_uid is not None:
        select_mailbox(target, copy_target_folder, readonly=False)
        ensure_target_delete_will_be_safe(target, old_target_uid)
    begin_pending_operation(
        db,
        target,
        copy_target_folder,
        message,
        account_label_name,
        desired_folder_labels,
        old_target_uid,
        primary_folder_label,
        target_email,
        target_service_address,
        sync_source,
    )
    try:
        target_uidvalidity, target_uid = append_target_copy(
            target,
            copy_target_folder,
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
        delete_target_copy(target, copy_target_folder, old_target_uid)
    upsert_state(
        db,
        message,
        target_uidvalidity,
        target_uid,
        account_label_name,
        desired_folder_labels,
        run_id,
        sync_action=action,
        sync_source=sync_source,
        target_folder=copy_target_folder,
        target_email=target_email,
        target_service_address=target_service_address,
    )
    upsert_message_location(
        db,
        message,
        primary_folder_label,
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


def _label_names(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.splitlines() if item.strip())
    return tuple(str(item).strip() for item in value if str(item).strip())


def _stored_target_folder(row: sqlite3.Row | None) -> str:
    if row is None or "target_folder" not in row.keys():
        return ""
    return str(row["target_folder"] or "").strip()


def _has_target_copy(row: sqlite3.Row | None) -> bool:
    if row is None:
        return False
    try:
        return int(row["target_uid"] or 0) > 0
    except (TypeError, ValueError):
        return False
