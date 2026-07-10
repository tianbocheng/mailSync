import json
import queue
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.header import Header
from pathlib import Path
from unittest import mock


import mail_sync
from connection import client as connection_module
from imap import source as source_module
from monitoring import listener as listener_module
from domain import message as mail_sync_message

from tests import helpers  # noqa: F401

class SourceFetchTests(unittest.TestCase):
    def test_response_int_skips_none_and_accepts_int_values(self):
        client = mock.Mock()
        client.untagged_responses = {"UIDNEXT": [None, 42]}

        self.assertEqual(mail_sync.response_int(client, "UIDNEXT"), 42)
        client.response.assert_not_called()

    def test_response_int_uses_response_when_untagged_is_unparseable(self):
        client = mock.Mock()
        client.untagged_responses = {"UIDNEXT": [None]}
        client.response.return_value = ("OK", [b"UIDNEXT 51"])

        self.assertEqual(mail_sync.response_int(client, "UIDNEXT"), 51)

    def test_response_int_reports_invalid_response_without_type_error(self):
        client = mock.Mock()
        client.untagged_responses = {"UIDNEXT": [None]}
        client.response.return_value = ("OK", [None])

        with self.assertRaisesRegex(mail_sync.SyncError, "Invalid UIDNEXT"):
            mail_sync.response_int(client, "UIDNEXT")

    def test_select_mailbox_error_decodes_modified_utf7_name(self):
        client = mock.Mock()
        client.select.return_value = ("NO", [b"Unsafe Login"])

        with self.assertRaisesRegex(
            mail_sync.SyncError,
            r"Cannot select mailbox 垃圾邮件 \(&V4NXPpCuTvY-\): "
            r"\[b'Unsafe Login'\]",
        ):
            mail_sync.select_mailbox(client, "&V4NXPpCuTvY-", readonly=True)

    def test_missing_source_body_has_specific_error(self):
        client = mock.Mock()
        client.uid.return_value = ("OK", [b"1563346595 (UID 1563346595)"])

        with self.assertRaisesRegex(
            mail_sync.SourceMessageUnavailable,
            "UID 1563346595",
        ):
            mail_sync.fetch_source_message(
                client,
                "source@example.com",
                "INBOX",
                123,
                1563346595,
            )

    def test_missing_imap_body_uses_pop_fallback(self):
        raw = b"Message-ID: <large@example.com>\r\nSubject: large\r\n\r\nbody\r\n"
        header = b"Message-ID: <large@example.com>\r\nSubject: large\r\n\r\n"
        metadata = (
            b'1 (UID 9 RFC822.SIZE '
            + str(len(raw)).encode("ascii")
            + b' INTERNALDATE "01-Jun-2026 09:38:23 +0800" FLAGS (\\Seen))'
        )
        client = mock.Mock()
        client.uid.side_effect = [
            ("OK", [None]),
            ("OK", [(metadata, header), b")"]),
        ]
        profile = {
            "email": "source@example.com",
            "password": "secret",
            "pop_fallback": {"server": "pop.example.com"},
        }
        with mock.patch.object(
            source_module,
            "fetch_pop_message_by_identity",
            return_value=raw,
        ) as fallback:
            message = mail_sync.fetch_source_message(
                client,
                "source@example.com",
                "INBOX",
                123,
                9,
                profile,
            )
        self.assertEqual(message.raw, raw)
        fallback.assert_called_once_with(
            profile,
            "<large@example.com>",
            len(raw),
        )

    def test_missing_pop_fallback_headers_is_skipped(self):
        client = mock.Mock()
        client.uid.side_effect = [
            ("OK", [None]),
            ("OK", [b"9 (UID 9 RFC822.SIZE 123)"]),
        ]
        profile = {
            "email": "source@example.com",
            "password": "secret",
            "pop_fallback": {"server": "pop.example.com"},
        }

        with self.assertRaisesRegex(
            mail_sync.SourceMessageUnavailable,
            "UID 9",
        ):
            mail_sync.fetch_source_message(
                client,
                "source@example.com",
                "INBOX",
                123,
                9,
                profile,
            )

    def test_pop_stream_unstuffs_dot_lines(self):
        client = mock.Mock()
        client._getline.side_effect = [
            (b"Header: value", 15),
            (b"..dot-start", 12),
            (b".", 3),
        ]
        raw = mail_sync.pop_retrieve_raw(client, 4)
        self.assertEqual(raw, b"Header: value\r\n.dot-start\r\n")
        client._putcmd.assert_called_once_with("RETR 4")

