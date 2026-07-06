from __future__ import annotations

import imaplib
import io
import poplib
import re
import ssl
from datetime import datetime
from email import message_from_bytes
from typing import Dict, List, Optional, Sequence, Tuple

from common.errors import SyncError
from log.events import emit_json
from domain.message import (
    SourceMessage,
    content_hash,
    parse_fetch_tuple,
    parse_flags,
    parse_internal_date,
    source_message_date,
)
from imap.protocol import FolderInfo, parse_list_row, quote_mailbox


UID_RE = re.compile(rb"\bUID (\d+)\b")
RFC822_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)\b")
STATUS_VALUE_RE = re.compile(rb"\b(UIDVALIDITY|UIDNEXT|MESSAGES)\s+(\d+)\b")


class SourceMessageUnavailable(SyncError):
    pass


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


def parse_mailbox_status(rows: Sequence[object]) -> Tuple[int, int, int]:
    payload = b" ".join(row for row in rows if isinstance(row, bytes))
    values = {
        key.decode("ascii"): int(value)
        for key, value in STATUS_VALUE_RE.findall(payload)
    }
    missing = {"UIDVALIDITY", "MESSAGES"} - set(values)
    if missing:
        raise SyncError(
            f"IMAP STATUS omitted values {sorted(missing)}: {payload!r}"
        )
    return (
        values["UIDVALIDITY"],
        values.get("UIDNEXT", 0),
        values["MESSAGES"],
    )


def mailbox_status(
    client: imaplib.IMAP4,
    mailbox: str,
) -> Tuple[int, int, int]:
    typ, rows = client.status(
        quote_mailbox(mailbox),
        "(UIDVALIDITY UIDNEXT MESSAGES)",
    )
    if typ != "OK":
        raise SyncError(f"IMAP STATUS failed for {mailbox}: {rows!r}")
    return parse_mailbox_status(rows or [])


def fetch_message_metadata(
    client: imaplib.IMAP4,
    uids: Sequence[int],
) -> Dict[int, Tuple[Optional[datetime], Tuple[str, ...]]]:
    """Fetch lightweight source metadata without downloading message bodies."""
    results: Dict[int, Tuple[Optional[datetime], Tuple[str, ...]]] = {}
    for offset in range(0, len(uids), 200):
        batch = uids[offset : offset + 200]
        if not batch:
            continue
        typ, rows = client.uid(
            "fetch",
            ",".join(str(uid) for uid in batch),
            "(UID INTERNALDATE FLAGS)",
        )
        if typ != "OK":
            raise SyncError("IMAP metadata FETCH failed")
        for row in rows or []:
            metadata = row[0] if isinstance(row, tuple) else row
            if not isinstance(metadata, bytes):
                continue
            uid_match = UID_RE.search(metadata)
            if uid_match:
                results[int(uid_match.group(1))] = (
                    parse_internal_date(metadata),
                    parse_flags(metadata),
                )
    return results


def fetch_date_header_presence(
    client: imaplib.IMAP4,
    uids: Sequence[int],
) -> Dict[int, bool]:
    """Check Date header presence without downloading complete messages."""
    results: Dict[int, bool] = {}
    for offset in range(0, len(uids), 200):
        batch = uids[offset : offset + 200]
        if not batch:
            continue
        typ, rows = client.uid(
            "fetch",
            ",".join(str(uid) for uid in batch),
            "(UID BODY.PEEK[HEADER.FIELDS (DATE)])",
        )
        if typ != "OK":
            raise SyncError("IMAP Date header FETCH failed")
        for row in rows or []:
            if not isinstance(row, tuple) or len(row) != 2:
                continue
            metadata, header = row
            if not isinstance(metadata, bytes) or not isinstance(header, bytes):
                continue
            uid_match = UID_RE.search(metadata)
            if uid_match:
                results[int(uid_match.group(1))] = (
                    message_from_bytes(header).get("Date") is not None
                )
    return results


def pop_retrieve_raw(client: poplib.POP3, message_number: int) -> bytes:
    """Stream one POP message into a compact bytes buffer."""
    client._putcmd(f"RETR {message_number}")
    client._getresp()
    output = io.BytesIO()
    while True:
        line, _ = client._getline()
        if line == b".":
            break
        if line.startswith(b".."):
            line = line[1:]
        output.write(line)
        output.write(b"\r\n")
    return output.getvalue()


