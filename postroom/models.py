"""Plain data types shared by the mail, cache and UI layers."""
from __future__ import annotations

from dataclasses import dataclass, field

# Special folder kinds, in the order the folder tree lists them.
SPECIAL_ORDER = ["inbox", "flagged", "important", "sent", "drafts", "archive", "all", "junk", "trash"]
SPECIAL_NAMES = {
    "inbox": "Inbox", "flagged": "Starred", "important": "Important", "sent": "Sent",
    "drafts": "Drafts", "archive": "Archive", "all": "All Mail", "junk": "Spam", "trash": "Trash",
}


@dataclass
class Folder:
    path: str                 # raw IMAP mailbox name (modified UTF-7), used on the wire
    name: str                 # decoded leaf name for display
    delimiter: str | None
    flags: frozenset[str] = frozenset()
    special: str | None = None

    @property
    def selectable(self) -> bool:
        return "\\noselect" not in self.flags and "\\nonexistent" not in self.flags

    @property
    def display_name(self) -> str:
        return SPECIAL_NAMES.get(self.special, self.name) if self.special else self.name


@dataclass
class Message:
    account_id: str
    folder: str
    uid: int
    subject: str = ""
    from_name: str = ""
    from_addr: str = ""
    to: str = ""
    cc: str = ""
    date_ts: float = 0.0
    flags: set[str] = field(default_factory=set)
    size: int = 0
    has_attachment: bool = False
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    thread_id: str = ""      # Gmail X-GM-THRID when available

    @property
    def seen(self) -> bool:
        return "\\seen" in self.flags

    @property
    def flagged(self) -> bool:
        return "\\flagged" in self.flags

    @property
    def answered(self) -> bool:
        return "\\answered" in self.flags

    @property
    def sender(self) -> str:
        return self.from_name or self.from_addr or "(no sender)"

    @property
    def key(self) -> tuple:
        return (self.account_id, self.folder, self.uid)
