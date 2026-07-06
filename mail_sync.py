#!/usr/bin/env python3
"""Compatibility launcher for the mail synchronizer."""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import cli as _cli
from cli import *  # noqa: F401,F403
from common.errors import SyncError


if __name__ != "__main__":
    sys.modules[__name__] = _cli
else:
    try:
        raise SystemExit(_cli.main())
    except KeyboardInterrupt:
        _cli.emit_json({"event": "synchronizer_stopped", "time": _cli.now_iso()})
        raise SystemExit(0)
    except (SyncError, imaplib.IMAP4.error, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
