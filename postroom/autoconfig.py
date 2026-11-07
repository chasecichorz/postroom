# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Ported to Python for Postroom from Thunderbird's account setup
# (mail/components/accountcreation/modules: FindConfig, FetchConfig, readFromXML,
# GuessConfig and InputSanitizer), https://github.com/thunderbird/thunderbird-desktop.
"""Find an email provider's server settings from just the email address.

The lookups run in Thunderbird's order, and the first one that answers wins:

1. The provider itself: https://autoconfig.<domain>/mail/config-v1.1.xml, then
   https://<domain>/.well-known/autoconfig/mail/config-v1.1.xml
2. Thunderbird's public provider database (ISPDB)
3. The domain's MX record. A custom domain whose mail is handled by Google or Microsoft
   gets Google's or Microsoft's settings.
4. Guessing: try imap.<domain> and mail.<domain> on the standard ports.

Thunderbird also tries plain-HTTP and sends the email address to the provider. Postroom
only uses HTTPS, so an attacker on the network can't hand it fake servers, and sends just
the domain.
"""
from __future__ import annotations

import imaplib
import random
import re
import smtplib
import socket
import ssl
import struct
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

ISPDB_URL = "https://live.thunderbird.net/autoconfig/v1.1/"
USER_AGENT = "Postroom"
TIMEOUT = 10

# Security of a connection.
SSL = "ssl"            # TLS from the first byte (IMAP 993, SMTP 465)
STARTTLS = "starttls"  # plain connection upgraded with STARTTLS (IMAP 143, SMTP 587)
PLAIN = "plain"        # no encryption; Postroom never uses these

# Ways of signing in.
AUTH_PASSWORD = "password"
AUTH_OAUTH2 = "oauth2"


@dataclass
class Server:
    hostname: str
    port: int
    security: str                 # SSL, STARTTLS or PLAIN
    username: str = "%EMAILADDRESS%"
    auth: list[str] = field(default_factory=lambda: [AUTH_PASSWORD])


@dataclass
class ServerConfig:
    incoming: Server
    outgoing: Server
    display_name: str = ""
    source: str = ""              # where it came from: "isp", "db", "mx", "guess"

    def username_for(self, email: str, server: Server | None = None) -> str:
        return expand(server.username if server else self.incoming.username, email)

    @property
    def oauth2(self) -> bool:
        return AUTH_OAUTH2 in self.incoming.auth


def expand(template: str, email: str) -> str:
    """Fill in Thunderbird's placeholders: %EMAILADDRESS%, %EMAILLOCALPART%, %EMAILDOMAIN%."""
    local, _, domain = email.partition("@")
    return (template.replace("%EMAILADDRESS%", email).replace("%EMAILLOCALPART%", local)
            .replace("%EMAILDOMAIN%", domain))


