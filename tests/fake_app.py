"""Run Mailpane against the in-memory fake Gmail account (for manual and screen reader testing).

    python3 -m tests.fake_app
"""
import os
import sys
import tempfile

os.environ.setdefault("MAILPANE_PROFILE", tempfile.mkdtemp(prefix="mailpane-fake-"))
os.environ["MAILPANE_NO_KEYRING"] = "1"

from mailpane import passwords, sending, session  # noqa: E402
from mailpane.config import Account, Config  # noqa: E402
from tests.fake_imap import FakeClient  # noqa: E402

session.ImapClient = FakeClient
sending.send = lambda account, pw, msg: print("SENT:", msg["To"], "|", msg["Subject"], flush=True)

cfg = Config.load()
if not cfg.accounts:
    acct = Account(name="Personal", email="me@gmail.com", full_name="Me")
    cfg.accounts.append(acct)
    cfg.save()
    passwords.store(acct.id, "x", "secret")

from mailpane.app import main  # noqa: E402

sys.exit(main([sys.argv[0]]))
