import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from unittest import mock


import mail_sync
from connection import client as connection_module
from imap import target as target_module
from domain import message as mail_sync_message

from tests import helpers  # noqa: F401

class NeteaseImapIdTests(unittest.TestCase):
    def test_netease_mailbox_sends_default_imap_id(self):
        client = mock.Mock()
        client.login.return_value = ("OK", [b"logged in"])
        client.xatom.return_value = ("OK", [b"id accepted"])

        with mock.patch.object(
            connection_module,
            "DirectIMAP4_SSL",
            return_value=client,
        ):
            result = mail_sync.connect(
                {
                    "email": "user@163.com",
                    "password": "secret",
                    "imap_server": "imap.163.com",
                    "imap_port": 993,
                    "use_ssl": True,
                }
            )

        self.assertIs(result, client)
        client.login.assert_called_once_with("user@163.com", "secret")
        client.xatom.assert_called_once()
        self.assertEqual(client.xatom.call_args.args[0], "ID")
        payload = client.xatom.call_args.args[1]
        self.assertIn('"name" "mail-sync"', payload)
        self.assertIn('"support-email" "user@163.com"', payload)

    def test_saved_feature_can_disable_default_imap_id(self):
        client = mock.Mock()
        client.login.return_value = ("OK", [b"logged in"])

        with mock.patch.object(
            connection_module,
            "DirectIMAP4_SSL",
            return_value=client,
        ):
            result = mail_sync.connect(
                {
                    "email": "user@163.com",
                    "password": "secret",
                    "imap_server": "imap.163.com",
                    "imap_port": 993,
                    "use_ssl": True,
                    "mailbox_feature_groups": {
                        "common": {"imap_id_required": False},
                    },
                }
            )

        self.assertIs(result, client)
        client.xatom.assert_not_called()

    def test_saved_feature_can_enable_default_imap_id(self):
        client = mock.Mock()
        client.login.return_value = ("OK", [b"logged in"])
        client.xatom.return_value = ("OK", [b"id accepted"])

        with mock.patch.object(
            connection_module,
            "DirectIMAP4_SSL",
            return_value=client,
        ):
            result = mail_sync.connect(
                {
                    "email": "user@example.com",
                    "password": "secret",
                    "imap_server": "imap.example.com",
                    "imap_port": 993,
                    "use_ssl": True,
                    "mailbox_feature_groups": {
                        "common": {"imap_id_required": True},
                    },
                }
            )

        self.assertIs(result, client)
        client.xatom.assert_called_once()

    def test_non_netease_mailbox_does_not_send_default_imap_id(self):
        client = mock.Mock()
        client.login.return_value = ("OK", [b"logged in"])

        with mock.patch.object(
            connection_module,
            "DirectIMAP4_SSL",
            return_value=client,
        ):
            result = mail_sync.connect(
                {
                    "email": "user@example.com",
                    "password": "secret",
                    "imap_server": "imap.example.com",
                    "imap_port": 993,
                    "use_ssl": True,
                }
            )

        self.assertIs(result, client)
        client.xatom.assert_not_called()

