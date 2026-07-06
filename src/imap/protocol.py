from __future__ import annotations

import imaplib
import re
from dataclasses import dataclass
from typing import List, Tuple

from common.errors import SyncError


UID_RE = re.compile(rb"\bUID (\d+)\b")
LIST_RE = re.compile(
    rb"^\((?P<flags>[^)]*)\)\s+"
    rb"(?P<delimiter>NIL|\"(?:[^\"\\]|\\.)*\")\s+"
    rb"(?P<mailbox>.+)$"
)


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


@dataclass(frozen=True)
class FolderInfo:
    raw_name: str
    display_name: str
    attributes: Tuple[str, ...]


def quote_imap_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def quote_mailbox(name: str) -> str:
    return quote_imap_string(name)


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
