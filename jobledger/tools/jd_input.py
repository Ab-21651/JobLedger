"""Job descriptions from the clipboard: reading the OS clipboard and matching copied text to a job.

Only read_clipboard() touches the outside world, and only through a local clipboard tool
(PowerShell on Windows, pbpaste on macOS, wl-paste / xclip / xsel on Linux). Nothing is fetched
from the network here, and matching is plain text work against jobs already in the database.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys

from jobledger.db import queries as q

MIN_CLIP_CHARS = 200            # a copied job posting is much longer; shorter clipboard text is never stored
TITLE_WINDOW = 2500             # copied pages put the real job at the top and list other jobs lower down
CLIP_TIMEOUT_S = 5
CLOSED_WORKFLOW = ("closed", "expired")


class ClipboardError(RuntimeError):
    """The clipboard could not be read right now (empty, not text, tool error, timeout)."""


class ClipboardUnavailable(ClipboardError):
    """No clipboard tool is installed, so reading can never work on this machine."""


def normalize(text: str) -> str:
    """Lowercase and collapse whitespace. Used for every title, company and location comparison."""
    return " ".join((text or "").lower().split())


def find_term(hay: str, term: str) -> int:
    """Index of the first whole-word occurrence of ``term`` in ``hay`` (both normalized), or -1."""
    term = normalize(term)
    if not term:
        return -1
    m = re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", normalize(hay))
    return m.start() if m else -1


_PARENTHESISED = re.compile(r"\s*\([^)]*\)")


def name_variants(value: str) -> list:
    """Normalized forms a job's title or company may take on a page: the full string and, when it differs
    and keeps at least 3 characters, the same string without parenthesised parts
    ('Apertera (formerly Alexa Translations)' -> 'apertera'). A name that is only '(Remote)' has no second form."""
    full = normalize(value)
    if not full:
        return []
    bare = normalize(_PARENTHESISED.sub(" ", full)).strip(" -|,:")
    return [full] + ([bare] if len(bare) >= 3 and bare != full else [])


def first_mention(hay: str, value: str) -> int:
    """Earliest whole-word position in ``hay`` of any name variant of ``value``, or -1."""
    hits = [at for at in (find_term(hay, v) for v in name_variants(value)) if at >= 0]
    return min(hits) if hits else -1


def mentions(hay: str, value: str) -> bool:
    return first_mention(hay, value) >= 0


def _run(cmd: list, name: str) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=CLIP_TIMEOUT_S, check=False)
    except FileNotFoundError:
        raise ClipboardUnavailable(f"{name} is not installed")
    except subprocess.TimeoutExpired:
        raise ClipboardError(f"{name} timed out")
    except OSError as exc:
        raise ClipboardError(f"{name} failed: {exc}")
    if p.returncode != 0:
        detail = p.stderr.decode("utf-8", errors="replace").strip()[:120]
        raise ClipboardError(f"{name} exited with {p.returncode}" + (f": {detail}" if detail else ""))
    return p.stdout.decode("utf-8", errors="replace")


def read_clipboard() -> str:
    """The clipboard's text ('' when it holds no text). Raises ClipboardError or ClipboardUnavailable."""
    if sys.platform == "win32":
        return _run(["powershell", "-NoProfile", "-Command",
                     "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"], "PowerShell")
    if sys.platform == "darwin":
        return _run(["pbpaste"], "pbpaste")
    tools = [(["wl-paste", "--no-newline"], "wl-paste"),
             (["xclip", "-selection", "clipboard", "-o"], "xclip"),
             (["xsel", "-ob"], "xsel")]
    installed = [(cmd, name) for cmd, name in tools if shutil.which(cmd[0])]
    if not installed:
        raise ClipboardUnavailable("no clipboard tool found: install wl-clipboard (Wayland) or xclip / xsel (X11)")
    problems = []
    for cmd, name in installed:
        try:
            return _run(cmd, name)
        except ClipboardError as exc:
            problems.append(str(exc))
    raise ClipboardError("could not read the clipboard (" + "; ".join(problems)
                         + "); it may be empty or hold non-text data")


def match_job_by_text(conn, profile_id: int, text: str) -> tuple:
    """Which open job of this profile does copied text describe?

    Returns ("match", job row) | ("ambiguous", [job rows]) | ("none", None).
    A job is a candidate when its title AND company both occur in the text and the title's first
    occurrence starts within the first TITLE_WINDOW characters. Candidates are ranked by
    (title position, company position); the lowest key wins. If several share the lowest key, the
    one whose location occurs in the text wins; otherwise the result is ambiguous.
    Closed and expired jobs are never candidates.
    """
    if len(text.strip()) < MIN_CLIP_CHARS:
        return ("none", None)
    hay = normalize(text)
    candidates = []
    for job in q.list_jobs(conn, profile_id, limit=1_000_000):
        if (job["workflow_status"] or "") in CLOSED_WORKFLOW:
            continue
        title_at = first_mention(hay, job["title"])
        company_at = first_mention(hay, job["company"])
        if not (0 <= title_at < TITLE_WINDOW) or company_at < 0:
            continue
        candidates.append(((title_at, company_at), job))
    if not candidates:
        return ("none", None)
    # The real job's company sits right under its title; sidebar companies sit far down and lose.
    best = min(key for key, _ in candidates)
    top = [job for key, job in candidates if key == best]
    if len(top) == 1:
        return ("match", top[0])
    # Several jobs tie on title and company position (same role in two cities, or a page that names only
    # 'Acme' for 'Acme (UK)' and 'Acme (US)'): the location phrase is what can tell them apart.
    located = [j for j in top if j["location"] and find_term(hay, j["location"]) >= 0]
    if len(located) == 1:
        return ("match", located[0])
    return ("ambiguous", top)


