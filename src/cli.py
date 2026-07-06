#!/usr/bin/env python3
"""Incrementally mirror source IMAP mail into one target INBOX.

Synology MailPlus label removals are not reliably reflected by standard IMAP.
When a source message changes folders, this synchronizer therefore appends and
verifies a fresh target copy with the desired labels before deleting the old
target copy.
"""

from __future__ import annotations

import argparse
import hashlib
import imaplib
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from auth.oauth import (
    TARGET_PROFILE_KEY,
    command_authorize_outlook,
    credential_profiles,
    microsoft_access_token,
    resolve_auth_token_paths,
)
from common.files import load_json, resolve_path
from common.clock import now_iso
from commands.folders import command_list_folders
from commands.repair_dates import command_repair_dates
from config.sources import selected_source_rules, source_rule_for
from config.validation import (
    MICROSOFT_IMAP_SCOPE,
    label_id_map,
    validate_config,
)
from common.errors import SyncError
from log.events import emit_json
from connection import connect, ensure_source_connection, safe_logout
from domain.message import (
    SourceMessage,
    content_hash,
    content_hash_matches,
    is_qq_source,
    needs_qq_date_header,
    normalized_content_hash,
    parse_fetch_tuple,
    parse_message_date,
    parse_flags,
    parse_internal_date,
    source_normalized_hash as cached_source_normalized_hash,
    source_message_date,
    target_message_raw,
    target_content_hash,
    target_normalized_hash,
)
from imap.protocol import (
    FolderInfo,
    quote_imap_string,
    quote_mailbox,
    response_int,
    search_uids,
    select_mailbox,
)
from imap.source import (
    SourceMessageUnavailable,
    fetch_date_header_presence,
    fetch_message_metadata,
    fetch_pop_message_by_identity,
    fetch_source_message,
    list_folders,
    mailbox_status,
    parse_mailbox_status,
    pop_retrieve_raw,
    selectable_source_folders,
)
from storage.state import DB_SCHEMA_VERSION, init_db, process_lock
from sync.engine import purge_single_location_message, sync_message
from sync.incremental import (
    bulk_mark_locations_seen,
    incremental_candidate_uids,
    verified_known_location_uids,
)
from sync.labels import normalize_folder_labels, row_folder_labels
from sync.constants import NO_CHANGE_ACTIONS
from sync.pending import (
    begin_pending_operation,
    pending_row_message,
    quarantine_pending_operation,
    update_pending_target_uid,
)
from sync.records import find_by_location, upsert_message_location, upsert_state
from sync.reconcile import reconcile_deleted_messages
from sync.recovery import recover_pending_operations
from sync.rules import (
    is_deleted_draft_message,
    source_folder_label,
    source_folder_skipped_uids,
)
from imap.target import (
    TargetContentVerificationError,
    append_target_copy,
    delete_target_copy,
    ensure_target_delete_will_be_safe,
    find_untracked_target_by_message_id,
    repair_target_copy,
    replace_target_copy,
    target_fetch,
)
from monitoring.listener import monitor_realtime_changes, monitor_source_changes
from sync.schedule import build_source_schedule
from sync.settings import DEFAULT_SYNC_INTERVAL_SECONDS


