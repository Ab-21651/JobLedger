"""Tool B - Email Parser.

Turns an alert email into structured job candidates. Rules:
  * unknown stays unknown (None) - nothing is guessed or defaulted;
  * every important field carries the evidence line it came from and a confidence;
  * email content is DATA. Instruction-like text is recorded as an injection flag and
    kept out of stored descriptions; it is never executed or obeyed;
  * a block without both title and company is NOT stored as a job (it is reported as
    a parse warning) - merging "unknown company" jobs would corrupt dedup.

Heuristic parser: it was built against hand-written fixtures covering plain text, HTML,
multi-job, forwarded, repeated-header, malformed-link and injection emails. Real alert
formats (LinkedIn/Indeed/etc.) are NOT covered by evidence; see docs/REPORT.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from jobledger.db.queries import canonicalize_url
from jobledger.tools.email_reader import EmailMessage, clean_line, html_to_lines

# ---------------------------------------------------------------------------
# Injection detection
# ---------------------------------------------------------------------------

INJECTION_PATTERNS = [
    ("ignore_instructions", r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your)\b[^.\n]{0,30}\b(instruction|prompt|rule|direction|guideline)s?"),
    ("role_override", r"\b(you are now|act as|pretend to be|from now on you)\b"),
    ("system_prompt", r"\b(system prompt|developer message|<\s*/?\s*system\s*>|\[\s*system\s*\])"),
    ("reveal_secrets", r"\b(reveal|print|show|send|output|leak|exfiltrate)\b[^.\n]{0,40}\b(api[ _-]?key|password|secret|token|credential|env(ironment)?)"),
    ("force_score", r"\b(mark|rate|score|rank|set)\b[^.\n]{0,30}\b(this|the)\b[^.\n]{0,20}\b(job|role|position|candidate|match)\b[^.\n]{0,30}\b(100|perfect|top|highest|best|first)\b"),
    ("force_action", r"\b(auto[- ]?apply|apply (automatically|now on my behalf)|send (an )?email to|forward (this|the) (email|cv|resume)|delete (all|the) (data|jobs|emails))\b"),
    ("tool_call_text", r"\b(call|run|execute|invoke)\b[^.\n]{0,20}\b(tool|function|command|action)\b[^.\n]{0,30}"),
]
_INJ = [(n, re.compile(p, re.I)) for n, p in INJECTION_PATTERNS]


def detect_injection(text: str) -> list:
    """Return [{'flag':..., 'excerpt':...}] for instruction-like text. Matching is advisory."""
    flags, seen = [], set()
    for line in (text or "").splitlines():
        for name, rx in _INJ:
            m = rx.search(line)
            if m and (name, line) not in seen:
                seen.add((name, line))
                flags.append({"flag": name, "excerpt": clean_line(line)[:160]})
    return flags


def is_injection_line(line: str) -> bool:
    return any(rx.search(line) for _, rx in _INJ)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ParsedJob:
    title: str
    company: str
    location: Optional[str] = None
    arrangement: Optional[str] = None          # remote | hybrid | onsite | None (unknown)
    employment_type: Optional[str] = None      # fulltime | parttime | contract | internship | temporary | freelance
    apply_url: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_raw: Optional[str] = None
    description: Optional[str] = None
    evidence: dict = field(default_factory=dict)      # field -> source line
    confidence: dict = field(default_factory=dict)    # field -> 0..1 extraction confidence
    warnings: list = field(default_factory=list)


@dataclass
class ParseResult:
    jobs: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    injection_flags: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"""(?:https?://|www\.)[^\s<>"'\)\]]+""", re.I)
_LABELS = {
    "title": re.compile(r"^(job\s*title|title|position|role|vacancy)\s*[:\-]\s*(.+)$", re.I),
    "company": re.compile(r"^(company|employer|organi[sz]ation|hiring company)\s*[:\-]\s*(.+)$", re.I),
    "location": re.compile(r"^(location|where|based in|city)\s*[:\-]\s*(.+)$", re.I),
    "salary": re.compile(r"^(salary|pay|compensation|rate|package)\s*[:\-]\s*(.+)$", re.I),
    "type": re.compile(r"^(employment\s*type|job\s*type|contract\s*type|type)\s*[:\-]\s*(.+)$", re.I),
    "url": re.compile(r"^(apply(\s*(here|now|link|url))?|link|url|job\s*link|view\s*job)\s*[:\-]\s*(.*)$", re.I),
}
_ARRANGEMENT_RE = re.compile(r"\b(remote|hybrid|on[- ]?site|in[- ]office|work from home)\b", re.I)
_TYPE_RE = [
    ("fulltime", re.compile(r"\bfull[- ]?time\b", re.I)),
    ("parttime", re.compile(r"\bpart[- ]?time\b", re.I)),
    ("contract", re.compile(r"\b(contract(or)?|fixed[- ]term)\b", re.I)),
    ("internship", re.compile(r"\bintern(ship)?\b", re.I)),
    ("temporary", re.compile(r"\btemp(orary)?\b", re.I)),
    ("freelance", re.compile(r"\bfreelance\b", re.I)),
]
_CUR = {"£": "GBP", "$": "USD", "€": "EUR", "gbp": "GBP", "usd": "USD", "eur": "EUR", "cad": "CAD", "aud": "AUD"}
_SALARY_RE = re.compile(
    r"(?P<cur1>£|\$|€|GBP|USD|EUR|CAD|AUD)\s?(?P<a>\d[\d,]*(?:\.\d+)?)\s?(?P<ak>[kK])?"
    r"(?:\s?(?:-|–|—|to)\s?(?P<cur2>£|\$|€|GBP|USD|EUR|CAD|AUD)?\s?(?P<b>\d[\d,]*(?:\.\d+)?)\s?(?P<bk>[kK])?)?"
    r"(?P<unit>\s?(?:/|per\s|a\s|p\.?)\s?(?:year|yr|annum|a\b|day|hour|hr|month|week|d\b|h\b)\w*)?", re.I)
_UNSUB_RE = re.compile(r"unsubscribe|opt[- ]?out|manage (your )?(alerts?|preferences|subscription)|privacy|"
                       r"terms|view in browser|mailto:|tel:|click here to stop|notification settings", re.I)
_DIVIDER_RE = re.compile(r"^[\-=_*~•·\s]{3,}$")
_FWD_RE = re.compile(r"^-{2,}\s*(forwarded message|original message)\s*-{2,}$|^begin forwarded message:?$", re.I)
_HDR_RE = re.compile(r"^(from|to|cc|bcc|date|sent|subject|reply-to)\s*:", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[\-\*•·▪►>]+|\d{1,2}[.)])\s+")
_ARR_MAP = {"remote": "remote", "work from home": "remote", "hybrid": "hybrid",
            "on-site": "onsite", "onsite": "onsite", "on site": "onsite", "in-office": "onsite", "in office": "onsite"}
