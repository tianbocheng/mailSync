from __future__ import annotations

import threading
from pathlib import Path
from typing import BinaryIO


class SizeRotatingLog:
    """Append bytes to a file and retain bounded numbered backups."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        backup_count: int,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if backup_count < 0:
            raise ValueError("backup_count cannot be negative")
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._chmod(self.path.parent, 0o700)
        for index in range(1, self.backup_count + 1):
            self._chmod(
                self.path.with_name(f"{self.path.name}.{index}"),
                0o600,
            )
        self._handle = self._open_append()
        self._size = self.path.stat().st_size

    def write(self, value: bytes | str) -> int:
        payload = value.encode("utf-8") if isinstance(value, str) else value
        if not payload:
            return 0
        with self._lock:
            self._ensure_open()
            if self._size > 0 and self._size + len(payload) > self.max_bytes:
                self._rotate()
            written = self._handle.write(payload)
            self._size += written
            return written

    def flush(self) -> None:
        with self._lock:
            self._ensure_open()
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.flush()
                self._handle.close()

    @property
    def closed(self) -> bool:
        return self._handle.closed

    def _open_append(self) -> BinaryIO:
        handle = self.path.open("ab")
        self._chmod(self.path, 0o600)
        return handle

    def _open_truncated(self) -> BinaryIO:
        handle = self.path.open("wb")
        self._chmod(self.path, 0o600)
        return handle

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except (FileNotFoundError, PermissionError):
            pass

    def _ensure_open(self) -> None:
        if self._handle.closed:
            raise ValueError("I/O operation on closed log")

    def _rotate(self) -> None:
        self._handle.flush()
        self._handle.close()
        if self.backup_count == 0:
            self._handle = self._open_truncated()
            self._size = 0
            return

        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                destination = self.path.with_name(f"{self.path.name}.{index + 1}")
                source.replace(destination)
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self._handle = self._open_append()
        self._size = 0
