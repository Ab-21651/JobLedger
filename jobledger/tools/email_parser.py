"""
Tool B: Email Parser
Extract structured job information from email body text.
Handles plain text, HTML, multiple jobs per email, forwarded alerts.
Missing fields remain None - never fabricated.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Optional

from bs4 import BeautifulSoup

from jobledger.logging_setup import get_logger

logger = get_logger("email_parser")

# ── Regex patterns ────────────────────────────────────────────────────────────

# URLs (http/https only)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
HREF_RE = re.compile(r'href=["\']?(https?://[^\s"\'<>]+)["\']?', re.IGNORECASE)

# Salary patterns
SALARY_RE = re.compile(
    r"((?:£|\$|€|USD|GBP|EUR)\s?[\d,]+(?:\.\d+)?(?:k|K)?)"
    r"\s*(?:-|–|to)\s*"
    r"((?:£|\$|€|USD|GBP|EUR)?\s?[\d,]+(?:\.\d+)?(?:k|K)?)"
    r"(?:\s*(?:per\s+(?:year|annum|month)|p\.?a\.?|/yr|/year))?",
    re.IGNORECASE,
)
SALARY_SINGLE_RE = re.compile(
    r"(?:salary|pay|compensation|remuneration)[\s:]+?"
    r"((?:£|\$|€|USD|GBP|EUR)\s?[\d,]+(?:\.\d+)?(?:k|K)?(?:\s*(?:-|–|to)\s*(?:£|\$|€|USD|GBP|EUR)?\s?[\d,]+(?:\.\d+)?(?:k|K)?)?)",
    re.IGNORECASE,
)

# Work arrangement keywords
REMOTE_RE = re.compile(
    r"\b(fully\s+remote|100%\s*remote|remote\s+first|remote[-\s]only|work\s+from\s+home|wfh)\b",
    re.IGNORECASE,
)
HYBRID_RE = re.compile(
    r"\b(hybrid|partially\s+remote|\d+\s+days?\s+(in\s+)?office|\d+\s+days?\s+wfh)\b",
    re.IGNORECASE,
)
ONSITE_RE = re.compile(
    r"\b(on[-\s]?site|in[-\s]?office|office[-\s]?based|in\s+person|no\s+remote)\b",
    re.IGNORECASE,
)

# Employment type keywords
FULLTIME_RE = re.compile(
    r"\b(full[-\s]?time|permanent|perm\b|full\s+time)\b", re.IGNORECASE
)
PARTTIME_RE = re.compile(r"\b(part[-\s]?time|part\s+time)\b", re.IGNORECASE)
CONTRACT_RE = re.compile(
    r"\b(contract|freelance|temporary|temp\b|fixed[-\s]?term|\d+[-\s]month\s+contract)\b",
    re.IGNORECASE,
)

# Job block separators (common in multi-job alerts)
BLOCK_SEPARATORS = re.compile(
    r"(?:\n\s*[-─═*]{3,}\s*\n|\n\s*\n\s*\n|(?=\n\d+\.\s+[A-Z]))",
    re.MULTILINE,
)

# Title/company patterns
TITLE_AT_COMPANY_RE = re.compile(
    r"^([A-Z][A-Za-z\s/&\-,\.]+?)\s+(?:at|@|with|for)\s+([A-Z][A-Za-z0-9\s&\.\-,']+?)(?:\s*[|\n]|$)",
    re.MULTILINE,
)
TITLE_PIPE_COMPANY_RE = re.compile(
    r"^([A-Z][A-Za-z\s/&\-,\.]{4,40})\s*\|\s*([A-Z][A-Za-z0-9\s&\.\-,']{2,40})(?:\s*\||\n|$)",
    re.MULTILINE,
)
LOCATION_RE = re.compile(
    r"(?:Location|Based in|Office)[:\s]+([A-Z][A-Za-z\s,\-]+?)(?:\s*[|\n(]|$)",
    re.IGNORECASE | re.MULTILINE,
)
LOCATION_CITY_RE = re.compile(
    r"\b(London|Manchester|Birmingham|Leeds|Edinburgh|Glasgow|Bristol|Liverpool|"
    r"New York|San Francisco|Chicago|Boston|Austin|Seattle|Toronto|Dublin|"
    r"Berlin|Amsterdam|Paris|Sydney|Melbourne|Singapore|Remote)\b",
    re.IGNORECASE,
)


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class ParsedJob:
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    arrangement: Optional[str] = None   # remote/hybrid/onsite/unknown
    employment_type: Optional[str] = None  # fulltime/parttime/contract/unknown
    apply_url: Optional[str] = None
    salary_raw: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    description_snippet: Optional[str] = None
    confidence: float = 0.0
    evidence: dict = dataclasses.field(default_factory=dict)


# ── Helpers ───────────────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    """Convert HTML to plain text using BeautifulSoup."""
    try:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "head", "meta", "noscript"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def extract_urls_from_html(html: str) -> list[str]:
    """Extract all href URLs from HTML content."""
    return [m.group(1) for m in HREF_RE.finditer(html)]


def normalize_arrangement(text: str) -> str:
    """Map text to one of: remote, hybrid, onsite, unknown."""
    if REMOTE_RE.search(text):
        return "remote"
    if HYBRID_RE.search(text):
        return "hybrid"
    if ONSITE_RE.search(text):
        return "onsite"
    return "unknown"


def normalize_employment_type(text: str) -> str:
    """Map text to one of: fulltime, parttime, contract, unknown."""
    if CONTRACT_RE.search(text):
        return "contract"
    if PARTTIME_RE.search(text):
        return "parttime"
    if FULLTIME_RE.search(text):
        return "fulltime"
    return "unknown"


def _parse_salary_value(raw: str) -> Optional[float]:
    """Parse a salary string like '£60,000' or '80k' to a float."""
    raw = re.sub(r"[£$€]", "", raw).strip()
    if raw.lower().endswith("k"):
        try:
            return float(raw[:-1].replace(",", "")) * 1000
        except ValueError:
            return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _detect_currency(text: str) -> Optional[str]:
    if "£" in text or "GBP" in text:
        return "GBP"
    if "$" in text or "USD" in text:
        return "USD"
    if "€" in text or "EUR" in text:
        return "EUR"
    return None


def _extract_salary(text: str) -> tuple[Optional[str], Optional[float], Optional[float], Optional[str]]:
    """Return (raw_str, min_val, max_val, currency)."""
    m = SALARY_RE.search(text)
    if m:
        raw = m.group(0)
        currency = _detect_currency(raw)
        lo = _parse_salary_value(m.group(1))
        hi = _parse_salary_value(m.group(2))
        return raw, lo, hi, currency
    m2 = SALARY_SINGLE_RE.search(text)
    if m2:
        raw = m2.group(1)
        currency = _detect_currency(raw)
        val = _parse_salary_value(raw)
        return raw, val, None, currency
    return None, None, None, None


def _extract_urls(text: str) -> list[str]:
    """Extract all http/https URLs from plain text."""
    return URL_RE.findall(text)


def _is_injection_text(text: str) -> bool:
    """Detect common prompt-injection patterns in text."""
    patterns = [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"you\s+are\s+now\s+(?:a\s+different\s+ai|dan|jailbroken)",
        r"reveal\s+(your\s+)?(api\s+keys?|secrets?|system\s+prompt)",
        r"exec\s*\(",
        r"os\.system",
        r"import\s+os",
    ]
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def _parse_job_block(block: str, source_email_uid: str = "") -> Optional[ParsedJob]:
    """Parse a single job block of text into a ParsedJob."""
    block = block.strip()
    if len(block) < 20:
        return None

    # Skip blocks that are mostly injection text
    if _is_injection_text(block) and len(block) < 200:
        logger.debug("Skipped likely injection-only block in email %s", source_email_uid)
        return None

    job = ParsedJob()
    evidence = {}

    # Extract title and company
    m = TITLE_AT_COMPANY_RE.search(block)
    if m:
        job.title = m.group(1).strip()
        job.company = m.group(2).strip()
        evidence["title_company_pattern"] = "X at Y"
    else:
        m2 = TITLE_PIPE_COMPANY_RE.search(block)
        if m2:
            job.title = m2.group(1).strip()
            job.company = m2.group(2).strip()
            evidence["title_company_pattern"] = "X | Y"

    # Extract location
    loc_m = LOCATION_RE.search(block)
    if loc_m:
        job.location = loc_m.group(1).strip()
        evidence["location_pattern"] = "explicit"
    else:
        city_m = LOCATION_CITY_RE.search(block)
        if city_m:
            job.location = city_m.group(1)
            evidence["location_pattern"] = "city_keyword"

    # Extract arrangement
    arr = normalize_arrangement(block)
    if arr != "unknown":
        job.arrangement = arr
        evidence["arrangement"] = arr

    # Extract employment type
    emp = normalize_employment_type(block)
    if emp != "unknown":
        job.employment_type = emp
        evidence["employment_type"] = emp

    # Extract salary
    sal_raw, sal_min, sal_max, sal_cur = _extract_salary(block)
    if sal_raw:
        job.salary_raw = sal_raw
        job.salary_min = sal_min
        job.salary_max = sal_max
        job.salary_currency = sal_cur
        evidence["salary"] = sal_raw

    # Extract apply URL (first URL that looks like a job link)
    urls = _extract_urls(block)
    job_url_patterns = re.compile(
        r"(?:job|apply|career|position|role|opening|vacancy|post)", re.IGNORECASE
    )
    for url in urls:
        if job_url_patterns.search(url):
            job.apply_url = url
            evidence["url_pattern"] = "job_url"
            break
    if not job.apply_url and urls:
        job.apply_url = urls[0]
        evidence["url_pattern"] = "first_url"

    # Description snippet (first 300 chars of block, no injection text)
    snippet = re.sub(r"\s+", " ", block[:300]).strip()
    if not _is_injection_text(snippet):
        job.description_snippet = snippet

    # Calculate confidence
    fields_found = sum(
        1 for v in [job.title, job.company, job.location, job.apply_url] if v
    )
    job.confidence = fields_found / 4.0
    job.evidence = evidence

    # Only return if we found at least a URL or both title+company
    if job.apply_url or (job.title and job.company):
        return job
    return None


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_email_body(
    body_text: str,
    source_email_uid: str = "",
    is_html: bool = False,
) -> list[ParsedJob]:
    """
    Parse email body and return list of ParsedJob.
    Treats all email content as DATA, not instructions.
    """
    if not body_text:
        return []

    if is_html:
        body_text = html_to_text(body_text)

    # Try splitting into job blocks
    blocks = BLOCK_SEPARATORS.split(body_text)
    if len(blocks) <= 1:
        # Try paragraph splitting
        blocks = re.split(r"\n\s*\n", body_text)

    jobs = []
    seen_urls = set()
    for block in blocks:
        parsed = _parse_job_block(block, source_email_uid)
        if parsed:
            # Dedup by URL within email
            url_key = parsed.apply_url or ""
            title_key = f"{parsed.title}|{parsed.company}"
            key = url_key or title_key
            if key and key not in seen_urls:
                seen_urls.add(key)
                jobs.append(parsed)

    # If no jobs found via blocks, try whole email as one block
    if not jobs:
        parsed = _parse_job_block(body_text, source_email_uid)
        if parsed:
            jobs.append(parsed)

    logger.debug(
        f"Parsed {len(jobs)} jobs from email {source_email_uid or '?'}"
    )
    return jobs
