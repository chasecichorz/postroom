"""Read contacts from a CardDAV address book (Google Contacts). Never changes anything.

Lists the address book with PROPFIND, then fetches the vCards in batches with an
addressbook-multiget REPORT (RFC 6352), which Google's CardDAV server supports.
"""
from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .contacts import Contact, parse_vcards

GOOGLE_BOOK = "https://www.googleapis.com/carddav/v1/principals/{email}/lists/default/"
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}
BATCH = 100
TIMEOUT = 30

_PROPFIND = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/><d:resourcetype/></d:prop></d:propfind>"""


class CardDavError(Exception):
    pass


def _request(method: str, url: str, token: str, body: bytes, depth: str) -> bytes:
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/xml; charset=utf-8",
        "Depth": depth, "User-Agent": "Postroom"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as r:
            return r.read(32 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise CardDavError("Google refused access to your contacts. Sign in again with "
                               "Sign in with browser to allow it.") from e
        raise CardDavError(f"Google contacts failed: HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise CardDavError(f"Could not reach Google contacts: {e}") from e


def _multiget(hrefs: list[str]) -> bytes:
    items = "".join(f"<d:href>{h}</d:href>" for h in hrefs)
    return (f'<?xml version="1.0" encoding="utf-8"?><c:addressbook-multiget xmlns:d="DAV:" '
            f'xmlns:c="urn:ietf:params:xml:ns:carddav"><d:prop><d:getetag/><c:address-data/>'
            f"</d:prop>{items}</c:addressbook-multiget>").encode()


def list_cards(xml: bytes, book_path: str) -> list[str]:
    """The card hrefs in a PROPFIND reply, leaving out the address book itself."""
    root = ET.fromstring(xml)
    out = []
    for resp in root.findall("d:response", NS):
        href = (resp.findtext("d:href", "", NS) or "").strip()
        is_collection = resp.find(".//d:resourcetype/d:collection", NS) is not None
        if href and not is_collection and href.rstrip("/") != book_path.rstrip("/"):
            out.append(href)
    return out


def card_data(xml: bytes) -> str:
    root = ET.fromstring(xml)
    return "\n".join(el.text or "" for el in root.iter("{urn:ietf:params:xml:ns:carddav}address-data"))


def fetch(book_url: str, token: str) -> list[Contact]:
    try:
        listing = _request("PROPFIND", book_url, token, _PROPFIND, "1")
        hrefs = list_cards(listing, urllib.parse.urlsplit(book_url).path)
    except ET.ParseError as e:
        raise CardDavError("Google sent a reply Postroom couldn't read.") from e
    found: list[Contact] = []
    for i in range(0, len(hrefs), BATCH):
        reply = _request("REPORT", book_url, token, _multiget(hrefs[i:i + BATCH]), "1")
        try:
            found += parse_vcards(card_data(reply))
        except ET.ParseError as e:
            raise CardDavError("Google sent contacts Postroom couldn't read.") from e
    return found


def google_contacts(email: str, token: str) -> list[Contact]:
    return fetch(GOOGLE_BOOK.format(email=urllib.parse.quote(email)), token)
