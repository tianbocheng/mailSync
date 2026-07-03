#!/usr/bin/env python3
"""Incrementally mirror source IMAP mail into one target INBOX.

Synology MailPlus label removals are not reliably reflected by standard IMAP.
When a source message changes folders, this synchronizer therefore appends and
verifies a fresh target copy with the desired labels before deleting the old
target copy.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import imaplib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


SYSTEM_FLAGS = {
    r"\Seen",
    r"\Answered",
    r"\Flagged",
    r"\Draft",
}
LIST_RE = re.compile(
    rb"^\((?P<flags>[^)]*)\)\s+"
    rb"(?P<delimiter>NIL|\"(?:[^\"\\]|\\.)*\")\s+"
    rb"(?P<mailbox>.+)$"
)
INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
FLAGS_RE = re.compile(rb"FLAGS \((.*?)\)")
UID_RE = re.compile(rb"\bUID (\d+)\b")


@dataclass(frozen=True)
class FolderInfo:
    raw_name: str
    display_name: str
    attributes: Tuple[str, ...]


@dataclass(frozen=True)
class SourceMessage:
    source_email: str
    folder: str
    uidvalidity: int
    uid: int
    raw: bytes
    content_hash: str
    message_id: Optional[str]
    internal_date: Optional[datetime]
    system_flags: Tuple[str, ...]


class SyncError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def quote_mailbox(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def decode_modified_utf7(value: str) -> str:
    """Decode IMAP modified UTF-7 without third-party dependencies."""
    import base64

    result: List[str] = []
    index = 0
    while index < len(value):
        amp = value.find("&", index)
        if amp < 0:
            result.append(value[index:])
            break
        result.append(value[index:amp])
        end = value.find("-", amp)
        if end < 0:
            result.append(value[amp:])
            break
        token = value[amp + 1 : end]
        if not token:
            result.append("&")
        else:
            padded = token.replace(",", "/")
            padded += "=" * ((4 - len(padded) % 4) % 4)
            result.append(base64.b64decode(padded).decode("utf-16-be"))
        index = end + 1
    return "".join(result)


def unquote_imap(value: bytes) -> str:
    value = value.strip()
    if value.startswith(b'"') and value.endswith(b'"'):
        value = value[1:-1]
        value = re.sub(rb"\\(.)", rb"\1", value)
    return value.decode("ascii", "strict")


def parse_list_row(row: bytes) -> FolderInfo:
    match = LIST_RE.match(row)
    if not match:
        raise SyncError(f"Unrecognized IMAP LIST row: {row!r}")
    raw_name = unquote_imap(match.group("mailbox"))
    attributes = tuple(
        item.decode("ascii", "replace")
        for item in match.group("flags").split()
    )
    return FolderInfo(
        raw_name=raw_name,
        display_name=decode_modified_utf7(raw_name),
        attributes=attributes,
    )


def connect(profile: dict) -> imaplib.IMAP4:
    host = profile["imap_server"]
    port = int(profile.get("imap_port", 993 if profile.get("use_ssl", True) else 143))
    if profile.get("use_ssl", True):
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port)
    else:
        client = imaplib.IMAP4(host, port)
    typ, _ = client.login(profile["email"], profile["password"])
    if typ != "OK":
        raise SyncError(f"IMAP login failed for {profile['email']}")
    identity = profile.get("imap_id")
    if identity:
        fields = []
        for key, value in identity.items():
            escaped_key = str(key).replace("\\", "\\\\").replace('"', '\\"')
            escaped_value = str(value).replace("\\", "\\\\").replace('"', '\\"')
            fields.extend((f'"{escaped_key}"', f'"{escaped_value}"'))
        typ, response = client.xatom("ID", "(" + " ".join(fields) + ")")
        if typ != "OK":
            raise SyncError(
                f"IMAP ID rejected for {profile['email']}: {response!r}"
            )
    return client


def safe_logout(client: imaplib.IMAP4) -> None:
    try:
        client.logout()
    except (imaplib.IMAP4.error, OSError, EOFError):
        pass


def credential_profiles(config: dict) -> Dict[str, dict]:
    profiles: Dict[str, dict] = {}
    target = config.get("target")
    if target and target.get("email"):
        profiles[target["email"]] = dict(target)
    for candidate in config.get("sources", []):
        email = candidate.get("email")
        if not email:
            continue
        if email in profiles:
            raise SyncError(f"Duplicate mailbox configuration for {email}")
        profiles[email] = dict(candidate)
    return profiles


def list_folders(client: imaplib.IMAP4) -> List[FolderInfo]:
    typ, rows = client.list()
    if typ != "OK":
        raise SyncError("IMAP LIST failed")
    return [parse_list_row(row) for row in rows or []]


def selectable_source_folders(
    folders: Sequence[FolderInfo],
    source_rule: dict,
) -> List[FolderInfo]:
    excluded = {name.casefold() for name in source_rule.get("exclude_folders", [])}
    selected = []
    for folder in folders:
        attrs = {attr.casefold() for attr in folder.attributes}
        if r"\noselect" in attrs or r"\drafts" in attrs:
            continue
        if folder.raw_name.casefold() in excluded or folder.display_name.casefold() in excluded:
            continue
        selected.append(folder)
    return selected


def response_int(client: imaplib.IMAP4, name: str) -> int:
    values = client.untagged_responses.get(name.upper(), [])
    if not values:
        typ, response = client.response(name)
        values = response or []
    if not values:
        raise SyncError(f"Server did not return {name}")
    match = re.search(rb"\d+", values[-1])
    if not match:
        raise SyncError(f"Invalid {name}: {values[-1]!r}")
    return int(match.group())


def parse_fetch_tuple(rows: Sequence[object]) -> Tuple[bytes, bytes]:
    for row in rows:
        if isinstance(row, tuple) and len(row) == 2:
            return row[0], row[1]
    raise SyncError("IMAP FETCH did not return a message body")


def parse_internal_date(metadata: bytes) -> Optional[datetime]:
    match = INTERNALDATE_RE.search(metadata)
    if not match:
        return None
    return datetime.strptime(
        match.group(1).decode("ascii"),
        "%d-%b-%Y %H:%M:%S %z",
    )


def parse_flags(metadata: bytes) -> Tuple[str, ...]:
    match = FLAGS_RE.search(metadata)
    if not match:
        return ()
    flags = [
        value.decode("ascii", "replace")
        for value in match.group(1).split()
    ]
    return tuple(sorted(flag for flag in flags if flag in SYSTEM_FLAGS))


def fetch_source_message(
    client: imaplib.IMAP4,
    source_email: str,
    folder: str,
    uidvalidity: int,
    uid: int,
) -> SourceMessage:
    typ, rows = client.uid(
        "fetch",
        str(uid),
        "(BODY.PEEK[] INTERNALDATE FLAGS)",
    )
    if typ != "OK":
        raise SyncError(f"FETCH failed for {source_email} {folder} UID {uid}")
    metadata, raw = parse_fetch_tuple(rows or [])
    parsed = message_from_bytes(raw)
    message_id = parsed.get("Message-ID")
    return SourceMessage(
        source_email=source_email,
        folder=folder,
        uidvalidity=uidvalidity,
        uid=uid,
        raw=raw,
        content_hash=hashlib.sha256(raw).hexdigest(),
        message_id=message_id.strip() if message_id else None,
        internal_date=parse_internal_date(metadata),
        system_flags=parse_flags(metadata),
    )


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            source_email TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            message_id TEXT,
            source_folder TEXT NOT NULL,
            source_uidvalidity INTEGER NOT NULL,
            source_uid INTEGER NOT NULL,
            source_flags TEXT NOT NULL,
            internal_date TEXT,
            target_uidvalidity INTEGER NOT NULL,
            target_uid INTEGER NOT NULL,
            account_label TEXT NOT NULL,
            folder_label TEXT NOT NULL,
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
            old_target_uid INTEGER,
            target_uidvalidity INTEGER NOT NULL,
            target_uid_before INTEGER NOT NULL,
            new_target_uid INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_email, content_hash)
        );
        """
    )
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "message_id" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_mailbox(
    client: imaplib.IMAP4,
    folder: str,
    readonly: bool,
) -> Tuple[int, int]:
    typ, data = client.select(quote_mailbox(folder), readonly=readonly)
    if typ != "OK":
        raise SyncError(f"Cannot select mailbox {folder}")
    count = int(data[0]) if data and data[0] else 0
    return count, response_int(client, "UIDVALIDITY")


