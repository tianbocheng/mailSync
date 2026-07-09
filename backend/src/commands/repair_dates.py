from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common.clock import now_iso
from common.errors import SyncError
from config.runtime import labels_for_rule, target_context_key, target_for_rule
from config.sources import selected_source_rules
from connection import connect, safe_logout
from domain.message import is_qq_source, needs_qq_date_header
from imap.protocol import select_mailbox
from imap.source import (
    fetch_date_header_presence,
    fetch_message_metadata,
    fetch_source_message,
    list_folders,
    selectable_source_folders,
)
from storage.state import init_db
from sync.engine import sync_message
from sync.recovery import recover_pending_operations
from sync.rules import (
    is_deleted_draft_message,
    source_folder_labels,
)
from sync.target_identity import target_identity_filter, target_identity_from_profile


def command_repair_dates(
    config: dict,
    profiles: Dict[str, dict],
    labels_by_name: Dict[str, str],
    state_path: Path,
    source_email: Optional[str],
) -> int:
    """Verify stored dates against source INTERNALDATE and rebuild mismatches."""
    db = init_db(state_path)
    run_id = uuid.uuid4().hex
    totals = {
        "verified": 0,
        "date_rebuilt": 0,
        "qq_header_rebuilt": 0,
        "unresolved": 0,
    }
    target_clients: Dict[str, object] = {}
    try:
        for rule in selected_source_rules(config, source_email):
            email = rule["email"]
            target_rule = target_for_rule(config, rule)
            target_email, target_service_address = target_identity_from_profile(
                target_rule
            )
            rule_labels_by_name = labels_for_rule(config, rule)
            target_key = target_context_key(target_rule)
            target = target_clients.get(target_key)
            if target is None:
                target = connect(target_rule)
                target_clients[target_key] = target
            recover_pending_operations(
                db,
                target,
                target_rule["folder"],
                rule_labels_by_name,
                source_emails=(email,),
                target_email=target_email,
                target_service_address=target_service_address,
            )
            source = connect(profiles[email])
            account_verified = 0
            account_rebuilt = 0
            try:
                folders = {
                    folder.raw_name: folder
                    for folder in selectable_source_folders(
                        list_folders(source),
                        rule,
                    )
                }
                target_filter, target_params = target_identity_filter(
                    "messages",
                    target_email,
                    target_service_address,
                )
                rows = db.execute(
                    f"""
                    SELECT * FROM messages
                    WHERE source_email=?
                      {target_filter}
                      AND (
                          source_date_verified=0
                          OR target_date_header_verified=0
                      )
                      AND source_folder<>'__deleted__'
                    ORDER BY source_folder, source_uidvalidity, source_uid
                    """,
                    (
                        email,
                        *target_params,
                    ),
                ).fetchall()
                select_mailbox(
                    target,
                    target_rule["folder"],
                    readonly=True,
                )
                target_metadata = fetch_message_metadata(
                    target,
                    [int(row["target_uid"]) for row in rows],
                )
                target_date_headers = fetch_date_header_presence(
                    target,
                    [int(row["target_uid"]) for row in rows],
                )
                rows_by_folder: Dict[Tuple[str, int], List[sqlite3.Row]] = {}
                for row in rows:
                    key = (
                        row["source_folder"],
                        int(row["source_uidvalidity"]),
                    )
                    rows_by_folder.setdefault(key, []).append(row)

                for (folder_name, expected_uidvalidity), folder_rows in (
                    rows_by_folder.items()
                ):
                    folder = folders.get(folder_name)
                    if folder is None:
                        totals["unresolved"] += len(folder_rows)
                        continue
                    _, uidvalidity = select_mailbox(
                        source,
                        folder_name,
                        readonly=True,
                    )
                    if uidvalidity != expected_uidvalidity:
                        raise SyncError(
                            f"Source UIDVALIDITY changed during date repair: "
                            f"{email} {folder_name}"
                        )
                    metadata = fetch_message_metadata(
                        source,
                        [int(row["source_uid"]) for row in folder_rows],
                    )
                    for row in folder_rows:
                        uid = int(row["source_uid"])
                        current = metadata.get(uid)
                        if current is None:
                            totals["unresolved"] += 1
                            continue
                        source_date, _ = current
                        target_date = target_metadata.get(
                            int(row["target_uid"]),
                            (None, ()),
                        )[0]
                        stored_date = (
                            datetime.fromisoformat(row["internal_date"])
                            if row["internal_date"]
                            else None
                        )
                        target_header_audit_required = (
                            not row["target_date_header_verified"]
                            and is_qq_source(email)
                        )
                        target_has_date_header = target_date_headers.get(
                            int(row["target_uid"]),
                            False,
                        )
                        if (
                            source_date is not None
                            and source_date == stored_date
                            and target_date == stored_date
                            and (
                                not target_header_audit_required
                                or target_has_date_header
                            )
                        ):
                            db.execute(
                                """
                                UPDATE messages
                                SET source_date_verified=1,
                                    target_date_header_verified=1,
                                    updated_at=?
                                WHERE source_email=? AND content_hash=?
                                """,
                                (
                                    now_iso(),
                                    email,
                                    row["content_hash"],
                                ),
                            )
                            account_verified += 1
                            totals["verified"] += 1
                            continue

                        message = fetch_source_message(
                            source,
                            email,
                            folder_name,
                            uidvalidity,
                            uid,
                            profiles[email],
                        )
                        if message.content_hash != row["content_hash"]:
                            raise SyncError(
                                f"Source content changed during date repair: "
                                f"{email} {folder_name} UID {uid}"
                            )
                        if is_deleted_draft_message(rule, folder, message):
                            raise SyncError(
                                f"Tracked Outlook deleted draft encountered "
                                f"during date repair: {email} UID {uid}"
                            )
                        qq_header_rebuild = (
                            is_qq_source(email)
                            and needs_qq_date_header(message)
                            and message.internal_date is not None
                        )
                        action = sync_message(
                            db,
                            target,
                            target_rule["folder"],
                            message,
                            rule["account_label"],
                            source_folder_labels(rule, folder),
                            rule_labels_by_name,
                            run_id,
                            force_rebuild=(
                                target_date != message.internal_date
                                or qq_header_rebuild
                            ),
                            target_email=target_email,
                            target_service_address=target_service_address,
                        )
                        if action == "date_rebuilt":
                            account_rebuilt += 1
                            totals["date_rebuilt"] += 1
                            if qq_header_rebuild:
                                totals["qq_header_rebuilt"] += 1
                        else:
                            account_verified += 1
                            totals["verified"] += 1
                        if (account_rebuilt + account_verified) % 100 == 0:
                            print(
                                json.dumps(
                                    {
                                        "event": "date_repair_progress",
                                        "source": email,
                                        "verified": account_verified,
                                        "date_rebuilt": account_rebuilt,
                                        "qq_header_rebuilt": totals[
                                            "qq_header_rebuilt"
                                        ],
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                    db.commit()
                unresolved_deleted = db.execute(
                    f"""
                    SELECT COUNT(*) FROM messages
                    WHERE source_email=?
                      {target_filter}
                      AND (
                          source_date_verified=0
                          OR target_date_header_verified=0
                      )
                      AND source_folder='__deleted__'
                    """,
                    (
                        email,
                        *target_params,
                    ),
                ).fetchone()[0]
                totals["unresolved"] += int(unresolved_deleted)
                print(
                    json.dumps(
                        {
                            "event": "date_repair_source_completed",
                            "source": email,
                            "verified": account_verified,
                            "date_rebuilt": account_rebuilt,
                            "unresolved_deleted": int(unresolved_deleted),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            finally:
                safe_logout(source)
        remaining = db.execute(
            """
            SELECT COUNT(*) FROM messages
            WHERE source_date_verified=0
               OR target_date_header_verified=0
            """
        ).fetchone()[0]
        print(
            json.dumps(
                {
                    "event": "date_repair_completed",
                    **totals,
                    "remaining_unverified": int(remaining),
                    "state_db": str(state_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0 if remaining == 0 else 1
    finally:
        try:
            for target in target_clients.values():
                safe_logout(target)
        finally:
            db.close()
