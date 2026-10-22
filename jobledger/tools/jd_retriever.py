"""
Tool C: Job Description Retriever
Retrieves and verifies job descriptions from public URLs.
Security: validates URLs, rejects private/loopback IPs, re-validates after redirects.
Never substitutes a different job's description on failure.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from jobledger.logging_setup import get_logger

logger = get_logger("jd_retriever")

# ── Security ──────────────────────────────────────────────────────────────────

ALLOWED_SCHEMES = {"http", "https"}

PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # Shared address space
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def is_private_ip(ip_str: str) -> bool:
    """Return True if IP is in a private/loopback/link-local range."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in PRIVATE_NETWORKS)
    except ValueError:
        return False


def validate_url(url: str) -> tuple[bool, str]:
    """
    Validate URL scheme and check for SSRF risks.
    Returns (is_valid, reason).
    """
    if not url or not url.strip():
        return False, "Empty URL"

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL parse error: {e}"

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False, f"Disallowed scheme: {parsed.scheme}"

    hostname = parsed.hostname
    if not hostname:
        return False, "No hostname in URL"

    # Check for localhost variants
    if hostname.lower() in ("localhost", "localhost.", "0.0.0.0"):
        return False, f"Loopback hostname blocked: {hostname}"

    # Resolve hostname to IP
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        # Cannot resolve - allow (don't block on DNS failure for public URLs)
        return True, f"DNS unresolvable (allowed): {e}"

    for info in infos:
        ip_str = info[4][0]
        if is_private_ip(ip_str):
            return False, f"Resolved to private IP {ip_str} - SSRF blocked"

    return True, "ok"


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class JDResult:
    source_url: str
    final_url: str
    status: str  # ok, failed, blocked, rate_limited, timeout, js_required, access_denied, ssrf_blocked
    failure_reason: Optional[str]
    content_text: Optional[str]
    content_hash: Optional[str]
    title_found: Optional[str]
    company_found: Optional[str]
    verification_status: Optional[str]  # verified, partial, mismatch, unverified
    verification_evidence: Optional[str]
    response_bytes: Optional[int]
    retry_count: int = 0


# ── Text extraction ───────────────────────────────────────────────────────────

def extract_text_from_html(html: str, max_chars: int = 5000) -> str:
    """Extract meaningful text from HTML, removing scripts/styles."""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "head", "meta", "noscript", "nav", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # Collapse whitespace
        import re
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:max_chars]
    except Exception:
        import re
        return re.sub(r"<[^>]+>", " ", html)[:max_chars]


def extract_title_company_from_text(
    text: str,
) -> tuple[Optional[str], Optional[str]]:
    """Try to extract job title and company from page text."""
    import re

    title = None
    company = None

    # Pattern: "Job Title: X" or "Position: X"
    title_patterns = [
        r"(?:job\s+title|position|role)[:\s]+([A-Z][A-Za-z\s/&\-,\.]{4,60})(?:\n|$|at\s)",
        r"<title[^>]*>([^<]{5,80})</title>",
    ]
    for pat in title_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            break

    # Pattern: "Company: X" or "About X" at start
    company_patterns = [
        r"(?:company|employer|organization|about\s+us)[:\s]+([A-Z][A-Za-z0-9\s&\.\-,']{2,50})(?:\n|$)",
        r"(?:at|with|for)\s+([A-Z][A-Za-z0-9\s&\.\-,']{2,50})(?:\n|\s*[\|\-]|$)",
    ]
    for pat in company_patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            company = m.group(1).strip()
            break

    return title, company


def verify_jd_match(
    retrieved_title: Optional[str],
    retrieved_company: Optional[str],
    expected_title: str,
    expected_company: str,
) -> tuple[str, str]:
    """
    Compare retrieved title/company with expected.
    Returns (verification_status, evidence).
    Status: verified, partial, mismatch, unverified
    """
    if not retrieved_title and not retrieved_company:
        return "unverified", "No title or company extracted from page"

    def _norm(s: str) -> str:
        return s.lower().strip() if s else ""

    t_exp = _norm(expected_title)
    c_exp = _norm(expected_company)
    t_ret = _norm(retrieved_title)
    c_ret = _norm(retrieved_company)

    title_match = bool(
        t_ret and t_exp and (t_ret in t_exp or t_exp in t_ret)
    )
    company_match = bool(
        c_ret and c_exp and (c_ret in c_exp or c_exp in c_ret)
    )

    evidence_parts = []
    if retrieved_title:
        evidence_parts.append(f"page_title='{retrieved_title}'")
    if retrieved_company:
        evidence_parts.append(f"page_company='{retrieved_company}'")
    evidence_parts.append(f"expected_title='{expected_title}'")
    evidence_parts.append(f"expected_company='{expected_company}'")
    evidence = "; ".join(evidence_parts)

    if title_match and company_match:
        return "verified", evidence
    if title_match or company_match:
        return "partial", evidence
    return "mismatch", evidence


# ── Main retrieval ────────────────────────────────────────────────────────────

USER_AGENT = "Mozilla/5.0 (compatible; JobLedger/0.1; research-bot)"