# ----- input checks (InputSanitizer.hostname) ------------------------------------------------
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def clean_hostname(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise ValueError(f"Not a valid host name: {host}") from e
    if not _HOST_RE.match(host):
        raise ValueError(f"Not a valid host name: {host}")
    return host


def domain_of(email: str) -> str:
    if email.count("@") != 1:
        raise ValueError("Enter a full email address, like name@example.com.")
    return clean_hostname(email.rsplit("@", 1)[1])


# ----- reading config-v1.1.xml (readFromXML) -------------------------------------------------
_SOCKET = {"SSL": SSL, "STARTTLS": STARTTLS, "plain": PLAIN}
_AUTH = {"password-cleartext": AUTH_PASSWORD, "plain": AUTH_PASSWORD,
         "password-encrypted": AUTH_PASSWORD, "secure": AUTH_PASSWORD, "OAuth2": AUTH_OAUTH2}


def _server(el: ET.Element) -> Server | None:
    def text(tag):
        child = el.find(tag)
        return (child.text or "").strip() if child is not None and child.text else ""
    try:
        host = clean_hostname(text("hostname"))
        port = int(text("port"))
    except ValueError:
        return None
    if not 0 < port < 65536:
        return None
    security = _SOCKET.get(text("socketType"), PLAIN)
    auths = [_AUTH[a.text.strip()] for a in el.findall("authentication")
             if a.text and a.text.strip() in _AUTH]
    auths = list(dict.fromkeys(auths)) or [AUTH_PASSWORD]
    return Server(host, port, security, text("username") or "%EMAILADDRESS%", auths)


def parse_config_xml(data: bytes, source: str) -> ServerConfig:
    """Read a Thunderbird autoconfig file. Picks the first usable encrypted IMAP and SMTP
    servers, as Thunderbird does when you don't choose."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"Bad autoconfig file: {e}") from e
    provider = root.find("emailProvider")
    if provider is None:
        raise ValueError("No emailProvider in autoconfig file")
    name = (provider.findtext("displayName") or "").strip()
    incoming = [s for el in provider.findall("incomingServer") if el.get("type") == "imap"
                if (s := _server(el)) and s.security != PLAIN]
    outgoing = [s for el in provider.findall("outgoingServer") if el.get("type") == "smtp"
                if (s := _server(el)) and s.security != PLAIN]
    if not incoming or not outgoing:
        raise ValueError("No encrypted IMAP and SMTP servers in autoconfig file")
    # Prefer SSL over STARTTLS, as Thunderbird's default ordering usually ends up doing.
    incoming.sort(key=lambda s: s.security != SSL)
    outgoing.sort(key=lambda s: s.security != SSL)
    return ServerConfig(incoming[0], outgoing[0], name, source)


# ----- fetching (FetchConfig) ----------------------------------------------------------------
def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/xml,text/xml,*/*"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        if not resp.geturl().startswith("https://"):
            raise ValueError("Refusing a redirect away from HTTPS")
        return resp.read(256 * 1024)


def from_isp(domain: str) -> ServerConfig:
    errors = []
    for url in (f"https://autoconfig.{domain}/mail/config-v1.1.xml",
                f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml"):
        try:
            return parse_config_xml(_get(url), "isp")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    raise LookupError(f"Provider has no autoconfig: {errors[-1]}")


def from_db(domain: str) -> ServerConfig:
    return parse_config_xml(_get(ISPDB_URL + domain), "db")


def from_mx(domain: str) -> ServerConfig:
    mx = lookup_mx(domain)
    if not mx:
        raise LookupError("No MX record")
    base = base_domain(mx)
    if base == domain:
        raise LookupError("MX is in the same domain")
    # Thunderbird also tries the next subdomain down (e.g. "mail.protection.outlook.com").
    candidates = [base]
    parts = mx.split(".")
    if len(parts) > len(base.split(".")) + 1:
        sub = ".".join(parts[-len(base.split(".")) - 1:])
        candidates.insert(0, sub)
    for cand in candidates:
        for fetch in (from_isp, from_db):
            try:
                cfg = fetch(cand)
                cfg.source = "mx"
                return cfg
            except Exception:  # noqa: BLE001
                continue
    raise LookupError(f"Nothing known for the mail host {mx}")


# Common two-part public suffixes; enough to find the provider behind an MX host.
_TWO_PART_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.nz", "co.jp",
                      "com.br", "co.za", "com.mx", "co.in", "com.tr", "com.cn"}


def base_domain(host: str) -> str:
    parts = host.lower().rstrip(".").split(".")
    n = 3 if ".".join(parts[-2:]) in _TWO_PART_SUFFIXES else 2
    return ".".join(parts[-n:])


# ----- guessing (GuessConfig) ----------------------------------------------------------------
def _try_imap(host: str, security: str, port: int) -> bool:
    ctx = ssl.create_default_context()
    try:
        if security == SSL:
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=6)
        else:
            conn = imaplib.IMAP4(host, port, timeout=6)
            if "STARTTLS" not in conn.capabilities:
                conn.shutdown()
                return False
            conn.starttls(ssl_context=ctx)
        conn.shutdown()
        return True
    except Exception:  # noqa: BLE001
        return False


def _try_smtp(host: str, security: str, port: int) -> bool:
    ctx = ssl.create_default_context()
    try:
        if security == SSL:
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=6)
        else:
            s = smtplib.SMTP(host, port, timeout=6)
            s.ehlo()
            if not s.has_extn("starttls"):
                s.close()
                return False
            s.starttls(context=ctx)
        s.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def guess(domain: str) -> ServerConfig:
    """Try the usual server names, encrypted ports only (Thunderbird's try order, minus
    POP3 and unencrypted connections)."""
    incoming = [(h, sec, port) for h in (f"imap.{domain}", f"mail.{domain}", domain)
                for sec, port in ((SSL, 993), (STARTTLS, 143))]
    outgoing = [(h, sec, port) for h in (f"smtp.{domain}", f"mail.{domain}", domain)
                for sec, port in ((SSL, 465), (STARTTLS, 587))]
    with ThreadPoolExecutor(max_workers=12) as pool:
        inc = list(pool.map(lambda t: _try_imap(*t), incoming))
        out = list(pool.map(lambda t: _try_smtp(*t), outgoing))
    try:
        i = incoming[inc.index(True)]
        o = outgoing[out.index(True)]
    except ValueError:
        raise LookupError(f"Could not find mail servers for {domain}") from None
    return ServerConfig(Server(*i), Server(*o), domain, "guess")


# ----- the whole search (FindConfig.parallelAutoDiscovery) -----------------------------------
def find_config(email: str, allow_guess: bool = True) -> ServerConfig:
    """Return the best settings for `email`, or raise LookupError.

    Like Thunderbird, the provider, the database and the MX lookup run at the same time,
    and the earliest in that list that succeeds wins.
    """
    domain = domain_of(email)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(f, domain) for f in (from_isp, from_db, from_mx)]
        for fut in futures:
            try:
                return fut.result()
            except Exception:  # noqa: BLE001
                continue
    if allow_guess:
        return guess(domain)
    raise LookupError(f"No settings found for {domain}")


# ----- a minimal DNS MX lookup (Thunderbird uses its own DNS module) -------------------------
def _nameservers() -> list[str]:
    servers = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    servers.append(parts[1])
    except OSError:
        pass
    return servers or ["127.0.0.53"]


def _read_name(msg: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end = [], False, off
    for _ in range(64):
        n = msg[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack(">H", msg[off:off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            jumped, off = True, ptr
            continue
        labels.append(msg[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += 1 + n
    return ".".join(labels), (end if jumped else off)


def lookup_mx(domain: str) -> str | None:
    """Return the highest-priority MX host for `domain`, or None."""
    qid = random.randrange(65536)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in domain.split(".")) + b"\0"
    q += struct.pack(">HH", 15, 1)  # MX, IN
    for ns in _nameservers():
        fam = socket.AF_INET6 if ":" in ns else socket.AF_INET
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as s:
                s.settimeout(4)
                s.sendto(q, (ns, 53))
                msg, _ = s.recvfrom(4096)
        except OSError:
            continue
        if len(msg) < 12 or struct.unpack(">H", msg[:2])[0] != qid:
            continue
        _, flags, qd, an, _, _ = struct.unpack(">HHHHHH", msg[:12])
        if flags & 0x000F:
            return None
        off = 12
        for _ in range(qd):
            _, off = _read_name(msg, off)
            off += 4
        best = None
        for _ in range(an):
            _, off = _read_name(msg, off)
            rtype, _, _, rdlen = struct.unpack(">HHIH", msg[off:off + 10])
            off += 10
            if rtype == 15:
                pref = struct.unpack(">H", msg[off:off + 2])[0]
                host, _ = _read_name(msg, off + 2)
                if host and (best is None or pref < best[0]):
                    best = (pref, host)
            off += rdlen
        return best[1].lower() if best else None
    return None
