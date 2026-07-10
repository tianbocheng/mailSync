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
import subprocess
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
from common.files import load_json
from common.clock import now_iso
from commands.folders import command_list_folders
from commands.repair_dates import command_repair_dates
from config.sources import selected_source_rules, source_rule_for
from config.runtime import labels_for_rule, target_context_key, target_for_rule
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
from storage.config_db import import_legacy_config, load_runtime_config
from storage.rule_status import mark_sync_rule_failure, mark_sync_rule_success
from sync.engine import purge_single_location_message, sync_message
from sync.incremental import (
    bulk_mark_locations_seen,
    folder_uid_list_state_unchanged,
    folder_scan_state_unchanged,
    incremental_candidate_uids,
    upsert_folder_scan_state,
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
    source_folder_labels,
    source_folder_skipped_uids,
    source_folder_target_folder,
)
from sync.target_identity import target_identity_from_profile
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
from sync.schedule import build_source_schedule, refresh_source_schedule
from sync.settings import DEFAULT_SYNC_INTERVAL_SECONDS


RUN_CHILD_STOP_TIMEOUT_SECONDS = 8.0


def _folder_scan_signature(
    rule: dict,
    folder_labels: Sequence[str],
    skipped_uids: Set[int],
    target_folder: str,
    labels_by_name: Dict[str, str],
) -> str:
    return json.dumps(
        {
            "account_label": rule.get("account_label") or "",
            "folder_labels": list(folder_labels),
            "labels_by_name": sorted(labels_by_name.items()),
            "skip_deleted_drafts": bool(rule.get("skip_deleted_drafts", False)),
            "skipped_uids": sorted(int(uid) for uid in skipped_uids),
            "target_folder": target_folder,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


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
    sync_source: str = "imap_scan",
) -> int:
    if incremental_only and only_folder is None:
        raise SyncError("Fast incremental sync requires one source folder")
    db = init_db(state_path)
    total_scanned = 0
    total_changed = 0
    run_id = uuid.uuid4().hex
    all_scans_complete = True
    reconciled_sources = 0
    summary: Dict[str, int] = {}
    target_clients: Dict[str, imaplib.IMAP4] = {}
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
            recovered = recover_pending_operations(
                db,
                target,
                target_rule["folder"],
                rule_labels_by_name,
                source_emails=(email,),
                target_email=target_email,
                target_service_address=target_service_address,
                include_quarantined=True,
            )
            if recovered:
                summary["pending_operation_recovered"] = (
                    summary.get("pending_operation_recovered", 0)
                    + recovered
                )
            source_changed = 0
            source_processed = 0
            source_scan_complete = True
            watermark_unchanged_folders: Set[Tuple[str, int]] = set()
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
                    target_folder = source_folder_target_folder(
                        rule,
                        folder,
                        target_rule["folder"],
                    )
                    folder_labels = source_folder_labels(rule, folder)
                    skipped_uids = source_folder_skipped_uids(rule, folder)
                    inspect_deleted_drafts = (
                        rule.get("skip_deleted_drafts", False)
                        and rule.get("deleted_label") in folder_labels
                    )
                    scan_signature = _folder_scan_signature(
                        rule,
                        folder_labels,
                        skipped_uids,
                        target_folder,
                        rule_labels_by_name,
                    )
                    folder_status = None
                    watermark_allowed = (
                        not incremental_only
                        and only_folder is None
                        and not inspect_deleted_drafts
                    )
                    if watermark_allowed:
                        try:
                            folder_status = mailbox_status(source, folder.raw_name)
                        except (SyncError, TypeError, ValueError) as exc:
                            emit_json(
                                {
                                    "event": "source_folder_watermark_unavailable",
                                    "source": email,
                                    "folder": folder.display_name,
                                    "reason": str(exc),
                                }
                            )
                        if (
                            folder_status is not None
                            and folder_scan_state_unchanged(
                                db,
                                email,
                                folder.raw_name,
                                folder_status,
                                scan_signature,
                                target_email,
                                target_service_address,
                            )
                        ):
                            watermark_unchanged_folders.add(
                                (folder.raw_name, int(folder_status[0]))
                            )
                            summary["folder_watermark_unchanged"] = (
                                summary.get("folder_watermark_unchanged", 0)
                                + 1
                            )
                            continue
                    _, uidvalidity = select_mailbox(
                        source,
                        folder.raw_name,
                        readonly=True,
                    )
                    folder_scan_complete = True
                    if (
                        folder_status is not None
                        and int(folder_status[0]) != int(uidvalidity)
                    ):
                        folder_status = None
                    uids = search_uids(source)
                    if (
                        watermark_allowed
                        and folder_status is not None
                        and int(folder_status[1]) <= 0
                        and folder_uid_list_state_unchanged(
                            db,
                            email,
                            folder.raw_name,
                            folder_status,
                            uids,
                            scan_signature,
                            target_email,
                            target_service_address,
                        )
                    ):
                        watermark_unchanged_folders.add(
                            (folder.raw_name, int(uidvalidity))
                        )
                        summary["folder_uid_list_unchanged"] = (
                            summary.get("folder_uid_list_unchanged", 0) + 1
                        )
                        continue
                    if incremental_only:
                        uids = incremental_candidate_uids(
                            db,
                            email,
                            folder.raw_name,
                            uidvalidity,
                            uids,
                            target_email,
                            target_service_address,
                        )
                    if not incremental_only and not inspect_deleted_drafts:
                        verified_uids = verified_known_location_uids(
                            db,
                            email,
                            folder.raw_name,
                            uidvalidity,
                            target_email,
                            target_service_address,
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
                                target_email,
                                target_service_address,
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
                            target_email,
                            target_service_address,
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
                                UPDATE messages SET
                                    last_sync_action='unchanged',
                                    last_sync_source=?,
                                    last_seen_run=?, updated_at=?
                                WHERE source_email=? AND content_hash=?
                                """,
                                (
                                    sync_source,
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
                            if limit is not None and source_processed >= limit:
                                source_scan_complete = False
                                folder_scan_complete = False
                                break
                            source_processed += 1
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
                                watermark_allowed = False
                                if known is not None:
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
                                        UPDATE messages SET
                                            last_sync_action=?,
                                            last_sync_source=?,
                                            last_seen_run=?, updated_at=?
                                        WHERE source_email=? AND content_hash=?
                                        """,
                                        (
                                            "source_message_unavailable_seen",
                                            sync_source,
                                            run_id,
                                            now_iso(),
                                            email,
                                            known["content_hash"],
                                        ),
                                    )
                                    action = "source_message_unavailable_seen"
                                else:
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
                                        target_folder,
                                        known,
                                        target_email,
                                        target_service_address,
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
                            try:
                                action = sync_message(
                                    db,
                                    target,
                                    target_folder,
                                    message,
                                    rule["account_label"],
                                    folder_labels,
                                    rule_labels_by_name,
                                    run_id,
                                    commit_state=False,
                                    target_email=target_email,
                                    target_service_address=target_service_address,
                                    sync_source=sync_source,
                                )
                            except TargetContentVerificationError as exc:
                                action = "quarantined"
                                emit_json(
                                    {
                                        "event": "target_copy_quarantined",
                                        "source": email,
                                        "folder": folder.display_name,
                                        "uid": uid,
                                        "target_uid": exc.uid,
                                        "error": str(exc),
                                    }
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
                    if (
                        watermark_allowed
                        and folder_scan_complete
                        and folder_status is not None
                    ):
                        upsert_folder_scan_state(
                            db,
                            email,
                            folder.raw_name,
                            folder_status,
                            run_id,
                            scan_signature,
                            target_email,
                            target_service_address,
                            source_uids=uids,
                        )
                    db.commit()
                    if not source_scan_complete:
                        break

                if only_folder is None and source_scan_complete:
                    if (
                        watermark_unchanged_folders
                        and len(watermark_unchanged_folders) == len(folders)
                        and source_changed == 0
                        and source_processed == 0
                    ):
                        rebuilt_deleted = 0
                        summary["reconciliation_watermark_skipped"] = (
                            summary.get("reconciliation_watermark_skipped", 0)
                            + 1
                        )
                    else:
                        rebuilt_deleted = reconcile_deleted_messages(
                            db,
                            target,
                            target_rule["folder"],
                            rule,
                            rule_labels_by_name,
                            run_id,
                            target_email,
                            target_service_address,
                            rebuild_limit=limit,
                            preserved_source_folders=watermark_unchanged_folders,
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
            for target in target_clients.values():
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
    rules = _watch_source_rules(config, source_email)

    schedule = build_source_schedule(rules, interval) if rules else []
    active_config = config
    active_profiles = profiles
    active_labels_by_name = labels_by_name

    changes: "queue.Queue[dict]" = queue.Queue()
    monitor_records: Dict[Tuple[str, str], dict] = {}

    cycle = 0
    pending_changes: Dict[Tuple[str, str], dict] = {}
    try:
        while True:
            (
                active_config,
                active_profiles,
                active_labels_by_name,
                rules,
            ) = _watch_runtime_context(state_path, source_email)
            schedule = refresh_source_schedule(rules, interval, schedule)
            _sync_source_monitors(
                monitor_records,
                rules,
                active_profiles,
                only_folder,
                poll_interval,
                changes,
            )
            if not schedule:
                time.sleep(1)
                continue
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
                if change["email"] not in {rule["email"] for rule in rules}:
                    continue
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
                        active_config,
                        active_profiles,
                        active_labels_by_name,
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
                        sync_source=_change_sync_source(change),
                    )
                    success = True
                    _record_watch_success(state_path, change["email"])
                except (
                    SyncError,
                    imaplib.IMAP4.error,
                    OSError,
                    ValueError,
                ) as exc:
                    success = False
                    _record_watch_failure(state_path, change["email"], exc)
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
                        active_config,
                        active_profiles,
                        active_labels_by_name,
                        state_path,
                        full_job["email"],
                        only_folder,
                        (
                            limit
                            if limit is not None
                            else full_job["batch_size"]
                        ),
                        newest_first,
                        sync_source=_rule_full_scan_source(full_job),
                    )
                    success = True
                    _record_watch_success(state_path, full_job["email"])
                except (
                    SyncError,
                    imaplib.IMAP4.error,
                    OSError,
                    ValueError,
                ) as exc:
                    success = False
                    _record_watch_failure(state_path, full_job["email"], exc)
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
                ran_at = time.monotonic()
                full_job["last_run"] = ran_at
                full_job["next_run"] = ran_at + full_job["interval"]

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
        for record in monitor_records.values():
            record["stop_event"].set()
        for record in monitor_records.values():
            record["thread"].join(timeout=2)


def _source_realtime_poll_interval(profile: dict, default: float) -> float:
    raw = profile.get("realtime_poll_interval")
    if raw in (None, ""):
        return default
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return default
    if parsed < 1:
        return default
    return parsed


def _source_monitor_key(rule: dict, profile: dict) -> Tuple[str, str]:
    return (
        str(rule.get("email") or ""),
        str(rule.get("service_address") or profile.get("service_address") or ""),
    )


def _source_monitor_signature(
    rule: dict,
    profile: dict,
    only_folder: Optional[str],
    poll_interval: float,
) -> Tuple[object, ...]:
    return (
        str(rule.get("email") or ""),
        str(rule.get("service_address") or profile.get("service_address") or ""),
        str(profile.get("imap_server") or ""),
        str(profile.get("imap_port") or ""),
        bool(profile.get("use_ssl", True)),
        str(profile.get("imap_proxy") or ""),
        str(profile.get("mailbox_features") or ""),
        str(profile.get("mailbox_feature_groups") or ""),
        str(only_folder or ""),
        float(poll_interval),
    )


def _sync_source_monitors(
    monitor_records: Dict[Tuple[str, str], dict],
    rules: Sequence[dict],
    profiles: Dict[str, dict],
    only_folder: Optional[str],
    poll_interval: float,
    changes: "queue.Queue[dict]",
) -> None:
    desired: Dict[Tuple[str, str], Tuple[dict, dict, float, Tuple[object, ...]]] = {}
    if poll_interval > 0:
        for rule in rules:
            if rule.get("sync_mode") == "scheduled":
                continue
            profile = profiles[rule["email"]]
            source_poll_interval = _source_realtime_poll_interval(
                profile,
                poll_interval,
            )
            key = _source_monitor_key(rule, profile)
            desired[key] = (
                rule,
                profile,
                source_poll_interval,
                _source_monitor_signature(
                    rule,
                    profile,
                    only_folder,
                    source_poll_interval,
                ),
            )

    for key, record in list(monitor_records.items()):
        desired_entry = desired.get(key)
        if desired_entry is not None and record["signature"] == desired_entry[3]:
            continue
        record["stop_event"].set()
        record["thread"].join(timeout=2)
        monitor_records.pop(key, None)

    for key, (rule, profile, source_poll_interval, signature) in desired.items():
        if key in monitor_records:
            continue
        stop_event = threading.Event()
        monitor = threading.Thread(
            target=monitor_realtime_changes,
            args=(
                profile,
                rule,
                only_folder,
                source_poll_interval,
                changes,
                stop_event,
            ),
            name=f"mail-monitor-{rule['email']}",
            daemon=True,
        )
        monitor.start()
        monitor_records[key] = {
            "thread": monitor,
            "stop_event": stop_event,
            "signature": signature,
        }


def _change_sync_source(change: dict) -> str:
    return {
        "idle": "imap_idle",
        "idle_compensation": "imap_idle_compensation",
        "poll": "imap_poll",
    }.get(str(change.get("detected_by") or ""), "imap_poll")


def _rule_full_scan_source(rule: dict) -> str:
    if str(rule.get("sync_mode") or "realtime") == "scheduled":
        return "imap_scheduled"
    return "imap_scan"


def _watch_runtime_context(
    state_path: Path,
    source_email: Optional[str],
) -> tuple[dict, Dict[str, dict], Dict[str, str], list[dict]]:
    db = init_db(state_path)
    try:
        config = _load_watch_runtime_config(db, state_path)
    finally:
        db.close()
    labels_by_name = label_id_map(config.get("synology_labels"))
    validate_config(config, labels_by_name, allow_empty_sources=True)
    resolve_auth_token_paths(config, state_path.parent)
    profiles = credential_profiles(config)
    rules = _watch_source_rules(config, source_email)
    required_emails = {rule["email"] for rule in rules}
    missing = required_emails - set(profiles)
    if missing:
        raise SyncError(f"Missing credentials for: {sorted(missing)}")
    return config, profiles, labels_by_name, rules


def _load_watch_runtime_config(db: sqlite3.Connection, state_path: Path) -> dict:
    try:
        return load_runtime_config(db, state_path)
    except SyncError as exc:
        if str(exc).startswith("No target mailbox is configured"):
            return {
                "state_db": str(state_path),
                "target": None,
                "sources": [],
                "synology_labels": {},
            }
        raise


def _watch_source_rules(config: dict, source_email: Optional[str]) -> list[dict]:
    rules = list(config.get("sources", []))
    if not source_email:
        return rules
    return [rule for rule in rules if rule.get("email") == source_email]


def _record_watch_success(state_path: Path, source_email: str) -> None:
    db = init_db(state_path)
    try:
        mark_sync_rule_success(db, source_email)
    finally:
        db.close()


def _record_watch_failure(
    state_path: Path,
    source_email: str,
    error: object,
) -> None:
    db = init_db(state_path)
    try:
        mark_sync_rule_failure(db, source_email, error)
    finally:
        db.close()


def command_run(
    state_path: Path,
    host: str,
    port: int,
    source_email: Optional[str],
    only_folder: Optional[str],
    limit: Optional[int],
    newest_first: bool,
    interval: int,
    poll_interval: float,
    log_dir: Optional[str],
) -> int:
    log_root = _run_log_dir(state_path, log_dir)
    web_log_path = log_root / "web.log"
    sync_log_path = log_root / "sync.log"
    launcher = Path(__file__).resolve().parents[1] / "mail_sync.py"
    base_command = [
        sys.executable,
        str(launcher),
        "--db",
        str(state_path),
    ]
    web_command = [
        *base_command,
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    sync_command = [
        *base_command,
        "watch",
        "--interval",
        str(interval),
        "--poll-interval",
        str(poll_interval),
        "--newest-first" if newest_first else "--oldest-first",
    ]
    if source_email:
        sync_command.extend(["--source", source_email])
    if only_folder:
        sync_command.extend(["--folder", only_folder])
    if limit is not None:
        sync_command.extend(["--limit", str(limit)])

    web_log = web_log_path.open("a", encoding="utf-8", buffering=1)
    sync_log = sync_log_path.open("a", encoding="utf-8", buffering=1)
    processes: dict[str, subprocess.Popen] = {}
    try:
        _write_run_log_header(web_log, "web")
        _write_run_log_header(sync_log, "sync")
        processes["web"] = subprocess.Popen(
            web_command,
            stdout=web_log,
            stderr=subprocess.STDOUT,
            cwd=str(launcher.parent.parent),
        )
        processes["sync"] = subprocess.Popen(
            sync_command,
            stdout=sync_log,
            stderr=subprocess.STDOUT,
            cwd=str(launcher.parent.parent),
        )
        emit_json(
            {
                "event": "combined_runner_started",
                "web_pid": processes["web"].pid,
                "sync_pid": processes["sync"].pid,
                "web_log": str(web_log_path),
                "sync_log": str(sync_log_path),
            }
        )
        while True:
            for name, process in processes.items():
                return_code = process.poll()
                if return_code is None:
                    continue
                emit_json(
                    {
                        "event": "combined_runner_child_exited",
                        "service": name,
                        "return_code": return_code,
                    },
                    error=return_code != 0,
                )
                _stop_run_processes(
                    {
                        other_name: other_process
                        for other_name, other_process in processes.items()
                        if other_name != name
                    }
                )
                return return_code if return_code != 0 else 1
            time.sleep(1)
    except KeyboardInterrupt:
        emit_json({"event": "combined_runner_stopping", "time": now_iso()})
        _stop_run_processes(processes)
        return 0
    finally:
        web_log.close()
        sync_log.close()


def _run_log_dir(state_path: Path, log_dir: Optional[str]) -> Path:
    default_log_dir = os.environ.get("MAIL_SYNC_LOG_DIR")
    root = (
        Path(log_dir).expanduser()
        if log_dir
        else Path(default_log_dir).expanduser()
        if default_log_dir
        else _project_root() / "logs"
    )
    if not root.is_absolute():
        root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_root() -> Path:
    backend_or_app = Path(__file__).resolve().parents[1]
    if backend_or_app.name == "backend":
        return backend_or_app.parent
    return backend_or_app


def _write_run_log_header(handle, service: str) -> None:
    handle.write(
        json.dumps(
            {
                "event": "service_log_opened",
                "service": service,
                "time": now_iso(),
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def _stop_run_processes(processes: dict[str, subprocess.Popen]) -> None:
    for process in processes.values():
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + RUN_CHILD_STOP_TIMEOUT_SECONDS
    for process in processes.values():
        remaining = max(0.0, deadline - time.monotonic())
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
    for process in processes.values():
        if process.poll() is None:
            process.wait()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=(
            os.environ.get("MAIL_SYNC_STATE_DB")
            or os.environ.get("MAIL_SYNC_DB")
            or "data/mail_sync_state.sqlite3"
        ),
        help=(
            "SQLite config/state database path "
            "(default: data/mail_sync_state.sqlite3)"
        ),
    )
    parser.add_argument(
        "--config",
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-config",
        help="Import a legacy JSON configuration into SQLite",
    )
    import_parser.add_argument("path", help="Legacy config.json path")
    import_parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing SQLite configuration",
    )

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
    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the web management console",
    )
    serve_parser.add_argument(
        "--host",
        default=os.environ.get("MAIL_SYNC_WEB_HOST", "127.0.0.1"),
        help="Web console bind host (default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MAIL_SYNC_WEB_PORT", "24000")),
        help="Web console bind port (default: 24000)",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="Run the web console and continuous synchronizer together",
    )
    run_parser.add_argument(
        "--host",
        default=os.environ.get("MAIL_SYNC_WEB_HOST", "127.0.0.1"),
        help="Web console bind host (default: 127.0.0.1)",
    )
    run_parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MAIL_SYNC_WEB_PORT", "24000")),
        help="Web console bind port (default: 24000)",
    )
    run_parser.add_argument(
        "--log-dir",
        default=os.environ.get("MAIL_SYNC_LOG_DIR"),
        help="Directory for web.log and sync.log (default: DB directory/logs)",
    )
    run_parser.add_argument("--source", help="Only this source email")
    run_parser.add_argument("--folder", help="Only this source folder")
    run_parser.add_argument("--limit", type=int, help="Maximum messages per cycle")
    run_order = run_parser.add_mutually_exclusive_group()
    run_order.add_argument(
        "--newest-first",
        dest="newest_first",
        action="store_true",
        help="Process higher source UIDs first (default)",
    )
    run_order.add_argument(
        "--oldest-first",
        dest="newest_first",
        action="store_false",
        help="Process lower source UIDs first",
    )
    run_parser.set_defaults(newest_first=True)
    run_parser.add_argument(
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
    run_parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("SYNC_POLL_INTERVAL", "3")),
        help=(
            "Fallback STATUS polling interval in seconds when INBOX IDLE "
            "is unavailable; set 0 to disable realtime monitoring (default: 3)"
        ),
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
    if args.config:
        print(
            "warning: --config is deprecated; use import-config and --db instead",
            file=sys.stderr,
        )
        if args.command != "import-config":
            raise SyncError(
                "--config is no longer read at runtime; run import-config first"
            )
    state_path = Path(args.db).expanduser()
    if not state_path.is_absolute():
        state_path = state_path.resolve()

    if args.command == "import-config":
        legacy_path = Path(args.path).expanduser()
        if not legacy_path.is_absolute():
            legacy_path = legacy_path.resolve()
        config = load_json(legacy_path)
        labels_by_name = label_id_map(config.get("synology_labels"))
        validate_config(config, labels_by_name)
        resolve_auth_token_paths(config, legacy_path.parent)
        with process_lock(state_path):
            db = init_db(state_path)
            try:
                import_legacy_config(
                    db,
                    config,
                    replace=args.replace,
                    imported_from=legacy_path,
                )
            finally:
                db.close()
        emit_json(
            {
                "event": "config_imported",
                "source": str(legacy_path),
                "state_db": str(state_path),
            }
        )
        return 0

    if args.command == "serve":
        if args.port < 1 or args.port > 65535:
            raise SyncError("--port must be 1-65535")
        from web.app import serve_web

        return serve_web(state_path, args.host, args.port)
    if args.command == "run":
        if args.port < 1 or args.port > 65535:
            raise SyncError("--port must be 1-65535")
        if args.interval < 1:
            raise SyncError("--interval must be at least 1 second")
        if args.poll_interval < 0:
            raise SyncError("--poll-interval cannot be negative")
        return command_run(
            state_path,
            args.host,
            args.port,
            args.source,
            args.folder,
            args.limit,
            args.newest_first,
            args.interval,
            args.poll_interval,
            args.log_dir,
        )

    db = init_db(state_path)
    try:
        if args.command == "watch":
            config = _load_watch_runtime_config(db, state_path)
        else:
            config = load_runtime_config(
                db,
                state_path,
                include_source_email=(
                    args.source
                    if args.command in {"sync", "repair-dates"} and args.source
                    else None
                ),
            )
    finally:
        db.close()
    base = state_path.parent
    labels_by_name = label_id_map(config.get("synology_labels"))
    validate_config(
        config,
        labels_by_name,
        allow_empty_sources=args.command == "watch",
    )
    resolve_auth_token_paths(config, base)
    profiles = credential_profiles(config)
    if args.command == "watch":
        required_emails = {
            rule["email"] for rule in _watch_source_rules(config, args.source)
        }
    else:
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
