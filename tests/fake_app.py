"""Run Postroom against the in-memory fake Gmail account (for manual and screen reader testing).

    python3 -m tests.fake_app
"""
import os
import sys
import tempfile

os.environ.setdefault("POSTROOM_PROFILE", tempfile.mkdtemp(prefix="postroom-fake-"))
os.environ["POSTROOM_NO_KEYRING"] = "1"

from postroom import passwords, sending, session  # noqa: E402
from postroom.config import Account, Config  # noqa: E402
from tests.fake_imap import FakeClient  # noqa: E402

session.ImapClient = FakeClient
sending.send = lambda account, pw, msg, **kwargs: print("SENT:", msg["To"], "|", msg["Subject"], flush=True)

cfg = Config.load()
if not cfg.accounts:
    acct = Account(name="Personal", email="me@gmail.com", full_name="Me")
    cfg.accounts.append(acct)
    cfg.save()
    passwords.store(acct.id, "x", "secret")

from postroom.app import main  # noqa: E402

sys.exit(main([sys.argv[0]]))
