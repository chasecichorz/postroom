"""App-password storage.

Uses the desktop keyring (Secret Service: GNOME Keyring, KWallet, KeePassXC) when one is
running. Without one -- common on i3 and other bare window managers -- it falls back to a
file only your user can read (mode 600) in the config directory.
"""
from __future__ import annotations

import json
import os

from .config import config_dir

try:
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import GLib, Secret
    _SCHEMA = Secret.Schema.new(
        "io.github.postroom.Password", Secret.SchemaFlags.NONE,
        {"account_id": Secret.SchemaAttributeType.STRING})
except (ImportError, ValueError):  # pragma: no cover - libsecret missing
    Secret = None
    _SCHEMA = None


def _file():
    return config_dir() / "passwords.json"


def _read_file() -> dict:
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}


def _write_file(data: dict) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(path.with_suffix(".tmp"), path)
    os.chmod(path, 0o600)


def _keyring_available() -> bool:
    if Secret is None or os.environ.get("POSTROOM_NO_KEYRING"):
        return False
    try:
        Secret.Service.get_sync(Secret.ServiceFlags.NONE, None)
        return True
    except GLib.Error:
        return False


def backend_name() -> str:
    return "keyring" if _keyring_available() else "file"


def store(account_id: str, label: str, password: str) -> None:
    if _keyring_available():
        try:
            Secret.password_store_sync(_SCHEMA, {"account_id": account_id},
                                       Secret.COLLECTION_DEFAULT, label, password, None)
            return
        except GLib.Error:
            pass
    data = _read_file()
    data[account_id] = password
    _write_file(data)


def lookup(account_id: str) -> str | None:
    if _keyring_available():
        try:
            pw = Secret.password_lookup_sync(_SCHEMA, {"account_id": account_id}, None)
            if pw is not None:
                return pw
        except GLib.Error:
            pass
    return _read_file().get(account_id)


def delete(account_id: str) -> None:
    if _keyring_available():
        try:
            Secret.password_clear_sync(_SCHEMA, {"account_id": account_id}, None)
        except GLib.Error:
            pass
    data = _read_file()
    if data.pop(account_id, None) is not None:
        _write_file(data)
