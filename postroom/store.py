"""Local SQLite cache of folder lists, message headers and message bodies."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .contacts import GOOGLE, Contact
from .models import Folder, Message

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
    account TEXT, folder TEXT, uid INTEGER,
    subject TEXT, from_name TEXT, from_addr TEXT, to_ TEXT, cc TEXT, date_ts REAL,
    flags TEXT, size INTEGER, has_attach INTEGER,
    msgid TEXT, in_reply_to TEXT, refs TEXT, thrid TEXT,
    PRIMARY KEY(account, folder, uid));
CREATE TABLE IF NOT EXISTS folder_state(
    account TEXT, folder TEXT, uidvalidity INTEGER, PRIMARY KEY(account, folder));
CREATE TABLE IF NOT EXISTS folder_list(account TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS bodies(
    account TEXT, folder TEXT, uid INTEGER, raw BLOB, PRIMARY KEY(account, folder, uid));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS contacts(
    email TEXT PRIMARY KEY, name TEXT, source TEXT, score INTEGER, last_ts REAL);
"""

_COLS = ("account, folder, uid, subject, from_name, from_addr, to_, cc, date_ts, flags, size, "
         "has_attach, msgid, in_reply_to, refs, thrid")

# Bodies are cached only for the most recently read messages, to keep the cache small.
BODY_CACHE_LIMIT = 300


def _row(m: Message) -> tuple:
    return (m.account_id, m.folder, m.uid, m.subject, m.from_name, m.from_addr, m.to, m.cc,
            m.date_ts, " ".join(sorted(m.flags)), m.size, int(m.has_attachment), m.message_id,
            m.in_reply_to, m.references, m.thread_id)


