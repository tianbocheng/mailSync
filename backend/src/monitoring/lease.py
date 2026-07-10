from __future__ import annotations

import imaplib
import threading
import uuid
from typing import Optional


SOURCE_CLIENT_LEASE_KEY = "_source_client_lease"
SOURCE_CONNECTION_ID_ATTR = "_mail_sync_connection_id"


def source_connection_id(client: imaplib.IMAP4) -> str:
    existing = getattr(client, SOURCE_CONNECTION_ID_ATTR, "")
    if isinstance(existing, str) and existing:
        return existing
    connection_id = uuid.uuid4().hex[:12]
    try:
        setattr(client, SOURCE_CONNECTION_ID_ATTR, connection_id)
    except Exception:
        pass
    return connection_id


class SourceClientLease:
    def __init__(self, client: imaplib.IMAP4):
        self._client: Optional[imaplib.IMAP4] = client
        self._connection_id = source_connection_id(client)
        self._reusable = True
        self._returned = threading.Event()
        self._claimed = threading.Event()
        self._lock = threading.Lock()

    @property
    def connection_id(self) -> str:
        with self._lock:
            return self._connection_id

    def client_for_sync(self) -> Optional[imaplib.IMAP4]:
        with self._lock:
            return self._client if self._reusable else None

    def replace_client(self, client: imaplib.IMAP4) -> None:
        with self._lock:
            self._client = client
            self._connection_id = source_connection_id(client)

    def return_to_monitor(self) -> None:
        self._returned.set()

    def wait_until_claimed(self, timeout: float) -> bool:
        return self._claimed.wait(timeout)

    def invalidate(self) -> None:
        with self._lock:
            self._reusable = False
        self._returned.set()

    def wait_for_return(
        self,
        stop_event: threading.Event,
    ) -> tuple[Optional[imaplib.IMAP4], bool]:
        while not self._returned.wait(0.25):
            if stop_event.is_set():
                self.invalidate()
        with self._lock:
            reusable = self._reusable and not stop_event.is_set()
            client = self._client
        self._claimed.set()
        return client, reusable
