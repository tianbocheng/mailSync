from __future__ import annotations

from typing import Dict

from common.errors import SyncError
from config.validation import label_id_map


def target_for_rule(config: dict, rule: dict) -> dict:
    target = rule.get("target") or config.get("target")
    if not isinstance(target, dict) or not target.get("email"):
        source = rule.get("email") or "<unknown>"
        raise SyncError(f"Missing target mailbox for source {source}")
    return target


def labels_for_rule(config: dict, rule: dict) -> Dict[str, str]:
    labels = rule.get("synology_labels")
    if labels is None:
        labels = config.get("synology_labels")
    return label_id_map(labels)


def target_context_key(target: dict) -> str:
    parts = (
        target.get("email", ""),
        target.get("service_address", ""),
        target.get("imap_server", ""),
        target.get("folder", "INBOX"),
    )
    return "\0".join(str(part).casefold() for part in parts)
