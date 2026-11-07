"""The address book: people you have mail from or have written to, plus Google contacts.

Addresses are collected from message headers as folders sync. People you write to count
more than people who write to you, and automated senders (no-reply and the like) are left
out unless they are in your Google contacts.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from email.utils import formataddr, getaddresses

from .models import Message

GOOGLE = "google"
MAIL = "mail"

# Weights: a Google contact always sorts above people seen only in mail.
W_GOOGLE, W_WROTE_TO, W_HEARD_FROM = 1000, 3, 1

_AUTOMATED = re.compile(
    r"(^|[._+-])(no-?reply|do-?not-?reply|donotreply|notifications?|mailer-daemon|postmaster|"
    r"bounces?|alerts?|newsletters?|news|info|support|updates?|marketing|automated)([._+-]|@)",
    re.I)
_ADDR = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


@dataclass
class Contact:
    email: str
    name: str = ""
    source: str = MAIL
    score: int = 0
    last_ts: float = 0.0

    @property
    def address(self) -> str:
        """As it goes in a To field: Name <email>."""
        return formataddr((self.name, self.email)) if self.name else self.email

    @property
    def spoken(self) -> str:
        return f"{self.name}, {self.email}" if self.name and self.name != self.email else self.email


def valid(addr: str) -> bool:
    return bool(_ADDR.match(addr or ""))


def automated(addr: str) -> bool:
    return bool(_AUTOMATED.search(addr or ""))


def _clean_name(name: str, addr: str) -> str:
    name = (name or "").strip().strip("'\"").strip()
    return "" if name.lower() == addr.lower() else name


def harvest(messages: list[Message], own: set[str], sent_folder: bool) -> list[Contact]:
    """Contacts seen in these headers. In Sent, the recipients count; elsewhere the sender."""
    found: dict[str, Contact] = {}

    def add(name, addr, weight, ts):
        addr = (addr or "").strip().lower()
        # Someone you wrote to is kept even if the address looks automated (info@, support@).
        if not valid(addr) or addr in own or (not sent_folder and automated(addr)):
            return
        c = found.setdefault(addr, Contact(addr))
        c.name = c.name or _clean_name(name, addr)
        c.score += weight
        c.last_ts = max(c.last_ts, ts)

    for m in messages:
        if sent_folder:
            for name, addr in getaddresses([m.to, m.cc]):
                add(name, addr, W_WROTE_TO, m.date_ts)
        else:
            add(m.from_name, m.from_addr, W_HEARD_FROM, m.date_ts)
    return list(found.values())


def parse_vcards(text: str) -> list[Contact]:
    """Names and email addresses from vCard 3/4 text (Google CardDAV)."""
    # Unfold continuation lines (RFC 6350 3.2).
    text = re.sub(r"\r?\n[ \t]", "", text)
    out: list[Contact] = []
    name, emails, n_name = "", [], ""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        prop = key.split(";", 1)[0].split(".")[-1].upper()  # "item1.EMAIL;TYPE=..." -> EMAIL
        value = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
        if prop == "BEGIN":
            name, emails, n_name = "", [], ""
        elif prop == "FN":
            name = value
        elif prop == "N" and value:
            parts = [p.strip() for p in value.split(";")]
            n_name = " ".join(p for p in (parts[1] if len(parts) > 1 else "", parts[0]) if p)
        elif prop == "EMAIL" and valid(value.lower()):
            emails.append(value.lower())
        elif prop == "END":
            for e in dict.fromkeys(emails):
                out.append(Contact(e, _clean_name(name or n_name, e), GOOGLE, W_GOOGLE, time.time()))
    return out


def current_token(text: str, cursor: int) -> tuple[int, int, str]:
    """The address being typed at `cursor` in a comma-separated field: (start, end, text)."""
    start = text.rfind(",", 0, cursor) + 1
    end = text.find(",", cursor)
    end = len(text) if end < 0 else end
    raw = text[start:end]
    lead = len(raw) - len(raw.lstrip())
    return start + lead, end, raw.strip()
