from __future__ import annotations

import imaplib
import json
import queue
import select
import sys
import threading
import time
from typing import Optional

from common.clock import now_iso
from common.errors import SyncError
from connection import connect, safe_logout
from imap.protocol import select_mailbox
from imap.source import list_folders, mailbox_status, selectable_source_folders

DEFAULT_IDLE_RESTART_SECONDS = 20 * 60
DEFAULT_IDLE_ERROR_FALLBACK_THRESHOLD = 3
DEFAULT_IDLE_MISSED_PUSH_FALLBACK_THRESHOLD = 3


class IdleUnsupported(SyncError):
    pass


def _capability_names(client: imaplib.IMAP4) -> set[str]:
    result: set[str] = set()
    capabilities = getattr(client, "capabilities", ())
    if isinstance(capabilities, (list, tuple, set)):
        for item in capabilities:
            if isinstance(item, bytes):
                result.update(
                    part.upper()
                    for part in item.decode("ascii", "replace").split()
                    if part
                )
            else:
                result.update(part.upper() for part in str(item).split() if part)
    if result:
        return result

    capability = getattr(client, "capability", None)
    if not callable(capability):
        return result
    try:
        typ, rows = capability()
    except Exception:
        return result
    if typ != "OK":
        return result
    for row in rows or []:
        if isinstance(row, bytes):
            result.update(
                part.upper()
                for part in row.decode("ascii", "replace").split()
                if part
            )
        else:
            result.update(part.upper() for part in str(row).split() if part)
    return result


def idle_supported(client: imaplib.IMAP4) -> bool:
    return "IDLE" in _capability_names(client)


def _start_idle(client: imaplib.IMAP4) -> bytes:
    try:
        tag = client._new_tag()
        client.send(tag + b" IDLE\r\n")
        response = client._get_response()
    except (imaplib.IMAP4.error, OSError, EOFError) as exc:
        raise SyncError(f"IMAP IDLE start failed: {exc}") from exc
    if response is not None:
        raise SyncError(f"IMAP IDLE was not accepted: {response!r}")
    return tag


def _finish_idle(client: imaplib.IMAP4, tag: bytes) -> None:
    try:
        client.send(b"DONE\r\n")
        typ, response = client._command_complete("IDLE", tag)
    except (imaplib.IMAP4.error, OSError, EOFError) as exc:
        raise SyncError(f"IMAP IDLE finish failed: {exc}") from exc
    if typ != "OK":
        raise SyncError(f"IMAP IDLE finished with {typ}: {response!r}")


def idle_wait_for_change(
    client: imaplib.IMAP4,
    stop_event: threading.Event,
    timeout: float,
) -> bool:
    tag = _start_idle(client)
    changed = False
    deadline = time.monotonic() + timeout
    try:
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select(
                [client.sock],
                [],
                [],
                min(1.0, remaining),
            )
            if not readable:
                continue
            response = client._get_response()
            if response is None:
                continue
            if isinstance(response, bytes) and response.startswith(b"*"):
                changed = True
                break
        return changed
    finally:
        _finish_idle(client, tag)


def _inbox_folder(folders: list) -> Optional[object]:
    for folder in folders:
        if folder.raw_name.casefold() == "inbox":
            return folder
    for folder in folders:
        if folder.display_name.casefold() == "inbox":
            return folder
    return None


def _only_inbox_requested(only_folder: Optional[str]) -> bool:
    return only_folder is None or only_folder.casefold() == "inbox"


def _prioritize_inbox(folders: list) -> list:
    return sorted(
        folders,
        key=lambda folder: (
            0
            if folder.raw_name.casefold() == "inbox"
            or folder.display_name.casefold() == "inbox"
            else 1
        ),
    )


