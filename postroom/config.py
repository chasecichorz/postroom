"""Accounts and settings, stored as JSON under the XDG config directory."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_ID = "io.github.postroom.Postroom"
APP_NAME = "Postroom"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_dir() -> Path:
    override = os.environ.get("POSTROOM_PROFILE")
    if override:
        return Path(override) / "config"
    return _xdg("XDG_CONFIG_HOME", ".config") / "postroom"


def cache_dir() -> Path:
    override = os.environ.get("POSTROOM_PROFILE")
    if override:
        return Path(override) / "cache"
    return _xdg("XDG_CACHE_HOME", ".cache") / "postroom"


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
    imap_security: str = "ssl"     # "ssl" (TLS from the start) or "starttls"
    smtp_security: str = ""        # "ssl" or "starttls"; empty means by port (465 is ssl)
    auth: str = "password"         # "password" (app password) or "oauth2" (browser sign-in)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    account_type: str = "auto"

    @property
    def login(self) -> str:
        return self.username or self.email

    @property
    def smtp_uses_ssl(self) -> bool:
        return self.smtp_security == "ssl" if self.smtp_security else self.smtp_port == 465

    @property
    def is_gmail(self) -> bool:
        return self.imap_host.lower().endswith(("gmail.com", "googlemail.com"))


@dataclass
class Settings:
    conversations: bool = True       # group the message list into conversations
    prefer_html: bool = False        # read the HTML part (as text) instead of the plain part
    mark_read_on_open: bool = True   # mark a message read when you open it (Enter / reading pane)
    check_minutes: int = 2           # how often to check for new mail
    new_mail_sound: bool = True      # play the chime when new mail arrives
    new_mail_announce: bool = True   # say who it is from
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
