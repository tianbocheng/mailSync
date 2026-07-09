from __future__ import annotations

import os
import time
from typing import Sequence

from common.errors import SyncError
from sync.settings import DEFAULT_SYNC_BATCH_SIZE


def build_source_schedule(
    rules: Sequence[dict],
    interval: int,
) -> list[dict]:
    slot_seconds = interval / len(rules)
    started_at = time.monotonic()
    schedule = []
    for index, rule in enumerate(rules):
        source_interval, batch_size = _source_schedule_values(rule, interval)
        next_run = started_at + index * slot_seconds
        schedule.append(
            {
                "email": rule["email"],
                "interval": source_interval,
                "batch_size": batch_size,
                "last_run": next_run - source_interval,
                "next_run": next_run,
            }
        )
    return schedule


def refresh_source_schedule(
    rules: Sequence[dict],
    interval: int,
    schedule: Sequence[dict],
) -> list[dict]:
    if not rules:
        return []
    if not schedule:
        return build_source_schedule(rules, interval)

    existing_by_email = {str(item["email"]): item for item in schedule}
    slot_seconds = interval / len(rules)
    now = time.monotonic()
    refreshed = []
    for index, rule in enumerate(rules):
        source_interval, batch_size = _source_schedule_values(rule, interval)
        existing = existing_by_email.get(str(rule["email"]))
        if existing is None:
            next_run = now + index * slot_seconds
            last_run = next_run - source_interval
        else:
            previous_interval = int(existing["interval"])
            last_run = float(
                existing.get(
                    "last_run",
                    float(existing["next_run"]) - previous_interval,
                )
            )
            next_run = float(existing["next_run"])
            if source_interval != previous_interval:
                next_run = last_run + source_interval
        refreshed.append(
            {
                "email": rule["email"],
                "interval": source_interval,
                "batch_size": batch_size,
                "last_run": last_run,
                "next_run": next_run,
            }
        )
    return refreshed


def _source_schedule_values(rule: dict, interval: int) -> tuple[int, int]:
    source_interval = int(rule.get("sync_interval", interval))
    if source_interval < 1:
        raise SyncError(
            f"sync_interval for {rule['email']} must be at least 1 second"
        )
    batch_size = int(
        rule.get(
            "batch_size",
            os.environ.get("SYNC_BATCH_SIZE", str(DEFAULT_SYNC_BATCH_SIZE)),
        )
    )
    if batch_size < 1:
        raise SyncError(
            f"batch_size for {rule['email']} must be at least 1"
        )
    return source_interval, batch_size
