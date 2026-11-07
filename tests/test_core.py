import os
import tempfile
import time
import unittest

os.environ["POSTROOM_PROFILE"] = tempfile.mkdtemp()
os.environ["POSTROOM_NO_KEYRING"] = "1"

from postroom import folder_tree, imap, mime, passwords, sending, threads, utf7  # noqa: E402
from postroom.config import Account, Config  # noqa: E402
from postroom.models import Message  # noqa: E402
from postroom.store import Store  # noqa: E402
from pathlib import Path  # noqa: E402


class Utf7Tests(unittest.TestCase):
    def test_round_trip(self):
        for s in ["Inbox", "Entwürfe", "Bills & Receipts", "日本語", "a&b"]:
            self.assertEqual(utf7.decode(utf7.encode(s)), s)
        self.assertEqual(utf7.decode("Entw&APw-rfe"), "Entwürfe")
        self.assertEqual(utf7.encode("a&b"), "a&-b")


GMAIL_LIST = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
    b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Drafts"',
    b'(\\HasNoChildren \\Important) "/" "[Gmail]/Important"',
    b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
    b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
    b'(\\Flagged \\HasNoChildren) "/" "[Gmail]/Starred"',
    b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
    b'(\\HasChildren) "/" "Work"',
    b'(\\HasNoChildren) "/" "Work/Projects"',
    b'(\\HasNoChildren) "/" "Receipts"',
    b'(\\HasNoChildren) "/" "Entw&APw-rfe"',
    (b'(\\HasNoChildren) "/" {12}', b'Odd "folder"'),
]


class FolderTests(unittest.TestCase):
    def test_parse_and_tree(self):
        folders = [imap.parse_list_line(x) for x in GMAIL_LIST]
        self.assertTrue(all(folders))
        by = {f.path: f for f in folders}
        self.assertEqual(by["INBOX"].special, "inbox")
        self.assertEqual(by["[Gmail]/Sent Mail"].special, "sent")
        self.assertFalse(by["[Gmail]"].selectable)
        self.assertEqual(by["Entw&APw-rfe"].name, "Entwürfe")
        self.assertIn('Odd "folder"', by)
        tree = folder_tree.build(folders)
        names = [n.name for n in tree]
        self.assertEqual(names[:8], ["Inbox", "Starred", "Important", "Sent", "Drafts",
                                     "All Mail", "Spam", "Trash"])
        self.assertNotIn("[Gmail]", names)
        work = next(n for n in tree if n.name == "Work")
        self.assertEqual([c.name for c in work.children], ["Projects"])
        self.assertEqual(folder_tree.find(tree, "trash").folder.path, "[Gmail]/Trash")

    def test_plain_server_names(self):
        lines = [b'(\\HasChildren) "." "INBOX"', b'(\\HasNoChildren) "." "INBOX.Sent"',
                 b'(\\HasNoChildren) "." "INBOX.Trash"', b'(\\HasNoChildren) "." "INBOX.Work"']
        folders = [imap.parse_list_line(x) for x in lines]
        self.assertEqual([f.special for f in folders], ["inbox", "sent", "trash", None])
        tree = folder_tree.build(folders)
        self.assertEqual([n.name for n in tree], ["Inbox", "Sent", "Trash", "Work"])


class FetchParseTests(unittest.TestCase):
    def test_flags_after_literal(self):
        header = (b"From: =?utf-8?q?J=C3=B6rg?= <jorg@example.com>\r\nSubject: Hello\r\n"
                  b"Date: Tue, 03 Nov 2026 10:00:00 +0000\r\n"
                  b"Content-Type: multipart/mixed; boundary=x\r\n\r\n")
        data = [(b'1 (UID 42 RFC822.SIZE 900 X-GM-THRID 777 BODY[HEADER.FIELDS (FROM)] {%d}'
                 % len(header), header), b' FLAGS (\\Seen \\Flagged))',
                (b'2 (UID 43 FLAGS () BODY[HEADER.FIELDS (FROM)] {3}', b'\r\n\r'), b')']
        items = list(imap.iter_fetch(data))
        self.assertEqual(len(items), 2)
        m = imap.parse_header_block("a", "INBOX", *items[0])
        self.assertEqual((m.uid, m.from_name, m.subject, m.thread_id), (42, "Jörg", "Hello", "777"))
        self.assertEqual(m.flags, {"\\seen", "\\flagged"})
        self.assertTrue(m.has_attachment)
        self.assertGreater(m.date_ts, 0)

    def test_uid_set(self):
        self.assertEqual(imap.uid_set([7, 1, 2, 3, 9, 10]), "1:3,7,9:10")


def msg(uid, subject, ts, sender="Ann", seen=True, thrid=""):
    return Message("a", "INBOX", uid, subject=subject, from_name=sender, date_ts=ts,
                   flags={"\\seen"} if seen else set(), thread_id=thrid)


