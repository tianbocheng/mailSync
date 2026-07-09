from __future__ import annotations

import json
import sqlite3
from typing import Dict, Iterable, Tuple


def normalize_folder_labels(values: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    return tuple(
        sorted({str(value).strip() for value in values if str(value).strip()})
    )


def row_folder_labels(row: sqlite3.Row) -> Tuple[str, ...]:
    raw = row["folder_labels_json"]
    if raw:
        return normalize_folder_labels(json.loads(raw))
    return normalize_folder_labels((row["folder_label"],))


def configured_label_names(
    labels_by_name: Dict[str, str],
    values: Iterable[str],
) -> Tuple[str, ...]:
    return tuple(name for name in normalize_folder_labels(values) if name in labels_by_name)


def configured_label_ids(
    labels_by_name: Dict[str, str],
    values: Iterable[str],
) -> list[str]:
    return [labels_by_name[name] for name in configured_label_names(labels_by_name, values)]
