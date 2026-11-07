"""Accounts and settings, stored as JSON under the XDG config directory."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_ID = "io.github.mailpane.Mailpane"
APP_NAME = "Mailpane"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_dir() -> Path:
    override = os.environ.get("MAILPANE_PROFILE")
    if override:
        return Path(override) / "config"
    return _xdg("XDG_CONFIG_HOME", ".config") / "mailpane"


def cache_dir() -> Path:
    override = os.environ.get("MAILPANE_PROFILE")
    if override:
        return Path(override) / "cache"
    return _xdg("XDG_CACHE_HOME", ".cache") / "mailpane"


@dataclass
class Account:
    name: str                      # what the account list shows, e.g. "Personal"
    email: str
    full_name: str = ""            # shown to recipients in From
    username: str = ""             # login; defaults to email
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def login(self) -> str:
        return self.username or self.email

    @property
    def is_gmail(self) -> bool:
        return self.imap_host.lower().endswith(("gmail.com", "googlemail.com"))


@dataclass
class Settings:
    conversations: bool = True       # group the message list into conversations
    prefer_html: bool = False        # read the HTML part (as text) instead of the plain part
    mark_read_on_open: bool = True   # mark a message read when you open it (Enter / reading pane)
    check_minutes: int = 2           # how often to check for new mail
    window_width: int = 1200
    window_height: int = 800


@dataclass
class Config:
    accounts: list[Account] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)

    @property
    def path(self) -> Path:
        return config_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            data = json.loads(cfg.path.read_text())
        except (OSError, ValueError):
            return cfg
        known_a = {f.name for f in fields(Account)}
        known_s = {f.name for f in fields(Settings)}
        cfg.accounts = [Account(**{k: v for k, v in a.items() if k in known_a})
                        for a in data.get("accounts", [])]
        cfg.settings = Settings(**{k: v for k, v in data.get("settings", {}).items() if k in known_s})
        return cfg

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "accounts": [asdict(a) for a in self.accounts],
            "settings": asdict(self.settings),
        }, indent=2))
        tmp.replace(self.path)

    def account(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == account_id), None)
