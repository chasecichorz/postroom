"""Check for new mail while Postroom itself is closed: `postroom --background`.

Every few minutes each account's Inbox is opened read-only (IMAP EXAMINE) and anything
newer than what was last seen is announced: Postroom's chime, a spoken message through
speech-dispatcher (as I38's own scripts speak), and a desktop notification when a
notification service is running. Nothing on the server is changed.

While the Postroom window is open this stays quiet, because the app announces new mail
itself, and carries on from the app's cache afterwards so old mail is never announced.
Only one copy runs at a time.
"""
from __future__ import annotations

import fcntl
import logging
import shutil
import signal
import subprocess
import threading
import time

from gi.repository import Gio, GLib

from . import credentials, sounds
from .config import APP_ID, Config, cache_dir
from .imap import ImapClient, ImapError
from .models import Message
from .store import Store

log = logging.getLogger("postroom.background")
INBOX = "INBOX"


def app_is_open() -> bool:
    """True when the Postroom window is running (it owns its D-Bus name)."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", (APP_ID,)), GLib.VariantType("(b)"),
                              Gio.DBusCallFlags.NONE, 2000, None)
        return reply.unpack()[0]
    except GLib.Error:
        return False


def _notifications_available() -> bool:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", ("org.freedesktop.Notifications",)),
                              GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 2000, None)
        return reply.unpack()[0]
    except GLib.Error:
        return False


def message_text(account_name: str, fresh: list[Message], several_accounts: bool) -> str:
    latest = max(fresh, key=lambda m: m.date_ts)
    who = latest.from_name or latest.from_addr or "unknown sender"
    where = f"In {account_name}: " if several_accounts else ""
    if len(fresh) == 1:
        return f"{where}New mail from {who}: {latest.subject or 'no subject'}"
    return f"{where}{len(fresh)} new messages, the latest from {who}"


def tell(text: str, sound: bool, speak: bool) -> None:
    if sound:
        sounds.play_new_mail()
    if speak and shutil.which("spd-say"):
        subprocess.Popen(["spd-say", "-P", "message", "--", text], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    if _notifications_available() and shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "--app-name=Postroom", "--icon=mail-unread",
                          "Postroom", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


class Checker:
    def __init__(self, store: Store):
        self.store = store

    def _baseline(self, acct_id: str, uidvalidity: int) -> int | None:
        """The highest Inbox UID already known, from our own record or the app's cache."""
        mine = self.store.meta(f"bg:{acct_id}")
        last = None
        if mine:
            uv, uid = (int(x) for x in mine.split(":"))
            if uv == uidvalidity:
                last = uid
        if self.store.uidvalidity(acct_id, INBOX) == uidvalidity:
            cached = self.store.uids(acct_id, INBOX)
            if cached:
                last = max(last or 0, cached[-1])
        return last

    def check_account(self, acct) -> list[Message]:
        """New unread Inbox mail since last time. The first check only records where we are."""
        c = ImapClient(acct, lambda: credentials.secret(acct))
        try:
            c.connect()
            exists, uidvalidity = c.select(INBOX, readonly=True)
            last = self._baseline(acct.id, uidvalidity)
            if exists == 0:
                self.store.set_meta(f"bg:{acct.id}", f"{uidvalidity}:{last or 0}")
                return []
            if last is None:  # first run: start from the newest message, announce nothing
                newest = c.fetch_headers(INBOX, f"{exists}:{exists}", by_uid=False)
                top = max((m.uid for m in newest), default=0)
                self.store.set_meta(f"bg:{acct.id}", f"{uidvalidity}:{top}")
                return []
            new = [m for m in c.fetch_headers(INBOX, f"{last + 1}:*", by_uid=True) if m.uid > last]
            if new:
                self.store.set_meta(f"bg:{acct.id}", f"{uidvalidity}:{max(m.uid for m in new)}")
            return [m for m in new if not m.seen]
        finally:
            c.close()

    def check_all(self) -> None:
        cfg = Config.load()
        quiet = app_is_open()
        for acct in cfg.accounts:
            try:
                fresh = self.check_account(acct)
            except (ImapError, credentials.CredentialError, OSError) as e:
                log.warning("%s: %s", acct.name, e)
                continue
            if fresh and not quiet:
                text = message_text(acct.name, fresh, len(cfg.accounts) > 1)
                log.info("%s: %d new", acct.name, len(fresh))
                tell(text, cfg.settings.new_mail_sound, cfg.settings.new_mail_announce)


def run() -> int:
    lock_path = cache_dir() / "background.lock"
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Postroom's background check is already running.")
        return 0
    logging.getLogger().handlers.clear()
    logging.basicConfig(filename=cache_dir() / "background.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("started")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    checker = Checker(Store(cache_dir() / "cache.db"))
    while not stop.is_set():
        try:
            checker.check_all()
        except Exception:  # noqa: BLE001 - keep checking next time
            log.exception("check failed")
        minutes = max(1, Config.load().settings.check_minutes)
        stop.wait(minutes * 60)
    log.info("stopped")
    return 0
