"""An in-memory stand-in for ImapClient, so the UI can be exercised without a real server."""
from __future__ import annotations

import time
from email.message import EmailMessage

from postroom.imap import ImapError, parse_header_block
from postroom.models import Folder

FOLDERS = [
    Folder("INBOX", "Inbox", "/", frozenset({"\\hasnochildren"}), "inbox"),
    Folder("[Gmail]", "[Gmail]", "/", frozenset({"\\noselect", "\\haschildren"})),
    Folder("[Gmail]/All Mail", "All Mail", "/", frozenset({"\\all"}), "all"),
    Folder("[Gmail]/Sent Mail", "Sent Mail", "/", frozenset({"\\sent"}), "sent"),
    Folder("[Gmail]/Trash", "Trash", "/", frozenset({"\\trash"}), "trash"),
    Folder("Work", "Work", "/", frozenset({"\\haschildren"})),
    Folder("Work/Projects", "Projects", "/", frozenset()),
]


def _raw(uid, frm, subject, body, ts, thread, attach=False, html=False):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "me@gmail.com"
    m["Subject"] = subject
    m["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(ts))
    m["Message-ID"] = f"<m{uid}@example.com>"
    m.set_content(body)
    if html:
        m.add_alternative(f"<p>{body}</p><p><a href='https://example.com/{uid}'>Read more</a></p>",
                          subtype="html")
    if attach:
        m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="plan.pdf")
    return m.as_bytes()


class Mailbox:
    def __init__(self):
        now = time.time()
        self.uidvalidity = 1
        self.next_uid = 1
        self.folders = {f.path: {} for f in FOLDERS if f.selectable}
        self.thr = {}
        rows = [
            ("Ann Lee <ann@example.com>", "Lunch on Friday?", "Are you free Friday at noon?", 7200, "t1"),
            ("Bob Stone <bob@example.com>", "Re: Lunch on Friday?", "Count me in.", 3600, "t1"),
            ("Ann Lee <ann@example.com>", "Re: Lunch on Friday?", "Great, see you both.", 1800, "t1"),
            ("Carol <carol@example.com>", "Project plan", "The plan is attached.", 86400, "t2"),
            ("Newsletter <news@example.com>", "This week", "Top stories this week.", 172800, "t3"),
        ]
        for i, (frm, subj, body, age, thr) in enumerate(rows):
            self.add("INBOX", _raw(self.next_uid, frm, subj, body, now - age, thr,
                                   attach=subj == "Project plan", html=thr == "t3"),
                     set() if i in (1, 2) else {"\\seen"}, thr)
        self.add("Work/Projects", _raw(99, "Dee <dee@example.com>", "Kickoff", "Monday 9am.",
                                       now - 5000, "t9"), {"\\seen"}, "t9")

    def add(self, folder, raw, flags, thr):
        uid = self.next_uid
        self.next_uid += 1
        self.folders[folder][uid] = (raw, set(flags))
        self.thr[uid] = thr
        return uid


class FakeClient:
    mailbox: Mailbox | None = None
    fail_login = False
    select_delay = 0.0  # seconds; lets a test act while a folder refresh is still running

    def __init__(self, account, password):
        self.account = account
        self.password = password
        self.selected = None
        self.caps = {"X-GM-EXT-1", "MOVE", "UIDPLUS"}
        if FakeClient.mailbox is None:
            FakeClient.mailbox = Mailbox()

    @property
    def mb(self):
        return FakeClient.mailbox

    is_gmail = True

    def connect(self):
        pw = self.password() if callable(self.password) else self.password
        if FakeClient.fail_login or pw != "secret":
            raise ImapError("Sign-in failed: [AUTHENTICATIONFAILED] Invalid credentials")

    def close(self):
        pass

    def list_folders(self):
        return list(FOLDERS)

    def status(self, path):
        msgs = self.mb.folders[path]
        return len(msgs), sum(1 for _, f in msgs.values() if "\\seen" not in f)

    def select(self, path, readonly=False):
        if FakeClient.select_delay:
            import time as _t
            _t.sleep(FakeClient.select_delay)
        self.selected = path
        return len(self.mb.folders[path]), self.mb.uidvalidity

    def _headers(self, path, uids):
        out = []
        for uid in uids:
            raw, flags = self.mb.folders[path][uid]
            header = raw.split(b"\n\n", 1)[0] + b"\n\n"
            meta = (f'1 (UID {uid} X-GM-THRID {abs(hash(self.mb.thr[uid])) % 10**9} '
                    f'FLAGS ({" ".join(flags)}) RFC822.SIZE {len(raw)}').encode()
            out.append(parse_header_block(self.account.id, path, meta, header))
        return out

    def fetch_headers(self, path, msg_set, by_uid):
        uids = sorted(self.mb.folders[path])
        if by_uid:
            lo = int(msg_set.split(":")[0])
            sel = [u for u in uids if u >= lo] or uids[-1:]
        else:
            a, b = (int(x) for x in msg_set.split(":"))
            sel = uids[a - 1:b]
        return self._headers(path, sel)

    def fetch_flags(self, path, uid_set):
        return {u: set(f) for u, (_, f) in self.mb.folders[path].items()}

    def fetch_body(self, path, uid):
        return self.mb.folders[path][uid][0]

    def set_flags(self, path, uids, flags, add):
        for u in uids:
            f = self.mb.folders[path][u][1]
            (f.add if add else f.discard)(flags.lower())

    def move(self, path, uids, dest):
        for u in uids:
            raw, f = self.mb.folders[path].pop(u)
            self.mb.folders[dest][u] = (raw, f)

    def delete_forever(self, path, uids):
        for u in uids:
            self.mb.folders[path].pop(u, None)

    def append(self, path, raw, flags="(\\Seen)"):
        self.mb.add(path, raw, {"\\seen"}, "sent")
