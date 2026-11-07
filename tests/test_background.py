"""The background new-mail check (postroom --background), against the fake server.

    python3 -m unittest tests.test_background
"""
import os
import tempfile
import time
import unittest

os.environ["POSTROOM_PROFILE"] = tempfile.mkdtemp(prefix="postroom-bg-")
os.environ["POSTROOM_NO_KEYRING"] = "1"

from postroom import background, passwords  # noqa: E402
from postroom.config import Account, Config, cache_dir  # noqa: E402
from postroom.store import Store  # noqa: E402
from tests.fake_imap import FakeClient, Mailbox, _raw  # noqa: E402

background.ImapClient = FakeClient


class BackgroundTest(unittest.TestCase):
    def setUp(self):
        FakeClient.mailbox = Mailbox()
        self.store = Store(cache_dir() / f"cache-{time.time_ns()}.db")
        self.acct = Account(name="Personal", email="me@gmail.com")
        passwords.store(self.acct.id, "x", "secret")
        self.checker = background.Checker(self.store)
        self.told = []
        background.tell = lambda text, sound, speak: self.told.append((text, sound, speak))
        background.app_is_open = lambda: False

    def add(self, uid, frm, subject, seen=False):
        FakeClient.mailbox.add("INBOX", _raw(uid, frm, subject, "body", time.time(), f"t{uid}"),
                               {"\\seen"} if seen else set(), f"t{uid}")

    def test_first_check_announces_nothing_then_only_new_unread(self):
        self.assertEqual(self.checker.check_account(self.acct), [])  # existing mail is old news
        self.add(900, "Zed Ray <zed@example.com>", "Hello")
        self.add(901, "Me <me@gmail.com>", "Already read", seen=True)
        new = self.checker.check_account(self.acct)
        self.assertEqual([m.subject for m in new], ["Hello"])
        self.assertEqual(self.checker.check_account(self.acct), [])  # not announced twice

    def test_starts_from_the_apps_cache(self):
        c = FakeClient(self.acct, "secret")
        _, uv = c.select("INBOX")
        top = max(FakeClient.mailbox.folders["INBOX"])
        self.store.reset_folder(self.acct.id, "INBOX", uv)
        self.store.upsert(c.fetch_headers("INBOX", f"{top}:{top}", by_uid=True))
        self.add(902, "Ann Lee <ann@example.com>", "New since the app closed")
        self.assertEqual([m.subject for m in self.checker.check_account(self.acct)],
                         ["New since the app closed"])

    def test_quiet_while_the_app_is_open(self):
        cfg = Config(accounts=[self.acct])
        cfg.save()
        self.checker.check_all()
        self.add(903, "Zed Ray <zed@example.com>", "While open")
        background.app_is_open = lambda: True
        self.checker.check_all()
        self.assertEqual(self.told, [])
        background.app_is_open = lambda: False
        self.add(904, "Zed Ray <zed@example.com>", "After closing")
        self.checker.check_all()
        self.assertEqual(len(self.told), 1)
        self.assertEqual(self.told[0][0], "New mail from Zed Ray: After closing")

    def test_message_text(self):
        from postroom.models import Message
        m1 = Message(account_id="a", folder="INBOX", uid=1, subject="One", from_name="Ann", date_ts=1)
        m2 = Message(account_id="a", folder="INBOX", uid=2, subject="Two", from_name="Bob", date_ts=2)
        self.assertEqual(background.message_text("Work", [m1, m2], True),
                         "In Work: 2 new messages, the latest from Bob")


if __name__ == "__main__":
    unittest.main()