def _no_match_problem(title: str, company: str, title_at: int, company_at: int) -> str:
    if title_at >= 0 and company_at >= 0:
        if title_at >= TITLE_WINDOW:
            return f"title found at character {title_at} but only the first {TITLE_WINDOW} characters are searched"
        return "matched title and company but could not be ranked"
    if title_at >= 0:
        return f'title found but company "{company}" was not found in the text'
    return f'company found but the title "{title}" was not found'


def explain_no_match(conn, profile_id: int, text: str, limit: int = 3) -> list:
    """Why copied text matched no job: the open jobs whose title or company appears in the text, closest first.

    Each item is {"job_id", "title", "company", "title_at", "company_at", "problem"}; a position of -1 means
    that name was not found. Ranking: title and company found, then title only, then company only; ties go to
    the smaller title position (the company position for company-only jobs). At most ``limit`` items.
    """
    hay = normalize(text)
    scored = []
    for job in q.list_jobs(conn, profile_id, limit=1_000_000):
        if (job["workflow_status"] or "") in CLOSED_WORKFLOW:
            continue
        title_at = first_mention(hay, job["title"])
        company_at = first_mention(hay, job["company"])
        if title_at < 0 and company_at < 0:
            continue
        group = 0 if title_at >= 0 and company_at >= 0 else (1 if title_at >= 0 else 2)
        tie = title_at if title_at >= 0 else company_at
        scored.append(((group, tie), job, title_at, company_at))
    scored.sort(key=lambda s: s[0])
    return [{"job_id": job["id"], "title": job["title"], "company": job["company"],
             "title_at": title_at, "company_at": company_at,
             "problem": _no_match_problem(job["title"], job["company"], title_at, company_at)}
            for _, job, title_at, company_at in scored[:limit]]


DESCRIPTION_MIN_PERCENT = 67    # share of a title's words that must appear in copied text for a weaker match
_TITLE_STOPWORDS = frozenset("m w d f x all genders gender remote hybrid onsite and the of for with at in to a an".split())
_WORD = re.compile(r"[^\W_]+")


def _company_rank(hay: str, company: str) -> int:
    """0 when the company's full name is in the text, 1 when only its first word is (a multi-word name whose
    first word has at least 4 characters: 'Dolby Laboratories' -> 'dolby'), -1 when neither is."""
    if any(find_term(hay, v) >= 0 for v in name_variants(company)):
        return 0
    words = normalize(_PARENTHESISED.sub(" ", normalize(company))).split()
    if len(words) >= 2 and len(words[0]) >= 4 and find_term(hay, words[0]) >= 0:
        return 1
    return -1


def _title_words(title: str) -> list:
    """The words of a job title that identify it: parenthesised parts such as '(m/w/d)' and stopwords dropped."""
    bare = normalize(_PARENTHESISED.sub(" ", normalize(title)))
    return [w for w in _WORD.findall(bare) if len(w) >= 2 and w not in _TITLE_STOPWORDS]


def match_job_by_description(conn, profile_id: int, text: str) -> tuple:
    """Weaker match for copied text without a title and company line (e.g. only LinkedIn's 'About the job' box).

    Returns ("weak", job row, detail) | ("ambiguous", [job rows], None) | ("none", None, None).
    A job is a candidate when its company occurs anywhere in the text (see _company_rank) and at least
    DESCRIPTION_MIN_PERCENT of its title words occur as whole words. Candidates are ranked by
    (share of title words found, count of title words found, full company name before first word).
    One top candidate wins; several tied at the top are ambiguous. Closed and expired jobs are never candidates.
    """
    hay = normalize(text)
    scored = []
    for job in q.list_jobs(conn, profile_id, limit=1_000_000):
        if (job["workflow_status"] or "") in CLOSED_WORKFLOW:
            continue
        company_rank = _company_rank(hay, job["company"])
        words = _title_words(job["title"])
        if company_rank < 0 or not words:
            continue
        found = sum(1 for w in words if find_term(hay, w) >= 0)
        if found * 100 < DESCRIPTION_MIN_PERCENT * len(words):
            continue
        scored.append(((-found / len(words), -found, company_rank), job, found, len(words)))
    if not scored:
        return ("none", None, None)
    best = min(s[0] for s in scored)
    top = [(job, found, total) for key, job, found, total in scored if key == best]
    if len(top) > 1:
        return ("ambiguous", [job for job, _, _ in top], None)
    job, found, total = top[0]
    return ("weak", job, f"company {job['company']}; {found} of {total} title words")