def _msg(r) -> Message:
    return Message(account_id=r[0], folder=r[1], uid=r[2], subject=r[3] or "", from_name=r[4] or "",
                   from_addr=r[5] or "", to=r[6] or "", cc=r[7] or "", date_ts=r[8] or 0.0,
                   flags=set((r[9] or "").split()), size=r[10] or 0, has_attachment=bool(r[11]),
                   message_id=r[12] or "", in_reply_to=r[13] or "", references=r[14] or "",
                   thread_id=r[15] or "")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(_SCHEMA)
            self.db.execute("PRAGMA journal_mode=WAL")

    # -- folders ----------------------------------------------------------------------------
    def save_folders(self, account: str, folders: list[Folder]) -> None:
        data = json.dumps([{"path": f.path, "name": f.name, "delimiter": f.delimiter,
                            "flags": sorted(f.flags), "special": f.special} for f in folders])
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO folder_list VALUES (?, ?)", (account, data))

    def load_folders(self, account: str) -> list[Folder] | None:
        with self.lock:
            r = self.db.execute("SELECT data FROM folder_list WHERE account=?", (account,)).fetchone()
        if not r:
            return None
        return [Folder(path=d["path"], name=d["name"], delimiter=d["delimiter"],
                       flags=frozenset(d["flags"]), special=d["special"]) for d in json.loads(r[0])]

    def uidvalidity(self, account: str, folder: str) -> int | None:
        with self.lock:
            r = self.db.execute("SELECT uidvalidity FROM folder_state WHERE account=? AND folder=?",
                                (account, folder)).fetchone()
        return r[0] if r else None

    def reset_folder(self, account: str, folder: str, uidvalidity: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM messages WHERE account=? AND folder=?", (account, folder))
            self.db.execute("DELETE FROM bodies WHERE account=? AND folder=?", (account, folder))
            self.db.execute("INSERT OR REPLACE INTO folder_state VALUES (?, ?, ?)",
                            (account, folder, uidvalidity))

    # -- messages ---------------------------------------------------------------------------
    def messages(self, account: str, folder: str) -> list[Message]:
        with self.lock:
            rows = self.db.execute(f"SELECT {_COLS} FROM messages WHERE account=? AND folder=? "
                                   "ORDER BY date_ts", (account, folder)).fetchall()
        return [_msg(r) for r in rows]

    def uids(self, account: str, folder: str) -> list[int]:
        with self.lock:
            rows = self.db.execute("SELECT uid FROM messages WHERE account=? AND folder=? ORDER BY uid",
                                   (account, folder)).fetchall()
        return [r[0] for r in rows]

    def upsert(self, msgs: list[Message]) -> None:
        if not msgs:
            return
        with self.lock, self.db:
            self.db.executemany(f"INSERT OR REPLACE INTO messages ({_COLS}) VALUES "
                                f"({','.join('?' * 16)})", [_row(m) for m in msgs])

    def update_flags(self, account: str, folder: str, flags: dict[int, set[str]]) -> int:
        """Store new flags; return how many messages actually changed."""
        if not flags:
            return 0
        with self.lock, self.db:
            current = dict(self.db.execute(
                "SELECT uid, flags FROM messages WHERE account=? AND folder=?", (account, folder)))
            changed = [(" ".join(sorted(f)), account, folder, uid) for uid, f in flags.items()
                       if uid in current and set((current[uid] or "").split()) != f]
            self.db.executemany("UPDATE messages SET flags=? WHERE account=? AND folder=? AND uid=?",
                                changed)
        return len(changed)

    def delete(self, account: str, folder: str, uids) -> None:
        uids = list(uids)
        if not uids:
            return
        with self.lock, self.db:
            for table in ("messages", "bodies"):
                self.db.executemany(f"DELETE FROM {table} WHERE account=? AND folder=? AND uid=?",
                                    [(account, folder, u) for u in uids])

    def trim(self, account: str, folder: str, keep: int) -> None:
        """Keep only the newest `keep` messages of a folder in the cache."""
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM messages WHERE account=? AND folder=? AND uid NOT IN ("
                "SELECT uid FROM messages WHERE account=? AND folder=? ORDER BY uid DESC LIMIT ?)",
                (account, folder, account, folder, keep))

    # -- bodies -----------------------------------------------------------------------------
    def body(self, account: str, folder: str, uid: int) -> bytes | None:
        with self.lock:
            r = self.db.execute("SELECT raw FROM bodies WHERE account=? AND folder=? AND uid=?",
                                (account, folder, uid)).fetchone()
        return r[0] if r else None

    def save_body(self, account: str, folder: str, uid: int, raw: bytes) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO bodies VALUES (?, ?, ?, ?)",
                            (account, folder, uid, raw))
            self.db.execute("DELETE FROM bodies WHERE rowid NOT IN "
                            "(SELECT rowid FROM bodies ORDER BY rowid DESC LIMIT ?)",
                            (BODY_CACHE_LIMIT,))

    # -- address book -----------------------------------------------------------------------
    def add_contacts(self, contacts: list[Contact]) -> None:
        """Merge in contacts: scores add up, and a Google contact's name wins."""
        if not contacts:
            return
        with self.lock, self.db:
            for c in contacts:
                row = self.db.execute("SELECT name, source, score, last_ts FROM contacts WHERE email=?",
                                      (c.email,)).fetchone()
                if row is None:
                    self.db.execute("INSERT INTO contacts VALUES (?, ?, ?, ?, ?)",
                                    (c.email, c.name, c.source, c.score, c.last_ts))
                    continue
                name, source, score, last = row
                if c.source == GOOGLE:
                    if source != GOOGLE:  # the Google boost is added once
                        score += c.score
                    name, source = c.name or name, GOOGLE
                else:
                    name, score = name or c.name, score + c.score
                self.db.execute("UPDATE contacts SET name=?, source=?, score=?, last_ts=? WHERE email=?",
                                (name, source, score, max(last or 0, c.last_ts), c.email))

    def search_contacts(self, text: str, limit: int = 10) -> list[Contact]:
        """Contacts whose address, name or any word of the name starts with `text`."""
        t = text.strip().lower()
        if not t:
            return []
        like = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.lock:
            rows = self.db.execute(
                "SELECT email, name, source, score, last_ts FROM contacts "
                "WHERE email LIKE ? ESCAPE '\\' OR lower(name) LIKE ? ESCAPE '\\' "
                "OR lower(name) LIKE ? ESCAPE '\\' "
                "ORDER BY score DESC, last_ts DESC LIMIT ?",
                (like, like, "% " + like, limit)).fetchall()
        return [Contact(*r) for r in rows]

    def all_messages(self, account: str) -> list[tuple[str, Message]]:
        with self.lock:
            rows = self.db.execute(f"SELECT {_COLS} FROM messages WHERE account=?", (account,)).fetchall()
        return [(r[1], _msg(r)) for r in rows]

    def meta(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def contact_count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT count(*) FROM contacts").fetchone()[0]

    def forget_account(self, account: str) -> None:
        with self.lock, self.db:
            for table in ("messages", "folder_state", "folder_list", "bodies"):
                self.db.execute(f"DELETE FROM {table} WHERE account=?", (account,))
