"""Validated message-search criteria shared by IMAP and offline search."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re


@dataclass(frozen=True)
class Query:
    text: str = ""
    field: str = "text"  # text includes all headers and body on the server
    unread: bool = False
    starred: bool = False
    since: str = ""  # YYYY-MM-DD, inclusive
    before: str = ""  # YYYY-MM-DD, exclusive

    def criteria(self) -> bytes:
        if self.field not in ("text", "from", "to", "subject", "body"):
            raise ValueError("Choose a valid search field.")
        if any(ord(c) < 32 or ord(c) == 127 for c in self.text):
            raise ValueError("Search text must be on one line without control characters.")
        parts = []
        if self.text.strip():
            escaped = self.text.strip().replace('\\', '\\\\').replace('"', '\\"')
            parts.append(f'OR TO "{escaped}" CC "{escaped}"' if self.field == "to" else
                         f'{self.field.upper()} "{escaped}"')
        if self.unread:
            parts.append('UNSEEN')
        if self.starred:
            parts.append('FLAGGED')
        for key, value in (('SINCE', self.since), ('BEFORE', self.before)):
            if value:
                try:
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                        raise ValueError('Invalid date format')
                    date = datetime.strptime(value, '%Y-%m-%d')
                except ValueError as exc:
                    raise ValueError('Use YYYY-MM-DD for search dates, for example 2026-11-10.') from exc
                # IMAP requires English month names, independent of the desktop locale.
                month = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()[date.month - 1]
                parts.append(f'{key} {date.day:02d}-{month}-{date.year}')
        if self.since and self.before and self.since >= self.before:
            raise ValueError('The before date must be later than the since date.')
        return (' '.join(parts) or 'ALL').encode('utf-8')

    def matches(self, message, raw=None):
        self.criteria()
        if self.unread and message.seen or self.starred and not message.flagged:
            return False
        date = datetime.fromtimestamp(message.date_ts).strftime('%Y-%m-%d')
        if self.since and date < self.since or self.before and date >= self.before:
            return False
        text = {'from': message.sender + ' ' + message.from_addr,
                'to': message.to + ' ' + message.cc, 'subject': message.subject}
        if raw is not None:
            from .mime import parse
            text['body'] = parse(raw).text
        else:
            text['body'] = ''
        text['text'] = ' '.join(text.values())
        return self.text.strip().casefold() in text[self.field].casefold()