def retrieve_jd(
    url: str,
    expected_title: str,
    expected_company: str,
    timeout: int = 15,
    max_bytes: int = 500_000,
    max_retries: int = 3,
    user_agent: str = USER_AGENT,
) -> JDResult:
    """
    Retrieve a job description from a public URL.
    Returns JDResult - never raises exceptions.
    """
    _failed = lambda reason, status="failed": JDResult(
        source_url=url,
        final_url=url,
        status=status,
        failure_reason=reason,
        content_text=None,
        content_hash=None,
        title_found=None,
        company_found=None,
        verification_status=None,
        verification_evidence=None,
        response_bytes=None,
    )

    # 1. Validate URL
    valid, reason = validate_url(url)
    if not valid:
        logger.warning(f"URL blocked (SSRF): {url} | reason={reason}")
        return JDResult(
            source_url=url, final_url=url, status="ssrf_blocked",
            failure_reason=reason, content_text=None, content_hash=None,
            title_found=None, company_found=None, verification_status=None,
            verification_evidence=None, response_bytes=None,
        )

    headers = {"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"}
    retry_count = 0
    last_error = ""

    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait = 2 ** attempt
            logger.info(f"Retry {attempt}/{max_retries} for {url} (wait {wait}s)")
            time.sleep(wait)

        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
                stream=True,
            )
            retry_count = attempt

            # Re-validate final URL after redirects
            final_url = resp.url
            if final_url != url:
                valid2, reason2 = validate_url(final_url)
                if not valid2:
                    return JDResult(
                        source_url=url, final_url=final_url, status="ssrf_blocked",
                        failure_reason=f"Redirect to blocked URL: {reason2}",
                        content_text=None, content_hash=None, title_found=None,
                        company_found=None, verification_status=None,
                        verification_evidence=None, response_bytes=None,
                        retry_count=retry_count,
                    )

            # Handle HTTP error codes
            if resp.status_code == 429:
                last_error = "Rate limited (HTTP 429)"
                if attempt < max_retries:
                    continue
                return JDResult(
                    source_url=url, final_url=final_url, status="rate_limited",
                    failure_reason=last_error, content_text=None, content_hash=None,
                    title_found=None, company_found=None, verification_status=None,
                    verification_evidence=None, response_bytes=None,
                    retry_count=retry_count,
                )

            if resp.status_code in (401, 403):
                return JDResult(
                    source_url=url, final_url=final_url, status="access_denied",
                    failure_reason=f"HTTP {resp.status_code} Access Denied",
                    content_text=None, content_hash=None, title_found=None,
                    company_found=None, verification_status=None,
                    verification_evidence=None, response_bytes=None,
                    retry_count=retry_count,
                )

            if resp.status_code == 404:
                return JDResult(
                    source_url=url, final_url=final_url, status="failed",
                    failure_reason="HTTP 404 Not Found (job may have been removed)",
                    content_text=None, content_hash=None, title_found=None,
                    company_found=None, verification_status=None,
                    verification_evidence=None, response_bytes=None,
                    retry_count=retry_count,
                )

            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code} Server Error"
                if attempt < max_retries:
                    continue
                return JDResult(
                    source_url=url, final_url=final_url, status="failed",
                    failure_reason=last_error, content_text=None, content_hash=None,
                    title_found=None, company_found=None, verification_status=None,
                    verification_evidence=None, response_bytes=None,
                    retry_count=retry_count,
                )

            if resp.status_code != 200:
                return _failed(f"HTTP {resp.status_code}")

            # Read body with size limit
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=8192):
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    logger.debug(f"Response size limit reached ({max_bytes} bytes) for {url}")
                    break
            raw = b"".join(chunks)
            response_bytes = len(raw)

            # Detect content type
            content_type = resp.headers.get("Content-Type", "").lower()
            if "javascript" in content_type or "json" not in content_type and "<html" not in raw[:200].decode("utf-8", errors="ignore").lower():
                if response_bytes < 500:
                    # Likely a redirect to JS-rendered page
                    return JDResult(
                        source_url=url, final_url=final_url, status="js_required",
                        failure_reason="Page appears to require JavaScript rendering",
                        content_text=None, content_hash=None, title_found=None,
                        company_found=None, verification_status=None,
                        verification_evidence=None, response_bytes=response_bytes,
                        retry_count=retry_count,
                    )

            # Decode HTML
            try:
                encoding = resp.encoding or "utf-8"
                html = raw.decode(encoding, errors="replace")
            except Exception:
                html = raw.decode("utf-8", errors="replace")

            content_text = extract_text_from_html(html, max_chars=5000)
            content_hash = hashlib.sha256(content_text.encode("utf-8", errors="replace")).hexdigest()

            # Extract title/company for verification
            title_found, company_found = extract_title_company_from_text(content_text)

            # Verify match
            ver_status, ver_evidence = verify_jd_match(
                title_found, company_found, expected_title, expected_company
            )

            logger.info(
                f"JD retrieved: {url} | {response_bytes}B | "
                f"verification={ver_status} | title='{title_found}' company='{company_found}'"
            )

            return JDResult(
                source_url=url,
                final_url=final_url,
                status="ok",
                failure_reason=None,
                content_text=content_text,
                content_hash=content_hash,
                title_found=title_found,
                company_found=company_found,
                verification_status=ver_status,
                verification_evidence=ver_evidence,
                response_bytes=response_bytes,
                retry_count=retry_count,
            )

        except requests.exceptions.Timeout:
            last_error = f"Request timed out after {timeout}s"
            if attempt < max_retries:
                continue
            return JDResult(
                source_url=url, final_url=url, status="timeout",
                failure_reason=last_error, content_text=None, content_hash=None,
                title_found=None, company_found=None, verification_status=None,
                verification_evidence=None, response_bytes=None,
                retry_count=retry_count,
            )

        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error: {e}"
            if attempt < max_retries:
                continue
            return _failed(last_error)

        except Exception as e:
            last_error = f"Unexpected error: {type(e).__name__}: {e}"
            logger.exception(f"Unexpected error retrieving {url}")
            return _failed(last_error)

    return _failed(f"All {max_retries + 1} attempts failed: {last_error}")
