"""Tool A - Email Reader (read-only IMAP).

Safety properties (each covered by tests against a recording fake IMAP server):
  * mailbox is opened with ``select(readonly=True)``; only SEARCH/FETCH ``BODY.PEEK``
    are ever issued (no STORE/COPY/APPEND/EXPUNGE, nothing is marked read or moved);
  * UID commands + UIDVALIDITY, so identifiers are stable across sessions;
  * a cursor (last UID) bounds each read; the returned ``cursor_uid`` never skips
    past a message that failed to fetch;
  * auth failures are reported, never retried; timeouts/IO errors are reported with
    the exact stage;
  * full bodies are never logged - only counts and UIDs.

LIMIT: this module has only been exercised against a fake IMAP server in this
repository's tests. A live mailbox has NOT been verified (no credentials were
available). See docs/REPORT.md.
"""
from __future__ import annotations

import email
import email.header
import email.message
import email.policy
import hashlib
import imaplib
import json
import re
import socket
from dataclasses import dataclass, field
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional

from bs4 import BeautifulSoup

from jobledger.logging_setup import get_logger
from jobledger.timeutil import utcnow

logger = get_logger("email_reader")

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_SEPARATOR_TAGS = {"tr", "li", "table", "section", "article"}
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5",
               "h6", "section", "article", "hr", "td", "th", "tbody", "thead", "blockquote"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|"
                           r"opacity\s*:\s*0(?:\.0+)?(?![\d.])|max-height\s*:\s*0", re.I)


@dataclass
class EmailMessage:
    uid: str                      # IMAP UID as text; stable id together with uidvalidity
    sender: str
    subject: str
    received_at: str              # ISO-8601 (UTC) or "" when the Date header is unusable
    body_text: str                # plain text (HTML converted if no text/plain part)
    body_html: str = ""
    body_hash: str = ""
    message_id: str = ""
    source: str = "imap"
    hidden_text: str = ""         # text in visually hidden HTML elements (quarantined)
    warnings: list = field(default_factory=list)

    def __post_init__(self):
        if not self.body_hash:
            blob = (self.body_text + "\x00" + self.body_html).encode("utf-8", errors="replace")
            self.body_hash = hashlib.sha256(blob).hexdigest()


@dataclass
class ReadOutcome:
    messages: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    status: str = "ok"            # ok | auth_failed | connect_failed | timeout | folder_error | search_failed
    uidvalidity: str = ""
    cursor_uid: int = 0           # safe to persist as the new last-seen UID
    more_available: bool = False
    skipped_nonmatching: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------