def search_uids(client: imaplib.IMAP4, criteria: str = "ALL") -> List[int]:
    typ, data = client.uid("search", None, criteria)
    if typ != "OK":
        raise SyncError(f"UID SEARCH {criteria} failed")
    return [int(value) for value in (data[0].split() if data and data[0] else [])]


def label_id_map(raw: dict) -> Dict[str, str]:
    inverse: Dict[str, str] = {}
    for internal_id, display_name in raw.items():
        if display_name in inverse:
            raise SyncError(f"Duplicate label name in mapping: {display_name}")
        inverse[display_name] = internal_id
    return inverse


def source_folder_label(source_rule: dict, folder: FolderInfo) -> str:
    mapping = source_rule.get("folder_labels", {})
    return (
        mapping.get(folder.raw_name)
        or mapping.get(folder.display_name)
        or source_rule["default_folder_label"]
    )


def format_flag_list(flags: Iterable[str]) -> str:
    return "(" + " ".join(sorted(set(flags))) + ")"


def target_fetch(
    client: imaplib.IMAP4,
    uid: int,
    include_body: bool = False,
) -> Tuple[bytes, Optional[bytes]]:
    query = "(FLAGS BODY.PEEK[])" if include_body else "(FLAGS)"
    typ, rows = client.uid("fetch", str(uid), query)
    if typ != "OK":
        raise SyncError(f"Target FETCH failed for UID {uid}")
    if include_body:
        metadata, raw = parse_fetch_tuple(rows or [])
        return metadata, raw
    for row in rows or []:
        if isinstance(row, bytes) and UID_RE.search(row):
            return row, None
    raise SyncError(f"Target UID {uid} does not exist")


