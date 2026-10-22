"""Test doubles and helpers shared by the pytest suite and the scenario runner (`jobledger.evaluation`).

Everything here is clearly a double: FakeWeb serves saved HTML fixtures, FakeIMAP is an in-memory
mailbox that RECORDS every command so tests can prove the reader is read-only, and ScriptedLLM
(in jobledger.llm) replays canned model answers. Nothing here is imported by the production path.
"""
from __future__ import annotations

import email.message
import imaplib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

from jobledger.agent import JobLedgerAgent, SimulatedCrash
from jobledger.config import Settings
from jobledger.db import queries as q
from jobledger.db.schema import init_db
from jobledger.tools.email_reader import EmailMessage, ReadOutcome, load_email_fixtures

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
PUBLIC_IP = "93.184.216.34"


# ------------------------------------------------------------------ settings / db
def make_settings(workdir: str, **over) -> Settings:
    base = dict(db_path=os.path.join(workdir, "jobledger.db"), export_dir=os.path.join(workdir, "exports"),
                log_dir=os.path.join(workdir, "logs"), llm_provider="none", imap_user="", imap_password="",
                max_retries=3, http_timeout=5)
    base.update(over)
    return Settings(_env_file=None, **base)


def sam_profile(conn, **over) -> int:
    """The labeled-set candidate ('Sam Test'), used by every scenario."""
    cand = json.loads((FIXTURES / "labeled_jobs.json").read_text(encoding="utf-8"))["candidate"]
    cv = cand["cv"]
    args = dict(name=cand["name"], email=cand["email"], phone=None, location=cand["location"],
                work_authorization=cand["work_authorization"], preferred_arrangements=cand["preferred_arrangements"],
                preferred_types=cand["preferred_types"], excluded_companies=cand["excluded_companies"],
                excluded_keywords=cand["excluded_keywords"], min_salary=cand["min_salary"], currency=cand["currency"],
                notes=None)
    args.update(over)
    pid = q.create_profile(conn, **args)
    q.add_cv_version(conn, pid, cv["version_label"], cv["content_text"], cv["skills"], cv["experience_years"], cv["education"])
    return pid


def riley_profile(conn) -> int:
    """A deliberately different second person (Java, Manchester, excludes Acme Cloud)."""
    pid = q.create_profile(conn, "Riley Other", "riley.other@example.com", None, "Manchester", "UK", "onsite,hybrid",
                           "fulltime,contract", "Acme Cloud", "", 50000, "GBP", None)
    q.add_cv_version(conn, pid, "cv-r1", "Java developer, Spring Boot, Kafka, 7 years.", "Java,Spring,Kafka,SQL", 7, "MSc")
    return pid


# ------------------------------------------------------------------ mailbox fixtures
def fixture_outcome(*names: str) -> ReadOutcome:
    out = ReadOutcome()
    for n in names:
        part = load_email_fixtures(str(FIXTURES / "emails" / n))
        out.messages.extend(part.messages)
        out.errors.extend(part.errors)
    return out


def fixture_reader(*names: str) -> Callable[[int], ReadOutcome]:
    def reader(last_uid: int) -> ReadOutcome:
        return fixture_outcome(*names)
    return reader


def imap_reader(fake: "FakeIMAP", subjects=("job alert", "new jobs", "hiring")) -> Callable[[int], ReadOutcome]:
    """Mail reader that runs the REAL read-only IMAP code against an in-memory FakeIMAP."""
    from jobledger.tools.email_reader import fetch_job_alert_emails

    def reader(last_uid: int) -> ReadOutcome:
        return fetch_job_alert_emails("imap.example", 993, "user@example.com", "pw", "INBOX",
                                      filter_subjects=list(subjects), last_uid=last_uid, since_days=3650,
                                      imap_factory=fake.factory)
    return reader


def fixture_imap(*names: str, **kw) -> "FakeIMAP":
    """FakeIMAP holding the fixture emails as raw RFC822 messages (UIDs 1..n)."""
    msgs, n = {}, 0
    for name in names:
        for m in json.loads((FIXTURES / "emails" / name).read_text(encoding="utf-8")):
            n += 1
            msgs[n] = make_raw(m["subject"], m["sender"], m["body_text"], date="Thu, 01 Oct 2026 09:00:00 +0000")
    return FakeIMAP(msgs, **kw)


# ------------------------------------------------------------------ fake web
@dataclass
class Page:
    status: int = 200
    body: str = ""
    content_type: str = "text/html; charset=utf-8"
    headers: dict = field(default_factory=dict)
    exc: Optional[BaseException] = None
    body_bytes: Optional[bytes] = None


