"""Private, durable local drafts and copies of accepted outgoing messages."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from .mime import Attachment
from .sending import Draft


class DraftStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Set permissions before SQLite can put any message text into the file.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self._attachment_signatures = {}
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS drafts(
                id TEXT PRIMARY KEY, account TEXT, updated REAL, cursor INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS attachments(
                draft TEXT, position INTEGER, filename TEXT, content_type TEXT, data BLOB,
                PRIMARY KEY(draft, position));
            CREATE TABLE IF NOT EXISTS sent_copies(
                id INTEGER PRIMARY KEY, account TEXT, created REAL, raw BLOB, recipients TEXT);
        ''')

    def _save(self, account: str, draft: Draft, cursor: int):
        data = asdict(draft)
        data.pop('attachments')
        self.db.execute('INSERT OR REPLACE INTO drafts VALUES (?, ?, ?, ?, ?)',
                        (draft.id, account, time.time(), cursor, json.dumps(data)))
        signature = tuple((a.filename, a.content_type, a.data) for a in draft.attachments)
        if self._attachment_signatures.get(draft.id) != signature:
            self.db.execute('DELETE FROM attachments WHERE draft=?', (draft.id,))
            self.db.executemany('INSERT INTO attachments VALUES (?, ?, ?, ?, ?)',
                                [(draft.id, i, a.filename, a.content_type, a.data)
                                 for i, a in enumerate(draft.attachments)])

    def save(self, account: str, draft: Draft, cursor: int = 0):
        with self.lock, self.db:
            self._save(account, draft, cursor)
        self._attachment_signatures[draft.id] = tuple((a.filename, a.content_type, a.data) for a in draft.attachments)

    def load(self, draft_id: str) -> tuple[str, Draft, int]:
        with self.lock:
            row = self.db.execute('SELECT account, cursor, data FROM drafts WHERE id=?',
                                  (draft_id,)).fetchone()
            if row is None:
                raise KeyError(draft_id)
            data = json.loads(row[2])
            data['reply_source'] = tuple(data['reply_source']) if data.get('reply_source') else None
            data['attachments'] = [Attachment(*a) for a in self.db.execute(
                'SELECT filename, content_type, data FROM attachments WHERE draft=? ORDER BY position',
                (draft_id,))]
        return row[0], Draft(**data), row[1]

    def list(self):
        """Metadata only; don't load every attachment just to populate the picker."""
        with self.lock:
            rows = self.db.execute('SELECT id, account, updated, data FROM drafts ORDER BY updated DESC').fetchall()
        return [(i, a, t, json.loads(d)) for i, a, t, d in rows]

    def delete(self, draft_id: str):
        with self.lock, self.db:
            self.db.execute('DELETE FROM drafts WHERE id=?', (draft_id,))
            self.db.execute('DELETE FROM attachments WHERE draft=?', (draft_id,))
        self._attachment_signatures.pop(draft_id, None)

    def accepted(self, account: str, draft: Draft, raw: bytes, recipients: list[str], cursor=0):
        """Journal SMTP acceptance and the remaining draft in one transaction."""
        with self.lock, self.db:
            self.db.execute('INSERT INTO sent_copies(account, created, raw, recipients) VALUES (?, ?, ?, ?)',
                            (account, time.time(), raw, json.dumps(recipients)))
            if draft.delivery_state == 'sent':
                self.db.execute('DELETE FROM drafts WHERE id=?', (draft.id,))
                self.db.execute('DELETE FROM attachments WHERE draft=?', (draft.id,))
            else:
                self._save(account, draft, cursor)
        if draft.delivery_state == "sent":
            self._attachment_signatures.pop(draft.id, None)

    def list_sent(self):
        with self.lock:
            return self.db.execute('SELECT id, account, created, recipients FROM sent_copies ORDER BY id DESC').fetchall()

    def sent_raw(self, copy_id):
        with self.lock:
            row = self.db.execute('SELECT raw FROM sent_copies WHERE id=?', (copy_id,)).fetchone()
        if row is None:
            raise KeyError(copy_id)
        return row[0]

    def close(self):
        with self.lock:
            self.db.close()
