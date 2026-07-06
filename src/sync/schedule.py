from __future__ import annotations

import os
import time
from typing import Sequence

from common.errors import SyncError


def build_source_schedule(
    rules: Sequence[dict],
    interval: int,
) -> list[dict]:
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
    return schedule

