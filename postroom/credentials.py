"""The secret each account signs in with: an app password, or a browser sign-in (OAuth 2.0).

A browser sign-in is kept as its refresh token, stored the same way as a password. Access
tokens last about an hour; `secret()` renews them silently and keeps the current one in
memory only.
"""
from __future__ import annotations

import threading

from . import oauth2, passwords
from .config import Account

AUTH_PASSWORD = "password"
AUTH_OAUTH2 = "oauth2"

_lock = threading.Lock()
_tokens: dict[str, oauth2.Tokens] = {}


class CredentialError(Exception):
    pass


def _key(account_id: str) -> str:
    return account_id + ":oauth2"


def save_oauth(account: Account, tokens: oauth2.Tokens) -> None:
    passwords.store(_key(account.id), f"Postroom sign-in: {account.email}", tokens.dumps())
    with _lock:
        _tokens[account.id] = tokens


def forget(account: Account) -> None:
    """Delete everything stored for the account (on removal)."""
    passwords.delete(account.id)
    passwords.delete(_key(account.id))
    with _lock:
        _tokens.pop(account.id, None)


def invalidate(account: Account) -> None:
    """The server refused the access token; fetch a new one next time."""
    with _lock:
        t = _tokens.get(account.id)
        if t:
            t.access_token, t.expires = "", 0.0


def issuer_and_client(account: Account) -> tuple[oauth2.Issuer, oauth2.Client]:
    issuer = oauth2.issuer_for(account.imap_host) or oauth2.issuer_for(account.email.rsplit("@", 1)[-1])
    if issuer is None:
        raise CredentialError(f"Browser sign-in isn't available for {account.imap_host}.")
    if not issuer.browser_ok:
        raise CredentialError(f"{issuer.name} doesn't allow browser sign-in from Postroom. "
                              "Use an app password instead.")
    client = oauth2.client_for(issuer)
    if client is None:
        raise CredentialError(
            f"Postroom has no registration with {issuer.name} yet, so browser sign-in can't "
            f"be used. Add one to {oauth2.clients_path()}, or use an app password.")
    return issuer, client


def secret(account: Account) -> str:
    """The password, or a valid access token, for signing in to IMAP and SMTP."""
    if account.auth != AUTH_OAUTH2:
        pw = passwords.lookup(account.id)
        if not pw:
            raise CredentialError("No password is saved for this account.")
        return pw
    with _lock:
        tokens = _tokens.get(account.id)
        if tokens is None:
            saved = passwords.lookup(_key(account.id))
            if not saved:
                raise CredentialError("This account isn't signed in. Use Edit Account to sign in.")
            try:
                tokens = oauth2.Tokens.loads(saved)
            except oauth2.OAuthError as e:
                raise CredentialError(str(e)) from e
            _tokens[account.id] = tokens
        if tokens.valid():
            return tokens.access_token
        issuer, client = issuer_and_client(account)
        before = tokens.dumps()
        try:
            oauth2.refresh(issuer, client, tokens)
        except oauth2.OAuthError as e:
            raise CredentialError(str(e)) from e
        if tokens.dumps() != before:  # a rotated refresh token, or a newly known scope
            passwords.store(_key(account.id), f"Postroom sign-in: {account.email}", tokens.dumps())
        return tokens.access_token


def grants(account: Account, scope: str) -> bool:
    """Whether the account's browser sign-in allows `scope` (call after secret())."""
    with _lock:
        t = _tokens.get(account.id)
        return bool(t and t.grants(scope))
