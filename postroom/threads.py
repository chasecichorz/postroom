"""Grouping messages into conversations, and the text a screen reader hears for each row."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from .models import Message

_PREFIX_RE = re.compile(r"^\s*((re|fwd?|aw|sv|tr|wg)(\[\d+\])?\s*:\s*)+", re.I)


def normalize_subject(s: str) -> str:
    return " ".join(_PREFIX_RE.sub("", s or "").split()).lower()


def thread_key(m: Message) -> tuple:
    if m.thread_id:
        return ("t", m.thread_id)
    subj = normalize_subject(m.subject)
    return ("s", subj) if subj else ("u", m.uid)


def group(messages: list[Message], conversations: bool) -> list[list[Message]]:
    """Return conversations newest-first; messages inside each are oldest-first."""
    if not conversations:
        return [[m] for m in sorted(messages, key=lambda m: m.date_ts, reverse=True)]
    groups: dict[tuple, list[Message]] = {}
    for m in messages:
        groups.setdefault(thread_key(m), []).append(m)
    out = [sorted(g, key=lambda m: m.date_ts) for g in groups.values()]
    out.sort(key=lambda g: g[-1].date_ts, reverse=True)
    return out


def friendly_date(ts: float, now: float | None = None) -> str:
    if not ts:
        return ""
    d = datetime.fromtimestamp(ts)
    today = datetime.fromtimestamp(now or time.time()).date()
    clock = d.strftime("%I:%M %p").lstrip("0")
    if d.date() == today:
        return clock
    if d.date() == today - timedelta(days=1):
        return f"Yesterday {clock}"
    if timedelta(0) < today - d.date() < timedelta(days=7):
        return f"{d.strftime('%A')} {clock}"
    if d.year == today.year:
        return f"{d.strftime('%B')} {d.day}"
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def message_speech(m: Message) -> str:
    parts = []
    if not m.seen:
        parts.append("Unread")
    parts += [m.sender, m.subject or "(no subject)", friendly_date(m.date_ts)]
    if m.has_attachment:
        parts.append("has attachment")
    if m.flagged:
        parts.append("starred")
    if m.answered:
        parts.append("replied")
    return ", ".join(p for p in parts if p)


def senders(msgs: list[Message], limit: int = 3) -> str:
    names: list[str] = []
    for m in reversed(msgs):
        n = m.from_name or m.from_addr
        if n and n not in names:
            names.append(n)
    more = len(names) - limit
    return ", ".join(names[:limit]) + (f" and {more} more" if more > 0 else "")


def thread_speech(msgs: list[Message], expanded: bool) -> str:
    latest = msgs[-1]
    unread = sum(1 for m in msgs if not m.seen)
    parts = []
    if unread:
        parts.append("Unread")
    parts += [senders(msgs), latest.subject or "(no subject)", friendly_date(latest.date_ts),
              f"{len(msgs)} messages"]
    if unread and unread != len(msgs):
        parts.append(f"{unread} unread")
    if any(m.has_attachment for m in msgs):
        parts.append("has attachment")
    if any(m.flagged for m in msgs):
        parts.append("starred")
    parts.append("expanded" if expanded else "collapsed")
    return ", ".join(p for p in parts if p)
