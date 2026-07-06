from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.utils import format_datetime, parsedate_to_datetime
from typing import Callable, List, Optional, Sequence, Tuple

from common.errors import SyncError


SYSTEM_FLAGS = {
    r"\Seen",
    r"\Answered",
    r"\Flagged",
    r"\Draft",
}
INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
FLAGS_RE = re.compile(rb"FLAGS \((.*?)\)")
HASH_CACHE_LIMIT = 4096


_hash_cache: "OrderedDict[tuple, str]" = OrderedDict()


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


def parse_fetch_tuple(rows: Sequence[object]) -> Tuple[bytes, bytes]:
    metadata_parts = []
    body = None
    for row in rows:
        if isinstance(row, tuple) and len(row) == 2:
            if isinstance(row[0], bytes):
                metadata_parts.append(row[0])
            if body is None and isinstance(row[1], bytes):
                body = row[1]
        elif isinstance(row, bytes):
            # Some IMAP servers return attributes following a BODY literal
            # (notably INTERNALDATE and FLAGS) as a separate response item.
            metadata_parts.append(row)
    if body is None:
        raise SyncError("IMAP FETCH did not return a message body")
    return b" ".join(metadata_parts), body


def parse_internal_date(metadata: bytes) -> Optional[datetime]:
    match = INTERNALDATE_RE.search(metadata)
    if not match:
        return None
    return datetime.strptime(
        match.group(1).decode("ascii"),
        "%d-%b-%Y %H:%M:%S %z",
    )


def parse_message_date(raw: bytes) -> Optional[datetime]:
    """Return the RFC message date when a server omits IMAP INTERNALDATE."""
    value = message_from_bytes(raw).get("Date")
    if not value:
        return None
    try:
        # Compat32 can return email.header.Header for malformed or programmatically
        # constructed messages; parsedate_to_datetime requires a string.
        parsed = parsedate_to_datetime(str(value))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def source_message_date(metadata: bytes, raw: bytes) -> Optional[datetime]:
    return parse_internal_date(metadata) or parse_message_date(raw)


def is_qq_source(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].casefold()
    return domain in {"qq.com", "foxmail.com"}


def needs_qq_date_header(message: SourceMessage) -> bool:
    """Identify QQ system messages whose missing Date confuses MailPlus."""
    parsed = message_from_bytes(message.raw)
    return parsed.get("Date") is None and (
        is_qq_source(message.source_email)
        or parsed.get("X-QQ-mid") is not None
    )


def target_message_raw(message: SourceMessage) -> bytes:
    """Return the deterministic representation written to the target mailbox."""
    if message.internal_date is None or not needs_qq_date_header(message):
        return message.raw
    date_header = f"Date: {format_datetime(message.internal_date)}\r\n".encode(
        "ascii"
    )
    return date_header + message.raw


def parse_flags(metadata: bytes) -> Tuple[str, ...]:
    match = FLAGS_RE.search(metadata)
    if not match:
        return ()
    flags = [
        value.decode("ascii", "replace")
        for value in match.group(1).split()
    ]
    return tuple(sorted(flag for flag in flags if flag in SYSTEM_FLAGS))


def normalize_message_content(raw: bytes) -> bytes:
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    # Synology MailPlus rewrites NUL bytes in APPENDed messages as 0x80.
    return normalized.replace(b"\x00", b"\x80")


def normalized_content_hash(raw: bytes) -> str:
    return hashlib.sha256(normalize_message_content(raw)).hexdigest()


def content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _cached_hash(key: tuple, compute: Callable[[], str]) -> str:
    cached = _hash_cache.get(key)
    if cached is not None:
        _hash_cache.move_to_end(key)
        return cached
    value = compute()
    _hash_cache[key] = value
    if len(_hash_cache) > HASH_CACHE_LIMIT:
        _hash_cache.popitem(last=False)
    return value


def _message_cache_key(message: SourceMessage, kind: str) -> tuple:
    return (
        kind,
        message.source_email,
        message.content_hash,
        message.internal_date.isoformat() if message.internal_date else None,
    )


def source_normalized_hash(message: SourceMessage) -> str:
    return _cached_hash(
        _message_cache_key(message, "source-normalized"),
        lambda: normalized_content_hash(message.raw),
    )


def target_content_hash(message: SourceMessage) -> str:
    return _cached_hash(
        _message_cache_key(message, "target-content"),
        lambda: content_hash(target_message_raw(message)),
    )


def target_normalized_hash(message: SourceMessage) -> str:
    return _cached_hash(
        _message_cache_key(message, "target-normalized"),
        lambda: normalized_content_hash(target_message_raw(message)),
    )


def content_hash_matches(
    raw: bytes,
    expected_hash: str,
    expected_raw: Optional[bytes] = None,
) -> bool:
    """Compare message content while tolerating IMAP CRLF normalization."""
    if content_hash(raw) == expected_hash:
        return True
    if expected_raw is not None:
        return normalize_message_content(raw) == normalize_message_content(expected_raw)
    return normalized_content_hash(raw) == expected_hash
