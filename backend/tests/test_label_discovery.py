import imaplib
import re
import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "src"))

from imap.labels import discover_target_labels  # noqa: E402
from common.errors import SyncError  # noqa: E402


class FakeTarget:
    def __init__(self, search_result=b"7 9"):
        self.search_result = search_result
        self.calls = []
        self.literals = []
        self.literal = None

    def select(self, *args, **kwargs):
        self.calls.append(("select", args, kwargs))
        self.untagged_responses = {"UIDVALIDITY": [b"1"]}
        return "OK", [b"1"]

    def uid(self, *args):
        self.calls.append(("uid", args))
        if args[0] == "search":
            self.literals.append(self.literal)
            return "OK", [self.search_result]
        return "OK", [b"9 (UID 9 FLAGS (\\Seen $synolabel_n65xejvo $synolabel_htqloxto))"]


class MemoryIMAP(imaplib.IMAP4):
    """Exercise imaplib's real command encoding without a network mailbox."""

    def open(self, host="", port=143, timeout=None):
        self.lines = [b"* PREAUTH ready\r\n"]
        self.sent = []
        self.pending_search_tag = None
        self.pending_literal_size = None

    def send(self, data):
        self.sent.append(data)
        if self.pending_literal_size is not None:
            if len(data) != self.pending_literal_size:
                raise AssertionError("literal length does not match UTF-8 payload")
            self.pending_literal_size = None
            return
        if self.pending_search_tag is not None and data == b"\r\n":
            tag, self.pending_search_tag = self.pending_search_tag, None
            self.lines.extend([b"* SEARCH 9\r\n", tag + b" OK search\r\n"])
            return
        tag, command = data.rstrip(b"\r\n").split(b" ", 1)
        if command == b"CAPABILITY":
            self.lines.extend([
                b"* CAPABILITY IMAP4rev1\r\n",
                tag + b" OK capability\r\n",
            ])
        elif command.startswith(b"EXAMINE "):
            self.lines.extend([
                b"* 1 EXISTS\r\n",
                b"* OK [UIDVALIDITY 1] valid\r\n",
                tag + b" OK examine\r\n",
            ])
        elif command.startswith(b"UID SEARCH HEADER Message-ID "):
            self.lines.extend([b"* SEARCH 9\r\n", tag + b" OK search\r\n"])
        elif command.startswith(b"UID SEARCH "):
            literal = re.search(rb"\{(\d+)\}$", command)
            if literal is None:
                raise AssertionError("search subject must be a literal")
            self.pending_literal_size = int(literal.group(1))
            self.pending_search_tag = tag
            self.lines.append(b"+ send literal\r\n")
        elif command == b"UID FETCH 9 (UID FLAGS)":
            self.lines.extend([
                b"* 1 FETCH (UID 9 FLAGS ($synolabel_new123))\r\n",
                tag + b" OK fetch\r\n",
            ])
        else:
            raise AssertionError(f"Unexpected command: {command!r}")

    def readline(self):
        return self.lines.pop(0)

    def shutdown(self):
        pass


