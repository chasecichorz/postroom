# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Ported to Python for Postroom from Thunderbird's OAuth2 support
# (mailnews/base/src: OAuth2.sys.mjs, OAuth2Providers.sys.mjs and OAuth2Module.sys.mjs),
# https://github.com/thunderbird/thunderbird-desktop.
"""Sign in with the provider's own web page instead of a password (OAuth 2.0).

This follows the flow Thunderbird uses with an external browser:

1. Postroom listens on a random port on 127.0.0.1.
2. It opens the provider's sign-in page in your web browser. The address carries a random
   `state` and a PKCE challenge (RFC 7636).
3. After you sign in, the provider sends the browser back to 127.0.0.1 with a one-time
   code. Postroom checks `state`, then trades the code for tokens.
4. The refresh token is stored like a password. Access tokens last about an hour and are
   renewed from it silently.

THUNDERBIRD_CLIENTS below holds Thunderbird's public client IDs from
OAuth2Providers.sys.mjs, so sign-in pages name Thunderbird. A registration in
~/.config/postroom/oauth-clients.json takes priority:

    {"accounts.google.com": {"client_id": "...", "client_secret": "..."},
     "login.microsoftonline.com": {"client_id": "..."}}

(Google "Desktop app" clients come with a client_secret that Google says is not secret;
Microsoft public clients have none.)
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import config_dir

# ----- providers (OAuth2Providers.sys.mjs) ---------------------------------------------------
# Mail and contacts in one sign-in, as Thunderbird asks for Google mail accounts.
GOOGLE_CONTACTS_SCOPE = "https://www.googleapis.com/auth/carddav"
GOOGLE_SCOPES = "https://mail.google.com/ " + GOOGLE_CONTACTS_SCOPE
MICROSOFT_SCOPES = ("https://outlook.office.com/IMAP.AccessAsUser.All "
                    "https://outlook.office.com/SMTP.Send offline_access")
FASTMAIL_SCOPES = ("https://www.fastmail.com/dev/protocol-imap "
                   "https://www.fastmail.com/dev/protocol-smtp")
YAHOO_SCOPES = "mail-w"


@dataclass(frozen=True)
class Issuer:
    name: str
    authorization_endpoint: str
    token_endpoint: str
    scope: str
    use_pkce: bool = True
    redirect: str = "http://127.0.0.1"
    extra_auth_params: tuple = ()
    # Yahoo and AOL only accept an https://127.0.0.1 return, which Thunderbird catches in its
    # built-in browser; a normal web browser can't hand that back to Postroom.
    browser_ok: bool = True


ISSUERS = {i.name: i for i in (
    Issuer("accounts.google.com", "https://accounts.google.com/o/oauth2/auth",
           "https://www.googleapis.com/oauth2/v3/token", GOOGLE_SCOPES,
           extra_auth_params=(("access_type", "offline"), ("prompt", "consent"))),
    Issuer("login.microsoftonline.com",
           "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
           "https://login.microsoftonline.com/common/oauth2/v2.0/token", MICROSOFT_SCOPES),
    Issuer("login.yahoo.com", "https://api.login.yahoo.com/oauth2/request_auth",
           "https://api.login.yahoo.com/oauth2/get_token", YAHOO_SCOPES,
           redirect="https://127.0.0.1", browser_ok=False),
    Issuer("login.aol.com", "https://api.login.aol.com/oauth2/request_auth",
           "https://api.login.aol.com/oauth2/get_token", YAHOO_SCOPES,
           redirect="https://127.0.0.1", browser_ok=False),
    Issuer("www.fastmail.com", "https://api.fastmail.com/oauth/authorize",
           "https://api.fastmail.com/oauth/refresh", FASTMAIL_SCOPES,
           redirect="http://127.0.0.1/fastmail"),
)}

# Mail host or address domain -> issuer (Thunderbird's kHostnames, mail entries only).
HOSTNAMES = {
    "gmail.com": "accounts.google.com", "googlemail.com": "accounts.google.com",
    "imap.gmail.com": "accounts.google.com", "smtp.gmail.com": "accounts.google.com",
    "outlook.com": "login.microsoftonline.com", "hotmail.com": "login.microsoftonline.com",
    "office365.com": "login.microsoftonline.com",
    "outlook.office365.com": "login.microsoftonline.com",
    "smtp-mail.outlook.com": "login.microsoftonline.com",
    "smtp.office365.com": "login.microsoftonline.com",
    "yahoo.com": "login.yahoo.com", "imap.mail.yahoo.com": "login.yahoo.com",
    "smtp.mail.yahoo.com": "login.yahoo.com", "att.net": "login.yahoo.com",
    "aol.com": "login.aol.com", "imap.aol.com": "login.aol.com", "smtp.aol.com": "login.aol.com",
    "imap.fastmail.com": "www.fastmail.com", "smtp.fastmail.com": "www.fastmail.com",
}


def issuer_for(hostname: str) -> Issuer | None:
    """The OAuth issuer for a mail server or domain, matching parent domains too."""
    host = hostname.lower().rstrip(".")
    while host:
        if host in HOSTNAMES:
            return ISSUERS[HOSTNAMES[host]]
        if "." not in host:
            break
        host = host.split(".", 1)[1]
    return None


@dataclass
class Client:
    client_id: str
    client_secret: str = ""


def clients_path():
    return config_dir() / "oauth-clients.json"


# Thunderbird's built-in registrations (OAuth2Providers.sys.mjs). Only Google's has a
# secret, which Google treats as public for desktop apps.
THUNDERBIRD_CLIENTS = {
    "accounts.google.com": Client(
        "406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com",
        "kSmqreRr0qwBWJgbf5Y-PjSU"),
    "login.microsoftonline.com": Client("9e5f94bc-e8a4-4e73-b8be-63364c29d753"),
    "login.yahoo.com": Client(
        "dj0yJmk9WVZUaWRNUUZSQTBNJmQ9WVdrOVNqbHJUMGhtTkU4bWNHbzlNQT09JnM9Y29uc3VtZXJzZWNyZXQmc3Y9MCZ4PTgz"),
    "login.aol.com": Client(
        "dj0yJmk9MGNoTTQ0SjhIN1dSJmQ9WVdrOVdIVnFkVVp2UkZNbWNHbzlNQT09JnM9Y29uc3VtZXJzZWNyZXQmc3Y9MCZ4PTNm"),
    "www.fastmail.com": Client("35f141ae"),
}


def client_for(issuer: Issuer) -> Client | None:
    """The user's own registration from oauth-clients.json, else Thunderbird's."""
    try:
        data = json.loads(clients_path().read_text())
    except (OSError, ValueError):
        data = {}
    entry = data.get(issuer.name) or {}
    if entry.get("client_id"):
        return Client(entry["client_id"], entry.get("client_secret", ""))
    return THUNDERBIRD_CLIENTS.get(issuer.name)


class OAuthError(Exception):
    pass


# ----- tokens --------------------------------------------------------------------------------
@dataclass
class Tokens:
    refresh_token: str
    access_token: str = ""
    expires: float = 0.0          # time.time() when the access token stops working
    scope: str = ""               # what the provider actually granted, space-separated

    def valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires - 60

    def grants(self, scope: str) -> bool:
        return scope in self.scope.split()

    def dumps(self) -> str:
        return json.dumps({"refresh_token": self.refresh_token, "scope": self.scope})

    @classmethod
    def loads(cls, s: str) -> "Tokens":
        try:
            d = json.loads(s)
            return cls(d["refresh_token"], scope=d.get("scope", ""))
        except (ValueError, KeyError, TypeError) as e:
            raise OAuthError("The saved sign-in is damaged. Sign in again.") from e


def _post(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "User-Agent": "Postroom"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body: dict = {}
        try:
            body = json.loads(e.read()) or {}
            msg = body.get("error_description") or body.get("error") or str(e)
        except (ValueError, AttributeError):
            msg = str(e)
        if body.get("error") == "invalid_grant":
            raise OAuthError("The provider no longer accepts this sign-in. Sign in again.") from e
        raise OAuthError(f"The provider refused: {msg}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OAuthError(f"Could not reach the sign-in server: {e}") from e


def refresh(issuer: Issuer, client: Client, tokens: Tokens) -> Tokens:
    """Get a fresh access token (OAuth2.requestAccessToken with a refresh token)."""
    form = {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token,
            "client_id": client.client_id}
    if client.client_secret:
        form["client_secret"] = client.client_secret
    result = _post(issuer.token_endpoint, form)
    if "access_token" not in result:
        raise OAuthError("The provider sent no access token.")
    tokens.access_token = result["access_token"]
    tokens.expires = time.time() + int(result.get("expires_in", 3600))
    if result.get("scope"):
        tokens.scope = result["scope"]
    if result.get("refresh_token"):
        tokens.refresh_token = result["refresh_token"]
    return tokens


# ----- the browser sign-in (OAuth2.requestAuthorization + ExternalRequest) -------------------
def _token(n: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(n)).rstrip(b"=").decode()


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{title}</title></head><body><h1>{title}</h1><p>{text}</p></body></html>"""