def fetch_pop_message_by_identity(
    profile: dict,
    message_id: str,
    expected_size: int,
) -> bytes:
    settings = profile.get("pop_fallback")
    if not isinstance(settings, dict):
        raise SyncError(f"POP fallback is not configured for {profile['email']}")
    if not profile.get("password"):
        raise SyncError(f"POP fallback requires a password for {profile['email']}")

    host = settings.get("server") or settings.get("host")
    if not host:
        raise SyncError(f"POP fallback server is missing for {profile['email']}")
    port = int(settings.get("port", 995))
    timeout = int(settings.get("timeout", 300))
    use_ssl = bool(settings.get("use_ssl", True))
    client: poplib.POP3
    if use_ssl:
        client = poplib.POP3_SSL(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        client = poplib.POP3(host, port, timeout=timeout)

    try:
        client.user(profile["email"])
        client.pass_(profile["password"])
        _, rows, _ = client.list()
        candidates = []
        for row in rows:
            number_raw, size_raw = row.split()
            if int(size_raw) == expected_size:
                candidates.append(int(number_raw))

        for number in candidates:
            _, header_lines, _ = client.top(number, 0)
            header = b"\r\n".join(header_lines) + b"\r\n\r\n"
            candidate_id = (
                message_from_bytes(header).get("Message-ID", "").strip()
            )
            if candidate_id != message_id:
                continue
            raw = pop_retrieve_raw(client, number)
            if len(raw) != expected_size:
                raise SyncError(
                    f"POP message size changed for {profile['email']}: "
                    f"expected {expected_size}, received {len(raw)}"
                )
            return raw
    finally:
        try:
            client.quit()
        except (poplib.error_proto, OSError, EOFError):
            pass

    raise SyncError(
        f"POP fallback could not find Message-ID {message_id} "
        f"with size {expected_size}"
    )


def fetch_source_message(
    client: imaplib.IMAP4,
    source_email: str,
    folder: str,
    uidvalidity: int,
    uid: int,
    source_profile: Optional[dict] = None,
) -> SourceMessage:
    typ, rows = client.uid(
        "fetch",
        str(uid),
        "(BODY.PEEK[] INTERNALDATE FLAGS)",
    )
    if typ != "OK":
        raise SyncError(f"FETCH failed for {source_email} {folder} UID {uid}")
    try:
        metadata, raw = parse_fetch_tuple(rows or [])
    except SyncError as exc:
        if (
            folder != "INBOX"
            or source_profile is None
            or not source_profile.get("pop_fallback")
        ):
            raise SourceMessageUnavailable(
                f"Source message has no fetchable body: "
                f"{source_email} {folder} UID {uid}"
            ) from exc

        typ, header_rows = client.uid(
            "fetch",
            str(uid),
            (
                "(BODY.PEEK[HEADER.FIELDS "
                "(MESSAGE-ID SUBJECT FROM TO DATE)] "
                "RFC822.SIZE INTERNALDATE FLAGS)"
            ),
        )
        if typ != "OK":
            raise SyncError(
                f"Header FETCH failed for POP fallback: "
                f"{source_email} {folder} UID {uid}"
            )
        try:
            header_metadata, header = parse_fetch_tuple(header_rows or [])
        except SyncError as exc:
            raise SourceMessageUnavailable(
                f"Source message has no fetchable body or headers: "
                f"{source_email} {folder} UID {uid}"
            ) from exc
        parsed_header = message_from_bytes(header)
        message_id = parsed_header.get("Message-ID", "").strip()
        size_match = RFC822_SIZE_RE.search(header_metadata)
        if not message_id or not size_match:
            raise SyncError(
                f"POP fallback requires Message-ID and RFC822.SIZE: "
                f"{source_email} {folder} UID {uid}"
            )
        expected_size = int(size_match.group(1))
        raw = fetch_pop_message_by_identity(
            source_profile,
            message_id,
            expected_size,
        )
        metadata = header_metadata
        emit_json(
            {
                "event": "source_message_pop_fallback",
                "source": source_email,
                "folder": folder,
                "uid": uid,
                "bytes": len(raw),
            }
        )
    parsed = message_from_bytes(raw)
    message_id = parsed.get("Message-ID")
    return SourceMessage(
        source_email=source_email,
        folder=folder,
        uidvalidity=uidvalidity,
        uid=uid,
        raw=raw,
        content_hash=content_hash(raw),
        message_id=message_id.strip() if message_id else None,
        internal_date=source_message_date(metadata, raw),
        system_flags=parse_flags(metadata),
    )
