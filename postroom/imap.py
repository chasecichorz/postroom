"""A small synchronous IMAP client on top of imaplib. Run it from a worker thread."""
from __future__ import annotations

import email
import imaplib
import re
import socket
import ssl
import time
from email import policy
from email.utils import getaddresses, parsedate_to_datetime

from . import utf7
from .config import Account
from .models import Folder, Message

HEADER_FIELDS = ("FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES CONTENT-TYPE")

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.*)$')
_UID_RE = re.compile(rb"\bUID (\d+)")
_FLAGS_RE = re.compile(rb"\bFLAGS \(([^)]*)\)")
_THRID_RE = re.compile(rb"\bX-GM-THRID (\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_STATUS_RE = re.compile(rb"\((.*)\)\s*$")

NETWORK_ERRORS = (imaplib.IMAP4.abort, OSError, ssl.SSLError, socket.timeout, EOFError)

_SPECIAL_FLAGS = {
    "\\inbox": "inbox", "\\sent": "sent", "\\drafts": "drafts", "\\trash": "trash",
    "\\junk": "junk", "\\all": "all", "\\archive": "archive", "\\flagged": "flagged",
    "\\important": "important",
}
_SPECIAL_NAMES = {
    "sent": "sent", "sent mail": "sent", "sent items": "sent", "drafts": "drafts",
    "trash": "trash", "deleted items": "trash", "deleted messages": "trash", "junk": "junk",
    "spam": "junk", "junk e-mail": "junk", "archive": "archive",
}


class ImapError(Exception):
    pass


def quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(b: bytes) -> str:
    s = b.decode("utf-8", "replace").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = re.sub(r'\\(.)', r'\1', s[1:-1])
    return s


def parse_list_line(line) -> Folder | None:
    if isinstance(line, tuple):  # name sent as a literal
        meta, name_b = line
        m = _LIST_RE.match(meta.rstrip() + b" X")
        if not m:
            return None
        raw_name = name_b.decode("utf-8", "replace")
    else:
        if not line:
            return None
        m = _LIST_RE.match(line)
        if not m:
            return None
        raw_name = _unquote(m.group("name"))
    flags = frozenset(f.lower() for f in m.group("flags").decode().split())
    d = m.group("delim")
    delim = None if d == b"NIL" else _unquote(d)
    leaf = raw_name.rsplit(delim, 1)[-1] if delim else raw_name
    special = None
    if raw_name.upper() == "INBOX":
        special = "inbox"
    else:
        for f in flags:
            if f in _SPECIAL_FLAGS:
                special = _SPECIAL_FLAGS[f]
                break
        parent = raw_name.rsplit(delim, 1)[0] if delim and delim in raw_name else ""
        if special is None and parent.upper() in ("", "INBOX"):
            special = _SPECIAL_NAMES.get(utf7.decode(leaf).lower())
    name = "Inbox" if special == "inbox" else utf7.decode(leaf)
    return Folder(path=raw_name, name=name, delimiter=delim, flags=flags, special=special)


def iter_fetch(data):
    """Yield (metadata bytes, literal bytes or None) for each message in a FETCH response.

    imaplib returns [(prefix, literal), b' FLAGS (...))', ...]; items after the literal land
    in the following bytes element, so it is joined onto the metadata.
    """
    i = 0
    while i < len(data):
        item = data[i]
        if isinstance(item, tuple):
            meta, lit = item[0], item[1]
            if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                meta += data[i + 1]
                i += 1
            yield meta, lit
        elif isinstance(item, bytes) and item.strip() not in (b"", b")"):
            yield item, None
        i += 1


def _flags(meta: bytes) -> set[str] | None:
    m = _FLAGS_RE.search(meta)
    if not m:
        return None
    return {f.lower() for f in m.group(1).decode("utf-8", "replace").split()}


def _addr_list(value: str) -> list[tuple[str, str]]:
    try:
        return [(n, a) for n, a in getaddresses([value]) if n or a]
    except Exception:  # noqa: BLE001 - malformed header
        return []


def parse_header_block(account_id: str, folder: str, meta: bytes, header: bytes) -> Message | None:
    m = _UID_RE.search(meta)
    if not m:
        return None
    try:
        h = email.message_from_bytes(header or b"", policy=policy.default)
        get = lambda k: str(h.get(k, "") or "")  # noqa: E731
        subject, frm, to, cc = get("Subject"), get("From"), get("To"), get("Cc")
        date_hdr, ctype = get("Date"), get("Content-Type").lower()
        msgid, irt, refs = get("Message-ID"), get("In-Reply-To"), get("References")
    except Exception:  # noqa: BLE001 - fall back to the lenient parser
        h = email.message_from_bytes(header or b"")
        get = lambda k: str(email.header.make_header(email.header.decode_header(h.get(k, "") or "")))  # noqa: E731
        subject, frm, to, cc = get("Subject"), get("From"), get("To"), get("Cc")
        date_hdr, ctype = get("Date"), get("Content-Type").lower()
        msgid, irt, refs = get("Message-ID"), get("In-Reply-To"), get("References")
    senders = _addr_list(frm)
    from_name, from_addr = senders[0] if senders else ("", frm)
    ts = 0.0
    try:
        ts = parsedate_to_datetime(date_hdr).timestamp() if date_hdr else 0.0
    except (TypeError, ValueError, IndexError):
        ts = 0.0
    if not ts:
        it = imaplib.Internaldate2tuple(meta)
        ts = time.mktime(it) if it else 0.0
    size = _SIZE_RE.search(meta)
    thr = _THRID_RE.search(meta)
    return Message(
        account_id=account_id, folder=folder, uid=int(m.group(1)),
        subject=" ".join(subject.split()), from_name=from_name.strip(), from_addr=from_addr.strip(),
        to=to, cc=cc, date_ts=ts, flags=_flags(meta) or set(),
        size=int(size.group(1)) if size else 0,
        has_attachment=ctype.startswith("multipart/mixed") or "name=" in ctype,
        message_id=msgid.strip(), in_reply_to=irt.strip(), references=" ".join(refs.split()),
        thread_id=thr.group(1).decode() if thr else "",
    )


class ImapClient:
    def __init__(self, account: Account, secret):
        """`secret` is the password or access token, or a function returning it (so a
        reconnect an hour later gets a fresh browser sign-in token)."""
        self.account = account
        self._secret = secret if callable(secret) else (lambda: secret)
        self.conn: imaplib.IMAP4_SSL | None = None
        self.caps: set[str] = set()
        self.selected: str | None = None

    # -- connection -------------------------------------------------------------------------
    def connect(self) -> None:
        try:
            ctx = ssl.create_default_context()
            if self.account.imap_security == "starttls":
                conn = imaplib.IMAP4(self.account.imap_host, self.account.imap_port, timeout=30)
                conn.starttls(ssl_context=ctx)
            else:
                conn = imaplib.IMAP4_SSL(self.account.imap_host, self.account.imap_port,
                                         ssl_context=ctx, timeout=30)
        except (OSError, ssl.SSLError, imaplib.IMAP4.error) as e:
            raise ImapError(f"Could not reach {self.account.imap_host}: {e}") from e
        try:
            from .credentials import CredentialError
            try:
                secret = self._secret()
            except CredentialError as e:
                conn.logout()
                raise ImapError(str(e)) from e
            if self.account.auth == "oauth2":
                from .oauth2 import xoauth2
                conn.authenticate("XOAUTH2", lambda _: xoauth2(self.account.login, secret).encode())
            else:
                conn.login(self.account.login, secret)
        except imaplib.IMAP4.error as e:
            if self.account.auth == "oauth2":
                from .credentials import invalidate
                invalidate(self.account)
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
            msg = e.args[0].decode() if e.args and isinstance(e.args[0], bytes) else str(e)
            raise ImapError(f"Sign-in failed: {msg}") from e
        self.conn = conn
        self.caps = {c.upper() for c in conn.capabilities}
        self.selected = None

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:  # noqa: BLE001
                pass
        self.conn = None
        self.selected = None

    def _c(self) -> imaplib.IMAP4_SSL:
        if self.conn is None:
            self.connect()
        return self.conn

    @staticmethod
    def _ok(res, what: str):
        typ, data = res
        if typ != "OK":
            detail = data[0].decode("utf-8", "replace") if data and isinstance(data[0], bytes) else ""
            raise ImapError(f"{what} failed: {detail}")
        return data

    @property
    def is_gmail(self) -> bool:
        return "X-GM-EXT-1" in self.caps

    # -- folders ----------------------------------------------------------------------------
    def list_folders(self) -> list[Folder]:
        data = self._ok(self._c().list(), "Listing folders")
        return [f for f in (parse_list_line(x) for x in data) if f]

    def status(self, path: str) -> tuple[int, int]:
        data = self._ok(self._c().status(quote(path), "(MESSAGES UNSEEN)"), "Status")
        m = _STATUS_RE.search(data[0] if isinstance(data[0], bytes) else data[0][0])
        vals = m.group(1).decode().split() if m else []
        d = dict(zip(vals[::2], vals[1::2]))
        return int(d.get("MESSAGES", 0)), int(d.get("UNSEEN", 0))

    def select(self, path: str, readonly: bool = False) -> tuple[int, int]:
        """Open a folder; `readonly` uses EXAMINE, so nothing in it can change."""
        c = self._c()
        data = self._ok(c.select(quote(path), readonly=readonly), f"Opening {path}")
        self.selected = path
        exists = int(data[0] or 0)
        uv = c.untagged_responses.pop("UIDVALIDITY", [b"0"])[-1]
        return exists, int(uv)

    def ensure_selected(self, path: str) -> None:
        if self.selected != path:
            self.select(path)

    # -- messages ---------------------------------------------------------------------------
    def _fetch_items(self) -> str:
        extra = " X-GM-THRID" if self.is_gmail else ""
        return f"(UID FLAGS INTERNALDATE RFC822.SIZE{extra} BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"

    def fetch_headers(self, folder: str, msg_set: str, by_uid: bool) -> list[Message]:
        c = self._c()
        self.ensure_selected(folder)
        if by_uid:
            data = self._ok(c.uid("FETCH", msg_set, self._fetch_items()), "Fetching headers")
        else:
            data = self._ok(c.fetch(msg_set, self._fetch_items()), "Fetching headers")
        out = []
        for meta, lit in iter_fetch(data):
            if lit is None:
                continue
            msg = parse_header_block(self.account.id, folder, meta, lit)
            if msg:
                out.append(msg)
        return out

    def fetch_flags(self, folder: str, uid_set: str) -> dict[int, set[str]]:
        c = self._c()
        self.ensure_selected(folder)
        data = self._ok(c.uid("FETCH", uid_set, "(UID FLAGS)"), "Fetching flags")
        out = {}
        for meta, _ in iter_fetch(data):
            u, f = _UID_RE.search(meta), _flags(meta)
            if u and f is not None:
                out[int(u.group(1))] = f
        return out

    def fetch_body(self, folder: str, uid: int) -> bytes:
        c = self._c()
        self.ensure_selected(folder)
        data = self._ok(c.uid("FETCH", str(uid), "(BODY.PEEK[])"), "Fetching message")
        for _meta, lit in iter_fetch(data):
            if lit is not None:
                return lit
        raise ImapError("The message is no longer on the server.")

    def set_flags(self, folder: str, uids: list[int], flags: str, add: bool) -> None:
        c = self._c()
        self.ensure_selected(folder)
        self._ok(c.uid("STORE", uid_set(uids), "+FLAGS.SILENT" if add else "-FLAGS.SILENT",
                       f"({flags})"), "Changing flags")

    def move(self, folder: str, uids: list[int], dest: str) -> None:
        c = self._c()
        self.ensure_selected(folder)
        s = uid_set(uids)
        if "MOVE" in self.caps:
            self._ok(c.uid("MOVE", s, quote(dest)), "Moving")
        else:
            self._ok(c.uid("COPY", s, quote(dest)), "Copying")
            self._ok(c.uid("STORE", s, "+FLAGS.SILENT", "(\\Deleted)"), "Deleting")
            self._expunge(s)

    def delete_forever(self, folder: str, uids: list[int]) -> None:
        c = self._c()
        self.ensure_selected(folder)
        s = uid_set(uids)
        self._ok(c.uid("STORE", s, "+FLAGS.SILENT", "(\\Deleted)"), "Deleting")
        self._expunge(s)

    def _expunge(self, s: str) -> None:
        c = self._c()
        if "UIDPLUS" in self.caps:
            self._ok(c.uid("EXPUNGE", s), "Expunging")
        else:
            self._ok(c.expunge(), "Expunging")

    def append(self, folder: str, raw: bytes, flags: str = "(\\Seen)") -> None:
        self._ok(self._c().append(quote(folder), flags, imaplib.Time2Internaldate(time.time()), raw),
                 "Saving copy")


def uid_set(uids) -> str:
    """Compress [1,2,3,7] into '1:3,7'."""
    uids = sorted(set(int(u) for u in uids))
    parts, start, prev = [], None, None
    for u in uids:
        if start is None:
            start = prev = u
        elif u == prev + 1:
            prev = u
        else:
            parts.append(f"{start}:{prev}" if start != prev else str(start))
            start = prev = u
    if start is not None:
        parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)
