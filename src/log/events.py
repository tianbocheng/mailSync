from __future__ import annotations

import json
import sys
from typing import Mapping, TextIO


def emit_json(
    payload: Mapping[str, object],
    *,
    error: bool = False,
    stream: TextIO | None = None,
) -> None:
    """Emit one structured JSON log line with stable UTF-8 text."""
    destination = stream if stream is not None else (sys.stderr if error else sys.stdout)
    print(json.dumps(dict(payload), ensure_ascii=False), file=destination, flush=True)