_LOC_RE = re.compile(r"^[A-Z][\w.'’\- ]+(?:,\s*[A-Z][\w.'’\- ]+){0,2}(?:\s*\((?:remote|hybrid|on-?site)[^)]*\))?$")


def normalize_url(raw: str) -> tuple[Optional[str], Optional[str]]:
    """(clean_url, warning). Fixes bare www., rejects malformed/unsafe schemes."""
    u = raw.strip().strip("<>()[]\"'.,;")
    if u.lower().startswith("www."):
        u = "https://" + u
    if re.match(r"^https?:/(?!/)", u, re.I) or not re.match(r"^https?://", u, re.I):
        return None, f"malformed_url:{raw[:60]}"
    canon = canonicalize_url(u)
    if not canon:
        return None, f"malformed_url:{raw[:60]}"
    return u, None


def parse_salary(text: str) -> Optional[dict]:
    """Annual salary only; hourly/daily/monthly figures stay in ``raw`` with min/max unknown."""
    m = _SALARY_RE.search(text or "")
    if not m or not m.group("a"):
        return None
    a = float(m.group("a").replace(",", ""))
    b = float(m.group("b").replace(",", "")) if m.group("b") else None
    if m.group("ak"):
        a *= 1000
    if b is not None and (m.group("bk") or (m.group("ak") and b < 1000)):
        b *= 1000
    unit = (m.group("unit") or "").lower()
    cur = _CUR.get(m.group("cur1").lower(), m.group("cur1").upper())
    non_annual = bool(re.search(r"day|hour|hr|month|week|\bd\b|\bh\b", unit))
    if not non_annual and a < 1000 and (b is None or b < 1000) and not m.group("ak"):
        non_annual = True          # e.g. "$40" with no unit: cannot tell, so do not compare
    raw = clean_line(m.group(0))
    if non_annual:
        return {"min": None, "max": None, "currency": cur, "raw": raw}
    return {"min": min(a, b) if b else a, "max": max(a, b) if b else a, "currency": cur, "raw": raw}