class AppendUidTests(unittest.TestCase):
    def _message_with_date(self):
        raw = b"Subject: feature append\r\n\r\nbody\r\n"
        internal_date = datetime(2026, 4, 29, 19, 42, 23, tzinfo=timezone.utc)
        return mail_sync.SourceMessage(
            source_email="source@example.com",
            folder="INBOX",
            uidvalidity=1,
            uid=2,
            raw=raw,
            content_hash=mail_sync.hashlib.sha256(raw).hexdigest(),
            message_id=None,
            internal_date=internal_date,
            system_flags=("\\Seen",),
        )

    def test_mailbox_feature_omits_internal_date_for_any_host(self):
        message = self._message_with_date()
        client = mock.Mock()
        client.host = "imap.example.com"
        client._mail_sync_feature_groups = {
            "target": {"omit_append_internal_date": True}
        }

        self.assertIsNone(target_module._append_internal_date(client, message))

    def test_mailbox_feature_can_confirm_internal_date_support(self):
        message = self._message_with_date()
        client = mock.Mock()
        client.host = "imap.163.com"
        client._mail_sync_feature_groups = {
            "target": {"append_internal_date_supported": True}
        }

        self.assertEqual(
            target_module._append_internal_date(client, message),
            message.internal_date,
        )

    def test_flat_mailbox_feature_still_works(self):
        message = self._message_with_date()
        client = mock.Mock()
        client.host = "imap.example.com"
        client._mail_sync_features = {"omit_append_internal_date": True}

        self.assertIsNone(target_module._append_internal_date(client, message))

    def test_netease_append_omits_internal_date(self):
        raw = b"Subject: netease append\r\n\r\nbody\r\n"
        internal_date = datetime(2026, 4, 29, 19, 42, 23, tzinfo=timezone.utc)
        message = mail_sync.SourceMessage(
            source_email="source@example.com",
            folder="INBOX",
            uidvalidity=1,
            uid=2,
            raw=raw,
            content_hash=mail_sync.hashlib.sha256(raw).hexdigest(),
            message_id=None,
            internal_date=internal_date,
            system_flags=("\\Seen",),
        )
        client = mock.Mock()
        client.host = "imap.163.com"
        client.append.return_value = ("OK", [b"[APPENDUID 20 31] APPEND completed"])
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
                    b"31 (UID 31 FLAGS (\\Seen))",
                    raw,
                ),
            ),
        ):
            result = mail_sync.append_target_copy(
                client,
                "INBOX",
                message,
                (),
            )

        self.assertEqual(result, (20, 31))
        self.assertIsNone(client.append.call_args.args[2])

    def test_non_netease_append_preserves_internal_date(self):
        raw = b"Subject: normal append\r\n\r\nbody\r\n"
        internal_date = datetime(2026, 4, 29, 19, 42, 23, tzinfo=timezone.utc)
        message = mail_sync.SourceMessage(
            source_email="source@example.com",
            folder="INBOX",
            uidvalidity=1,
            uid=2,
            raw=raw,
            content_hash=mail_sync.hashlib.sha256(raw).hexdigest(),
            message_id=None,
            internal_date=internal_date,
            system_flags=("\\Seen",),
        )
        client = mock.Mock()
        client.host = "imap.example.com"
        client.append.return_value = ("OK", [b"[APPENDUID 20 31] APPEND completed"])
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
                    b"31 (UID 31 FLAGS (\\Seen))",
                    raw,
                ),
            ),
        ):
            result = mail_sync.append_target_copy(
                client,
                "INBOX",
                message,
                (),
            )

        self.assertEqual(result, (20, 31))
        self.assertEqual(client.append.call_args.args[2], internal_date)

    def test_uidnext_falls_back_to_search_before_append(self):
        raw = b"Subject: append fallback\r\n\r\nbody\r\n"
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
        client.append.return_value = ("OK", [b"APPEND completed"])
        client.uid.return_value = ("OK", [b"stored"])

        def search_side_effect(_client, criteria="ALL"):
            if criteria == "ALL":
                return [4, 8]
            if criteria == "UID 9:*":
                return [9]
            return []

        with (
            mock.patch.object(
                target_module,
                "select_mailbox",
                return_value=(10, 20),
            ),
            mock.patch.object(
                target_module,
                "response_int",
                side_effect=mail_sync.SyncError("Invalid UIDNEXT: None"),
            ),
            mock.patch.object(
                target_module,
                "search_uids",
                side_effect=search_side_effect,
            ) as search,
            mock.patch.object(
                target_module,
                "target_fetch",
                return_value=(
                    b"9 (UID 9 FLAGS ($account $folder))",
                    raw,
                ),
            ),
        ):
            result = mail_sync.append_target_copy(
                client,
                "INBOX",
                message,
                ("$account", "$folder"),
            )

        self.assertEqual(result, (20, 9))
        search.assert_has_calls(
            [
                mock.call(client),
                mock.call(client, "UID 9:*"),
            ]
        )

    def test_append_search_filters_stale_uid_returned_by_server(self):
        raw = b"Subject: append stale uid\r\n\r\nbody\r\n"
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
        client.append.return_value = ("OK", [b"APPEND completed"])
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
                return_value=9,
            ),
            mock.patch.object(
                target_module,
                "search_uids",
                return_value=[8, 9],
            ),
            mock.patch.object(
                target_module,
                "target_fetch",
                return_value=(
                    b"9 (UID 9 FLAGS ($account))",
                    raw,
                ),
            ),
        ):
            result = mail_sync.append_target_copy(
                client,
                "INBOX",
                message,
                ("$account",),
            )

        self.assertEqual(result, (20, 9))

    def test_append_search_retries_until_new_uid_is_visible(self):
        raw = b"Subject: append delayed uid\r\n\r\nbody\r\n"
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
        client.append.return_value = ("OK", [b"APPEND completed"])
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
                return_value=9,
            ),
            mock.patch.object(
                target_module,
                "search_uids",
                side_effect=([8], [8, 9]),
            ),
            mock.patch.object(target_module.time, "sleep") as sleep,
            mock.patch.object(
                target_module,
                "target_fetch",
                return_value=(
                    b"9 (UID 9 FLAGS ($account))",
                    raw,
                ),
            ),
        ):
            result = mail_sync.append_target_copy(
                client,
                "INBOX",
                message,
                ("$account",),
            )

        self.assertEqual(result, (20, 9))
        sleep.assert_called_once()

    def test_target_fetch_retries_until_body_is_visible(self):
        raw = b"Subject: append delayed body\r\n\r\nbody\r\n"
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
                side_effect=(
                    mail_sync.SyncError("IMAP FETCH did not return a message body"),
                    (b"31 (UID 31 FLAGS ($account))", raw),
                ),
            ),
            mock.patch.object(target_module.time, "sleep") as sleep,
        ):
            result = mail_sync.append_target_copy(
                client,
                "INBOX",
                message,
                ("$account",),
            )

        self.assertEqual(result, (20, 31))
        sleep.assert_called_once()

    def test_target_verification_retries_until_labels_are_visible(self):
        raw = b"Subject: append delayed labels\r\n\r\nbody\r\n"
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
                side_effect=(
                    (b"31 (UID 31 FLAGS ($account))", raw),
                    (b"31 (UID 31 FLAGS ($account $folder))", raw),
                ),
            ),
            mock.patch.object(target_module.time, "sleep") as sleep,
        ):
            result = mail_sync.append_target_copy(
                client,
                "INBOX",
                message,
                ("$account", "$folder"),
            )

        self.assertEqual(result, (20, 31))
        sleep.assert_called_once()
        client.uid.assert_any_call(
            "store",
            "31",
            "+FLAGS.SILENT",
            "($folder)",
        )

    def test_missing_target_body_is_reported_as_content_verification_failure(self):
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
                side_effect=mail_sync.SyncError(
                    "IMAP FETCH did not return a message body"
                ),
            ),
            mock.patch.object(target_module.time, "sleep"),
            self.assertRaisesRegex(
                mail_sync.TargetContentVerificationError,
                "IMAP FETCH did not return a message body",
            ),
        ):
            mail_sync.append_target_copy(
                client,
                "INBOX",
                message,
                ("$account",),
            )

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
    def test_saved_uid_expunge_feature_is_used(self):
        client = mock.Mock()
        client.capabilities = ()
        client._mail_sync_feature_groups = {
            "target": {"uid_expunge_supported": True}
        }
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


class RepairTargetCopyTests(unittest.TestCase):
    def test_missing_target_uid_returns_without_body_retry(self):
        raw = b"Subject: missing target\r\n\r\nbody\r\n"
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
        with (
            mock.patch.object(
                target_module,
                "select_mailbox",
                return_value=(10, 20),
            ),
            mock.patch.object(
                target_module,
                "search_uids",
                return_value=[],
            ),
            mock.patch.object(target_module, "target_fetch") as target_fetch,
        ):
            repaired = mail_sync.repair_target_copy(
                client,
                "INBOX",
                31,
                message,
                ("$account",),
            )

        self.assertFalse(repaired)
        target_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
