"""Turn a raw RFC 822 message into readable text, links and attachments."""
from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from html.parser import HTMLParser

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class Link:
    text: str
    url: str


@dataclass
class ParsedMessage:
    subject: str = ""
    from_: str = ""
    to: str = ""
    cc: str = ""
    reply_to: str = ""
    date: str = ""
    message_id: str = ""
    references: str = ""
    text: str = ""
    links: list[Link] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    has_html: bool = False
    has_plain: bool = False


_BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "table",
          "tr", "ul", "ol", "dl", "dt", "dd", "blockquote", "pre", "form", "fieldset", "figure",
          "h1", "h2", "h3", "h4", "h5", "h6", "hr", "address", "center"}
_SKIP = {"script", "style", "head", "title", "noscript", "template", "svg"}


class _HtmlToText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.links: list[Link] = []
        self.skip = 0
        self.pre = 0
        self.href: str | None = None
        self.link_text: list[str] = []
        self.quote = 0

    def _nl(self, n=1):
        text = "".join(self.out)
        have = len(text) - len(text.rstrip("\n"))
        if text and have < n:
            self.out.append("\n" * (n - have))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP:
            self.skip += 1
        elif self.skip:
            return
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self._nl()
            self.out.append("• ")
        elif tag in ("td", "th"):
            self.out.append("  ")
        elif tag == "pre":
            self.pre += 1
            self._nl()
        elif tag == "blockquote":
            self.quote += 1
            self._nl(2)
        elif tag in _BLOCK:
            self._nl(2 if tag in ("p", "h1", "h2", "h3", "table", "ul", "ol") else 1)
        elif tag == "a":
            self.href = (a.get("href") or "").strip()
            self.link_text = []
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            if alt and len(alt) < 80:
                self.out.append(f" {alt} ")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._nl()
        elif tag == "blockquote":
            self.quote = max(0, self.quote - 1)
            self._nl(2)
        elif tag == "a":
            if self.href and re.match(r"(https?|mailto):", self.href, re.I):
                text = " ".join("".join(self.link_text).split()) or self.href
                self.links.append(Link(text, self.href))
            self.href = None
        elif tag in _BLOCK:
            self._nl(2 if tag in ("p", "h1", "h2", "h3", "table", "ul", "ol") else 1)

    def handle_data(self, data):
        if self.skip:
            return
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
        if self.href is not None:
            self.link_text.append(data)
        self.out.append(data)

    def text(self) -> str:
        lines = [ln.strip() if not self.pre else ln.rstrip() for ln in "".join(self.out).split("\n")]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> tuple[str, list[Link]]:
    p = _HtmlToText()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - never fail on bad markup
        pass
    return p.text(), p.links


def _content(part) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


def _links_in_text(text: str) -> list[Link]:
    seen, out = set(), []
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;:")
        if u not in seen:
            seen.add(u)
            out.append(Link(u, u))
    return out


def _attachments(msg: EmailMessage) -> list[Attachment]:
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        filename = part.get_filename()
        if disp == "attachment" or (filename and disp != "inline") or \
                (filename and part.get_content_maintype() not in ("text", "image")):
            data = part.get_payload(decode=True) or b""
            out.append(Attachment(filename or "attachment", part.get_content_type(), data))
        elif part.get_content_type() == "message/rfc822":
            out.append(Attachment((filename or "attached message") + ".eml", "message/rfc822",
                                  part.as_bytes()))
    return out


def parse(raw: bytes, prefer_html: bool = False) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=policy.default)
    h = lambda k: " ".join(str(msg.get(k, "") or "").split())  # noqa: E731
    pm = ParsedMessage(subject=h("Subject"), from_=h("From"), to=h("To"), cc=h("Cc"),
                       reply_to=h("Reply-To"), date=h("Date"), message_id=h("Message-ID"),
                       references=h("References"))
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    pm.has_plain, pm.has_html = plain is not None, html is not None
    if html is not None and (prefer_html or plain is None):
        pm.text, pm.links = html_to_text(_content(html))
    elif plain is not None:
        pm.text = _content(plain).replace("\r\n", "\n").strip()
        pm.links = _links_in_text(pm.text)
        if html is not None and not pm.links:
            pm.links = html_to_text(_content(html))[1]
    elif not msg.is_multipart():
        pm.text = _content(msg).strip()
    pm.attachments = _attachments(msg)
    return pm


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.0f} {unit}" if n >= 10 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"
