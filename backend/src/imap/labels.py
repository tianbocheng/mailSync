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
    subject: str,
    known_names: Mapping[str, str] | None = None,
) -> dict:
    """Find the newest target message by subject and return its Synology flags.

    IMAP exposes custom flags as IDs only. ``known_names`` is therefore used to
    attach names already stored in the local mapping table; unknown IDs are
    returned with an empty name for the UI to resolve.
    """
    subject = " ".join(str(subject or "").split())
    if not subject:
        raise SyncError("邮件主题不能为空")
    target_folder = str(target_folder or "INBOX").strip() or "INBOX"
    select_mailbox(client, target_folder, readonly=True)
    typ, data = client.uid(
        "search",
        None,
        "HEADER",
        "Subject",
        quote_imap_string(subject),
    )
    if typ != "OK":
        raise SyncError(f"按主题搜索邮件失败：{subject}")
    raw_uids = data[0] if data and isinstance(data[0], bytes) else b""
    uids = [int(value) for value in raw_uids.split() if value.isdigit()]
    if not uids:
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
        "folder": target_folder,
        "uid": uid,
        "labels": labels,
        "unknown_ids": [item["label_id"] for item in labels if not item["known"]],
    }