class ThreadTests(unittest.TestCase):
    def test_grouping(self):
        ms = [msg(1, "Lunch?", 100), msg(2, "Re: lunch?", 300, "Bob", seen=False),
              msg(3, "Other", 200), msg(4, "", 50), msg(5, "", 60)]
        g = threads.group(ms, True)
        self.assertEqual([[m.uid for m in t] for t in g], [[1, 2], [3], [5], [4]])
        self.assertEqual(len(threads.group(ms, False)), 5)

    def test_gmail_thread_id_wins(self):
        ms = [msg(1, "A", 1, thrid="x"), msg(2, "B", 2, thrid="x"), msg(3, "A", 3, thrid="y")]
        self.assertEqual(len(threads.group(ms, True)), 2)

    def test_speech(self):
        m = msg(2, "Re: lunch?", time.time(), "Bob", seen=False)
        self.assertTrue(threads.message_speech(m).startswith("Unread, Bob, Re: lunch?, "))
        s = threads.thread_speech([msg(1, "Lunch?", 1), m], expanded=False)
        self.assertTrue(s.startswith("Unread, Bob, Ann, Re: lunch?"))
        self.assertIn("2 messages, 1 unread", s)
        self.assertTrue(s.endswith("collapsed"))

    def test_friendly_date(self):
        now = time.mktime((2026, 11, 7, 15, 0, 0, 0, 0, -1))
        self.assertEqual(threads.friendly_date(now - 3600, now), "2:00 PM")
        self.assertTrue(threads.friendly_date(now - 86400, now).startswith("Yesterday"))
        self.assertEqual(threads.friendly_date(time.mktime((2025, 3, 4, 9, 0, 0, 0, 0, -1)), now),
                         "March 4, 2025")


RAW = (b"From: Ann <ann@example.com>\r\nTo: me@gmail.com, Cy <cy@example.com>\r\n"
       b"Cc: me@gmail.com\r\nSubject: Plans\r\nMessage-ID: <abc@example.com>\r\n"
       b"Date: Tue, 03 Nov 2026 10:00:00 +0000\r\nMIME-Version: 1.0\r\n"
       b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
       b"--B\r\nContent-Type: multipart/alternative; boundary=C\r\n\r\n"
       b"--C\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSee https://example.com/x.\r\nThanks\r\n"
       b"--C\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
       b"<html><head><style>p{}</style></head><body><p>See <a href='https://example.com/x'>"
       b"the plan</a></p><ul><li>One</li><li>Two</li></ul></body></html>\r\n--C--\r\n"
       b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=plan.pdf\r\n"
       b"Content-Transfer-Encoding: base64\r\n\r\nSGVsbG8=\r\n--B--\r\n")


class MimeTests(unittest.TestCase):
    def test_plain_preferred(self):
        pm = mime.parse(RAW)
        self.assertEqual(pm.text, "See https://example.com/x.\nThanks")
        self.assertEqual([l.url for l in pm.links], ["https://example.com/x"])
        self.assertEqual([(a.filename, a.data) for a in pm.attachments], [("plan.pdf", b"Hello")])

    def test_html(self):
        pm = mime.parse(RAW, prefer_html=True)
        self.assertEqual(pm.text, "See the plan\n\n• One\n• Two")
        self.assertEqual(pm.links[0].text, "the plan")
        self.assertNotIn("p{}", pm.text)


class SendingTests(unittest.TestCase):
    def test_reply_all_drops_self(self):
        acct = Account(name="P", email="me@gmail.com")
        d = sending.reply(acct, mime.parse(RAW), reply_all=True)
        self.assertEqual(d.to, "Ann <ann@example.com>")
        self.assertEqual(d.cc, "Cy <cy@example.com>")
        self.assertEqual(d.subject, "Re: Plans")
        self.assertIn("> See https://example.com/x.", d.body)
        self.assertEqual(d.in_reply_to, "<abc@example.com>")
        msg = sending.build(acct, d)
        self.assertEqual(msg["In-Reply-To"], "<abc@example.com>")

    def test_forward_keeps_attachments(self):
        d = sending.forward(mime.parse(RAW))
        self.assertEqual(d.subject, "Fwd: Plans")
        self.assertEqual(len(d.attachments), 1)
        msg = sending.build(Account(name="P", email="me@gmail.com"), sending.Draft(
            to="x@y.z", subject=d.subject, body=d.body, attachments=d.attachments))
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["plan.pdf"])

    def test_validate(self):
        self.assertIsNotNone(sending.validate(sending.Draft()))
        self.assertIsNotNone(sending.validate(sending.Draft(to="bob")))
        self.assertIsNone(sending.validate(sending.Draft(to="Bob <bob@x.com>")))


class StoreTests(unittest.TestCase):
    def test_round_trip_and_flags(self):
        s = Store(Path(tempfile.mkdtemp()) / "c.db")
        s.reset_folder("a", "INBOX", 5)
        s.upsert([msg(1, "x", 1), msg(2, "y", 2, seen=False)])
        self.assertEqual(s.uidvalidity("a", "INBOX"), 5)
        self.assertEqual(s.update_flags("a", "INBOX", {1: {"\\seen"}, 2: {"\\seen"}}), 1)
        self.assertTrue(all(m.seen for m in s.messages("a", "INBOX")))
        s.delete("a", "INBOX", [1])
        self.assertEqual(s.uids("a", "INBOX"), [2])


class ConfigTests(unittest.TestCase):
    def test_save_load_and_password_file(self):
        c = Config()
        c.accounts.append(Account(name="Personal", email="me@gmail.com"))
        c.save()
        again = Config.load()
        self.assertEqual(again.accounts[0].email, "me@gmail.com")
        passwords.store(again.accounts[0].id, "x", "abcd efgh")
        self.assertEqual(passwords.lookup(again.accounts[0].id), "abcd efgh")
        self.assertEqual(oct(os.stat(passwords._file()).st_mode & 0o777), "0o600")
        passwords.delete(again.accounts[0].id)
        self.assertIsNone(passwords.lookup(again.accounts[0].id))


if __name__ == "__main__":
    unittest.main()
