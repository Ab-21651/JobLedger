"""JobLedger command line.  Run with:  py -m jobledger <command> ...   (Windows)   or   python -m jobledger ...

Exit codes: 0 success, 1 the operation failed or was refused, 2 bad usage.
A success message is only printed after the work it describes has been committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

from jobledger.config import get_settings, reset_settings
from jobledger.db import queries as q
from jobledger.db.schema import init_db
from jobledger.llm import LLMError, build_llm
from jobledger.tools import approval_manager as am
from jobledger.tools import jd_input
from jobledger.tools import jd_retriever as jdr
from jobledger.tools import stage_manager as sm
from jobledger.tools.profile_reader import read_full_profile, read_preferences_history


class CLIError(Exception):
    """A user-facing failure (printed without a traceback)."""


def out(text: str = "") -> None:
    try:
        sys.stdout.write(str(text) + "\n")
    except UnicodeEncodeError:                       # old Windows consoles
        sys.stdout.write(str(text).encode("ascii", "replace").decode("ascii") + "\n")


def _connect():
    s = get_settings()
    return init_db(s.db_abs_path())


def _pick_profile(conn, wanted: Optional[int]) -> int:
    if wanted is not None:
        if q.get_profile(conn, wanted) is None:
            raise CLIError(f"profile {wanted} does not exist (see: profile list)")
        return wanted
    profs = q.list_profiles(conn)
    if len(profs) == 1:
        return profs[0]["id"]
    if not profs:
        raise CLIError("no profile yet: run `profile create` first")
    raise CLIError("several profiles exist; pass --profile N (see: profile list)")


def _fmt_score(ev) -> str:
    if ev is None:
        return "not evaluated"
    s = ev["status"].upper()
    return f"{s} {ev['total_score']:.1f}" if ev["total_score"] is not None else s


# ------------------------------------------------------------------ commands
def cmd_init(a):
    conn = _connect()
    out(f"database ready at {get_settings().db_abs_path()} (schema v{conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]})")


def cmd_doctor(a):
    s = get_settings()
    info = s.redacted_summary()
    info["mailbox_configured"] = s.has_email()
    out(json.dumps(info, indent=2))
    out("")
    out("LLM: configured (reachability is NOT checked here)" if s.has_llm() else
        "LLM: not configured - runs are fully deterministic (this is a supported mode, and every log says so)")
    out("Mailbox: credentials present (login is only attempted by `run`)" if s.has_email() else
        "Mailbox: no IMAP credentials - use `run --fixtures PATH` to process saved emails")


def cmd_profile(a):
    conn = _connect()
    if a.sub == "create":
        if q.get_profile_by_email(conn, a.email):
            raise CLIError(f"a profile with email {a.email} already exists")
        pid = q.create_profile(conn, a.name, a.email, None, a.location, a.work_auth, a.arrangements, a.types,
                               a.exclude_companies, a.exclude_keywords, a.min_salary, a.currency, a.notes)
        out(f"created profile {pid} ({a.name})")
    elif a.sub == "list":
        for p in q.list_profiles(conn):
            out(f"[{p['id']}] {p['name']} <{p['email']}>  location={p['location']}")
    elif a.sub == "show":
        pid = _pick_profile(conn, a.profile)
        p = read_full_profile(conn, pid)
        out(f"profile {p.id}: {p.name} <{p.email}>  prefs_version={p.prefs_version}")
        for k, v in p.preference_snapshot().items():
            out(f"  {k}: {v}")
        out(f"  cv: {p.cv.version_label if p.cv else 'none'} skills={p.cv.skills if p.cv else []} years={p.cv.experience_years if p.cv else None}")


def cmd_cv(a):
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    text = Path(a.file).read_text(encoding="utf-8") if a.file else (a.text or "")
    if not a.skills:
        raise CLIError("--skills is required (comma separated)")
    cv_id = q.add_cv_version(conn, pid, a.label, text, a.skills, a.years, a.education)
    out(f"added CV version {cv_id} ({a.label}); earlier versions are kept")


def cmd_prefs(a):
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    if a.sub == "history":
        for h in read_preferences_history(conn, pid):
            out(f"{h['changed_at']}  {h['field_name']}: {h['old_value']!r} -> {h['new_value']!r}  ({h['reason'] or 'no reason'})")
        return
    changes = {}
    for pair in a.set or []:
        if "=" not in pair:
            raise CLIError(f"expected FIELD=VALUE, got '{pair}'")
        k, v = pair.split("=", 1)
        changes[k.strip()] = (float(v) if k.strip() == "min_salary" and v else (v if v != "" else None))
    if not changes:
        raise CLIError("nothing to change: use --set FIELD=VALUE")
    applied = q.update_profile_fields(conn, pid, changes, a.reason or "")
    for c in applied:
        out(f"{c['field']}: {c['old']!r} -> {c['new']!r}")
    out(f"{len(applied)} change(s) saved; history kept. Re-run `run` to re-evaluate jobs under the new preferences.")


def cmd_run(a):
    from jobledger.agent import JobLedgerAgent
    from jobledger.tools.email_reader import load_email_fixtures
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    s = get_settings()
    llm = None if a.no_llm else build_llm(s)
    reader = None
    if a.fixtures:
        outcome = load_email_fixtures(a.fixtures)
        reader = lambda last_uid: outcome
    out("LLM: " + (f"{llm.provider}/{llm.model}" if llm else "none (deterministic mode)"))
    agent = JobLedgerAgent(conn, pid, s, llm=llm, mail_reader=reader, retry=a.retry)
    res = agent.run(read_mail=not a.no_mail)
    out(f"run {res.run_id}: {res.status} in {res.steps} step(s); mailbox={res.mail_status}")
    out("  " + ", ".join(f"{k}={v}" for k, v in res.counters.items()))
    out("  decisions: " + ", ".join(f"{k}={v}" for k, v in res.decisions.items()))
    if res.recovered_runs:
        out(f"  recovered interrupted run(s): {', '.join(res.recovered_runs)}")
    if res.export:
        out(f"  register: {res.export['path']} ({res.export['rows']} rows)" + (" [original was locked: wrote a new file]" if res.export["locked_fallback"] else ""))
    for e in res.errors:
        out(f"  ! {e}")
    if res.status in ("failed", "stopped_max_steps"):
        raise CLIError(f"run did not finish: {res.stopped_reason}")


def cmd_jobs(a):
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    if a.sub == "list":
        rows = q.list_jobs(conn, pid)
        for j in rows:
            ev = q.get_latest_evaluation(conn, j["id"], pid)
            app = q.get_application_by_job(conn, j["id"], pid)
            out(f"[{j['id']}] {j['title']} @ {j['company']} ({j['location'] or '?'})  {_fmt_score(ev)}  stage={app['stage'] if app else '-'}  jd={j['jd_status']}")
        if not rows:
            out("no jobs yet")
        return
    job = q.get_job(conn, a.job, pid)
    if job is None:
        raise CLIError(f"job {a.job} not found for profile {pid}")
    out(f"[{job['id']}] {job['title']} @ {job['company']}  location={job['location']} arrangement={job['arrangement']} type={job['employment_type']}")
    out(f"  salary={job['salary_raw']}  apply_url={job['apply_url']}  jd_status={job['jd_status']}  workflow={job['workflow_status']}")
    out("  sources:")
    for s_ in q.list_job_sources(conn, job["id"]):
        out(f"    - {s_['source_label']}  {s_['title_seen']} | {s_['company_seen']} | {s_['location_seen']} | {s_['arrangement_seen']}")
    for c in q.list_job_conflicts(conn, job["id"], only_open=False):
        out(f"  CONFLICT {c['field_name']}: '{c['value_a']}' ({c['source_a']}) vs '{c['value_b']}' ({c['source_b']}) [{c['status']}]")
    for ev in q.list_evaluations(conn, job["id"], pid):
        out(f"  eval#{ev['id']} {ev['status']} score={ev['total_score']} mode={ev['scoring_mode']} prefs_v={ev['prefs_version']} at {ev['evaluated_at']}")
        out(f"      {ev['match_rationale']}")


def _store_user_jd(conn, job_id: int, text: str, evidence: str) -> None:
    from jobledger.tools.email_parser import detect_injection
    q.save_jd_snapshot(conn, job_id, "user-provided", None, "ok", content_text=text, verification_status="verified",
                       verification_evidence=evidence, source_kind="user_provided",
                       injection_flags=detect_injection(text) or None)
    q.update_job_jd_status(conn, job_id, "RETRIEVED")


def _cmd_jd_clip(conn, job, force: bool) -> None:
    try:
        text = jd_input.read_clipboard()
    except jd_input.ClipboardError as exc:
        raise CLIError(str(exc))
    if len(text.strip()) < jd_input.MIN_CLIP_CHARS:
        raise CLIError(f"the clipboard holds {len(text.strip())} characters; a job description needs at least "
                       f"{jd_input.MIN_CLIP_CHARS}. Copy the whole posting and try again.")
    hay = jd_input.normalize(text)
    evidence = "copied from clipboard"
    if not (jd_input.mentions(hay, job["title"]) and jd_input.mentions(hay, job["company"])):
        if not force:
            raise CLIError(f"the clipboard text does not contain both '{job['title']}' and '{job['company']}' "
                           f"(job {job['id']}), so nothing was stored. Copy the right posting, or pass --force "
                           f"to store it anyway.")
        evidence += "; stored with --force although the title and company were not found in the text"
    _store_user_jd(conn, job["id"], text, evidence)
    out(f"stored the description for job {job['id']} (copied from clipboard, {len(text)} chars); "
        f"run `run --no-mail` to re-evaluate")


def _watch_event(conn, pid: int, text: str, counts: dict) -> None:
    if len(text.strip()) < jd_input.MIN_CLIP_CHARS:
        return                                   # not a job posting: say nothing
    chars = len(text)
    kind, found = jd_input.match_job_by_text(conn, pid, text)
    if kind == "none":
        counts["ignored"] += 1
        out(f"ignored: copied text ({chars} chars) matches no open job of this profile")
        preview = " ".join(text.split())[:80]
        out(f'  text starts: "{preview}"')
        closest = jd_input.explain_no_match(conn, pid, text)
        if not closest:
            out("  no job title or company from this profile appears in the text "
                "(is this a job from a different email batch?)")
        for item in closest:
            out(f'  closest: job {item["job_id"]} "{item["title"]}" @ {item["company"]} - {item["problem"]}')
    elif kind == "ambiguous":
        counts["ambiguous"] += 1
        ids = ", ".join(str(j["id"]) for j in sorted(found, key=lambda j: j["id"]))
        out(f"ambiguous: copied text matches jobs {ids}; choose one with `jd N --clip`")
        for j in sorted(found, key=lambda j: j["id"]):
            loc = f" ({j['location']})" if j["location"] else ""
            out(f'  job {j["id"]} "{j["title"]}" @ {j["company"]}{loc}')
    else:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if any(s["content_hash"] == digest for s in q.list_jd_snapshots(conn, found["id"])):
            counts["already stored"] += 1
            out(f"already stored: job {found['id']} '{found['title']}' @ {found['company']} (same text)")
            return
        _store_user_jd(conn, found["id"], text, "copied from clipboard; matched on title+company")
        counts["stored"] += 1
        out(f"stored: job {found['id']} '{found['title']}' @ {found['company']} ({chars} chars)")


def _cmd_jd_watch(a, conn, pid: int) -> None:
    interval = 1.0 if a.interval is None else a.interval
    if interval < 0:
        raise CLIError("--interval must be 0 or more seconds")
    counts = {"stored": 0, "ignored": 0, "ambiguous": 0, "already stored": 0}
    out(f"watching the clipboard every {interval:g}s: copy a whole job posting (Ctrl+C to stop)")
    last_digest = None
    last_problem = None
    started = time.monotonic()
    try:
        while True:
            text = None
            try:
                text = jd_input.read_clipboard()
                last_problem = None
            except jd_input.ClipboardUnavailable as exc:
                raise CLIError(str(exc))
            except jd_input.ClipboardError as exc:
                if str(exc) != last_problem:
                    out(f"  clipboard not readable right now: {exc} (still watching)")
                    last_problem = str(exc)
            if text is not None:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if digest != last_digest:            # a repeat of the text just seen is ignored
                    last_digest = digest
                    _watch_event(conn, pid, text, counts)
            if a.max_seconds is not None and time.monotonic() - started >= a.max_seconds:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        out("")
    out("summary: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    out("run `py -m jobledger run --no-mail` to score")


def _jd_fetch_deps() -> dict:
    """Session, resolver and sleep used by `jd N --url`. Tests replace this with the offline fakes."""
    return {"session": None, "resolver": None, "sleep": time.sleep}


def _cmd_jd_url(conn, job, url: str) -> None:
    s = get_settings()
    deps = _jd_fetch_deps()
    res = jdr.retrieve_jd(url, session=deps["session"], resolver=deps["resolver"], max_bytes=s.jd_max_bytes,
                          timeout=s.http_timeout, max_retries=s.max_retries, sleep=deps["sleep"], allow_private=False)
    if res.failure_code == "unsafe_url":
        raise CLIError(f"refused: {res.failure_reason}. Nothing was stored.")
    verification, evidence = (None, None)
    if res.ok:
        verification, evidence = jdr.verify_jd_match(job["title"], job["company"], res)
    reason = None if res.ok else f"{res.failure_code}: {res.failure_reason}"
    with q.tx(conn):
        q.save_jd_snapshot(
            conn, job["id"], res.source_url, res.final_url, "ok" if res.ok else "failed", res.retrieved_at, reason,
            res.text if res.ok else None, res.title_found, res.company_found, verification, evidence,
            res.response_bytes, res.retry_count, "user_url", res.injection_flags or None)
        if res.ok:
            status = "RETRIEVED" if verification in ("verified", "partial") else "UNVERIFIED"
        elif res.failure_code == "expired":
            status = "EXPIRED"
            q.set_job_workflow(conn, job["id"], "expired", "job page says the posting is closed")
        else:
            status = "FAILED"
        q.update_job_jd_status(conn, job["id"], status)
    if not res.ok:
        raise CLIError(f"could not fetch {url}: {res.failure_reason} (job {job['id']} is now {status})")
    if verification in ("verified", "partial"):
        out(f"stored the page for job {job['id']} (verification: {verification}); "
            f"run `run --no-mail` to re-evaluate")
    else:
        out(f"! stored the page for job {job['id']}, but it does not look like this job "
            f"({verification}: {evidence}). It will not be used for scoring; paste the text with "
            f"--file, --text or --clip instead.")


def cmd_jd(a):
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    chosen = [flag for flag, value in (("--file", a.file), ("--text", a.text), ("--clip", a.clip), ("--url", a.url))
              if value not in (None, False)]
    if a.job == "watch":
        if chosen or a.force:
            raise CLIError("`jd watch` takes no --file, --text, --clip, --url or --force")
        return _cmd_jd_watch(a, conn, pid)
    if a.interval is not None or a.max_seconds is not None:
        raise CLIError("--interval and --max-seconds only apply to `jd watch`")
    if not a.job.isdigit():
        raise CLIError("the job must be a job id (a number) or 'watch'")
    job = q.require_job(conn, int(a.job), pid)
    if len(chosen) != 1:
        raise CLIError("give exactly one of --file, --text, --clip or --url"
                       + (f" (got {' and '.join(chosen)})" if chosen else ""))
    if a.force and not a.clip:
        raise CLIError("--force only applies to --clip")
    if a.clip:
        return _cmd_jd_clip(conn, job, a.force)
    if a.url is not None:
        return _cmd_jd_url(conn, job, a.url)
    text = Path(a.file).read_text(encoding="utf-8") if a.file else a.text
    if not text or len(text.strip()) < 50:
        raise CLIError("provide the job description with --file or --text (at least 50 characters)")
    _store_user_jd(conn, job["id"], text, "supplied by the user")
    out(f"stored the description for job {job['id']}; run `run --no-mail` to re-evaluate")


def cmd_stage(a):
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    res = sm.transition_stage(conn, pid, a.job, a.to, a.reason or "", a.evidence or "", actor="user")
    out(f"job {a.job}: {res['from']} -> {res['to']}" if res["changed"] else f"job {a.job} is already '{res['to']}'")


def cmd_applied(a):
    a.to, a.reason = "applied", a.reason or "user reports having applied"
    cmd_stage(a)


def cmd_approvals(a):
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    if a.sub == "list":
        rows = q.list_approvals(conn, pid) if a.all else [r for r in q.get_pending_approvals(conn, pid)]
        for r in rows:
            out(am.format_approval(r))
        if not rows:
            out("no approval requests")
        return
    res = am.decide(conn, pid, a.id, approve=(a.sub == "approve"))
    out(res["message"])
    if not res.get("executed") and a.sub == "approve":
        raise CLIError(res["message"])


def cmd_followup(a):
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    prof = read_full_profile(conn, pid)
    app = q.get_application_by_job(conn, a.job, pid)
    if app is None:
        raise CLIError("no application record for that job")
    s = get_settings()
    if a.sub == "check":
        r = sm.check_follow_up_eligibility(conn, app["id"], wait_days=s.followup_wait_days)
        out(f"eligible={r['eligible']}  {r['code']}: {r['reason']}")
    elif a.sub == "draft":
        r = am.draft_follow_up(conn, prof, a.job, s.followup_wait_days)
        if not r["drafted"]:
            raise CLIError(f"no draft: {r['reason']}")
        out(r["draft"])
        out(f"-- DRAFT ONLY, nothing was sent. Approval request {r['approval_id']} recorded (`approvals approve {r['approval_id']}`).")
    elif a.sub == "show":
        fu = q.get_followup(conn, app["id"])
        out(fu["draft_text"] if fu and fu["draft_text"] else "no draft")
    elif a.sub == "mark-sent":
        q.mark_followup_sent(conn, app["id"])
        out("recorded that you sent the follow-up yourself")


def cmd_export(a):
    from jobledger.tools.excel_exporter import export_excel
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    s = get_settings()
    r = export_excel(conn, pid, s.export_dir, a.filename or f"jobledger_register_p{pid}.xlsx", wait_days=s.followup_wait_days)
    out(f"wrote {r['path']} ({r['rows']} rows)" + (" [original file was locked; wrote a timestamped copy]" if r["locked_fallback"] else ""))
    for w in r["warnings"]:
        out(f"  ! {w}")


def cmd_logs(a):
    conn = _connect()
    pid = _pick_profile(conn, a.profile)
    if a.runs:
        for r in q.list_runs(conn, pid):
            out(f"{r['run_id']}  {r['status']:<22} mode={r['mode']}  emails={r['emails_scanned']} new_jobs={r['jobs_found']} evaluated={r['jobs_evaluated']}  {r['started_at']}  {r['error'] or ''}")
        return
    rows = q.list_action_logs(conn, pid, run_id=a.run, limit=a.limit)
    for r in reversed(rows):
        out(f"#{r['step_no'] or '-':<3} {r['action_type']:<22} sel={r['llm_selected_action'] or '-':<18} valid={r['validation_outcome'] or '-':<28} "
            f"result={(r['tool_result_summary'] or r['error'] or '')[:90]}")


def cmd_eval(a):
    from jobledger import evaluation
    code = evaluation.main(a.which, report_dir=a.report_dir)
    if code:
        raise CLIError("evaluation reported failures (see the report)")


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobledger", description="JobLedger: persistent job-search memory agent")
    p.add_argument("--db", help="SQLite file (overrides DB_PATH)")
    p.add_argument("--profile", "-p", type=int, help="profile id (optional when only one exists)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create/upgrade the database").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="show redacted configuration").set_defaults(fn=cmd_doctor)

    pr = sub.add_parser("profile", help="manage profiles")
    prs = pr.add_subparsers(dest="sub", required=True)
    c = prs.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--email", required=True)
    c.add_argument("--location")
    c.add_argument("--work-auth")
    c.add_argument("--arrangements", help="comma list: remote,hybrid,onsite")
    c.add_argument("--types", help="comma list: fulltime,contract,...")
    c.add_argument("--exclude-companies")
    c.add_argument("--exclude-keywords")
    c.add_argument("--min-salary", type=float)
    c.add_argument("--currency", default="USD")
    c.add_argument("--notes")
    prs.add_parser("list")
    prs.add_parser("show")
    pr.set_defaults(fn=cmd_profile)

    cv = sub.add_parser("cv", help="add a CV version (old versions are kept)")
    cv.add_argument("--label", required=True)
    cv.add_argument("--skills", help="comma separated")
    cv.add_argument("--years", type=float)
    cv.add_argument("--education")
    cv.add_argument("--file")
    cv.add_argument("--text")
    cv.set_defaults(fn=cmd_cv)

    pf = sub.add_parser("prefs", help="correct preferences (history is preserved)")
    pfs = pf.add_subparsers(dest="sub", required=True)
    s_ = pfs.add_parser("set")
    s_.add_argument("--set", action="append", metavar="FIELD=VALUE")
    s_.add_argument("--reason")
    pfs.add_parser("history")
    pf.set_defaults(fn=cmd_prefs)

    r = sub.add_parser("run", help="read mailbox, retrieve JDs, evaluate, export")
    r.add_argument("--fixtures", help="process saved .eml/.json emails instead of the live mailbox")
    r.add_argument("--no-mail", action="store_true", help="skip the mailbox; just re-evaluate/retry")
    r.add_argument("--no-llm", action="store_true", help="force deterministic mode")
    r.add_argument("--retry", action="store_true", help="retry retryable NEEDS_JD jobs")
    r.set_defaults(fn=cmd_run)

    j = sub.add_parser("jobs")
    js = j.add_subparsers(dest="sub", required=True)
    js.add_parser("list")
    sh = js.add_parser("show")
    sh.add_argument("job", type=int)
    j.set_defaults(fn=cmd_jobs)

    jd = sub.add_parser("jd", help="supply a job description yourself (for NEEDS_JD jobs); `jd watch` watches the clipboard")
    jd.add_argument("job", help="job id, or 'watch'")
    jd.add_argument("--file")
    jd.add_argument("--text")
    jd.add_argument("--clip", action="store_true", help="use the text currently on the clipboard")
    jd.add_argument("--url", help="fetch one public job page (LinkedIn is refused by policy)")
    jd.add_argument("--force", action="store_true", help="with --clip: store even if the title or company is not in the text")
    jd.add_argument("--interval", type=float, default=None, help="jd watch: seconds between clipboard checks (default 1)")
    jd.add_argument("--max-seconds", type=float, default=None, help="jd watch: stop after this many seconds")
    jd.set_defaults(fn=cmd_jd)

    st = sub.add_parser("stage", help="move an application to a stage (you only; the agent cannot)")
    st.add_argument("job", type=int)
    st.add_argument("--to", required=True)
    st.add_argument("--reason")
    st.add_argument("--evidence")
    st.set_defaults(fn=cmd_stage)

    ap = sub.add_parser("applied", help="record that YOU applied (JobLedger never applies)")
    ap.add_argument("job", type=int)
    ap.add_argument("--reason")
    ap.add_argument("--evidence")
    ap.set_defaults(fn=cmd_applied)

    aps = sub.add_parser("approvals")
    apss = aps.add_subparsers(dest="sub", required=True)
    al = apss.add_parser("list")
    al.add_argument("--all", action="store_true")
    for name in ("approve", "reject"):
        x = apss.add_parser(name)
        x.add_argument("id", type=int)
    aps.set_defaults(fn=cmd_approvals)

    fu = sub.add_parser("followup")
    fus = fu.add_subparsers(dest="sub", required=True)
    for name in ("check", "draft", "show", "mark-sent"):
        x = fus.add_parser(name)
        x.add_argument("job", type=int)
    fu.set_defaults(fn=cmd_followup)

    ex = sub.add_parser("export", help="rebuild the Excel register from the database")
    ex.add_argument("--filename", default=None, help="default: jobledger_register_p<profile id>.xlsx")
    ex.set_defaults(fn=cmd_export)

    lg = sub.add_parser("logs", help="episodic memory: what the agent did and why")
    lg.add_argument("--run")
    lg.add_argument("--runs", action="store_true", help="list runs instead")
    lg.add_argument("--limit", type=int, default=40)
    lg.set_defaults(fn=cmd_logs)

    ev = sub.add_parser("eval", help="run the required scenarios / memory comparisons / labeled-set diagnostic")
    ev.add_argument("which", nargs="?", default="all", choices=["all", "scenarios", "memory", "labeled", "failures"])
    ev.add_argument("--report-dir", default="reports")
    ev.set_defaults(fn=cmd_eval)
    return p


def main(argv: Optional[list] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.db:
        os.environ["DB_PATH"] = args.db
        reset_settings()
    try:
        args.fn(args)
        return 0
    except CLIError as exc:
        out(f"error: {exc}")
        return 1
    except (sm.StageError, LookupError, q.ProfileIsolationError, ValueError, LLMError) as exc:
        out(f"error: {exc}")
        return 1
    except sqlite3.Error as exc:
        out(f"database error: {type(exc).__name__}: {exc}")
        return 1
    except OSError as exc:
        out(f"file error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