def decode_header_value(value) -> str:
    if not value:
        return ""
    out = []
    for part, charset in email.header.decode_header(str(value)):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:                      # unknown charset name
                out.append(part.decode("utf-8", errors="replace"))
        else:
            out.append(part)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _decode_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    for cs in (part.get_content_charset(), "utf-8", "latin-1"):
        if not cs:
            continue
        try:
            return payload.decode(cs, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def clean_line(text: str) -> str:
    return re.sub(r"[ \t\r\f\v\xa0]+", " ", text.translate(_ZERO_WIDTH)).strip()


def html_to_lines(html: str) -> tuple[list, str]:
    """HTML -> [(line_text, [(anchor_text, href), ...])], plus text of hidden elements."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "head", "title", "meta", "noscript"]):
        t.decompose()
    lines: list = []
    hidden: list = []
    cur_text: list = []
    cur_links: list = []

    def flush():
        txt = clean_line("".join(cur_text))
        if txt or cur_links:
            lines.append((txt, list(cur_links)))
        cur_text.clear()
        cur_links.clear()

    def walk(node):
        if isinstance(node, str):
            if type(node).__name__ in ("Comment", "Doctype", "CData", "ProcessingInstruction"):
                return
            cur_text.append(str(node))
            return
        name = (node.name or "").lower()
        style = node.get("style") or ""
        if node.get("hidden") is not None or _HIDDEN_STYLE.search(style):
            t = clean_line(node.get_text(" ", strip=True))
            if t:
                hidden.append(t)
            return
        if name in ("br", "hr"):
            flush()
            return
        block = name in _BLOCK_TAGS
        if block:
            flush()
        if name in _SEPARATOR_TAGS and lines and lines[-1][0]:
            lines.append(("", []))             # container boundary = record boundary for the parser
        if name == "a" and node.get("href"):
            cur_links.append((clean_line(node.get_text(" ", strip=True)), node["href"].strip()))
        for child in node.children:
            walk(child)
        if block:
            flush()
        if name in _SEPARATOR_TAGS and lines and lines[-1][0]:
            lines.append(("", []))

    try:
        walk(soup)
        flush()
    except RecursionError:                     # pathological nesting: degrade to flat text
        lines = [(clean_line(l), []) for l in soup.get_text("\n").splitlines() if clean_line(l)]
    return lines, " | ".join(hidden)


def html_to_text(html: str) -> str:
    lines, _ = html_to_lines(html)
    return "\n".join(t for t, _ in lines if t)


def message_from_raw(raw: bytes, uid: str, source: str = "imap") -> EmailMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    plain, html = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        ct = part.get_content_type()
        if ct == "text/plain":
            plain.append(_decode_payload(part))
        elif ct == "text/html":
            html.append(_decode_payload(part))
    warnings: list = []
    html_s = "\n".join(html)
    hidden = ""
    if plain:
        text = "\n".join(plain)
        if html_s:
            _, hidden = html_to_lines(html_s)
    elif html_s:
        lines, hidden = html_to_lines(html_s)
        text = "\n".join(t for t, _ in lines if t)
    else:
        text = ""
        warnings.append("no_text_body")
    date_hdr = msg.get("Date", "")
    try:
        received = parsedate_to_datetime(date_hdr).isoformat() if date_hdr else ""
    except (TypeError, ValueError):
        received = ""
        warnings.append("bad_date_header")
    return EmailMessage(
        uid=str(uid), sender=decode_header_value(msg.get("From")),
        subject=decode_header_value(msg.get("Subject")), received_at=received,
        body_text=text, body_html=html_s, message_id=(msg.get("Message-ID") or "").strip(),
        source=source, hidden_text=hidden, warnings=warnings)


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def _default_factory(host: str, port: int, timeout: int):
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


def _matches(subject: str, sender: str, keywords: list) -> bool:
    if not keywords:
        return True
    hay = f"{subject} {sender}".lower()
    return any(k in hay for k in keywords)


def _imap_date(days: int) -> str:
    return (utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")


def _parse_header_fetch(data) -> Optional[bytes]:
    for part in data or []:
        if isinstance(part, tuple) and len(part) >= 2:
            return part[1]
    return None


def fetch_job_alert_emails(
    host: str, port: int, user: str, password: str, folder: str = "INBOX",
    filter_subjects: Optional[list] = None, last_uid: int = 0, since_days: int = 30,
    max_messages: int = 50, timeout: int = 15,
    imap_factory: Optional[Callable] = None,
) -> ReadOutcome:
    """Read new matching messages. ``last_uid`` is the cursor from the previous read."""
    out = ReadOutcome()
    keywords = [k.lower() for k in (filter_subjects or [])]
    factory = imap_factory or _default_factory
    try:
        mail = factory(host, port, timeout)
    except (socket.timeout, TimeoutError):
        out.status, out.errors = "timeout", [f"timed out connecting to {host}:{port}"]
        return out
    except (OSError, imaplib.IMAP4.error) as exc:
        out.status, out.errors = "connect_failed", [f"cannot connect to {host}:{port}: {type(exc).__name__}"]
        return out

    try:
        try:
            mail.login(user, password)
        except imaplib.IMAP4.error as exc:
            # Deliberately not retried: repeated bad logins can lock the account.
            out.status, out.errors = "auth_failed", [f"authentication failed for {user}: {_safe(exc)}"]
            return out
        except (socket.timeout, TimeoutError):
            out.status, out.errors = "timeout", ["timed out during login"]
            return out

        typ, _ = mail.select(f'"{folder}"' if " " in folder else folder, readonly=True)
        if typ != "OK":
            out.status, out.errors = "folder_error", [f"cannot open folder '{folder}' read-only"]
            return out
        try:
            typ, uv = mail.response("UIDVALIDITY")
            out.uidvalidity = (uv[0].decode() if uv and isinstance(uv[0], bytes) else str(uv[0])) if uv and uv[0] else "0"
        except (imaplib.IMAP4.error, AttributeError, IndexError):
            out.uidvalidity = "0"

        criteria = ("UID", f"{int(last_uid) + 1}:*") if last_uid else ("SINCE", _imap_date(since_days))
        typ, data = mail.uid("SEARCH", None, *criteria)
        if typ != "OK":
            out.status, out.errors = "search_failed", [f"IMAP SEARCH failed: {data}"]
            return out
        raw = data[0].split() if data and data[0] else []
        # RFC 3501: "UID n:*" always returns the highest message even when n > highest. Filter.
        uids = sorted(u for u in (int(x) for x in raw) if u > int(last_uid))
        out.more_available = len(uids) > max_messages
        uids = uids[:max_messages]
        logger.info("mailbox: %d candidate message(s) after UID %s", len(uids), last_uid)

        cursor = int(last_uid)
        blocked = False
        for uid in uids:
            try:
                typ, hdr = mail.uid("FETCH", str(uid), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])")
                head_raw = _parse_header_fetch(hdr)
                if typ != "OK" or head_raw is None:
                    raise ValueError("header fetch returned nothing")
                head = email.message_from_bytes(head_raw)
                subj, sender = decode_header_value(head.get("Subject")), decode_header_value(head.get("From"))
                if not _matches(subj, sender, keywords):
                    out.skipped_nonmatching += 1
                else:
                    typ, body = mail.uid("FETCH", str(uid), "(BODY.PEEK[])")
                    raw_msg = _parse_header_fetch(body)
                    if typ != "OK" or raw_msg is None:
                        raise ValueError("body fetch returned nothing")
                    out.messages.append(message_from_raw(raw_msg, str(uid)))
                if not blocked:
                    cursor = uid
            except (socket.timeout, TimeoutError):
                out.errors.append(f"UID {uid}: timed out")
                blocked = True
            except (imaplib.IMAP4.error, ValueError, OSError) as exc:
                out.errors.append(f"UID {uid}: {_safe(exc)}")
                blocked = True            # cursor must not move past a failed message
        out.cursor_uid = cursor
        if out.errors and not out.messages:
            out.status = "timeout" if any("timed out" in e for e in out.errors) else "search_failed"
    except (socket.timeout, TimeoutError):
        out.status, out.errors = "timeout", out.errors + ["timed out talking to the server"]
    except (imaplib.IMAP4.error, OSError) as exc:
        out.status, out.errors = "search_failed", out.errors + [f"IMAP error: {_safe(exc)}"]
    finally:
        try:
            mail.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    logger.info("mailbox read: status=%s messages=%d errors=%d", out.status, len(out.messages), len(out.errors))
    return out


def _safe(exc: Exception) -> str:
    """Exception text without anything that could echo credentials."""
    s = str(exc)
    return re.sub(r"(?i)(password|pass|token)\S*", "[redacted]", s)[:200]


# ---------------------------------------------------------------------------
# Fixtures (development / tests). Never represented as live mailbox integration.
# ---------------------------------------------------------------------------

def load_email_fixtures(path: str) -> ReadOutcome:
    """Load ``.eml`` / ``.json`` fixtures from a file or directory. source='fixture'."""
    out = ReadOutcome()
    p = Path(path)
    files = sorted(p.glob("*")) if p.is_dir() else [p]
    if not p.exists():
        out.status, out.errors = "folder_error", [f"fixture path not found: {path}"]
        return out
    for n, f in enumerate(files, 1):
        try:
            if f.suffix.lower() == ".eml":
                msg = message_from_raw(f.read_bytes(), uid=f"fx-{f.stem}", source="fixture")
                out.messages.append(msg)
            elif f.suffix.lower() == ".json":
                data = json.loads(f.read_text(encoding="utf-8"))
                for item in (data if isinstance(data, list) else [data]):
                    body = item.get("body_text", "")
                    out.messages.append(EmailMessage(
                        uid=str(item["uid"]), sender=item.get("sender", ""), subject=item.get("subject", ""),
                        received_at=item.get("received_at", ""), body_text=body,
                        body_html=item.get("body_html", ""), source="fixture"))
        except (OSError, ValueError, KeyError) as exc:
            out.errors.append(f"{f.name}: {type(exc).__name__}: {exc}")
    return out