def jd_page(name: str) -> Page:
    return Page(200, (FIXTURES / "jd" / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, page: Page, url: str):
        self.status_code = page.status
        self.url = url
        self.headers = {"Content-Type": page.content_type, **page.headers}
        self._body = page.body_bytes if page.body_bytes is not None else page.body.encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeWeb:
    """Stand-in for requests.Session. ``pages`` maps URL -> Page or list of Pages (consumed in order,
    the last one repeats). Query strings are ignored when matching."""

    def __init__(self, pages: Optional[dict] = None):
        self.pages: dict = dict(standard_pages() if pages is None else pages)
        self.calls: list = []

    def set(self, url: str, page) -> None:
        self.pages[url] = page

    def count(self, url_part: str) -> int:
        return sum(1 for c in self.calls if url_part in c)

    def get(self, url, headers=None, timeout=None, stream=False, allow_redirects=True):
        self.calls.append(url)
        key = url if url in self.pages else url.split("?")[0]
        entry = self.pages.get(key)
        if entry is None:
            page = Page(404, "not found")
        elif isinstance(entry, list):
            page = entry[0] if len(entry) == 1 else entry.pop(0)
        else:
            page = entry
        if page.exc is not None:
            raise page.exc
        return FakeResponse(page, url)


def standard_pages() -> dict:
    return {
        "https://careers.acme.example/jobs/101": jd_page("acme_backend.html"),
        "https://jobs.dataworks.example/py-senior": jd_page("dataworks_senior.html"),
        "https://shady.example/jobs/9": jd_page("shady_python.html"),
        "https://cloudnine.example/careers/devops": jd_page("cloudnine_devops.html"),
        "https://brightside.example/jobs/data-engineer": jd_page("vertex_other_job.html"),
        "https://orbit.example/jobs/platform": jd_page("orbit_platform.html"),
        "https://evil.example/jobs/1": jd_page("evilcorp_python.html"),
        "https://hiddenco.example/jobs/sre": jd_page("hiddenco_sre.html"),
        # https://pixelforge.example/jobs/react and every careers page deliberately 404
    }


MAPPED_HOSTS = {"rebind.example": ["10.0.0.5"], "meta.example": ["169.254.169.254"], "localhost": ["127.0.0.1"]}


def public_resolver(host: str, port: int) -> list:
    """Offline DNS: every hostname is public unless listed in MAPPED_HOSTS."""
    return list(MAPPED_HOSTS.get(host, [PUBLIC_IP]))


class Sleeper:
    """Records back-off delays instead of sleeping."""

    def __init__(self):
        self.delays: list = []

    def __call__(self, secs: float) -> None:
        self.delays.append(secs)


def make_agent(conn, pid, settings: Settings, web: Optional[FakeWeb] = None, llm=None, mail=None, **kw) -> JobLedgerAgent:
    web = web if web is not None else FakeWeb()
    kw.setdefault("sleep", Sleeper())
    return JobLedgerAgent(conn, pid, settings, llm=llm, mail_reader=mail, session=web, resolver=public_resolver, **kw)


def crash_at(point: str, nth: int = 1, hard: bool = False) -> Callable:
    """Fault hook: die the nth time ``point`` is reached. hard=True kills the whole process (os._exit)."""
    seen = {"n": 0}

    def hook(p, ctx):
        if p != point:
            return
        seen["n"] += 1
        if seen["n"] == nth:
            if hard:
                os._exit(137)
            raise SimulatedCrash(point)
    return hook


# ------------------------------------------------------------------ fake IMAP
def make_raw(subject: str, sender: str, body: str, date: str = "Mon, 05 Oct 2026 09:00:00 +0000") -> bytes:
    m = email.message.EmailMessage()
    m["Subject"], m["From"], m["Date"] = subject, sender, date
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    """In-memory IMAP server that records every command. Write-capable commands raise AssertionError."""

    def __init__(self, messages: dict, uidvalidity: int = 7, auth_ok: bool = True, fail_uids: tuple = ()):
        self.messages = dict(messages)
        self.uidvalidity = uidvalidity
        self.auth_ok = auth_ok
        self.fail_uids = set(fail_uids)
        self.commands: list = []
        self.selected_readonly: Optional[bool] = None
        self.logins = 0

    def factory(self, host, port, timeout):
        return self

    def login(self, user, password):
        self.logins += 1
        self.commands.append(("LOGIN", user))
        if not self.auth_ok:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"logged in"]

    def select(self, folder="INBOX", readonly=False):
        self.commands.append(("SELECT", folder, readonly))
        self.selected_readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        self.commands.append(("UID", command) + tuple(str(a) for a in args))
        if command.upper() == "SEARCH":
            criteria = [str(a) for a in args[1:]]
            uids = sorted(self.messages)
            if criteria and criteria[0] == "UID":
                low = int(criteria[1].split(":")[0])
                hit = [u for u in uids if u >= low]
                if not hit and uids:
                    hit = [uids[-1]]            # RFC 3501: "n:*" always yields the highest message
                uids = hit
            return "OK", [" ".join(str(u) for u in uids).encode()]
        if command.upper() == "FETCH":
            uid, spec = int(args[0]), str(args[1])
            if "BODY.PEEK" not in spec:
                raise AssertionError(f"non-PEEK fetch would mark the message read: {spec}")
            if uid in self.fail_uids:
                raise imaplib.IMAP4.error("simulated FETCH failure")
            raw = self.messages[uid]
            if "HEADER" in spec:
                head = raw.split(b"\n\n", 1)[0] + b"\n\n"
                return "OK", [(b"1 (UID %d BODY[HEADER] {n}" % uid, head), b")"]
            return "OK", [(b"1 (UID %d BODY[] {n}" % uid, raw), b")"]
        raise AssertionError(f"unexpected UID command {command}")

    def logout(self):
        self.commands.append(("LOGOUT",))
        return "BYE", []

    def _forbidden(self, *a, **k):
        raise AssertionError("write-capable IMAP command used")
    store = expunge = copy = append = delete = create = rename = _forbidden


# ------------------------------------------------------------------ crash worker (real process death)
def _crash_worker(db_path: str, profile_id: int, fixture: str, point: str) -> None:
    """Child process for the 'full shutdown' scenario: runs the agent and kills itself at ``point``."""
    work = str(Path(db_path).parent)
    settings = make_settings(work, db_path=db_path)
    conn = init_db(db_path)
    agent = make_agent(conn, profile_id, settings, mail=fixture_reader(fixture), fault=crash_at(point, hard=True))
    agent.run()                       # never returns when the fault fires: the process is gone


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "crash-worker":
        _crash_worker(sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5])
    else:
        print("usage: python -m jobledger.testing crash-worker DB PROFILE_ID FIXTURE POINT")