def command_sync(
    config: dict,
    profiles: Dict[str, dict],
    labels_by_name: Dict[str, str],
    state_path: Path,
    source_email: Optional[str],
    only_folder: Optional[str],
    limit: Optional[int],
    newest_first: bool,
    incremental_only: bool = False,
) -> int:
    if incremental_only and only_folder is None:
        raise SyncError("Fast incremental sync requires one source folder")
    db = init_db(state_path)
    target_rule = config["target"]
    target_profile = profiles[TARGET_PROFILE_KEY]
    target = connect(target_profile)
    total_scanned = 0
    total_changed = 0
    run_id = uuid.uuid4().hex
    all_scans_complete = True
    reconciled_sources = 0
    summary: Dict[str, int] = {}
    try:
        recovered = recover_pending_operations(
            db,
            target,
            target_rule["folder"],
            labels_by_name,
        )
        if recovered:
            summary["pending_operation_recovered"] = recovered
        for rule in selected_source_rules(config, source_email):
            email = rule["email"]
            source_changed = 0
            source_scan_complete = True
            source = connect(profiles[email])
            try:
                folders = selectable_source_folders(list_folders(source), rule)
                if only_folder:
                    folders = [
                        folder
                        for folder in folders
                        if only_folder in (folder.raw_name, folder.display_name)
                    ]
                    if not folders:
                        raise SyncError(f"Source folder not found or excluded: {only_folder}")

                for folder in folders:
                    _, uidvalidity = select_mailbox(
                        source,
                        folder.raw_name,
                        readonly=True,
                    )
                    uids = search_uids(source)
                    if incremental_only:
                        uids = incremental_candidate_uids(
                            db,
                            email,
                            folder.raw_name,
                            uidvalidity,
                            uids,
                        )
                    skipped_uids = source_folder_skipped_uids(rule, folder)
                    inspect_deleted_drafts = (
                        rule.get("skip_deleted_drafts", False)
                        and source_folder_label(rule, folder)
                        == rule.get("deleted_label")
                    )
                    if not incremental_only and not inspect_deleted_drafts:
                        verified_uids = verified_known_location_uids(
                            db,
                            email,
                            folder.raw_name,
                            uidvalidity,
                        )
                        unchanged_uids = [
                            uid for uid in uids if uid in verified_uids
                        ]
                        if unchanged_uids:
                            bulk_mark_locations_seen(
                                db,
                                email,
                                folder.raw_name,
                                uidvalidity,
                                unchanged_uids,
                                run_id,
                            )
                            unchanged_count = len(unchanged_uids)
                            summary["unchanged"] = (
                                summary.get("unchanged", 0)
                                + unchanged_count
                            )
                            total_scanned += unchanged_count
                            uids = [
                                uid for uid in uids if uid not in verified_uids
                            ]
                    if newest_first:
                        uids.reverse()
                    for uid in uids:
                        known = find_by_location(
                            db,
                            email,
                            folder.raw_name,
                            uidvalidity,
                            uid,
                        )
                        if (
                            known is not None
                            and known["internal_date"] is not None
                            and known["source_date_verified"]
                            and not inspect_deleted_drafts
                        ):
                            db.execute(
                                """
                                UPDATE message_locations
                                SET last_seen_run=?, updated_at=?
                                WHERE source_email=? AND source_folder=?
                                  AND source_uidvalidity=? AND source_uid=?
                                """,
                                (
                                    run_id,
                                    now_iso(),
                                    email,
                                    folder.raw_name,
                                    uidvalidity,
                                    uid,
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
                                    email,
                                    known["content_hash"],
                                ),
                            )
                            action = "unchanged"
                        else:
                            if known is None and uid in skipped_uids:
                                action = "source_message_configured_skip"
                                summary[action] = summary.get(action, 0) + 1
                                total_scanned += 1
                                emit_json(
                                    {
                                        "event": action,
                                        "source": email,
                                        "folder": folder.display_name,
                                        "uid": uid,
                                    }
                                )
                                continue
                            if limit is not None and source_changed >= limit:
                                source_scan_complete = False
                                break
                            try:
                                message = fetch_source_message(
                                    source,
                                    email,
                                    folder.raw_name,
                                    uidvalidity,
                                    uid,
                                    profiles[email],
                                )
                            except SourceMessageUnavailable as exc:
                                action = "source_message_skipped"
                                summary[action] = summary.get(action, 0) + 1
                                total_scanned += 1
                                emit_json(
                                    {
                                        "event": action,
                                        "source": email,
                                        "folder": folder.display_name,
                                        "uid": uid,
                                        "reason": str(exc),
                                    }
                                )
                                continue
                            if is_deleted_draft_message(rule, folder, message):
                                purged = (
                                    known is not None
                                    and purge_single_location_message(
                                        db,
                                        target,
                                        target_rule["folder"],
                                        known,
                                    )
                                )
                                action = (
                                    "source_deleted_draft_purged"
                                    if purged
                                    else "source_deleted_draft_skipped"
                                )
                                if purged:
                                    source_changed += 1
                                    total_changed += 1
                                summary[action] = summary.get(action, 0) + 1
                                total_scanned += 1
                                emit_json(
                                    {
                                        "event": action,
                                        "source": email,
                                        "folder": folder.display_name,
                                        "uid": uid,
                                    }
                                )
                                continue
                            action = sync_message(
                                db,
                                target,
                                target_rule["folder"],
                                message,
                                rule["account_label"],
                                source_folder_label(rule, folder),
                                labels_by_name,
                                run_id,
                                commit_state=False,
                            )
                            if action not in NO_CHANGE_ACTIONS:
                                source_changed += 1
                                total_changed += 1
                                source = ensure_source_connection(
                                    profiles[email],
                                    source,
                                    folder.raw_name,
                                    uidvalidity,
                                )
                        summary[action] = summary.get(action, 0) + 1
                        total_scanned += 1
                        if action not in NO_CHANGE_ACTIONS:
                            emit_json(
                                {
                                    "action": action,
                                    "source": email,
                                    "folder": folder.display_name,
                                    "uid": uid,
                                }
                            )
                    db.commit()
                    if not source_scan_complete:
                        break

                if only_folder is None and source_scan_complete:
                    rebuilt_deleted = reconcile_deleted_messages(
                        db,
                        target,
                        target_rule["folder"],
                        rule,
                        labels_by_name,
                        run_id,
                    )
                    if rebuilt_deleted:
                        summary["source_deleted_rebuilt"] = (
                            summary.get("source_deleted_rebuilt", 0)
                            + rebuilt_deleted
                        )
                    reconciled_sources += 1
                else:
                    all_scans_complete = False
            finally:
                safe_logout(source)

        emit_json(
            {
                "scanned": total_scanned,
                "changed": total_changed,
                "summary": summary,
                "reconciliation": (
                    "completed"
                    if all_scans_complete and only_folder is None
                    else "deferred_until_complete_scan"
                ),
                "reconciled_sources": reconciled_sources,
                "state_db": str(state_path),
            }
        )
    finally:
        try:
            safe_logout(target)
        finally:
            db.close()
    return 0


