from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Tuple


def normalize_folder_labels(values: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    return tuple(sorted(set(values)))


def row_folder_labels(row: sqlite3.Row) -> Tuple[str, ...]:
    raw = row["folder_labels_json"]
    if raw:
        return normalize_folder_labels(json.loads(raw))
    return (row["folder_label"],)

