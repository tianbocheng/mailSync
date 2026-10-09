from __future__ import annotations

import re
from typing import Mapping

from common.errors import SyncError
from imap.protocol import quote_imap_string, select_mailbox
from imap.target import metadata_flags


SYNOLOGY_LABEL_RE = re.compile(r"^\$synolabel_[A-Za-z0-9]+$")


def discover_target_labels(
    client,
    target_folder: str,
    subject: str = "",
    known_names: Mapping[str, str] | None = None,
    *,
    message_id: str = "",
) -> dict:
    """Find a target message by Message-ID or subject and return its flags.

    IMAP exposes custom flags as IDs only. ``known_names`` is therefore used to
    attach names already stored in the local mapping table; unknown IDs are
    returned with an empty name for the UI to resolve.
    """
    subject = " ".join(str(subject or "").split())
    message_id = str(message_id or "").strip()
    if not subject and not message_id:
        raise SyncError("请填写邮件主题或 Message-ID")
    if message_id and (
        not message_id.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in message_id)
    ):
        raise SyncError("Message-ID 必须是 ASCII 字符，且不能包含换行或控制字符")
    target_folder = str(target_folder or "INBOX").strip() or "INBOX"
    select_mailbox(client, target_folder, readonly=True)
    uids = []
    lookup = "message_id" if message_id else "subject"
    if message_id:
        typ, data = client.uid(
            "search", None, "HEADER", "Message-ID", quote_imap_string(message_id)
        )
        if typ != "OK":
            raise SyncError("按 Message-ID 搜索邮件失败")
        uids = _search_result_uids(data)
    if not uids and subject:
        lookup = "subject"
        uids = _search_subject(client, subject)
    if not uids:
        if lookup == "message_id":
            raise SyncError(f"目标邮箱中没有找到 Message-ID 为“{message_id}”的邮件")
        raise SyncError(f"目标邮箱中没有找到主题为“{subject}”的邮件")

    uid = max(uids)
    typ, rows = client.uid("fetch", str(uid), "(UID FLAGS)")
    if typ != "OK":
        raise SyncError(f"读取邮件标签失败：UID {uid}")
    metadata = b" ".join(
        row[0] if isinstance(row, tuple) and row and isinstance(row[0], bytes) else row
        for row in (rows or [])
        if isinstance(row, bytes)
        or (isinstance(row, tuple) and row and isinstance(row[0], bytes))
    )
    label_ids = sorted(
        flag
        for flag in metadata_flags(metadata)
        if SYNOLOGY_LABEL_RE.match(flag)
    )
    if not label_ids:
        raise SyncError(f"邮件 UID {uid} 没有 Synology 标签")

    known_names = known_names or {}
    labels = [
        {
            "label_id": label_id,
            "label_name": str(known_names.get(label_id, "") or ""),
            "known": label_id in known_names,
        }
        for label_id in label_ids
    ]
    return {
        "subject": subject,
        "message_id": message_id,
        "lookup": lookup,
        "folder": target_folder,
        "uid": uid,
        "labels": labels,
        "unknown_ids": [item["label_id"] for item in labels if not item["known"]],
    }


def _search_subject(client, subject: str) -> list[int]:
    # imaplib encodes command arguments as ASCII until UTF8=ACCEPT is enabled.
    # A UTF-8 literal avoids UnicodeEncodeError and keeps a long subject out
    # of the command line. Explicit CHARSET is required on legacy sessions;
    # UTF8=ACCEPT sessions must omit it (RFC 6855 section 3).
    subject_bytes = subject.encode("utf-8")
    charset = () if getattr(client, "utf8_enabled", False) else ("CHARSET", "UTF-8")
    client.literal = subject_bytes
    try:
        typ, data = client.uid(
            "search",
            *charset,
            "HEADER",
            "Subject",
        )
    finally:
        # imaplib leaves ``literal`` in place when a command fails before it
        # can consume it.  Clear it so a later command cannot accidentally
        # send this subject as its body.
        client.literal = None
    if typ != "OK":
        raise SyncError(f"按主题搜索邮件失败：{subject}")
    return _search_result_uids(data)


def _search_result_uids(data) -> list[int]:
    raw_uids = data[0] if data and isinstance(data[0], bytes) else b""
    return [int(value) for value in raw_uids.split() if value.isdigit()]