@dataclass
class SignIn:
    """One browser sign-in. Call `start()`, open `url` in a browser, then `wait()`."""
    issuer: Issuer
    client: Client
    login_hint: str = ""
    url: str = ""
    _state: str = field(default_factory=lambda: _token(24))
    _verifier: str = field(default_factory=lambda: _token(48))
    _redirect: str = ""
    _sock: socket.socket | None = None
    _result: dict = field(default_factory=dict)
    _done: threading.Event = field(default_factory=threading.Event)

    def start(self) -> str:
        """Listen on 127.0.0.1 and build the sign-in address."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(1.0)  # wake up regularly so cancel() really stops the listener
        port = self._sock.getsockname()[1]
        base = urllib.parse.urlsplit(self.issuer.redirect)
        # No "/" is added when the endpoint has no path: providers may match the
        # registered redirect exactly, as Thunderbird's startLoopbackRedirectListener notes.
        self._redirect = urllib.parse.urlunsplit(
            (base.scheme, f"{base.hostname}:{port}", base.path, "", ""))
        params = [("response_type", "code"), ("client_id", self.client.client_id),
                  ("redirect_uri", self._redirect), ("scope", self.issuer.scope),
                  ("state", self._state)]
        if self.issuer.use_pkce:
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(self._verifier.encode()).digest()).rstrip(b"=").decode()
            params += [("code_challenge", challenge), ("code_challenge_method", "S256")]
        if self.login_hint:
            params.append(("login_hint", self.login_hint))
        params += list(self.issuer.extra_auth_params)
        self.url = self.issuer.authorization_endpoint + "?" + urllib.parse.urlencode(params)
        threading.Thread(target=self._serve, daemon=True).start()
        return self.url

    def cancel(self) -> None:
        self._result.setdefault("error", "cancelled")
        self._close()
        self._done.set()

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _serve(self) -> None:
        sock = self._sock
        while sock is not None and not self._done.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                if self._sock is None:
                    sock.close()
                    return
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(10)
                try:
                    request = conn.recv(8192).decode("latin-1")
                except OSError:
                    continue
                line = request.split("\r\n", 1)[0].split(" ")
                if len(line) < 2 or line[0] != "GET":
                    continue
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(line[1]).query))
                if "code" not in query and "error" not in query:
                    conn.sendall(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
                    continue  # e.g. the browser asking for /favicon.ico
                if query.get("state") != self._state:
                    ok, self._result = False, {"error": "The sign-in reply did not match. Try again."}
                elif "error" in query:
                    ok, self._result = False, {"error": query.get("error_description") or query["error"]}
                else:
                    ok, self._result = True, {"code": query["code"]}
                title = "Signed in to Postroom" if ok else "Postroom could not sign in"
                text = ("You can close this tab and go back to Postroom." if ok else
                        html.escape(self._result["error"]))
                body = _PAGE.format(title=title, text=text).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                             b"Content-Security-Policy: default-src 'none'\r\nCache-Control: no-store\r\n"
                             b"Connection: close\r\nContent-Length: " + str(len(body)).encode()
                             + b"\r\n\r\n" + body)
                self._close()
                self._done.set()
                return

    def wait(self, timeout: float = 600) -> Tokens:
        """Block until the browser comes back, then trade the code for tokens."""
        if not self._done.wait(timeout):
            self.cancel()
            raise OAuthError("Signing in took too long. Try again.")
        if "code" not in self._result:
            raise OAuthError(self._result.get("error", "Signing in failed."))
        form = {"grant_type": "authorization_code", "code": self._result["code"],
                "redirect_uri": self._redirect, "client_id": self.client.client_id}
        if self.client.client_secret:
            form["client_secret"] = self.client.client_secret
        if self.issuer.use_pkce:
            form["code_verifier"] = self._verifier
        result = _post(self.issuer.token_endpoint, form)
        if not result.get("refresh_token"):
            raise OAuthError("The provider did not allow staying signed in.")
        return Tokens(result["refresh_token"], result.get("access_token", ""),
                      time.time() + int(result.get("expires_in", 3600)),
                      result.get("scope", self.issuer.scope))


def xoauth2(user: str, access_token: str) -> str:
    """The SASL XOAUTH2 string IMAP and SMTP expect."""
    return f"user={user}\x01auth=Bearer {access_token}\x01\x01"
