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

    def select(self, *args, **kwargs):
        self.calls.append(("select", args, kwargs))
        self.untagged_responses = {"UIDVALIDITY": [b"1"]}
        return "OK", [b"1"]

    def uid(self, *args):
        self.calls.append(("uid", args))
        if args[0] == "search":
            return "OK", [self.search_result]
        return "OK", [b"9 (UID 9 FLAGS (\\Seen $synolabel_n65xejvo $synolabel_htqloxto))"]


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
        self.assertEqual(client.calls[1][1][0:3], ("search", None, "HEADER"))

    def test_empty_subject_and_no_match_are_rejected(self):
        with self.assertRaisesRegex(SyncError, "主题不能为空"):
            discover_target_labels(FakeTarget(), "INBOX", "   ")
        with self.assertRaisesRegex(SyncError, "没有找到"):
            discover_target_labels(FakeTarget(b""), "INBOX", "Missing")


if __name__ == "__main__":
    unittest.main()