def append_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    label_ids: Sequence[str],
) -> Tuple[int, int]:
    _, uidvalidity = select_mailbox(client, target_folder, readonly=False)
    before = set(search_uids(client))
    system_flags = format_flag_list(message.system_flags)
    typ, response = client.append(
        quote_mailbox(target_folder),
        system_flags,
        message.internal_date,
        message.raw,
    )
    if typ != "OK":
        raise SyncError(f"APPEND failed: {response!r}")
    after = set(search_uids(client))
    created = sorted(after - before)
    if len(created) != 1:
        raise SyncError(f"Expected one appended UID, found {created}")
    uid = created[0]
    for label_id in label_ids:
        typ, response = client.uid(
            "store",
            str(uid),
            "+FLAGS.SILENT",
            f"({label_id})",
        )
        if typ != "OK":
            raise SyncError(f"Failed adding {label_id} to target UID {uid}: {response!r}")
    metadata, raw = target_fetch(client, uid, include_body=True)
    if raw is None or hashlib.sha256(raw).hexdigest() != message.content_hash:
        raise SyncError(f"Target UID {uid} failed content verification")
    current_flags = set(
        value.decode("ascii", "replace")
        for value in (FLAGS_RE.search(metadata).group(1).split() if FLAGS_RE.search(metadata) else [])
    )
    missing = set(label_ids) - current_flags
    if missing:
        raise SyncError(f"Target UID {uid} is missing labels: {sorted(missing)}")
    return uidvalidity, uid


def ensure_no_preexisting_deleted(client: imaplib.IMAP4, old_uid: int) -> None:
    deleted = set(search_uids(client, "DELETED"))
    unexpected = deleted - {old_uid}
    if unexpected:
        raise SyncError(
            "Target mailbox contains unrelated messages already marked Deleted; "
            f"refusing broad EXPUNGE: {sorted(unexpected)}"
        )