class LabelDiscoveryTests(unittest.TestCase):
    def test_discovers_latest_uid_and_known_names(self):
        client = FakeTarget()

        result = discover_target_labels(
            client,
            "INBOX",
            "Fw: Reminder: Proof of CPR70286 for Cell Proliferation ready for review",
            {"$synolabel_n65xejvo": "BOX:收件箱"},
        )

        self.assertEqual(result["uid"], 9)
        self.assertEqual(
            result["labels"],
            [
                {
                    "label_id": "$synolabel_htqloxto",
                    "label_name": "",
                    "known": False,
                },
                {
                    "label_id": "$synolabel_n65xejvo",
                    "label_name": "BOX:收件箱",
                    "known": True,
                },
            ],
        )
        self.assertEqual(result["unknown_ids"], ["$synolabel_htqloxto"])
        self.assertEqual(client.calls[0][0], "select")
        self.assertEqual(
            client.calls[1][1],
            ("search", "CHARSET", "UTF-8", "HEADER", "Subject"),
        )
        self.assertEqual(client.literals, [result["subject"].encode("utf-8")])
        self.assertIsNone(client.literal)

    def test_unicode_and_long_subjects_use_utf8_literals_on_the_wire(self):
        reported_subject = (
            "the submission due：2026.11.13 CAD Design & Testing/"
            "Intelligent Automation and Soft Computing"
        )
        for subject in (
            reported_subject,
            "新标签测试邮件 😀",
            'Long subject "quoted" \\ ' + "details " * 1500,
        ):
            with self.subTest(subject=subject[:50]):
                client = MemoryIMAP()
                normalized = " ".join(subject.split())

                result = discover_target_labels(client, "INBOX", subject)

                self.assertEqual(result["uid"], 9)
                self.assertEqual(result["labels"][0]["label_id"], "$synolabel_new123")
                command = next(
                    value for value in client.sent if b" UID SEARCH " in value
                )
                self.assertIn(b"UID SEARCH CHARSET UTF-8 HEADER Subject {", command)
                self.assertLess(len(command), 100)
                self.assertIn(normalized.encode("utf-8"), client.sent)

    def test_utf8_enabled_session_omits_charset(self):
        client = MemoryIMAP()
        client._mode_utf8()

        discover_target_labels(client, "INBOX", "中文主题")

        command = next(value for value in client.sent if b" UID SEARCH " in value)
        self.assertNotIn(b"CHARSET", command)
        self.assertIn("中文主题".encode("utf-8"), client.sent)

    def test_failed_search_clears_pending_literal(self):
        class FailingTarget(FakeTarget):
            def uid(self, *args):
                raise imaplib.IMAP4.error("SEARCH failed")

        client = FailingTarget()
        with self.assertRaises(imaplib.IMAP4.error):
            discover_target_labels(client, "INBOX", "中文主题")
        self.assertIsNone(client.literal)

    def test_message_id_search_is_ascii_and_takes_priority_over_subject(self):
        message_id = "<test-123@example.com>"
        for subject in ("", "A different 中文 subject"):
            with self.subTest(subject=subject):
                client = MemoryIMAP()

                result = discover_target_labels(
                    client, "INBOX", subject, message_id=message_id
                )

                searches = [value for value in client.sent if b" UID SEARCH " in value]
                self.assertEqual(len(searches), 1)
                self.assertIn(
                    b'UID SEARCH HEADER Message-ID "<test-123@example.com>"\r\n',
                    searches[0],
                )
                self.assertEqual(result["lookup"], "message_id")
                self.assertEqual(result["message_id"], message_id)
                self.assertEqual(result["uid"], 9)

    def test_missing_message_id_can_fall_back_to_subject(self):
        class MissingIdTarget(FakeTarget):
            def uid(self, *args):
                result = super().uid(*args)
                return ("OK", [b""]) if "Message-ID" in args else result

        client = MissingIdTarget()

        result = discover_target_labels(
            client, "INBOX", "中文主题", message_id="<missing@example.com>"
        )

        self.assertEqual(result["lookup"], "subject")
        self.assertEqual(client.literals, [None, "中文主题".encode("utf-8")])
        self.assertEqual(result["uid"], 9)

    def test_message_id_invalid_or_missing_is_reported(self):
        for message_id in ("<中文@example.com>", "<a@example.com>\r\nUID SEARCH ALL"):
            with self.subTest(message_id=message_id):
                client = FakeTarget()
                with self.assertRaisesRegex(SyncError, "Message-ID 必须"):
                    discover_target_labels(client, "INBOX", message_id=message_id)
                self.assertEqual(client.calls, [])
        with self.assertRaisesRegex(SyncError, "没有找到 Message-ID"):
            discover_target_labels(
                FakeTarget(b""), "INBOX", message_id="<missing@example.com>"
            )

    def test_empty_subject_and_no_match_are_rejected(self):
        with self.assertRaisesRegex(SyncError, "请填写邮件主题或 Message-ID"):
            discover_target_labels(FakeTarget(), "INBOX", "   ")
        with self.assertRaisesRegex(SyncError, "没有找到"):
            discover_target_labels(FakeTarget(b""), "INBOX", "Missing")


if __name__ == "__main__":
    unittest.main()
