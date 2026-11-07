"""One background worker per account. All IMAP traffic happens here, never on the UI thread.

Each operation runs on the account's single worker thread (so commands never interleave on
the one IMAP connection) and its result is handed back to the GTK main loop.
"""
from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass

from gi.repository import GLib

from . import carddav, contacts, credentials, oauth2, passwords, sending
from .config import Account
from .imap import NETWORK_ERRORS, ImapClient, ImapError
from .models import Folder, Message
from .store import Store

log = logging.getLogger(__name__)

# How many of the newest messages per folder are kept locally.
WINDOW = 1000
FIRST_SYNC = 500


@dataclass
class SyncResult:
    new: list[Message]
    changed: bool
    first: bool = False   # the folder had nothing cached: everything in `new` is old mail


def _on_main(fn, *args):
    def run():
        fn(*args)
        return GLib.SOURCE_REMOVE
    GLib.idle_add(run)


class AccountSession:
    def __init__(self, account: Account, store: Store):
        self.account = account
        self.store = store
        self.client: ImapClient | None = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                          thread_name_prefix=f"imap-{account.name}")
        self.folders: list[Folder] = []

    def submit(self, fn, *args, on_done=None, on_error=None):
        def job():
            try:
                result = fn(*args)
            except Exception as e:  # noqa: BLE001 - reported to the UI
                log.exception("%s failed", getattr(fn, "__name__", fn))
                if on_error:
                    _on_main(on_error, e)
            else:
                if on_done:
                    _on_main(on_done, result)
        self.pool.submit(job)

    def shutdown(self):
        def bye():
            if self.client:
                self.client.close()
        self.pool.submit(bye)
        self.pool.shutdown(wait=False, cancel_futures=True)

    # -- runs on the worker thread ----------------------------------------------------------
    def _client(self) -> ImapClient:
        if self.client is None:
            self.client = ImapClient(self.account, lambda: credentials.secret(self.account))
        return self.client

    def _run(self, op):
        """Run op(client); on a dropped connection, reconnect once and retry."""
        c = self._client()
        try:
            return op(c)
        except NETWORK_ERRORS:
            c.close()
            return op(c)

    def list_folders(self) -> list[Folder]:
        folders = self._run(lambda c: c.list_folders())
        self.store.save_folders(self.account.id, folders)
        self.folders = folders
        return folders

    def folder_counts(self, paths: list[str]) -> dict[str, tuple[int, int]]:
        out = {}
        for p in paths:
            try:
                out[p] = self._run(lambda c, p=p: c.status(p))
            except ImapError:
                pass
        return out

    def sync_folder(self, path: str) -> SyncResult:
        acct = self.account.id

        def op(c: ImapClient) -> SyncResult:
            exists, uidvalidity = c.select(path)
            if self.store.uidvalidity(acct, path) != uidvalidity:
                self.store.reset_folder(acct, path, uidvalidity)
            cached = self.store.uids(acct, path)
            if exists == 0:
                self.store.delete(acct, path, cached)
                return SyncResult([], bool(cached))
            changed = False
            if not cached:
                start = max(1, exists - FIRST_SYNC + 1)
                new = c.fetch_headers(path, f"{start}:{exists}", by_uid=False)
            else:
                top = cached[-1]
                new = [m for m in c.fetch_headers(path, f"{top + 1}:*", by_uid=True) if m.uid > top]
                flags = c.fetch_flags(path, f"{cached[0]}:{top}")
                gone = set(cached) - set(flags)
                if gone:
                    self.store.delete(acct, path, gone)
                    changed = True
                changed |= self.store.update_flags(acct, path, flags) > 0
            self.store.upsert(new)
            self.store.trim(acct, path, WINDOW)
            self._harvest(path, new)
            return SyncResult(new, changed or bool(new), first=not cached)
        return self._run(op)

    # -- address book -----------------------------------------------------------------------
    def _is_sent(self, path: str) -> bool:
        return any(f.path == path and f.special == "sent" for f in self.folders)

    def _harvest(self, path: str, msgs: list[Message]) -> None:
        if msgs:
            own = {self.account.email.lower()}
            self.store.add_contacts(contacts.harvest(msgs, own, self._is_sent(path)))

    def backfill_contacts(self) -> int:
        """Once per account: collect addresses from mail already cached, and sync Sent so the
        people you write to are known. Returns how many contacts the book holds."""
        key = f"contacts-backfill:{self.account.id}"
        if self.store.meta(key):
            return self.store.contact_count()
        by_folder: dict[str, list[Message]] = {}
        for path, m in self.store.all_messages(self.account.id):
            by_folder.setdefault(path, []).append(m)
        for path, msgs in by_folder.items():
            self._harvest(path, msgs)
        sent = next((f for f in self.folders if f.special == "sent"), None)
        if sent and sent.path not in by_folder:
            try:
                self.sync_folder(sent.path)  # harvests as it goes
            except Exception:  # noqa: BLE001 - the address book is a nicety
                log.exception("syncing Sent for the address book failed")
        self.store.set_meta(key, "1")
        return self.store.contact_count()

    GOOGLE_CONTACTS_EVERY = 6 * 3600

    def can_import_google_contacts(self) -> bool:
        return self.account.auth == "oauth2" and self.account.is_gmail

    def import_google_contacts(self, force: bool = False) -> str | None:
        """Bring in Google contacts (at most every few hours unless `force`). Returns what
        to tell you, or None when nothing was done."""
        if not self.can_import_google_contacts():
            return ("Google contacts need browser sign-in. Use File, Edit Account, then Sign "
                    "in with browser.") if force else None
        key = f"google-contacts:{self.account.id}"
        last = float(self.store.meta(key) or 0)
        if not force and time.time() - last < self.GOOGLE_CONTACTS_EVERY:
            return None
        try:
            token = credentials.secret(self.account)
        except credentials.CredentialError as e:
            return str(e) if force else None
        if not credentials.grants(self.account, oauth2.GOOGLE_CONTACTS_SCOPE):
            log.info("Google contacts: the sign-in does not include contacts access")
            return ("To bring in your Google contacts, sign in again so Postroom may read them: "
                    "File, Edit Account, then Sign in with browser.") if force else None
        try:
            found = carddav.google_contacts(self.account.email, token)
        except carddav.CardDavError as e:
            log.warning("Google contacts: %s", e)
            return str(e) if force else None
        self.store.add_contacts(found)
        self.store.set_meta(key, str(time.time()))
        n = len({c.email for c in found})
        log.info("Google contacts: %d addresses read", n)
        return f"{n} Google contact{'s' if n != 1 else ''} in the address book"

    def fetch_body(self, path: str, uid: int) -> bytes:
        raw = self.store.body(self.account.id, path, uid)
        if raw is None:
            raw = self._run(lambda c: c.fetch_body(path, uid))
            self.store.save_body(self.account.id, path, uid, raw)
        return raw

    def set_flag(self, path: str, uids: list[int], flag: str, on: bool) -> None:
        self._run(lambda c: c.set_flags(path, uids, flag, on))

    def move(self, path: str, uids: list[int], dest: str) -> None:
        self._run(lambda c: c.move(path, uids, dest))
        self.store.delete(self.account.id, path, uids)

    def delete_forever(self, path: str, uids: list[int]) -> None:
        self._run(lambda c: c.delete_forever(path, uids))
        self.store.delete(self.account.id, path, uids)

    def send(self, draft: sending.Draft) -> None:
        try:
            pw = credentials.secret(self.account)
        except credentials.CredentialError as e:
            raise sending.SendError(str(e)) from e
        msg = sending.build(self.account, draft)
        sending.send(self.account, pw, msg)
        # Gmail files sent mail by itself; other servers need a copy in Sent.
        if not self.account.is_gmail:
            sent = next((f for f in self.folders if f.special == "sent"), None)
            if sent:
                try:
                    self._run(lambda c: c.append(sent.path, msg.as_bytes()))
                except Exception:  # noqa: BLE001 - the mail went out; the copy is a nicety
                    log.exception("saving to Sent failed")
        if draft.reply_source:
            _acct, folder, uid = draft.reply_source
            try:
                self._run(lambda c: c.set_flags(folder, [uid], "\\Answered", True))
            except Exception:  # noqa: BLE001
                log.exception("marking answered failed")


def check_login(account: Account, password: str) -> None:
    """Try signing in; raises ImapError with a readable message on failure."""
    c = ImapClient(account, password)
    c.connect()
    c.close()