def monitor_inbox_idle_changes(
    profile: dict,
    source_rule: dict,
    only_folder: Optional[str],
    changes: "queue.Queue[dict]",
    stop_event: threading.Event,
    idle_restart_seconds: float = DEFAULT_IDLE_RESTART_SECONDS,
    idle_error_fallback_threshold: int = DEFAULT_IDLE_ERROR_FALLBACK_THRESHOLD,
    compensation_interval: Optional[float] = None,
    missed_push_fallback_threshold: int = DEFAULT_IDLE_MISSED_PUSH_FALLBACK_THRESHOLD,
    monitor_state: Optional[dict] = None,
) -> None:
    if not _only_inbox_requested(only_folder):
        raise IdleUnsupported(
            "IMAP IDLE realtime monitor only watches INBOX for this sync mode"
        )

    email = source_rule["email"]
    reconnect_delay = 2.0
    consecutive_errors = 0
    missed_pushes = 0
    fallback_threshold = max(1, int(idle_error_fallback_threshold))
    missed_push_threshold = max(1, int(missed_push_fallback_threshold))
    probe_interval = (
        idle_restart_seconds
        if compensation_interval is None
        else max(1.0, float(compensation_interval))
    )
    while not stop_event.is_set():
        client: Optional[imaplib.IMAP4] = None
        try:
            client = connect(profile)
            if not idle_supported(client):
                raise IdleUnsupported(f"IMAP IDLE is not supported by {email}")
            folders = selectable_source_folders(
                list_folders(client),
                source_rule,
            )
            folder = _inbox_folder(folders)
            if folder is None:
                raise IdleUnsupported(f"INBOX is not selectable for {email}")

            _, selected_uidvalidity = select_mailbox(
                client,
                folder.raw_name,
                readonly=True,
            )
            previous = mailbox_status(client, folder.raw_name)
            if monitor_state is not None:
                monitor_state["coverage"] = "inbox"
                monitor_state["ready"] = True
                monitor_state["snapshots"] = {folder.raw_name: previous}
            print(
                json.dumps(
                    {
                        "event": "source_idle_monitor_ready",
                        "source": email,
                        "folder": folder.raw_name,
                        "idle_restart_seconds": idle_restart_seconds,
                        "compensation_interval": probe_interval,
                        "time": now_iso(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            reconnect_at = time.monotonic() + idle_restart_seconds
            while not stop_event.is_set():
                remaining = reconnect_at - time.monotonic()
                if remaining <= 0:
                    print(
                        json.dumps(
                            {
                                "event": "source_idle_monitor_restarting",
                                "source": email,
                                "folder": folder.raw_name,
                                "time": now_iso(),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    break
                changed = idle_wait_for_change(
                    client,
                    stop_event,
                    min(remaining, probe_interval),
                )
                if stop_event.is_set():
                    break
                current = mailbox_status(client, folder.raw_name)
                if changed:
                    missed_pushes = 0
                if current == previous:
                    continue
                if monitor_state is not None:
                    monitor_state["change_version"] = (
                        int(monitor_state.get("change_version", 0)) + 1
                    )
                    monitor_state["snapshots"] = {folder.raw_name: current}
                if not changed:
                    missed_pushes += 1
                    print(
                        json.dumps(
                            {
                                "event": "source_idle_compensation_detected_change",
                                "source": email,
                                "folder": folder.raw_name,
                                "missed_pushes": missed_pushes,
                                "threshold": missed_push_threshold,
                                "poll_interval": probe_interval,
                                "time": now_iso(),
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                changes.put(
                    {
                        "email": email,
                        "folder": folder.raw_name,
                        "detected_by": (
                            "idle" if changed else "idle_compensation"
                        ),
                        "uidvalidity_changed": (
                            current[0] != previous[0]
                            or current[0] != selected_uidvalidity
                        ),
                        "previous": previous,
                        "current": current,
                    }
                )
                previous = current
                if missed_pushes >= missed_push_threshold:
                    raise IdleUnsupported(
                        "IMAP IDLE missed repeated INBOX changes; falling back to polling"
                    )
            consecutive_errors = 0
        except IdleUnsupported:
            raise
        except (
            SyncError,
            imaplib.IMAP4.error,
            OSError,
            ValueError,
            EOFError,
        ) as exc:
            consecutive_errors += 1
            if not stop_event.is_set():
                print(
                    json.dumps(
                        {
                            "event": "source_idle_monitor_reconnecting",
                            "source": email,
                            "error": str(exc),
                            "seconds": reconnect_delay,
                            "time": now_iso(),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            if consecutive_errors >= fallback_threshold:
                raise IdleUnsupported(
                    "IMAP IDLE failed repeatedly; falling back to polling"
                ) from exc
        finally:
            if client is not None:
                safe_logout(client)
        stop_event.wait(reconnect_delay)


def monitor_realtime_changes(
    profile: dict,
    source_rule: dict,
    only_folder: Optional[str],
    poll_interval: float,
    changes: "queue.Queue[dict]",
    stop_event: threading.Event,
    monitor_state: Optional[dict] = None,
) -> None:
    if _saved_idle_supported(profile) is False:
        if not stop_event.is_set():
            print(
                json.dumps(
                    {
                        "event": "source_idle_monitor_skipped",
                        "source": source_rule["email"],
                        "reason": "saved mailbox feature idle_supported=false",
                        "poll_interval": poll_interval,
                        "time": now_iso(),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
        monitor_source_changes(
            profile,
            source_rule,
            only_folder,
            poll_interval,
            changes,
            stop_event,
            monitor_state=monitor_state,
        )
        return
    try:
        monitor_inbox_idle_changes(
            profile,
            source_rule,
            only_folder,
            changes,
            stop_event,
            compensation_interval=poll_interval,
            monitor_state=monitor_state,
        )
    except IdleUnsupported as exc:
        if not stop_event.is_set():
            print(
                json.dumps(
                    {
                        "event": "source_idle_monitor_fallback",
                        "source": source_rule["email"],
                        "reason": str(exc),
                        "poll_interval": poll_interval,
                        "time": now_iso(),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
        monitor_source_changes(
            profile,
            source_rule,
            only_folder,
            poll_interval,
            changes,
            stop_event,
            monitor_state=monitor_state,
        )


def _saved_idle_supported(profile: dict) -> Optional[bool]:
    groups = profile.get("mailbox_feature_groups")
    if isinstance(groups, dict):
        source = groups.get("source")
        if isinstance(source, dict) and "idle_supported" in source:
            return bool(source.get("idle_supported"))
    features = profile.get("mailbox_features")
    if not isinstance(features, dict) or "idle_supported" not in features:
        return None
    return bool(features.get("idle_supported"))


def monitor_source_changes(
    profile: dict,
    source_rule: dict,
    only_folder: Optional[str],
    poll_interval: float,
    changes: "queue.Queue[dict]",
    stop_event: threading.Event,
    monitor_state: Optional[dict] = None,
) -> None:
    email = source_rule["email"]
    reconnect_delay = max(2.0, poll_interval)
    while not stop_event.is_set():
        client: Optional[imaplib.IMAP4] = None
        try:
            client = connect(profile)
            folders = _prioritize_inbox(
                selectable_source_folders(
                    list_folders(client),
                    source_rule,
                )
            )
            if only_folder:
                folders = [
                    folder
                    for folder in folders
                    if only_folder in (folder.raw_name, folder.display_name)
                ]
            snapshots = {
                folder.raw_name: mailbox_status(client, folder.raw_name)
                for folder in folders
            }
            if monitor_state is not None:
                monitor_state["coverage"] = "all_folders"
                monitor_state["ready"] = True
                monitor_state["snapshots"] = dict(snapshots)
            print(
                json.dumps(
                    {
                        "event": "source_monitor_ready",
                        "source": email,
                        "folders": len(folders),
                        "poll_interval": poll_interval,
                        "time": now_iso(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            while not stop_event.wait(poll_interval):
                for folder in folders:
                    current = mailbox_status(client, folder.raw_name)
                    previous = snapshots[folder.raw_name]
                    if current == previous:
                        continue
                    snapshots[folder.raw_name] = current
                    if monitor_state is not None:
                        monitor_state["change_version"] = (
                            int(monitor_state.get("change_version", 0)) + 1
                        )
                        monitor_state["snapshots"] = dict(snapshots)
                    changes.put(
                        {
                            "email": email,
                            "folder": folder.raw_name,
                            "detected_by": "poll",
                            "uidvalidity_changed": current[0] != previous[0],
                            "previous": previous,
                            "current": current,
                        }
                    )
        except (
            SyncError,
            imaplib.IMAP4.error,
            OSError,
            ValueError,
            EOFError,
        ) as exc:
            if not stop_event.is_set():
                print(
                    json.dumps(
                        {
                            "event": "source_monitor_reconnecting",
                            "source": email,
                            "error": str(exc),
                            "seconds": reconnect_delay,
                            "time": now_iso(),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
        finally:
            if client is not None:
                safe_logout(client)
        stop_event.wait(reconnect_delay)