def delete_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    old_uid: int,
) -> None:
    select_mailbox(client, target_folder, readonly=False)
    ensure_no_preexisting_deleted(client, old_uid)
    existing = set(search_uids(client))
    if old_uid not in existing:
        return
    typ, response = client.uid(
        "store",
        str(old_uid),
        "+FLAGS.SILENT",
        r"(\Deleted)",
    )
    if typ != "OK":
        raise SyncError(f"Could not mark target UID {old_uid} deleted: {response!r}")
    typ, response = client.expunge()
    if typ != "OK":
        raise SyncError(f"EXPUNGE failed: {response!r}")


def find_untracked_target_by_message_id(
    db: sqlite3.Connection,
    client: imaplib.IMAP4,
    target_folder: str,
    message_id: Optional[str],
) -> Optional[int]:
    if not message_id:
        return None
    select_mailbox(client, target_folder, readonly=False)
    typ, data = client.uid(
        "search",
        None,
        "HEADER",
        "Message-ID",
        message_id,
    )
    if typ != "OK":
        raise SyncError(f"Target Message-ID search failed for {message_id}")
    candidates = [
        int(value)
        for value in (data[0].split() if data and data[0] else [])
    ]
    for uid in candidates:
        tracked = db.execute(
            "SELECT 1 FROM messages WHERE target_uid=?",
            (uid,),
        ).fetchone()
        if tracked is None:
            return uid
    return None