class SourceRuleTests(unittest.TestCase):
    def test_configured_skipped_uids(self):
        folder = mail_sync.FolderInfo("INBOX", "INBOX", ())
        rule = {"skip_uids": {"INBOX": [1761199015, "1761199014"]}}
        self.assertEqual(
            mail_sync.source_folder_skipped_uids(rule, folder),
            {1761199015, 1761199014},
        )

    def test_outlook_deleted_draft_without_delivery_headers_is_skipped(self):
        folder = mail_sync.FolderInfo("Deleted", "Deleted", (r"\Trash",))
        rule = {
            "skip_deleted_drafts": True,
            "folder_labels": {"Deleted": "BOX:已删除邮件"},
            "default_folder_label": "BOX:其他文件夹",
            "deleted_label": "BOX:已删除邮件",
        }
        message = mail_sync.SourceMessage(
            source_email="user@outlook.com",
            folder="Deleted",
            uidvalidity=1,
            uid=2,
            raw=b"From: a@example.com\r\nTo: b@example.com\r\nSubject: draft\r\n\r\n",
            content_hash="hash",
            message_id=None,
            internal_date=datetime.now(timezone.utc),
            system_flags=(),
        )

        self.assertTrue(
            mail_sync.is_deleted_draft_message(rule, folder, message)
        )

    def test_outlook_normal_deleted_message_is_not_skipped(self):
        folder = mail_sync.FolderInfo("Deleted", "Deleted", (r"\Trash",))
        rule = {
            "skip_deleted_drafts": True,
            "folder_labels": {"Deleted": "BOX:已删除邮件"},
            "default_folder_label": "BOX:其他文件夹",
            "deleted_label": "BOX:已删除邮件",
        }
        message = mail_sync.SourceMessage(
            source_email="user@outlook.com",
            folder="Deleted",
            uidvalidity=1,
            uid=3,
            raw=(
                b"Message-ID: <normal@example.com>\r\n"
                b"Date: Mon, 8 May 2023 15:05:39 +0800\r\n"
                b"From: a@example.com\r\nTo: b@example.com\r\n\r\n"
            ),
            content_hash="hash",
            message_id="<normal@example.com>",
            internal_date=datetime.now(timezone.utc),
            system_flags=(),
        )

        self.assertFalse(
            mail_sync.is_deleted_draft_message(rule, folder, message)
        )

class SourceReconnectTests(unittest.TestCase):
    def test_reconnects_after_source_autologout(self):
        original = mock.Mock()
        original.noop.side_effect = mail_sync.imaplib.IMAP4.error(
            "Autologout"
        )
        replacement = mock.Mock()
        profile = {
            "email": "source@example.com",
            "imap_server": "imap.example.com",
        }
        with (
            mock.patch.object(connection_module, "connect", return_value=replacement),
            mock.patch.object(
                connection_module,
                "select_mailbox",
                return_value=(10, 123),
            ) as select,
        ):
            result = mail_sync.ensure_source_connection(
                profile,
                original,
                "INBOX",
                123,
            )
        self.assertIs(result, replacement)
        select.assert_called_once_with(
            replacement,
            "INBOX",
            readonly=True,
        )

