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
