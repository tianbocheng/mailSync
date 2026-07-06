import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from unittest import mock


import mail_sync
from imap import target as target_module
from domain import message as mail_sync_message

from tests import helpers  # noqa: F401

class AppendUidTests(unittest.TestCase):
    def test_appenduid_avoids_full_mailbox_uid_search(self):
        raw = b"Subject: append\r\n\r\nbody\r\n"
        message = mail_sync.SourceMessage(
            source_email="source@example.com",
            folder="INBOX",
            uidvalidity=1,
            uid=2,
            raw=raw,
            content_hash=mail_sync.hashlib.sha256(raw).hexdigest(),
            message_id=None,
            internal_date=datetime.now(timezone.utc),
            system_flags=(),
        )
        client = mock.Mock()
        client.append.return_value = (
            "OK",
            [b"[APPENDUID 20 31] APPEND completed"],
        )
        client.uid.return_value = ("OK", [b"stored"])
        with (
            mock.patch.object(
                target_module,
                "select_mailbox",
                return_value=(10, 20),
            ),
            mock.patch.object(
                target_module,
                "response_int",
                return_value=31,
            ),
            mock.patch.object(
                target_module,
                "target_fetch",
                return_value=(
                    b"31 (UID 31 FLAGS ($account $folder))",
                    raw,
                ),
            ),
            mock.patch.object(target_module, "search_uids") as search,
        ):
            result = mail_sync.append_target_copy(
                client,
                "INBOX",
                message,
                ("$account", "$folder"),
            )

        self.assertEqual(result, (20, 31))
        search.assert_not_called()

class DeleteTargetCopyTests(unittest.TestCase):
    def test_uidplus_delete_uses_uid_expunge_without_broad_deleted_check(self):
        client = mock.Mock()
        client.capabilities = (b"IMAP4rev1", b"UIDPLUS")
        client.uid.return_value = ("OK", [b"ok"])

        def search_side_effect(_client, criteria="ALL"):
            self.assertNotEqual(criteria, "DELETED")
            if criteria == "UID 42":
                return [42]
            return []

        with (
            mock.patch.object(target_module, "select_mailbox", return_value=(1, 2)),
            mock.patch.object(
                target_module,
                "search_uids",
                side_effect=search_side_effect,
            ),
        ):
            mail_sync.delete_target_copy(client, "INBOX", 42)

        client.uid.assert_any_call("EXPUNGE", "42")
        client.expunge.assert_not_called()

    def test_non_uidplus_delete_refuses_unrelated_deleted_messages(self):
        client = mock.Mock()
        client.capabilities = ()

        def search_side_effect(_client, criteria="ALL"):
            if criteria == "UID 42":
                return [42]
            if criteria == "DELETED":
                return [7, 42]
            return []

        with (
            mock.patch.object(target_module, "select_mailbox", return_value=(1, 2)),
            mock.patch.object(
                target_module,
                "search_uids",
                side_effect=search_side_effect,
            ),
            self.assertRaisesRegex(mail_sync.SyncError, "unrelated"),
        ):
            mail_sync.delete_target_copy(client, "INBOX", 42)

        client.uid.assert_not_called()
        client.expunge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
