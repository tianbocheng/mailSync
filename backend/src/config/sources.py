from __future__ import annotations

from typing import List, Optional

from common.errors import SyncError


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

