import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from unittest import mock


import mail_sync
from domain import message as mail_sync_message

from tests import helpers  # noqa: F401

class ContentVerificationTests(unittest.TestCase):
    def test_exact_content_matches(self):
        raw = b"Subject: test\r\n\r\nbody\r\n"
        content_hash = mail_sync.hashlib.sha256(raw).hexdigest()
        self.assertTrue(mail_sync.content_hash_matches(raw, content_hash))

    def test_crlf_target_matches_lf_source_hash(self):
        source = b"Subject: test\n\nbody\n"
        target = b"Subject: test\r\n\r\nbody\r\n"
        content_hash = mail_sync.hashlib.sha256(source).hexdigest()
        self.assertTrue(mail_sync.content_hash_matches(target, content_hash))

    def test_crlf_target_matches_mixed_line_ending_source(self):
        source = b"Subject: test\r\nX-Test: value\n\nbody\n"
        target = b"Subject: test\r\nX-Test: value\r\n\r\nbody\r\n"
        content_hash = mail_sync.hashlib.sha256(source).hexdigest()
        self.assertTrue(
            mail_sync.content_hash_matches(target, content_hash, source)
        )

    def test_mailplus_nul_rewrite_matches_source(self):
        source = b"Subject: test\r\n\r\nbinary:\x00XY+\r\n"
        target = b"Subject: test\r\n\r\nbinary:\x80XY+\r\n"
        content_hash = mail_sync.hashlib.sha256(source).hexdigest()
        self.assertTrue(
            mail_sync.content_hash_matches(target, content_hash, source)
        )

    def test_changed_content_does_not_match(self):
        source = b"Subject: test\n\nbody\n"
        target = b"Subject: test\r\n\r\nchanged\r\n"
        content_hash = mail_sync.hashlib.sha256(source).hexdigest()
        self.assertFalse(mail_sync.content_hash_matches(target, content_hash))

    def test_target_content_hash_is_cached_by_message_identity(self):
        mail_sync_message._hash_cache.clear()
        raw = b"Subject: cached\r\n\r\nbody\r\n"
        message = mail_sync.SourceMessage(
            source_email="source@example.com",
            folder="INBOX",
            uidvalidity=1,
            uid=2,
            raw=raw,
            content_hash=mail_sync.content_hash(raw),
            message_id=None,
            internal_date=None,
            system_flags=(),
        )

        first = mail_sync.target_content_hash(message)
        with mock.patch.object(
            mail_sync_message,
            "content_hash",
            side_effect=AssertionError("cache miss"),
        ):
            second = mail_sync.target_content_hash(message)

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