def _arrangement(text: str) -> Optional[str]:
    m = _ARRANGEMENT_RE.search(text or "")
    return _ARR_MAP.get(re.sub(r"\s+", " ", m.group(1).lower())) if m else None


def _etype(text: str) -> Optional[str]:
    for name, rx in _TYPE_RE:
        if rx.search(text or ""):
            return name
    return None


# ---------------------------------------------------------------------------
# Line model + block splitting
# ---------------------------------------------------------------------------

@dataclass
class _Line:
    text: str
    links: list        # [(anchor_text, href)]


def _text_to_lines(text: str) -> list:
    lines = []
    for raw in text.splitlines():
        s = clean_line(re.sub(r"^(\s*>)+", "", raw))
        links = []
        for m in _URL_RE.finditer(s):
            links.append(("", m.group(0)))
        s2 = clean_line(_URL_RE.sub(" ", s)).strip("<>[]() ") if links else s
        lines.append(_Line(s2 if links else s, links))
    return lines


def _strip_forward_headers(lines: list) -> list:
    out, skipping = [], False
    for ln in lines:
        if _FWD_RE.match(ln.text):
            skipping = True
            continue
        if skipping:
            if not ln.text or _HDR_RE.match(ln.text):
                if not ln.text:
                    skipping = False
                continue
            skipping = False
        out.append(ln)
    return out


def _split_blocks(lines: list) -> list:
    blocks, cur = [], []

    def end():
        nonlocal cur
        if any(l.text or l.links for l in cur):
            blocks.append(cur)
        cur = []

    for ln in lines:
        if not ln.text and not ln.links:
            end()
        elif _DIVIDER_RE.match(ln.text) and not ln.links:
            end()
        elif _BULLET_RE.match(ln.text) and re.match(r"^\s*\d{1,2}[.)]\s", ln.text) and cur:
            end()
            cur.append(_Line(_BULLET_RE.sub("", ln.text), ln.links))
        else:
            cur.append(ln)
    end()
    return blocks


# ---------------------------------------------------------------------------
# Block -> job
# ---------------------------------------------------------------------------

def _split_company_location(line: str) -> tuple[str, Optional[str]]:
    for sep in (" · ", " • ", " | ", " – ", " — ", " - "):
        if sep in line:
            left, right = [p.strip() for p in line.split(sep, 1)]
            if right and (_ARRANGEMENT_RE.search(right) or _LOC_RE.match(right) or "," in right):
                return left, right
    return line.strip(), None


def _looks_like_location(s: str) -> bool:
    return bool(s) and len(s) < 60 and (bool(_ARRANGEMENT_RE.search(s)) or bool(_LOC_RE.match(s)))


