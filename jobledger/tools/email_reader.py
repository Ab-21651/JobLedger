"""
Tool A: Email Reader
Read-only IMAP integration for fetching job-alert emails.
- Connects via SSL using Python's built-in imaplib
- Searches by configurable subject keywords
- Fetches with BODY.PEEK[] to avoid marking as read
- Deduplicates by IMAP UID
- Handles HTML/multipart MIME
- Returns structured EmailReadResult dicts
- Never logs passwords or raw email bodies
"""
from __future__ import annotations

import email
import email.header
import hashlib
import imaplib
import json
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from jobledger.logging_setup import get_logger

logger = get_logger("email_reader")


@dataclass
class EmailReadResult:
    uid: str
    sender: str
    subject: str
    received_at: str        # ISO-8601
    body_text: str          # decoded plain text (HTML stripped)
    body_hash: str          # SHA-256 of body_text
    source: str = "imap"


def _decode_header_value(value: str) -> str:
    """Decode encoded email header (RFC 2047)."""
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except Exception:
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded).strip()


def _extract_body_text(msg: email.message.Message) -> str:
    """Extract plain text from email, falling back to HTML->text."""
    plain_parts = []
    html_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if ct == "text/plain":
                plain_parts.append(text)
            elif ct == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        return "\n".join(plain_parts)
    if html_parts:
        return _html_to_text("\n".join(html_parts))
    return ""


def _html_to_text(html: str) -> str:
    """Strip HTML tags and return plain text."""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "head", "meta"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except Exception:
        import re
        return re.sub(r"<[^>]+>", " ", html)


def _make_since_string(since_date: Optional[str]) -> str:
    """Return IMAP SINCE date string (DD-Mon-YYYY)."""
    if since_date:
        return since_date
    dt = datetime.utcnow() - timedelta(days=30)
    return dt.strftime("%d-%b-%Y")


def _build_search_criteria(filter_subjects: list[str], since_str: str) -> str:
    """Build IMAP SEARCH command criteria string."""
    base = f'(SINCE "{since_str}")'
    if not filter_subjects:
        return base
    # Build OR chain for subjects
    if len(filter_subjects) == 1:
        return f'(SINCE "{since_str}" SUBJECT "{filter_subjects[0]}")'
    # Multiple subjects: OR them
    criteria = f'SUBJECT "{filter_subjects[0]}"'
    for subj in filter_subjects[1:]:
        criteria = f'OR ({criteria}) (SUBJECT "{subj}")'
    return f'(SINCE "{since_str}" ({criteria}))'


def fetch_job_alert_emails(
    host: str,
    port: int,
    user: str,
    password: str,
    folder: str = "INBOX",
    filter_subjects: list[str] = None,
    since_date: str = None,
    max_messages: int = 50,
    timeout: int = 15,
) -> tuple[list[EmailReadResult], list[str]]:
    """
    Fetch job alert emails via IMAP SSL.
    Returns (results, errors).
    Never marks emails as read. Uses BODY.PEEK[].
    """
    results: list[EmailReadResult] = []
    errors: list[str] = []

    logger.info(f"Connecting to IMAP: {host}:{port} user={user}")

    socket.setdefaulttimeout(timeout)
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(host, port)
    except imaplib.IMAP4.error as e:
        errors.append(f"IMAP connection error: {e}")
        return results, errors
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        errors.append(f"Network error connecting to {host}: {e}")
        return results, errors

    try:
        mail.login(user, password)
    except imaplib.IMAP4.error as e:
        errors.append(f"Authentication failed for {user}: {e}")
        try:
            mail.logout()
        except Exception:
            pass
        return results, errors

    try:
        status, _ = mail.select(folder, readonly=True)
        if status != "OK":
            errors.append(f"Could not select folder '{folder}'")
            return results, errors

        since_str = _make_since_string(since_date)
        criteria = _build_search_criteria(filter_subjects or [], since_str)
        logger.debug(f"IMAP search criteria: {criteria}")

        status, data = mail.search(None, criteria)
        if status != "OK":
            errors.append(f"IMAP SEARCH failed: {data}")
            return results, errors

        uid_list = data[0].split() if data[0] else []
        logger.info(f"Found {len(uid_list)} messages matching criteria")

        # Take last max_messages (most recent)
        uid_list = uid_list[-max_messages:]

        for uid_bytes in uid_list:
            uid = uid_bytes.decode("ascii")
            try:
                status, msg_data = mail.fetch(uid_bytes, "(BODY.PEEK[])")
                if status != "OK" or not msg_data:
                    errors.append(f"Failed to fetch UID {uid}")
                    continue

                raw = None
                for part in msg_data:
                    if isinstance(part, tuple):
                        raw = part[1]
                        break
                if raw is None:
                    errors.append(f"Empty response for UID {uid}")
                    continue

                msg = email.message_from_bytes(raw)
                subject = _decode_header_value(msg.get("Subject", ""))
                sender = _decode_header_value(msg.get("From", ""))
                date_str = msg.get("Date", "")
                try:
                    received_at = parsedate_to_datetime(date_str).isoformat()
                except Exception:
                    received_at = datetime.utcnow().isoformat()

                body_text = _extract_body_text(msg)
                body_hash = hashlib.sha256(body_text.encode("utf-8", errors="replace")).hexdigest()

                results.append(EmailReadResult(
                    uid=uid,
                    sender=sender,
                    subject=subject,
                    received_at=received_at,
                    body_text=body_text,
                    body_hash=body_hash,
                    source="imap",
                ))
            except Exception as e:
                errors.append(f"Error processing UID {uid}: {e}")

    except (imaplib.IMAP4.error, socket.timeout) as e:
        errors.append(f"IMAP error during fetch: {e}")
    finally:
        try:
            mail.close()
            mail.logout()
        except Exception:
            pass
        socket.setdefaulttimeout(None)

    logger.info(f"Fetched {len(results)} emails, {len(errors)} errors")
    return results, errors


def fetch_emails_fixture(fixture_path: str) -> tuple[list[EmailReadResult], list[str]]:
    """
    Load emails from a JSON fixture file for testing.
    Fixture format: list of dicts with keys: uid, sender, subject, received_at, body_text, source
    """
    errors: list[str] = []
    results: list[EmailReadResult] = []

    path = Path(fixture_path)
    if not path.exists():
        errors.append(f"Fixture file not found: {fixture_path}")
        return results, errors

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        errors.append(f"Failed to load fixture {fixture_path}: {e}")
        return results, errors

    for item in data:
        try:
            body_text = item.get("body_text", "")
            body_hash = hashlib.sha256(body_text.encode("utf-8", errors="replace")).hexdigest()
            results.append(EmailReadResult(
                uid=item["uid"],
                sender=item.get("sender", ""),
                subject=item.get("subject", ""),
                received_at=item.get("received_at", datetime.utcnow().isoformat()),
                body_text=body_text,
                body_hash=body_hash,
                source=item.get("source", "fixture"),
            ))
        except KeyError as e:
            errors.append(f"Fixture item missing field {e}: {item.get('uid', '?')}")

    logger.info(f"Loaded {len(results)} fixture emails from {fixture_path}")
    return results, errors
