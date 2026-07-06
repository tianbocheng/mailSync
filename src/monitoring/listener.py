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


def monitor_inbox_idle_changes(
    profile: dict,
    source_rule: dict,
    only_folder: Optional[str],
    changes: "queue.Queue[dict]",
    stop_event: threading.Event,
    idle_restart_seconds: float = DEFAULT_IDLE_RESTART_SECONDS,
) -> None:
    if not _only_inbox_requested(only_folder):
        raise IdleUnsupported(
            "IMAP IDLE realtime monitor only watches INBOX for this sync mode"
        )

    email = source_rule["email"]
    reconnect_delay = 2.0
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
            print(
                json.dumps(
                    {
                        "event": "source_idle_monitor_ready",
                        "source": email,
                        "folder": folder.raw_name,
                        "idle_restart_seconds": idle_restart_seconds,
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
                    remaining,
                )
                if stop_event.is_set():
                    break
                if not changed:
                    continue
                current = mailbox_status(client, folder.raw_name)
                if current == previous:
                    continue
                changes.put(
                    {
                        "email": email,
                        "folder": folder.raw_name,
                        "uidvalidity_changed": (
                            current[0] != previous[0]
                            or current[0] != selected_uidvalidity
                        ),
                        "previous": previous,
                        "current": current,
                    }
                )
                previous = current
        except IdleUnsupported:
            raise
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
) -> None:
    try:
        monitor_inbox_idle_changes(
            profile,
            source_rule,
            only_folder,
            changes,
            stop_event,
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
        )


def monitor_source_changes(
    profile: dict,
    source_rule: dict,
    only_folder: Optional[str],
    poll_interval: float,
    changes: "queue.Queue[dict]",
    stop_event: threading.Event,
) -> None:
    email = source_rule["email"]
    reconnect_delay = max(2.0, poll_interval)
    while not stop_event.is_set():
        client: Optional[imaplib.IMAP4] = None
        try:
            client = connect(profile)
            folders = selectable_source_folders(
                list_folders(client),
                source_rule,
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
                    changes.put(
                        {
                            "email": email,
                            "folder": folder.raw_name,
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