class RealtimeIncrementalTests(unittest.TestCase):
    class ReturningQueue(queue.Queue):
        def put(self, item, block=True, timeout=None):
            super().put(item, block=block, timeout=timeout)
            lease = listener_module.source_client_lease(item)
            if lease is not None:
                lease.return_to_monitor()

    class OnePollStopEvent:
        def __init__(self):
            self.waits = 0
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.waits += 1
            if self.waits == 1:
                return False
            self.stopped = True
            return True

    class ManualStopEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            return self.stopped

    class TwoPollStopEvent:
        def __init__(self):
            self.waits = 0
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout):
            self.waits += 1
            if self.waits <= 2:
                return False
            self.stopped = True
            return True

    def test_status_response_is_parsed_independent_of_value_order(self):
        result = mail_sync.parse_mailbox_status(
            [
                b'INBOX (MESSAGES 12 UIDVALIDITY 44 UIDNEXT 19)',
            ]
        )

        self.assertEqual(result, (44, 19, 12))

    def test_status_response_requires_all_incremental_markers(self):
        with self.assertRaises(mail_sync.SyncError):
            mail_sync.parse_mailbox_status(
                [b'INBOX (UIDNEXT 19 MESSAGES 12)']
            )

    def test_status_response_allows_servers_without_uidnext(self):
        result = mail_sync.parse_mailbox_status(
            [b'INBOX (MESSAGES 12 UIDVALIDITY 44)']
        )

        self.assertEqual(result, (44, 0, 12))

    def test_incremental_candidates_exclude_known_folder_uids(self):
        db = mail_sync.sqlite3.connect(":memory:")
        db.execute(
            """
            CREATE TABLE message_locations (
                source_email TEXT,
                source_folder TEXT,
                source_uidvalidity INTEGER,
                source_uid INTEGER
            )
            """
        )
        db.executemany(
            "INSERT INTO message_locations VALUES (?, ?, ?, ?)",
            [
                ("source@example.com", "INBOX", 9, 1),
                ("source@example.com", "INBOX", 9, 3),
                ("source@example.com", "Sent", 9, 2),
                ("source@example.com", "INBOX", 8, 2),
            ],
        )

        result = mail_sync.incremental_candidate_uids(
            db,
            "source@example.com",
            "INBOX",
            9,
            [1, 2, 3, 4],
        )

        self.assertEqual(result, [2, 4])

    def test_bulk_seen_update_only_marks_current_verified_locations(self):
        db = mail_sync.sqlite3.connect(":memory:")
        db.executescript(
            """
            CREATE TABLE messages (
                source_email TEXT,
                content_hash TEXT,
                internal_date TEXT,
                source_date_verified INTEGER,
                last_seen_run TEXT,
                updated_at TEXT
            );
            CREATE TABLE message_locations (
                source_email TEXT,
                content_hash TEXT,
                source_folder TEXT,
                source_uidvalidity INTEGER,
                source_uid INTEGER,
                last_seen_run TEXT,
                updated_at TEXT
            );
            """
        )
        db.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("source@example.com", "present", "2026-01-01", 1, "old", "old"),
                ("source@example.com", "deleted", "2026-01-01", 1, "old", "old"),
                ("source@example.com", "unverified", None, 0, "old", "old"),
            ],
        )
        db.executemany(
            "INSERT INTO message_locations VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("source@example.com", "present", "INBOX", 9, 1, "old", "old"),
                ("source@example.com", "deleted", "INBOX", 9, 2, "old", "old"),
                ("source@example.com", "unverified", "INBOX", 9, 3, "old", "old"),
            ],
        )

        verified = mail_sync.verified_known_location_uids(
            db,
            "source@example.com",
            "INBOX",
            9,
        )
        mail_sync.bulk_mark_locations_seen(
            db,
            "source@example.com",
            "INBOX",
            9,
            [1],
            "current",
        )

        self.assertEqual(verified, {1, 2})
        locations = dict(
            db.execute(
                "SELECT source_uid, last_seen_run FROM message_locations"
            ).fetchall()
        )
        messages = dict(
            db.execute(
                "SELECT content_hash, last_seen_run FROM messages"
            ).fetchall()
        )
        self.assertEqual(locations, {1: "current", 2: "old", 3: "old"})
        self.assertEqual(
            messages,
            {"present": "current", "deleted": "old", "unverified": "old"},
        )

    def test_source_monitor_queues_changed_folder(self):
        changes = self.ReturningQueue()
        stop_event = self.OnePollStopEvent()
        client = mock.Mock()
        client.noop.return_value = ("OK", [b"alive"])
        folder = mail_sync.FolderInfo("INBOX", "INBOX", ())
        with (
            mock.patch.object(listener_module, "connect", return_value=client),
            mock.patch.object(listener_module, "list_folders", return_value=[folder]),
            mock.patch.object(
                listener_module,
                "mailbox_status",
                side_effect=[(1, 10, 9), (1, 11, 10)],
            ),
            mock.patch.object(listener_module, "safe_logout"),
            mock.patch("builtins.print"),
        ):
            mail_sync.monitor_source_changes(
                {"email": "source@example.com"},
                {"email": "source@example.com"},
                None,
                1,
                changes,
                stop_event,
            )

        change = changes.get_nowait()
        self.assertEqual(change["email"], "source@example.com")
        self.assertEqual(change["folder"], "INBOX")
        self.assertEqual(change["current"], (1, 11, 10))
        lease = listener_module.source_client_lease(change)
        self.assertIsNotNone(lease)
        self.assertIs(lease.client_for_sync(), client)

    def test_source_monitor_checks_inbox_first(self):
        changes = mail_sync.queue.Queue()
        stop_event = self.OnePollStopEvent()
        client = mock.Mock()
        folders = [
            mail_sync.FolderInfo("Sent Items", "Sent Items", ()),
            mail_sync.FolderInfo("Trash", "Trash", ()),
            mail_sync.FolderInfo("INBOX", "INBOX", ()),
        ]
        checked = []

        def mailbox_status_side_effect(_client, folder):
            checked.append(folder)
            return (1, 10, 9)

        with (
            mock.patch.object(listener_module, "connect", return_value=client),
            mock.patch.object(listener_module, "list_folders", return_value=folders),
            mock.patch.object(
                listener_module,
                "mailbox_status",
                side_effect=mailbox_status_side_effect,
            ),
            mock.patch.object(listener_module, "safe_logout"),
            mock.patch("builtins.print"),
        ):
            mail_sync.monitor_source_changes(
                {"email": "source@example.com"},
                {"email": "source@example.com"},
                None,
                1,
                changes,
                stop_event,
            )

        self.assertEqual(checked[:3], ["INBOX", "Sent Items", "Trash"])
        self.assertEqual(checked[3:6], ["INBOX", "Sent Items", "Trash"])

    def test_source_poll_monitor_reuses_connection_after_sync(self):
        changes = self.ReturningQueue()
        stop_event = self.TwoPollStopEvent()
        client = mock.Mock()
        client.noop.return_value = ("OK", [b"alive"])
        folder = mail_sync.FolderInfo("INBOX", "INBOX", ())
        with (
            mock.patch.object(
                listener_module,
                "connect",
                return_value=client,
            ) as connect,
            mock.patch.object(listener_module, "list_folders", return_value=[folder]),
            mock.patch.object(
                listener_module,
                "mailbox_status",
                side_effect=[(1, 10, 9), (1, 11, 10), (1, 12, 11)],
            ),
            mock.patch.object(listener_module, "safe_logout"),
            mock.patch("builtins.print"),
        ):
            mail_sync.monitor_source_changes(
                {"email": "source@example.com"},
                {"email": "source@example.com"},
                None,
                1,
                changes,
                stop_event,
            )

        self.assertEqual(changes.qsize(), 2)
        self.assertEqual(connect.call_count, 1)
        connection_ids = {
            listener_module.source_client_lease(change).connection_id
            for change in list(changes.queue)
        }
        self.assertEqual(len(connection_ids), 1)

    def test_inbox_idle_monitor_queues_changed_inbox(self):
        changes = self.ReturningQueue()
        stop_event = self.ManualStopEvent()
        client = mock.Mock()
        client.capabilities = (b"IMAP4rev1", b"IDLE")
        folder = mail_sync.FolderInfo("INBOX", "INBOX", ())
        statuses = [(1, 10, 9), (1, 11, 10)]

        def mailbox_status_side_effect(_client, _folder):
            result = statuses.pop(0)
            if not statuses:
                stop_event.stopped = True
            return result

        with (
            mock.patch.object(listener_module, "connect", return_value=client),
            mock.patch.object(listener_module, "list_folders", return_value=[folder]),
            mock.patch.object(
                listener_module,
                "select_mailbox",
                return_value=(9, 1),
            ),
            mock.patch.object(
                listener_module,
                "mailbox_status",
                side_effect=mailbox_status_side_effect,
            ),
            mock.patch.object(
                listener_module,
                "idle_wait_for_change",
                return_value=True,
            ) as idle_wait,
            mock.patch.object(listener_module, "safe_logout"),
            mock.patch("builtins.print"),
        ):
            listener_module.monitor_inbox_idle_changes(
                {"email": "source@example.com"},
                {"email": "source@example.com"},
                None,
                changes,
                stop_event,
            )

        change = changes.get_nowait()
        self.assertEqual(change["email"], "source@example.com")
        self.assertEqual(change["folder"], "INBOX")
        self.assertFalse(change["uidvalidity_changed"])
        self.assertEqual(change["current"], (1, 11, 10))
        lease = listener_module.source_client_lease(change)
        self.assertIsNotNone(lease)
        self.assertIs(lease.client_for_sync(), client)
        self.assertAlmostEqual(idle_wait.call_args.args[2], 1200, places=3)

    def test_inbox_idle_monitor_uses_compensation_probe(self):
        changes = self.ReturningQueue()
        stop_event = self.ManualStopEvent()
        client = mock.Mock()
        client.capabilities = (b"IMAP4rev1", b"IDLE")
        folder = mail_sync.FolderInfo("INBOX", "INBOX", ())
        statuses = [(1, 10, 9), (1, 11, 10)]

        def mailbox_status_side_effect(_client, _folder):
            result = statuses.pop(0)
            if not statuses:
                stop_event.stopped = True
            return result

        with (
            mock.patch.object(listener_module, "connect", return_value=client),
            mock.patch.object(listener_module, "list_folders", return_value=[folder]),
            mock.patch.object(
                listener_module,
                "select_mailbox",
                return_value=(9, 1),
            ),
            mock.patch.object(
                listener_module,
                "mailbox_status",
                side_effect=mailbox_status_side_effect,
            ),
            mock.patch.object(
                listener_module,
                "idle_wait_for_change",
                return_value=False,
            ) as idle_wait,
            mock.patch.object(listener_module, "safe_logout"),
            mock.patch("builtins.print"),
        ):
            listener_module.monitor_inbox_idle_changes(
                {"email": "source@example.com"},
                {"email": "source@example.com"},
                None,
                changes,
                stop_event,
                compensation_interval=3,
            )

        change = changes.get_nowait()
        self.assertEqual(change["detected_by"], "idle_compensation")
        self.assertEqual(change["current"], (1, 11, 10))
        self.assertAlmostEqual(idle_wait.call_args.args[2], 3, places=3)

    def test_realtime_monitor_falls_back_when_idle_is_unavailable(self):
        changes = mail_sync.queue.Queue()
        stop_event = self.ManualStopEvent()
        with (
            mock.patch.object(
                listener_module,
                "monitor_inbox_idle_changes",
                side_effect=listener_module.IdleUnsupported("no idle"),
            ) as idle_monitor,
            mock.patch.object(listener_module, "monitor_source_changes") as status_monitor,
            mock.patch("builtins.print"),
        ):
            listener_module.monitor_realtime_changes(
                {"email": "source@example.com"},
                {"email": "source@example.com"},
                None,
                3,
                changes,
                stop_event,
            )

        idle_monitor.assert_called_once()
        status_monitor.assert_called_once()

    def test_idle_monitor_degrades_after_repeated_runtime_errors(self):
        changes = mail_sync.queue.Queue()
        stop_event = self.ManualStopEvent()
        with (
            mock.patch.object(
                listener_module,
                "connect",
                side_effect=listener_module.SyncError("broken idle path"),
            ) as connect,
            mock.patch("builtins.print"),
        ):
            with self.assertRaises(listener_module.IdleUnsupported):
                listener_module.monitor_inbox_idle_changes(
                    {"email": "source@example.com"},
                    {"email": "source@example.com"},
                    None,
                    changes,
                    stop_event,
                    idle_error_fallback_threshold=3,
                )

        self.assertEqual(connect.call_count, 3)

    def test_idle_monitor_degrades_after_repeated_missed_pushes(self):
        changes = self.ReturningQueue()
        stop_event = self.ManualStopEvent()
        client = mock.Mock()
        client.capabilities = (b"IMAP4rev1", b"IDLE")
        client.noop.return_value = ("OK", [b"alive"])
        folder = mail_sync.FolderInfo("INBOX", "INBOX", ())
        statuses = [
            (1, 10, 9),
            (1, 11, 10),
            (1, 12, 11),
            (1, 13, 12),
        ]

        with (
            mock.patch.object(
                listener_module,
                "connect",
                return_value=client,
            ) as connect,
            mock.patch.object(listener_module, "list_folders", return_value=[folder]),
            mock.patch.object(
                listener_module,
                "select_mailbox",
                return_value=(9, 1),
            ),
            mock.patch.object(
                listener_module,
                "mailbox_status",
                side_effect=statuses,
            ),
            mock.patch.object(
                listener_module,
                "idle_wait_for_change",
                return_value=False,
            ),
            mock.patch.object(listener_module, "safe_logout"),
            mock.patch("builtins.print"),
        ):
            with self.assertRaises(listener_module.IdleUnsupported):
                listener_module.monitor_inbox_idle_changes(
                    {"email": "source@example.com"},
                    {"email": "source@example.com"},
                    None,
                    changes,
                    stop_event,
                    compensation_interval=3,
                    missed_push_fallback_threshold=3,
                )

        self.assertEqual(changes.qsize(), 3)
        self.assertEqual(connect.call_count, 1)
        connection_ids = {
            listener_module.source_client_lease(change).connection_id
            for change in list(changes.queue)
        }
        self.assertEqual(len(connection_ids), 1)

    def test_realtime_monitor_uses_saved_idle_feature(self):
        changes = mail_sync.queue.Queue()
        stop_event = self.ManualStopEvent()
        with (
            mock.patch.object(listener_module, "monitor_inbox_idle_changes") as idle_monitor,
            mock.patch.object(listener_module, "monitor_source_changes") as status_monitor,
            mock.patch("builtins.print"),
        ):
            listener_module.monitor_realtime_changes(
                {
                    "email": "source@example.com",
                    "mailbox_feature_groups": {
                        "source": {"idle_supported": False},
                    },
                },
                {"email": "source@example.com"},
                None,
                3,
                changes,
                stop_event,
            )

        idle_monitor.assert_not_called()
        status_monitor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
