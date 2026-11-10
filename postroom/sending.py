"""Building outgoing messages (new, reply, forward) and sending them over SMTP."""
from __future__ import annotations

import mimetypes
import smtplib
import ssl
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr

from .config import Account
from .mime import Attachment, ParsedMessage


class SendError(Exception):
    pass


class DeliveryUncertain(SendError):
    """The connection failed after delivery may have begun; never retry automatically."""


@dataclass
class SendResult:
    accepted: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    warning: str = ""


@dataclass
class Draft:
    to: str = ""
    cc: str = ""
    subject: str = ""
    body: str = ""
    in_reply_to: str = ""
    references: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    # The message being replied to, so it can be marked as answered after sending.
    reply_source: tuple | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    message_id: str = ""
    delivery_state: str = "draft"
    pending_recipients: list[str] | None = None


def _addresses(value: str) -> list[tuple[str, str]]:
    return [(n, a) for n, a in getaddresses([value]) if a]


def _fmt(addrs) -> str:
    return ", ".join(formataddr((n, a)) for n, a in addrs)


def _prefixed(prefix: str, subject: str) -> str:
    s = subject.strip()
    return s if s.lower().startswith(prefix.lower()) else f"{prefix} {s}".strip()


def _quote(pm: ParsedMessage) -> str:
    who = parseaddr(pm.from_)[0] or parseaddr(pm.from_)[1] or "someone"
    quoted = "\n".join("> " + ln if ln else ">" for ln in pm.text.splitlines())
    return f"\n\nOn {pm.date}, {who} wrote:\n{quoted}\n"


def reply(account: Account, pm: ParsedMessage, reply_all: bool, source: tuple | None = None) -> Draft:
    me = account.email.lower()
    to = _addresses(pm.reply_to or pm.from_)
    cc: list[tuple[str, str]] = []
    if reply_all:
        seen = {a.lower() for _, a in to} | {me}
        for n, a in _addresses(pm.to) + _addresses(pm.cc):
            if a.lower() not in seen:
                seen.add(a.lower())
                cc.append((n, a))
        if not to:  # replying to your own sent message
            to, cc = cc, []
    refs = " ".join(x for x in (pm.references, pm.message_id) if x)
    return Draft(to=_fmt(to), cc=_fmt(cc), subject=_prefixed("Re:", pm.subject), body=_quote(pm),
                 in_reply_to=pm.message_id, references=refs, reply_source=source)


def forward(pm: ParsedMessage) -> Draft:
    header = ["", "", "---------- Forwarded message ----------", f"From: {pm.from_}",
              f"Date: {pm.date}", f"Subject: {pm.subject}", f"To: {pm.to}"]
    if pm.cc:
        header.append(f"Cc: {pm.cc}")
    body = "\n".join(header) + "\n\n" + pm.text + "\n"
    return Draft(subject=_prefixed("Fwd:", pm.subject), body=body,
                 attachments=list(pm.attachments))


def validate(draft: Draft) -> str | None:
    """Return a problem to tell the user about, or None if the draft can be sent."""
    if any("\r" in value or "\n" in value for value in (draft.to, draft.cc, draft.subject)):
        return "Recipient and subject fields must each be on one line."
    recipients = _addresses(draft.to) + _addresses(draft.cc)
    if not recipients:
        return "Add at least one recipient in To or Cc."
    bad = [a for _, a in recipients if "@" not in a or a.startswith("@") or a.endswith("@")]
    if bad:
        return f"This address doesn't look right: {bad[0]}"
    return None


def build(account: Account, draft: Draft) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((account.full_name, account.email))
    msg["To"] = _fmt(_addresses(draft.to))
    if draft.cc.strip():
        msg["Cc"] = _fmt(_addresses(draft.cc))
    msg["Subject"] = draft.subject
    msg["Date"] = formatdate(localtime=True)
    if not draft.message_id:
        draft.message_id = make_msgid(domain=account.email.rpartition("@")[2] or None)
    msg["Message-ID"] = draft.message_id
    if draft.in_reply_to:
        msg["In-Reply-To"] = draft.in_reply_to
    if draft.references:
        msg["References"] = draft.references
    msg.set_content(draft.body)
    for att in draft.attachments:
        ctype = att.content_type or mimetypes.guess_type(att.filename)[0] or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        if ctype == "message/rfc822":
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(att.data, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=att.filename)
    return msg


def send(account: Account, password: str, msg: EmailMessage,
         to_addrs: list[str] | None = None) -> SendResult:
    """Send `msg`. `password` is the app password, or the access token for a browser
    sign-in account."""
    ctx = ssl.create_default_context()
    server = None
    delivering = False
    recipients = list(dict.fromkeys(to_addrs if to_addrs is not None else
                                   [a for _, a in _addresses(str(msg.get("To", ""))) +
                                    _addresses(str(msg.get("Cc", "")))]))
    if not recipients:
        raise SendError("Add at least one recipient.")
    try:
        if account.smtp_uses_ssl:
            server = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, context=ctx, timeout=60)
        else:
            server = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=60)
            server.starttls(context=ctx)
        if account.auth == "oauth2":
            from .oauth2 import xoauth2
            server.ehlo()
            server.auth("XOAUTH2", lambda challenge=None: xoauth2(account.login, password),
                        initial_response_ok=True)
        else:
            server.login(account.login, password)
        delivering = True
        refused = server.send_message(msg, to_addrs=recipients)
        rejected = {a.casefold() for a in refused}
        return SendResult([a for a in recipients if a.casefold() not in rejected], list(refused))
    except smtplib.SMTPAuthenticationError as e:
        if account.auth == "oauth2":
            from .credentials import invalidate
            invalidate(account)
            raise SendError("The mail server refused the sign-in. Try sending again; if it "
                            "keeps failing, sign in again in Edit Account.") from e
        raise SendError("Sign-in to the mail server failed. Check the app password.") from e
    except smtplib.SMTPRecipientsRefused as e:
        raise SendError("No recipients accepted. The server refused: " + ", ".join(e.recipients)) from e
    except smtplib.SMTPResponseException as e:
        raise SendError(f"The server refused the message (SMTP {e.smtp_code}). It was not accepted.") from e
    except (smtplib.SMTPServerDisconnected, OSError) as e:
        if delivering:
            raise DeliveryUncertain("Delivery could not be confirmed. Check Sent and ask the recipients "
                                    "before retrying; sending again could create duplicates.") from e
        raise SendError("Could not connect or sign in to the sending server. Try again later.") from e
    except smtplib.SMTPException as e:
        raise SendError(f"Sending failed: {e}") from e
    finally:
        if server is not None:
            # A failed QUIT after SMTP acceptance must never turn success into a retry.
            try:
                server.close()
            except OSError:
                pass
