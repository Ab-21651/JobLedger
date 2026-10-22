"""Tool C - Job Description Retriever.

Order of use (enforced by the agent, not here):
  1. usable description inside the alert email   -> kind 'email_inline'
  2. a directly supplied public JD URL           -> ``retrieve_jd``
  3. bounded same-site careers page lookup       -> ``retrieve_via_careers``
  4. otherwise the job is marked NEEDS_JD / NEEDS_USER_INPUT (never a guessed JD).

SSRF defence (all covered by tests):
  * only http/https, no embedded credentials, only ports 80/443;
  * every DNS answer must be globally routable (loopback/private/link-local/CGNAT/
    multicast/reserved/IPv4-mapped-IPv6 are rejected; hostnames that fail to resolve
    are rejected);
  * redirects are followed manually and EVERY hop is re-validated;
  * response size, content type, time and retries are bounded.

KNOWN RESIDUAL RISK (documented, not solved): DNS rebinding between our validation and
the connection ``requests`` makes (TOCTOU). Closing it needs connect-time IP pinning.
LinkedIn is blocked by policy (no logged-in scraping, no automation).
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from jobledger.db.queries import norm_company, norm_text
from jobledger.logging_setup import get_logger
from jobledger.timeutil import now_iso
from jobledger.tools.email_parser import detect_injection, is_injection_line
from jobledger.tools.email_reader import clean_line, html_to_lines

logger = get_logger("jd_retriever")

POLICY_BLOCKED_DOMAINS = ("linkedin.com",)
BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")
ALLOWED_PORTS = (80, 443)
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
EXPIRED_RE = re.compile(r"no longer (accepting applications|available)|position (has been|is) (filled|closed)|"
                        r"(job|posting|vacancy) (has )?(expired|been removed|is closed)|this job is closed", re.I)
RETRYABLE = {"timeout", "network", "rate_limited", "server_error"}


@dataclass
class JDResult:
    status: str                          # ok | failed
    source_url: str
    final_url: Optional[str] = None
    failure_code: Optional[str] = None   # see module docstring / RETRYABLE
    failure_reason: Optional[str] = None
    text: str = ""
    title_found: Optional[str] = None
    company_found: Optional[str] = None
    location_found: Optional[str] = None
    employment_type_found: Optional[str] = None
    salary_raw: Optional[str] = None
    response_bytes: int = 0
    retry_count: int = 0
    injection_flags: list = field(default_factory=list)
    retrieved_at: str = field(default_factory=now_iso)
    extraction: str = ""                 # 'json-ld' | 'html'
    links: list = field(default_factory=list)   # [(anchor_text, href)] for careers-page lookups

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def retryable(self) -> bool:
        return self.failure_code in RETRYABLE


# ---------------------------------------------------------------------------
# URL / network safety
# ---------------------------------------------------------------------------

def _default_resolver(host: str, port: int) -> list:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})


def ip_is_blocked(ip_text: str) -> bool:
    """True unless the address is globally routable (also unwraps v4-mapped / NAT64 forms)."""
    try:
        ip = ipaddress.ip_address(ip_text.split("%")[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip_is_blocked(str(ip.ipv4_mapped))
        if ip.sixtofour is not None:
            return ip_is_blocked(str(ip.sixtofour))
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return ip_is_blocked(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    return (not ip.is_global) or ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local


def validate_url(url: str, resolver: Optional[Callable] = None, allow_private: bool = False) -> tuple[bool, str]:
    """(ok, reason). ``allow_private`` exists ONLY so tests can hit a loopback server."""
    if not url or not isinstance(url, str) or len(url) > 2048:
        return False, "empty or oversized URL"
    if any(c in url for c in "\r\n\t\x00") or url != url.strip():
        return False, "control characters in URL"
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return False, "malformed URL"
    if p.scheme.lower() not in ("http", "https"):
        return False, f"scheme '{p.scheme}' not allowed"
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return False, "missing host"
    if p.username or p.password or "@" in p.netloc:
        return False, "credentials in URL not allowed"
    if any(host == d or host.endswith("." + d) for d in POLICY_BLOCKED_DOMAINS):
        return False, f"{host} is blocked by policy (no logged-in scraping / automation)"
    if allow_private:
        return True, "ok (private allowed for tests)"
    if port is not None and port not in ALLOWED_PORTS:
        return False, f"port {port} not allowed"
    if re.fullmatch(r"[0-9a-fx.]+", host) and not _is_ip(host) and re.search(r"\d", host):
        return False, f"non-standard IP notation '{host}'"
    if host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES) or "." not in host and not _is_ip(host):
        return False, f"host '{host}' is not a public hostname"
    default_port = port or (443 if p.scheme.lower() == "https" else 80)
    try:
        ips = [host] if _is_ip(host) else (resolver or _default_resolver)(host, default_port)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        return False, f"DNS resolution failed ({type(exc).__name__})"
    if not ips:
        return False, "DNS returned no addresses"
    bad = [ip for ip in ips if ip_is_blocked(ip)]
    if bad:
        return False, f"resolves to non-public address {bad[0]}"
    return True, "ok"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_ETYPE_MAP = {"FULL_TIME": "fulltime", "PART_TIME": "parttime", "CONTRACTOR": "contract", "CONTRACT": "contract",
              "TEMPORARY": "temporary", "INTERN": "internship", "FREELANCE": "freelance"}


def _jobposting_nodes(data) -> list:
    out = []
    stack = [data]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            stack.extend(n)
        elif isinstance(n, dict):
            t = n.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                out.append(n)
            stack.extend(v for v in n.values() if isinstance(v, (list, dict)))
    return out


def _fmt_salary(base) -> Optional[str]:
    if not isinstance(base, dict):
        return None
    val = base.get("value") or {}
    if isinstance(val, dict):
        lo, hi, unit = val.get("minValue"), val.get("maxValue"), val.get("unitText", "")
        cur = base.get("currency", "")
        if lo or hi:
            return f"{cur} {lo or ''}-{hi or ''} {unit}".strip()
    return None


def extract_jd(html: str) -> dict:
    """Return dict(text, title, company, location, employment_type, salary_raw, hidden, extraction)."""
    soup = BeautifulSoup(html, "html.parser")
    info = dict(text="", title=None, company=None, location=None, employment_type=None,
                salary_raw=None, hidden="", extraction="html",
                links=[(clean_line(a.get_text(" ")), a["href"]) for a in soup.find_all("a", href=True)][:300])
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            continue
        for jp in _jobposting_nodes(data):
            desc = jp.get("description") or ""
            lines, _ = html_to_lines(desc) if "<" in desc else ([(clean_line(l), []) for l in desc.splitlines()], "")
            text = "\n".join(t for t, _ in lines if t)
            if len(text) < 80:
                continue
            org = jp.get("hiringOrganization")
            loc = jp.get("jobLocation")
            if isinstance(loc, list):
                loc = loc[0] if loc else None
            addr = (loc or {}).get("address") if isinstance(loc, dict) else None
            loc_s = None
            if isinstance(addr, dict):
                loc_s = ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressCountry")) if isinstance(x, str)) or None
            if jp.get("jobLocationType") == "TELECOMMUTE":
                loc_s = ("Remote " + (loc_s or "")).strip()
            et = jp.get("employmentType")
            et = et[0] if isinstance(et, list) and et else et
            info.update(text=text, title=jp.get("title"),
                        company=org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else None),
                        location=loc_s, employment_type=_ETYPE_MAP.get(str(et).upper()) if et else None,
                        salary_raw=_fmt_salary(jp.get("baseSalary")), extraction="json-ld")
            return info

    for t in soup(["script", "style", "nav", "footer", "aside", "form", "noscript", "iframe", "svg", "header"]):
        t.decompose()
    h1 = soup.find("h1")
    og = soup.find("meta", property="og:title")
    title_tag = soup.find("title")
    info["title"] = clean_line(h1.get_text(" ")) if h1 and h1.get_text(strip=True) else \
        (og.get("content") if og and og.get("content") else (clean_line(title_tag.get_text(" ")) if title_tag else None))
    site = soup.find("meta", property="og:site_name")
    if site and site.get("content"):
        info["company"] = site["content"].strip()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    lines, hidden = html_to_lines(str(root))
    info["text"] = "\n".join(t for t, _ in lines if t)
    info["hidden"] = hidden
    return info


# ---------------------------------------------------------------------------
# Verification (is this page really THIS job?)
# ---------------------------------------------------------------------------

_STOP = {"and", "the", "for", "with", "ltd", "inc", "llc", "plc", "job", "jobs", "remote", "hybrid", "new", "senior", "junior"}


def _tokens(s: str) -> set:
    return {t for t in norm_text(s).split() if len(t) >= 2 and t not in _STOP}


def verify_jd_match(job_title: str, job_company: str, res: JDResult) -> tuple[str, str]:
    """('verified'|'partial'|'mismatch'|'unverifiable', evidence). Mismatch is never usable."""
    want = _tokens(job_title)
    head = res.title_found or " ".join((res.text or "")[:300].split())
    got = _tokens(head)
    sim = len(want & got) / len(want) if want else 0.0
    comp = norm_company(job_company)

    def has(hay: str) -> bool:
        return bool(comp) and (comp in hay or comp.replace(" ", "") in hay.replace(" ", ""))

    found = norm_company(res.company_found) if res.company_found else ""
    named_ok = bool(found) and bool(comp) and (comp in found or found in comp)
    body_ok = has(norm_text((res.text or "")[:4000]))
    # The URL came from the alert itself, so it only counts when the page does not name a DIFFERENT company.
    conflict = bool(found) and not named_ok and not body_ok
    company_ok = named_ok or body_ok or (has(norm_text(res.final_url or "")) and not conflict)
    ev = f"title_similarity={sim:.2f} (found '{(res.title_found or head)[:80]}'), company_in_page={company_ok}"
    if conflict:
        return "mismatch", ev + f"; the page names a different company ('{res.company_found}') and never mentions '{job_company}'"
    if sim >= 0.6 and company_ok:
        return "verified", ev
    if sim < 0.34 and not company_ok and (res.title_found or len(got) >= 3):
        return "mismatch", ev
    if sim >= 0.6 or company_ok:
        return "partial", ev
    return "unverifiable", ev


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _read_limited(resp, max_bytes: int) -> tuple[Optional[bytes], bool]:
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=8192):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            return None, True
    return bytes(buf), False


def _decode(raw: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I) or re.search(rb"charset=[\"']?([\w\-]+)", raw[:2048], re.I)
    candidates = []
    if m:
        candidates.append(m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1))
    candidates += ["utf-8", "cp1252"]
    for cs in candidates:
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _fail(url, code, reason, final=None, retries=0, nbytes=0) -> JDResult:
    return JDResult("failed", url, final_url=final, failure_code=code, failure_reason=reason,
                    retry_count=retries, response_bytes=nbytes)


def retrieve_jd(
    url: str, session=None, resolver: Optional[Callable] = None, max_bytes: int = 500_000,
    timeout: int = 15, max_retries: int = 3, max_redirects: int = 5,
    sleep: Callable[[float], None] = time.sleep, allow_private: bool = False,
    min_text_chars: int = 200,
) -> JDResult:
    """Bounded, SSRF-validated fetch + extraction. Never raises for network problems."""
    sess = session or requests.Session()
    ok, why = validate_url(url, resolver, allow_private)
    if not ok:
        logger.warning("JD fetch refused (unsafe url): %s", why)
        return _fail(url, "unsafe_url", why)

    attempt = 0
    cur = url
    hops = 0
    last_fail: Optional[JDResult] = None
    retry_delay: Optional[float] = None
    while True:
        retry_delay = None
        try:
            resp = sess.get(cur, headers={"User-Agent": "JobLedger/1.0 (personal job-tracking; read-only)",
                                          "Accept": "text/html,text/plain;q=0.8"},
                            timeout=timeout, stream=True, allow_redirects=False)
        except requests.Timeout:
            last_fail = _fail(url, "timeout", f"no response within {timeout}s", cur, attempt)
        except requests.RequestException as exc:
            last_fail = _fail(url, "network", f"{type(exc).__name__}", cur, attempt)
        else:
            try:
                code = resp.status_code
                if code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        return _fail(url, "network", f"HTTP {code} without Location", cur, attempt)
                    hops += 1
                    if hops > max_redirects:
                        return _fail(url, "too_many_redirects", f"more than {max_redirects} redirects", cur, attempt)
                    nxt = urljoin(cur, loc)
                    ok, why = validate_url(nxt, resolver, allow_private)
                    if not ok:                              # revalidate EVERY hop
                        logger.warning("redirect blocked: %s", why)
                        return _fail(url, "unsafe_url", f"redirect to unsafe destination: {why}", nxt, attempt)
                    cur = nxt
                    continue
                if code == 429:
                    ra = _retry_after(resp.headers.get("Retry-After"))
                    if ra is not None and ra > 60:
                        return _fail(url, "rate_limited", f"429 with Retry-After {ra:.0f}s (too long; stopped)", cur, attempt)
                    last_fail = _fail(url, "rate_limited", "HTTP 429", cur, attempt)
                    last_fail.failure_reason += f" retry_after={ra}" if ra else ""
                    retry_delay = ra
                elif code >= 500:
                    last_fail = _fail(url, "server_error", f"HTTP {code}", cur, attempt)
                elif code in (404, 410):
                    return _fail(url, "not_found", f"HTTP {code} (page gone; job may be closed)", cur, attempt)
                elif code in (401, 403):
                    return _fail(url, "blocked", f"HTTP {code} (access refused; not bypassed)", cur, attempt)
                elif code != 200:
                    return _fail(url, "network", f"unexpected HTTP {code}", cur, attempt)
                else:
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if ctype not in ALLOWED_CONTENT_TYPES:
                        return _fail(url, "unsupported_content", f"content-type '{ctype or 'unknown'}'", cur, attempt)
                    clen = resp.headers.get("Content-Length")
                    if clen and clen.isdigit() and int(clen) > max_bytes:
                        return _fail(url, "too_large", f"Content-Length {clen} > {max_bytes}", cur, attempt)
                    raw, too_big = _read_limited(resp, max_bytes)
                    if too_big:
                        return _fail(url, "too_large", f"body exceeds {max_bytes} bytes", cur, attempt)
                    return _finish(url, cur, _decode(raw, resp.headers.get("Content-Type", "")),
                                   len(raw), attempt, ctype, min_text_chars)
            finally:
                try:
                    resp.close()
                except (OSError, AttributeError):        # closing is best-effort; never mask the real outcome
                    pass
        # transient failure path
        assert last_fail is not None
        if attempt >= max_retries:
            last_fail.retry_count = attempt
            return last_fail
        delay = retry_delay or 2 ** attempt
        attempt += 1
        logger.info("JD fetch transient failure (%s); retry %d/%d", last_fail.failure_code, attempt, max_retries)
        sleep(min(delay, 30))


def _retry_after(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _finish(url, final, body, nbytes, retries, ctype, min_chars) -> JDResult:
    if ctype == "text/plain":
        info = dict(text=clean_line_block(body), title=None, company=None, location=None,
                    employment_type=None, salary_raw=None, hidden="", extraction="text", links=[])
    else:
        try:
            info = extract_jd(body)
        except (ValueError, TypeError, AttributeError, RecursionError) as exc:   # bs4 on pathological markup
            return _fail(url, "parse_failed", f"HTML parse error: {type(exc).__name__}", final, retries, nbytes)
    # Instruction-like lines are data we refuse to feed to scoring or prompts; flags keep the evidence.
    text = "\n".join(l for l in info["text"].splitlines() if not is_injection_line(l))
    if EXPIRED_RE.search(text[:3000]):
        r = _fail(url, "expired", "page says the posting is closed/expired", final, retries, nbytes)
        r.text = text[:500]
        return r
    path = urlparse(final).path.lower()
    if re.search(r"/(login|signin|sign-in|sso|auth|authwall|checkpoint)\b", path) or \
            (len(text) < 800 and re.search(r"\bpassword\b", text, re.I) and re.search(r"\b(sign in|log in)\b", text, re.I)):
        return _fail(url, "login_required", "page requires login (not bypassed)", final, retries, nbytes)
    if len(text) < min_chars:
        return _fail(url, "empty_content", f"only {len(text)} chars of text (JavaScript-rendered or empty page?)",
                     final, retries, nbytes)
    flags = detect_injection(info["text"] + "\n" + info.get("hidden", ""))
    return JDResult("ok", url, final_url=final, links=info.get("links", []), text=text, title_found=info["title"], company_found=info["company"],
                    location_found=info["location"], employment_type_found=info["employment_type"],
                    salary_raw=info["salary_raw"], response_bytes=nbytes, retry_count=retries,
                    injection_flags=flags, extraction=info["extraction"])


def clean_line_block(text: str) -> str:
    return "\n".join(l for l in (clean_line(x) for x in text.splitlines()) if l)


# ---------------------------------------------------------------------------
# Step 3: bounded same-site careers-page lookup
# ---------------------------------------------------------------------------

def retrieve_via_careers(job_title: str, job_company: str, base_url: str, session=None,
                         resolver: Optional[Callable] = None, **kw) -> JDResult:
    """Look for ``job_title`` on the company's own /careers or /jobs page (max 2 listing
    fetches + 1 job fetch). Returns a failed JDResult(not_found) if the job isn't there -
    it never falls back to some other job's text."""
    p = urlparse(base_url)
    if not p.scheme or not p.netloc:
        return _fail(base_url, "unsafe_url", "no base URL to derive a careers page from")
    root = f"{p.scheme}://{p.netloc}"
    want = _tokens(job_title)
    sess = session or requests.Session()
    for path in ("/careers", "/jobs"):
        listing = retrieve_jd(root + path, session=sess, resolver=resolver, min_text_chars=0, **kw)
        if not listing.ok:
            continue
        best, best_sim = None, 0.0
        for text, href in listing.links:
            sim = len(want & _tokens(text)) / len(want) if want else 0
            if sim > best_sim:
                best, best_sim = href, sim
        if best is not None and best_sim >= 0.8:
            target = urljoin(listing.final_url or root + path, best)
            res = retrieve_jd(target, session=sess, resolver=resolver, **kw)
            res.source_url = base_url
            return res
    return _fail(base_url, "not_found", "job not found on the company's careers pages")


# ---------------------------------------------------------------------------
# Fixture capture (successful live retrievals become reproducible test input)
# ---------------------------------------------------------------------------

def save_fixture(res: JDResult, directory: str, name: str) -> str:
    from pathlib import Path
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80] or "capture"
    path = d / f"{safe}.json"
    path.write_text(json.dumps({
        "source_url": res.source_url, "final_url": res.final_url, "retrieved_at": res.retrieved_at,
        "title_found": res.title_found, "company_found": res.company_found, "text": res.text,
        "extraction": res.extraction}, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