def command_watch(
    config: dict,
    profiles: Dict[str, dict],
    labels_by_name: Dict[str, str],
    state_path: Path,
    source_email: Optional[str],
    only_folder: Optional[str],
    limit: Optional[int],
    newest_first: bool,
    interval: int,
    poll_interval: float,
    max_cycles: Optional[int],
) -> int:
    rules = selected_source_rules(config, source_email)
    if not rules:
        raise SyncError("No source mailboxes are configured")

    schedule = build_source_schedule(rules, interval)

    changes: "queue.Queue[dict]" = queue.Queue()
    stop_event = threading.Event()
    monitors = []
    if poll_interval > 0:
        for rule in rules:
            monitor = threading.Thread(
                target=monitor_realtime_changes,
                args=(
                    profiles[rule["email"]],
                    rule,
                    only_folder,
                    poll_interval,
                    changes,
                    stop_event,
                ),
                name=f"mail-monitor-{rule['email']}",
                daemon=True,
            )
            monitor.start()
            monitors.append(monitor)

    cycle = 0
    pending_changes: Dict[Tuple[str, str], dict] = {}
    try:
        while True:
            full_job = min(schedule, key=lambda item: item["next_run"])
            timeout = max(
                0.0,
                min(1.0, full_job["next_run"] - time.monotonic()),
            )
            try:
                change = changes.get(timeout=timeout)
                pending_changes[(change["email"], change["folder"])] = change
                while True:
                    change = changes.get_nowait()
                    pending_changes[(change["email"], change["folder"])] = change
            except queue.Empty:
                pass

            if pending_changes:
                _, change = pending_changes.popitem()
                cycle += 1
                print(
                    json.dumps(
                        {
                            "event": "source_change_detected",
                            "cycle": cycle,
                            "source": change["email"],
                            "folder": change["folder"],
                            "previous": change["previous"],
                            "current": change["current"],
                            "time": now_iso(),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                try:
                    command_sync(
                        config,
                        profiles,
                        labels_by_name,
                        state_path,
                        change["email"],
                        (
                            None
                            if change["uidvalidity_changed"]
                            else change["folder"]
                        ),
                        None,
                        True,
                        incremental_only=not change["uidvalidity_changed"],
                    )
                    success = True
                except (
                    SyncError,
                    imaplib.IMAP4.error,
                    OSError,
                    ValueError,
                ) as exc:
                    success = False
                    print(
                        json.dumps(
                            {
                                "event": "source_incremental_sync_failed",
                                "cycle": cycle,
                                "source": change["email"],
                                "folder": change["folder"],
                                "time": now_iso(),
                                "error": str(exc),
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                full_job = min(schedule, key=lambda item: item["next_run"])
                if full_job["next_run"] > time.monotonic():
                    continue
                cycle += 1
                print(
                    json.dumps(
                        {
                            "event": "source_sync_started",
                            "cycle": cycle,
                            "source": full_job["email"],
                            "interval": full_job["interval"],
                            "batch_size": full_job["batch_size"],
                            "time": now_iso(),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                try:
                    command_sync(
                        config,
                        profiles,
                        labels_by_name,
                        state_path,
                        full_job["email"],
                        only_folder,
                        (
                            limit
                            if limit is not None
                            else full_job["batch_size"]
                        ),
                        newest_first,
                    )
                    success = True
                except (
                    SyncError,
                    imaplib.IMAP4.error,
                    OSError,
                    ValueError,
                ) as exc:
                    success = False
                    print(
                        json.dumps(
                            {
                                "event": "source_sync_failed",
                                "cycle": cycle,
                                "source": full_job["email"],
                                "time": now_iso(),
                                "error": str(exc),
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                full_job["next_run"] = time.monotonic() + full_job["interval"]

            if max_cycles is not None and cycle >= max_cycles:
                return 0 if success else 1

            next_job = min(schedule, key=lambda item: item["next_run"])
            print(
                json.dumps(
                    {
                        "event": "next_source_sync_scheduled",
                        "source": next_job["email"],
                        "seconds": round(
                            max(
                                0.0,
                                next_job["next_run"] - time.monotonic(),
                            ),
                            3,
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    finally:
        stop_event.set()
        for monitor in monitors:
            monitor.join(timeout=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="config/config.json",
        help="Configuration file (default: config/config.json)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list-folders",
        help="List source folders and show which ones will sync",
    )
    list_parser.add_argument("--source", help="Only this source email")

    authorize_parser = subparsers.add_parser(
        "authorize-outlook",
        help="Authorize one Outlook source using the Microsoft device-code flow",
    )
    authorize_parser.add_argument(
        "--source",
        required=True,
        help="Outlook source email to authorize",
    )

    sync_parser = subparsers.add_parser("sync", help="Run incremental synchronization")
    sync_parser.add_argument("--source", help="Only this source email")
    sync_parser.add_argument("--folder", help="Only this source folder")
    sync_parser.add_argument("--limit", type=int, help="Maximum messages to process")
    sync_parser.add_argument(
        "--newest-first",
        action="store_true",
        help="Process higher source UIDs first",
    )
    repair_dates_parser = subparsers.add_parser(
        "repair-dates",
        help="Verify source INTERNALDATE values and rebuild target mismatches",
    )
    repair_dates_parser.add_argument(
        "--source",
        help="Only this source email",
    )
    watch_parser = subparsers.add_parser(
        "watch",
        help="Run synchronization continuously at a fixed interval",
    )
    watch_parser.add_argument("--source", help="Only this source email")
    watch_parser.add_argument("--folder", help="Only this source folder")
    watch_parser.add_argument("--limit", type=int, help="Maximum messages per cycle")
    watch_order = watch_parser.add_mutually_exclusive_group()
    watch_order.add_argument(
        "--newest-first",
        dest="newest_first",
        action="store_true",
        help="Process higher source UIDs first (default)",
    )
    watch_order.add_argument(
        "--oldest-first",
        dest="newest_first",
        action="store_false",
        help="Process lower source UIDs first",
    )
    watch_parser.set_defaults(newest_first=True)
    watch_parser.add_argument(
        "--interval",
        type=int,
        default=int(
            os.environ.get("SYNC_INTERVAL", str(DEFAULT_SYNC_INTERVAL_SECONDS))
        ),
        help=(
            "Seconds between full reconciliation scans "
            f"(default: {DEFAULT_SYNC_INTERVAL_SECONDS})"
        ),
    )
    watch_parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("SYNC_POLL_INTERVAL", "3")),
        help=(
            "Fallback STATUS polling interval in seconds when INBOX IDLE "
            "is unavailable; set 0 to disable realtime monitoring (default: 3)"
        ),
    )
    watch_parser.add_argument(
        "--max-cycles",
        type=int,
        help="Stop after this many cycles (primarily for verification)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    base = config_path.parent
    config = load_json(config_path)
    labels_by_name = label_id_map(config.get("synology_labels"))
    validate_config(config, labels_by_name)
    resolve_auth_token_paths(config, base)
    state_path = resolve_path(
        base,
        os.environ.get("MAIL_SYNC_STATE_DB", config["state_db"]),
    )
    profiles = credential_profiles(config)

    if TARGET_PROFILE_KEY not in profiles:
        raise SyncError("Missing target mailbox configuration")
    required_emails = {rule["email"] for rule in config.get("sources", [])}
    missing = required_emails - set(profiles)
    if missing:
        raise SyncError(f"Missing credentials for: {sorted(missing)}")

    if args.command == "list-folders":
        return command_list_folders(config, profiles, args.source)
    if args.command == "authorize-outlook":
        return command_authorize_outlook(config, profiles, args.source)
    if args.command == "sync":
        with process_lock(state_path):
            return command_sync(
                config,
                profiles,
                labels_by_name,
                state_path,
                args.source,
                args.folder,
                args.limit,
                args.newest_first,
            )
    if args.command == "repair-dates":
        with process_lock(state_path):
            return command_repair_dates(
                config,
                profiles,
                labels_by_name,
                state_path,
                args.source,
            )
    if args.command == "watch":
        if args.interval < 1:
            raise SyncError("--interval must be at least 1 second")
        if args.poll_interval < 0:
            raise SyncError("--poll-interval cannot be negative")
        if args.max_cycles is not None and args.max_cycles < 1:
            raise SyncError("--max-cycles must be positive")
        with process_lock(state_path):
            return command_watch(
                config,
                profiles,
                labels_by_name,
                state_path,
                args.source,
                args.folder,
                args.limit,
                args.newest_first,
                args.interval,
                args.poll_interval,
                args.max_cycles,
            )
    raise SyncError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"event": "synchronizer_stopped", "time": now_iso()},
                ensure_ascii=False,
            )
        )
        raise SystemExit(0)
    except (SyncError, imaplib.IMAP4.error, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