def begin_pending_operation(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    account_label: str,
    folder_label: str,
    old_target_uid: Optional[int],
) -> None:
    _, target_uidvalidity = select_mailbox(target, target_folder, readonly=False)
    target_uids = search_uids(target)
    target_uid_before = max(target_uids, default=0)
    timestamp = now_iso()
    db.execute(
        """
        INSERT INTO pending_operations (
            source_email, content_hash, message_id, source_folder,
            source_uidvalidity, source_uid, source_flags, internal_date,
            raw_message, account_label, folder_label, old_target_uid,
            target_uidvalidity, target_uid_before, new_target_uid,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(source_email, content_hash) DO UPDATE SET
            message_id=excluded.message_id,
            source_folder=excluded.source_folder,
            source_uidvalidity=excluded.source_uidvalidity,
            source_uid=excluded.source_uid,
            source_flags=excluded.source_flags,
            internal_date=excluded.internal_date,
            raw_message=excluded.raw_message,
            account_label=excluded.account_label,
            folder_label=excluded.folder_label,
            old_target_uid=excluded.old_target_uid,
            target_uidvalidity=excluded.target_uidvalidity,
            target_uid_before=excluded.target_uid_before,
            new_target_uid=NULL,
            updated_at=excluded.updated_at
        """,
        (
            message.source_email,
            message.content_hash,
            message.message_id,
            message.folder,
            message.uidvalidity,
            message.uid,
            json.dumps(message.system_flags),
            message.internal_date.isoformat() if message.internal_date else None,
            message.raw,
            account_label,
            folder_label,
            old_target_uid,
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


def metadata_flags(metadata: bytes) -> Set[str]:
    match = FLAGS_RE.search(metadata)
    if not match:
        return set()
    return {
        value.decode("ascii", "replace")
        for value in match.group(1).split()
    }


def target_copy_matches(
    client: imaplib.IMAP4,
    uid: int,
    content_hash: str,
    label_ids: Sequence[str],
) -> bool:
    try:
        metadata, raw = target_fetch(client, uid, include_body=True)
    except SyncError:
        return False
    return (
        raw is not None
        and hashlib.sha256(raw).hexdigest() == content_hash
        and set(label_ids).issubset(metadata_flags(metadata))
    )


def repair_target_copy(
    client: imaplib.IMAP4,
    uid: int,
    content_hash: str,
    label_ids: Sequence[str],
) -> bool:
    try:
        metadata, raw = target_fetch(client, uid, include_body=True)
    except SyncError:
        return False
    if raw is None or hashlib.sha256(raw).hexdigest() != content_hash:
        return False
    missing = set(label_ids) - metadata_flags(metadata)
    for label_id in sorted(missing):
        typ, response = client.uid(
            "store",
            str(uid),
            "+FLAGS.SILENT",
            f"({label_id})",
        )
        if typ != "OK":
            raise SyncError(
                f"Failed repairing {label_id} on target UID {uid}: {response!r}"
            )
    return target_copy_matches(client, uid, content_hash, label_ids)


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


def recover_pending_operations(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    labels_by_name: Dict[str, str],
) -> int:
    rows = db.execute(
        "SELECT * FROM pending_operations ORDER BY created_at"
    ).fetchall()
    recovered = 0
    for row in rows:
        message = pending_row_message(row)
        try:
            label_ids = [
                labels_by_name[row["account_label"]],
                labels_by_name[row["folder_label"]],
            ]
        except KeyError as exc:
            raise SyncError(
                f"Cannot recover pending operation; undefined label: {exc.args[0]}"
            ) from exc

        _, current_uidvalidity = select_mailbox(
            target,
            target_folder,
            readonly=False,
        )
        if current_uidvalidity != int(row["target_uidvalidity"]):
            raise SyncError(
                "Target UIDVALIDITY changed while an operation was pending; "
                "manual recovery is required"
            )

        new_uid = int(row["new_target_uid"]) if row["new_target_uid"] else None
        if new_uid is not None and not repair_target_copy(
            target,
            new_uid,
            message.content_hash,
            label_ids,
        ):
            new_uid = None

        if new_uid is None:
            candidates = [
                uid
                for uid in search_uids(target)
                if uid > int(row["target_uid_before"])
            ]
            for candidate in reversed(candidates):
                if repair_target_copy(
                    target,
                    candidate,
                    message.content_hash,
                    label_ids,
                ):
                    new_uid = candidate
                    update_pending_target_uid(db, message, candidate)
                    break

        if new_uid is None:
            _, new_uid = append_target_copy(
                target,
                target_folder,
                message,
                label_ids,
            )
            update_pending_target_uid(db, message, new_uid)

        old_uid = int(row["old_target_uid"]) if row["old_target_uid"] else None
        if old_uid is not None and old_uid != new_uid:
            delete_target_copy(target, target_folder, old_uid)

        upsert_state(
            db,
            message,
            current_uidvalidity,
            new_uid,
            row["account_label"],
            row["folder_label"],
            "recovered-" + uuid.uuid4().hex,
        )
        db.execute(
            """
            DELETE FROM pending_operations
            WHERE source_email=? AND content_hash=?
            """,
            (message.source_email, message.content_hash),
        )
        db.commit()
        recovered += 1
        print(
            json.dumps(
                {
                    "action": "pending_operation_recovered",
                    "source": message.source_email,
                    "target_uid": new_uid,
                },
                ensure_ascii=False,
            )
        )
    return recovered


def replace_target_copy(
    client: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    label_ids: Sequence[str],
    old_uid: int,
) -> Tuple[int, int]:
    select_mailbox(client, target_folder, readonly=False)
    ensure_no_preexisting_deleted(client, old_uid)
    target_uidvalidity, new_uid = append_target_copy(
        client,
        target_folder,
        message,
        label_ids,
    )
    try:
        delete_target_copy(client, target_folder, old_uid)
    except Exception:
        # Keep the verified new copy. The state is not updated, so a later run
        # will not silently forget the old UID and can report the duplicate.
        raise
    return target_uidvalidity, new_uid


def upsert_state(
    db: sqlite3.Connection,
    message: SourceMessage,
    target_uidvalidity: int,
    target_uid: int,
    account_label: str,
    folder_label: str,
    run_id: str,
) -> None:
    db.execute(
        """
        INSERT INTO messages (
            source_email, content_hash, message_id, source_folder, source_uidvalidity,
            source_uid, source_flags, internal_date, target_uidvalidity,
            target_uid, account_label, folder_label, last_seen_run, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_email, content_hash) DO UPDATE SET
            message_id=excluded.message_id,
            source_folder=excluded.source_folder,
            source_uidvalidity=excluded.source_uidvalidity,
            source_uid=excluded.source_uid,
            source_flags=excluded.source_flags,
            internal_date=excluded.internal_date,
            target_uidvalidity=excluded.target_uidvalidity,
            target_uid=excluded.target_uid,
            account_label=excluded.account_label,
            folder_label=excluded.folder_label,
            last_seen_run=excluded.last_seen_run,
            updated_at=excluded.updated_at
        """,
        (
            message.source_email,
            message.content_hash,
            message.message_id,
            message.folder,
            message.uidvalidity,
            message.uid,
            json.dumps(message.system_flags),
            message.internal_date.isoformat() if message.internal_date else None,
            target_uidvalidity,
            target_uid,
            account_label,
            folder_label,
            run_id,
            now_iso(),
        ),
    )


def update_seen_location(
    db: sqlite3.Connection,
    message: SourceMessage,
    run_id: str,
) -> None:
    db.execute(
        """
        UPDATE messages SET
            source_folder=?, source_uidvalidity=?, source_uid=?,
            source_flags=?, internal_date=?, last_seen_run=?, updated_at=?
        WHERE source_email=? AND content_hash=?
        """,
        (
            message.folder,
            message.uidvalidity,
            message.uid,
            json.dumps(message.system_flags),
            message.internal_date.isoformat() if message.internal_date else None,
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
        SELECT * FROM messages
        WHERE source_email=? AND source_folder=?
          AND source_uidvalidity=? AND source_uid=?
        """,
        (source_email, folder, uidvalidity, uid),
    ).fetchone()


def sync_message(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    message: SourceMessage,
    account_label_name: str,
    folder_label_name: str,
    labels_by_name: Dict[str, str],
    run_id: str,
) -> str:
    try:
        label_ids = [
            labels_by_name[account_label_name],
            labels_by_name[folder_label_name],
        ]
    except KeyError as exc:
        raise SyncError(f"Undefined Synology label: {exc.args[0]}") from exc

    existing = db.execute(
        "SELECT * FROM messages WHERE source_email=? AND content_hash=?",
        (message.source_email, message.content_hash),
    ).fetchone()
    old_target_uid: Optional[int] = None
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
        labels_changed = (
            existing["account_label"] != account_label_name
            or existing["folder_label"] != folder_label_name
        )
        target_location_changed = (
            existing["source_folder"] != message.folder
            or existing["source_uidvalidity"] != message.uidvalidity
            or existing["source_uid"] != message.uid
        )
        if labels_changed:
            old_target_uid = int(existing["target_uid"])
            action = "rebuilt"
        else:
            target_uidvalidity = int(existing["target_uidvalidity"])
            target_uid = int(existing["target_uid"])
            action = "relocated" if target_location_changed else "unchanged"
            upsert_state(
                db,
                message,
                target_uidvalidity,
                target_uid,
                account_label_name,
                folder_label_name,
                run_id,
            )
            db.commit()
            return action

    if old_target_uid is not None:
        select_mailbox(target, target_folder, readonly=False)
        ensure_no_preexisting_deleted(target, old_target_uid)
    begin_pending_operation(
        db,
        target,
        target_folder,
        message,
        account_label_name,
        folder_label_name,
        old_target_uid,
    )
    target_uidvalidity, target_uid = append_target_copy(
        target,
        target_folder,
        message,
        label_ids,
    )
    update_pending_target_uid(db, message, target_uid)
    if old_target_uid is not None:
        delete_target_copy(target, target_folder, old_target_uid)
    upsert_state(
        db,
        message,
        target_uidvalidity,
        target_uid,
        account_label_name,
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


def reconcile_deleted_messages(
    db: sqlite3.Connection,
    target: imaplib.IMAP4,
    target_folder: str,
    source_rule: dict,
    labels_by_name: Dict[str, str],
    run_id: str,
) -> int:
    """Replace labels for messages no longer present in any scanned folder."""
    source_email = source_rule["email"]
    deleted_label_name = source_rule["deleted_label"]
    account_label_name = source_rule["account_label"]
    missing = db.execute(
        """
        SELECT * FROM messages
        WHERE source_email=? AND last_seen_run<>?
        """,
        (source_email, run_id),
    ).fetchall()
    rebuilt = 0
    for row in missing:
        if row["source_folder"] == "__deleted__":
            db.execute(
                """
                UPDATE messages SET last_seen_run=?, updated_at=?
                WHERE source_email=? AND content_hash=?
                """,
                (run_id, now_iso(), source_email, row["content_hash"]),
            )
            continue

        if row["folder_label"] == deleted_label_name:
            db.execute(
                """
                UPDATE messages SET
                    source_folder='__deleted__', source_uidvalidity=0,
                    source_uid=0, last_seen_run=?, updated_at=?
                WHERE source_email=? AND content_hash=?
                """,
                (run_id, now_iso(), source_email, row["content_hash"]),
            )
            continue

        select_mailbox(target, target_folder, readonly=False)
        metadata, raw = target_fetch(
            target,
            int(row["target_uid"]),
            include_body=True,
        )
        if raw is None:
            raise SyncError(
                f"Cannot rebuild deleted source message {row['content_hash']}: "
                "target body is missing"
            )
        content_hash = hashlib.sha256(raw).hexdigest()
        if content_hash != row["content_hash"]:
            raise SyncError(
                f"Target UID {row['target_uid']} content hash changed; "
                "refusing deletion reconciliation"
            )
        internal_date = (
            datetime.fromisoformat(row["internal_date"])
            if row["internal_date"]
            else parse_internal_date(metadata)
        )
        message = SourceMessage(
            source_email=source_email,
            folder="__deleted__",
            uidvalidity=0,
            uid=0,
            raw=raw,
            content_hash=content_hash,
            message_id=(
                message_from_bytes(raw).get("Message-ID", "").strip() or None
            ),
            internal_date=internal_date,
            system_flags=tuple(json.loads(row["source_flags"])),
        )
        sync_message(
            db,
            target,
            target_folder,
            message,
            account_label_name,
            deleted_label_name,
            labels_by_name,
            run_id,
        )
        rebuilt += 1
        print(
            json.dumps(
                {
                    "action": "source_deleted_rebuilt",
                    "source": source_email,
                    "content_hash": content_hash,
                },
                ensure_ascii=False,
            )
        )
    db.commit()
    return rebuilt


def source_rule_for(config: dict, email: str) -> dict:
    for rule in config.get("sources", []):
        if rule.get("email") == email:
            return rule
    raise SyncError(f"No source rule for {email}")


def selected_source_rules(config: dict, email: Optional[str]) -> List[dict]:
    rules = config.get("sources", [])
    if email:
        return [source_rule_for(config, email)]
    return list(rules)


def command_list_folders(
    config: dict,
    profiles: Dict[str, dict],
    source_email: Optional[str],
) -> int:
    for rule in selected_source_rules(config, source_email):
        email = rule["email"]
        client = connect(profiles[email])
        try:
            folders = list_folders(client)
            selected = {item.raw_name for item in selectable_source_folders(folders, rule)}
            print(f"{email}:")
            for folder in folders:
                status = "sync" if folder.raw_name in selected else "excluded"
                attrs = " ".join(folder.attributes) or "-"
                print(
                    f"  [{status:8}] {folder.display_name} "
                    f"(raw={folder.raw_name!r}, attrs={attrs})"
                )
        finally:
            safe_logout(client)
    return 0


def command_sync(
    config: dict,
    profiles: Dict[str, dict],
    labels_by_name: Dict[str, str],
    state_path: Path,
    source_email: Optional[str],
    only_folder: Optional[str],
    limit: Optional[int],
    newest_first: bool,
) -> int:
    db = init_db(state_path)
    target_rule = config["target"]
    target_profile = profiles[target_rule["email"]]
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
                        if known is not None:
                            db.execute(
                                """
                                UPDATE messages SET last_seen_run=?, updated_at=?
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
                            action = "unchanged"
                        else:
                            if limit is not None and source_changed >= limit:
                                source_scan_complete = False
                                break
                            message = fetch_source_message(
                                source,
                                email,
                                folder.raw_name,
                                uidvalidity,
                                uid,
                            )
                            action = sync_message(
                                db,
                                target,
                                target_rule["folder"],
                                message,
                                rule["account_label"],
                                source_folder_label(rule, folder),
                                labels_by_name,
                                run_id,
                            )
                            source_changed += 1
                            total_changed += 1
                        summary[action] = summary.get(action, 0) + 1
                        total_scanned += 1
                        if action != "unchanged":
                            print(
                                json.dumps(
                                    {
                                        "action": action,
                                        "source": email,
                                        "folder": folder.display_name,
                                        "uid": uid,
                                    },
                                    ensure_ascii=False,
                                )
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

        print(
            json.dumps(
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
                },
                ensure_ascii=False,
            )
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
    max_cycles: Optional[int],
) -> int:
    rules = selected_source_rules(config, source_email)
    if not rules:
        raise SyncError("No source mailboxes are configured")

    slot_seconds = interval / len(rules)
    started_at = time.monotonic()
    schedule = []
    for index, rule in enumerate(rules):
        source_interval = int(rule.get("sync_interval", interval))
        if source_interval < 1:
            raise SyncError(
                f"sync_interval for {rule['email']} must be at least 1 second"
            )
        schedule.append(
            {
                "email": rule["email"],
                "interval": source_interval,
                "batch_size": int(
                    rule.get(
                        "batch_size",
                        os.environ.get("SYNC_BATCH_SIZE", "5"),
                    )
                ),
                "next_run": started_at + index * slot_seconds,
            }
        )
        if schedule[-1]["batch_size"] < 1:
            raise SyncError(
                f"batch_size for {rule['email']} must be at least 1"
            )

    cycle = 0
    while True:
        job = min(schedule, key=lambda item: item["next_run"])
        delay = job["next_run"] - time.monotonic()
        if delay > 0:
            time.sleep(delay)

        cycle += 1
        print(
            json.dumps(
                {
                    "event": "source_sync_started",
                    "cycle": cycle,
                    "source": job["email"],
                    "interval": job["interval"],
                    "batch_size": job["batch_size"],
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
                job["email"],
                only_folder,
                limit if limit is not None else job["batch_size"],
                newest_first,
            )
            success = True
        except (SyncError, imaplib.IMAP4.error, OSError, ValueError) as exc:
            success = False
            print(
                json.dumps(
                    {
                        "event": "source_sync_failed",
                        "cycle": cycle,
                        "source": job["email"],
                        "time": now_iso(),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )

        if max_cycles is not None and cycle >= max_cycles:
            return 0 if success else 1

        job["next_run"] = time.monotonic() + job["interval"]
        next_job = min(schedule, key=lambda item: item["next_run"])
        print(
            json.dumps(
                {
                    "event": "next_source_sync_scheduled",
                    "source": next_job["email"],
                    "seconds": round(
                        max(0.0, next_job["next_run"] - time.monotonic()),
                        3,
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="config.json",
        help="Configuration file (default: config.json)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list-folders",
        help="List source folders and show which ones will sync",
    )
    list_parser.add_argument("--source", help="Only this source email")

    sync_parser = subparsers.add_parser("sync", help="Run incremental synchronization")
    sync_parser.add_argument("--source", help="Only this source email")
    sync_parser.add_argument("--folder", help="Only this source folder")
    sync_parser.add_argument("--limit", type=int, help="Maximum messages to process")
    sync_parser.add_argument(
        "--newest-first",
        action="store_true",
        help="Process higher source UIDs first",
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
        default=int(os.environ.get("SYNC_INTERVAL", "60")),
        help="Seconds between cycle start times (default: SYNC_INTERVAL or 60)",
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
    state_path = resolve_path(
        base,
        os.environ.get("MAIL_SYNC_STATE_DB", config["state_db"]),
    )
    profiles = credential_profiles(config)
    labels_by_name = label_id_map(config["synology_labels"])

    required_emails = {config["target"]["email"]}
    required_emails.update(rule["email"] for rule in config.get("sources", []))
    missing = required_emails - set(profiles)
    if missing:
        raise SyncError(f"Missing credentials for: {sorted(missing)}")

    if args.command == "list-folders":
        return command_list_folders(config, profiles, args.source)
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
    if args.command == "watch":
        if args.interval < 1:
            raise SyncError("--interval must be at least 1 second")
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
