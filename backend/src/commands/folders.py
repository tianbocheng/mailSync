from __future__ import annotations

from typing import Dict, Optional

from config.sources import selected_source_rules
from connection import connect, safe_logout
from imap.source import list_folders, selectable_source_folders


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
            selected = {
                item.raw_name for item in selectable_source_folders(folders, rule)
            }
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