def _parse_block(block: list, email_warnings: list) -> Optional[ParsedJob]:
    fields: dict = {}
    evidence: dict = {}
    conf: dict = {}
    free: list = []
    links: list = []
    warns: list = []

    for ln in block:
        text = _BULLET_RE.sub("", ln.text).strip()
        links.extend(ln.links)
        if not text:
            continue
        if is_injection_line(text):
            continue                                       # never part of stored fields/description
        if _UNSUB_RE.search(text):
            continue
        matched = False
        for key, rx in _LABELS.items():
            m = rx.match(text)
            if m:
                val = m.group(m.lastindex).strip()
                if key == "url":
                    if val:
                        links.append(("", val))
                elif val:
                    fields.setdefault(key, val)
                    evidence.setdefault(key, text)
                    conf.setdefault(key, 0.95)
                matched = True
                break
        if not matched:
            free.append(text)

    title = fields.get("title")
    company = fields.get("company")
    location = fields.get("location")

    # Positional heuristics only for what labels did not give us.
    rest = list(free)
    if not title and rest:
        first = rest.pop(0)
        m = re.match(r"^(?P<t>.+?)\s+(?:at|@)\s+(?P<c>[^|·•]+?)(?:\s+(?:in|[-–|·•])\s+(?P<l>.+))?$", first, re.I)
        if m and not company:
            title, company = m.group("t").strip(), m.group("c").strip()
            location = location or (m.group("l") or "").strip() or None
            evidence["title"] = evidence["company"] = first
            conf["title"] = conf["company"] = 0.75
        else:
            title = first
            evidence["title"] = first
            conf["title"] = 0.7
    if not company and rest:
        cand = rest.pop(0)
        comp, loc = _split_company_location(cand)
        company = comp
        evidence["company"] = cand
        conf["company"] = 0.6
        if loc and not location:
            location, evidence["location"], conf["location"] = loc, cand, 0.6
    if not location:
        for i, r in enumerate(rest):
            if _looks_like_location(r) and len(r.split()) <= 6:
                location, evidence["location"], conf["location"] = r, r, 0.55
                rest.pop(i)
                break

    if not title or not company:
        return None
    if len(title) > 140 or len(company) > 120:
        return None

    if location:
        segs = [x for x in re.split(r"\s+[-–—·•|]\s+", location) if x.strip()]
        kept = [x for x in segs if not (_etype(x) and len(x.split()) <= 3)]
        location = " - ".join(kept) if kept else None
    comp_line = evidence.get("company")
    meta = [l for l in ([location or "", fields.get("type") or "", title] + free) if l and l != comp_line]
    arrangement = _arrangement(" ".join(meta))
    if arrangement:
        evidence["arrangement"] = next((t for t in meta if _arrangement(t)), "")
        conf["arrangement"] = 0.8
    etype = _etype(" ".join(meta))
    if etype:
        evidence["employment_type"] = next((t for t in meta if _etype(t)), "")
        conf["employment_type"] = 0.85 if fields.get("type") else 0.65
    sal = parse_salary(" ".join(filter(None, [fields.get("salary"), *free])))
    if sal:
        evidence["salary"] = sal["raw"]
        conf["salary"] = 0.8

    apply_url = None
    seen = set()
    for anchor_text, href in links:
        if href in seen or _UNSUB_RE.search(href) or _UNSUB_RE.search(anchor_text or ""):
            continue
        seen.add(href)
        u, w = normalize_url(href)
        if w:
            warns.append(w)
        elif apply_url is None:
            apply_url = u
            evidence["apply_url"] = href[:200]
            conf["apply_url"] = 0.9

    if not (apply_url or sal or arrangement or etype or fields) or title.rstrip().endswith((",", ":")):
        return None                       # prose / sign-offs have no job signal at all: not a job block

    desc_lines = [r for r in rest if not (sal and sal["raw"] in r) and not _looks_like_location(r) and len(r) > 25]
    description = clean_line(" ".join(desc_lines))[:2000] or None

    return ParsedJob(
        title=title.strip(" -|·•:"), company=company.strip(" -|·•:"), location=location,
        arrangement=arrangement, employment_type=etype, apply_url=apply_url,
        salary_min=sal["min"] if sal else None, salary_max=sal["max"] if sal else None,
        salary_currency=sal["currency"] if sal else None, salary_raw=sal["raw"] if sal else None,
        description=description, evidence=evidence, confidence=conf, warnings=warns)


def parse_email(msg: EmailMessage) -> ParseResult:
    """Parse one email. Never raises on odd content; problems become warnings."""
    res = ParseResult()
    flags = detect_injection("\n".join([msg.subject or "", msg.body_text or "", msg.hidden_text or ""]))
    h_lines, h_hidden = ([], "")
    if msg.body_html:
        h_lines, h_hidden = html_to_lines(msg.body_html)
    hidden = " | ".join(dict.fromkeys(x for x in (msg.hidden_text, h_hidden) if x))
    if hidden:
        flags.extend(detect_injection(hidden))
        flags.append({"flag": "hidden_html_text", "excerpt": clean_line(hidden)[:160]})
    res.injection_flags = flags

    if msg.body_html:
        lines = [_Line(t, l) for t, l in h_lines]
        if not any(l.text for l in lines):
            lines = _text_to_lines(msg.body_text)
    else:
        lines = _text_to_lines(msg.body_text)
    lines = _strip_forward_headers(lines)

    if not any(l.text or l.links for l in lines):
        res.warnings.append("empty_body")
        return res

    seen = set()
    for block in _split_blocks(lines):
        job = _parse_block(block, res.warnings)
        if job is None:
            continue
        key = (job.title.lower(), job.company.lower(), (job.location or "").lower())
        if key in seen:                       # repeated block inside the same email
            continue
        seen.add(key)
        res.jobs.append(job)
    if not res.jobs:
        res.warnings.append("no_jobs_found")
    for j in res.jobs:
        res.warnings.extend(f"{j.title}: {w}" for w in j.warnings)
    return res
