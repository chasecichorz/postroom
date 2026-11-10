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
from .imap import NETWORK_ERRORS, ImapClient, ImapError, uid_set
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
    def __init__(self, account: Account, store: Store, draft_store=None):
        self.account = account
        self.store = store
        self.draft_store = draft_store
        self.client: ImapClient | None = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                          thread_name_prefix=f"imap-{account.name}")
        self.folders: list[Folder] = []
        self._jobs = []

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
        self._jobs = [future for future in self._jobs if not future.done()]
        self._jobs.append(self.pool.submit(job))

    def shutdown(self):
        def bye():
            if self.client:
                self.client.close()
        for future in self._jobs:
            future.cancel()
        self.pool.submit(bye)
        self.pool.shutdown(wait=False, cancel_futures=False)

    # -- runs on the worker thread ----------------------------------------------------------
    def _client(self) -> ImapClient:
        if self.client is None:
            self.client = ImapClient(self.account, lambda: credentials.secret(self.account))
        return self.client

    def _run(self, op, retry=True):
        """Run op(client); on a dropped connection, reconnect once and retry."""
        c = self._client()
        try:
            return op(c)
        except NETWORK_ERRORS:
            c.close()
            if not retry:
                raise ImapError("The connection was lost. The operation may have completed; "
                                "refresh the folder before trying again.")
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
            self.store.trim(acct, path, max(WINDOW, int(self.store.meta(f"history:{acct}:{path}") or 0)))
            self._harvest(path, new)
            return SyncResult(new, changed or bool(new), first=not cached)
        return self._run(op)

    def load_older(self, path: str, limit=500):
        acct = self.account.id
        def op(c):
            _exists, validity = c.select(path)
            if self.store.uidvalidity(acct, path) != validity:
                self.store.reset_folder(acct, path, validity)
            cached = self.store.uids(acct, path)
            before = cached[0] if cached else None
            criteria = f"UID 1:{before - 1}".encode() if before and before > 1 else b"ALL"
            candidates = [] if before == 1 else c.search_uids(path, criteria)
            candidates = [u for u in candidates if before is None or u < before]
            chosen = candidates[-limit:]
            messages = c.fetch_headers(path, uid_set(chosen), by_uid=True) if chosen else []
            self.store.upsert(messages)
            self.store.set_meta(f"history:{acct}:{path}", str(max(WINDOW, len(cached) + len(messages))))
            return len(messages), len(candidates) > len(chosen)
        return self._run(op)

    def search(self, path, query, before=None, offline=False, limit=500):
        if offline:
            messages = [m for m in self.store.messages(self.account.id, path)
                        if (before is None or m.uid < before) and
                        query.matches(m, self.store.body(self.account.id, path, m.uid))]
            messages.sort(key=lambda m: m.uid)
            return messages[-limit:], len(messages) > limit
        def op(c):
            _exists, validity = c.select(path)
            if self.store.uidvalidity(self.account.id, path) != validity:
                self.store.reset_folder(self.account.id, path, validity)
            uids = c.search_uids(path, query.criteria())
            if before is not None:
                uids = [u for u in uids if u < before]
            chosen = uids[-limit:]
            messages = c.fetch_headers(path, uid_set(chosen), by_uid=True) if chosen else []
            # Search results must not create gaps in the normal folder cache.
            return messages, len(uids) > limit
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
        self._run(lambda c: c.move(path, uids, dest), retry=False)
        self.store.delete(self.account.id, path, uids)

    def delete_forever(self, path: str, uids: list[int]) -> None:
        self._run(lambda c: c.delete_forever(path, uids), retry=False)
        self.store.delete(self.account.id, path, uids)

    def send(self, draft: sending.Draft) -> sending.SendResult:
        try:
            pw = credentials.secret(self.account)
        except credentials.CredentialError as e:
            raise sending.SendError(str(e)) from e
        msg = sending.build(self.account, draft)
        try:
            result = sending.send(self.account, pw, msg, to_addrs=draft.pending_recipients)
        except sending.DeliveryUncertain:
            draft.delivery_state = "uncertain"
            if self.draft_store:
                try:
                    self.draft_store.save(self.account.id, draft)
                except Exception:
                    log.exception("saving uncertain delivery state failed")
            raise
        if result is None:  # compatibility with simple test transports
            result = sending.SendResult([a for _, a in sending._addresses(draft.to + "," + draft.cc)])
        raw = msg.as_bytes()
        draft.pending_recipients = result.refused or []
        draft.delivery_state = "partial" if result.refused else "sent"
        if result.refused:
            refused = {a.casefold() for a in result.refused}
            draft.to = sending._fmt([(n, a) for n, a in sending._addresses(draft.to) if a.casefold() in refused])
            draft.cc = sending._fmt([(n, a) for n, a in sending._addresses(draft.cc) if a.casefold() in refused])
        if self.draft_store:
            try:
                self.draft_store.accepted(self.account.id, draft, raw, result.accepted)
            except Exception:
                log.exception("saving local sent copy failed")
                result.warning = "Mail was accepted, but saving its local delivery record failed. Check Sent before retrying."
        if not self.account.is_gmail:
            sent = next((f for f in self.folders if f.special == "sent"), None)
            if sent:
                try:
                    self._run(lambda c: c.append(sent.path, raw), retry=False)
                except Exception:
                    log.exception("saving to Sent failed")
                    result.warning += " Mail was accepted, but its Sent folder copy could not be confirmed."
            else:
                result.warning += " Mail was accepted, but this account has no Sent folder."
        if draft.reply_source and draft.reply_source[0] == self.account.id:
            _acct, folder, uid = draft.reply_source
            try:
                self._run(lambda c: c.set_flags(folder, [uid], "\\Answered", True))
            except Exception:
                log.exception("marking answered failed")
        return result


def check_login(account: Account, password: str) -> None:
    """Try signing in; raises ImapError with a readable message on failure."""
    c = ImapClient(account, password)
    c.connect()
    c.close()
