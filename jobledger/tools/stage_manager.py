"""Tool G - Application Stage Manager + follow-up eligibility.

Principles:
  * a stage only changes on an explicit user action or an approved proposal - never because
    a link was opened, a job was shortlisted, or an email looked like a reply;
  * the agent itself (actor='agent') may not move stages at all: it proposes via the
    approval gate (tools/approval_manager.py);
  * every change is history (application_stage_history) with reason + evidence + actor;
  * stage and dates drive follow-up eligibility, and a suppression always records WHY.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from jobledger.db import queries as q
from jobledger.db.queries import VALID_STAGES
from jobledger.timeutil import now_iso, parse_iso, utcnow

TRANSITIONS = {
    "saved": {"shortlisted", "applied", "withdrawn", "closed"},
    "shortlisted": {"saved", "applied", "withdrawn", "closed"},
    "applied": {"replied", "interview", "rejected", "withdrawn", "closed"},
    "replied": {"interview", "rejected", "withdrawn", "closed", "applied"},
    "interview": {"replied", "rejected", "withdrawn", "closed"},
    "rejected": {"saved"},       # re-open only deliberately
    "withdrawn": {"saved"},
    "closed": {"saved"},
}
TERMINAL = {"rejected", "withdrawn", "closed"}
EVIDENCE_REQUIRED_WHEN_APPROVED = {"applied", "replied", "interview", "rejected"}
USER_ACTORS = {"user", "approval"}


class StageError(ValueError):
    pass


class ApprovalRequired(StageError):
    pass


def transition_stage(conn, profile_id: int, job_id: int, new_stage: str, reason: str = "",
                     evidence: str = "", actor: str = "user") -> dict:
    """Move a job's application to ``new_stage`` (creating a 'saved' application if none exists)."""
    if new_stage not in VALID_STAGES:
        raise StageError(f"invalid stage '{new_stage}'. valid: {VALID_STAGES}")
    if actor not in USER_ACTORS and actor != "system":
        raise ApprovalRequired(f"actor '{actor}' may not change stages; propose it through the approval gate")
    with q.tx(conn):
        app_id, current = q.get_or_create_application(conn, job_id, profile_id)   # enforces ownership
        if new_stage == current:
            return {"application_id": app_id, "from": current, "to": new_stage, "changed": False}
        if new_stage not in TRANSITIONS[current]:
            raise StageError(f"cannot move {current} -> {new_stage}. allowed from {current}: {sorted(TRANSITIONS[current])}")
        if actor == "approval" and new_stage in EVIDENCE_REQUIRED_WHEN_APPROVED and not (evidence or "").strip():
            raise StageError(f"moving to '{new_stage}' via approval requires supporting evidence")
        q.update_application_stage(conn, app_id, new_stage, reason, evidence, changed_by=actor)
        check_follow_up_eligibility(conn, app_id)
    return {"application_id": app_id, "from": current, "to": new_stage, "changed": True}


def check_follow_up_eligibility(conn, application_id: int, now=None, wait_days: int = 7,
                                persist: bool = True) -> dict:
    """Rules, first match wins. Returns {eligible, code, reason, due_at}."""
    now = now or utcnow()
    app = q.get_application(conn, application_id)
    if app is None:
        raise LookupError(f"application {application_id} not found")
    stage = app["stage"]
    job = q.get_job(conn, app["job_id"])
    fu = q.get_followup(conn, application_id)
    applied = parse_iso(app["applied_at"])

    def result(eligible, code, reason, due=None):
        out = {"eligible": eligible, "code": code, "reason": reason, "due_at": due}
        if persist:
            q.update_followup_eligibility(conn, application_id, eligible, f"{code}: {reason}", due)
        return out

    if stage in ("saved", "shortlisted"):
        return result(False, "not_applied", f"stage is '{stage}': nothing to follow up on")
    if stage in TERMINAL:
        return result(False, f"terminal_{stage}", f"application is {stage}")
    if stage == "replied":
        return result(False, "recruiter_replied", "recruiter has already replied")
    if stage == "interview":
        return result(False, "interview_in_progress", "interview stage: follow up through the interview process")
    if app["last_reply_at"]:
        return result(False, "recruiter_replied", f"recruiter replied on {app['last_reply_at'][:10]}")
    if job is not None and job["workflow_status"] in ("expired", "closed"):
        return result(False, "job_closed", f"job marked {job['workflow_status']}")
    if applied is None:
        return result(False, "no_applied_date", "stage is 'applied' but no application date is recorded")
    if fu is not None and fu["sent_at"]:
        again = parse_iso(fu["sent_at"]) + timedelta(days=2 * wait_days)
        if now < again:
            return result(False, "already_followed_up", f"follow-up sent {fu['sent_at'][:10]}", again.isoformat(timespec="seconds"))
    due = applied + timedelta(days=wait_days)
    if now < due:
        return result(False, "waiting_period", f"wait {wait_days} days after applying", due.isoformat(timespec="seconds"))
    return result(True, "eligible", f"applied {applied.date()} with no reply after {wait_days}+ days", due.isoformat(timespec="seconds"))


def recommended_next_action(stage: Optional[str], eval_status: Optional[str], jd_status: Optional[str],
                            follow_up: Optional[dict] = None, flags: Optional[list] = None) -> str:
    flags = flags or []
    if stage is None or stage in ("saved", "shortlisted"):
        if eval_status == "excluded":
            return "Skip: excluded by your rules"
        if eval_status == "needs_jd":
            return "Provide the job description or a public URL (NEEDS_JD)"
        if any(f.startswith("open_conflict") for f in flags):
            return "Resolve conflicting job details before deciding"
        if eval_status is None:
            return "Run processing to evaluate"
        return "Review and shortlist" if stage != "shortlisted" else "Apply (manually) if still interested"
    if stage == "applied":
        if follow_up and follow_up.get("eligible"):
            return "Follow-up eligible: draft a follow-up (needs your approval)"
        due = (follow_up or {}).get("due_at")
        return f"Wait for a reply (follow-up possible after {due[:10]})" if due else "Wait for a reply"
    if stage == "replied":
        return "Reply to the recruiter"
    if stage == "interview":
        return "Prepare for the interview"
    return "No action (closed out)"


def apply_reply_evidence(conn, profile_id: int, job_id: int, evidence: str, run_id: Optional[str] = None) -> int:
    """Recruiter-reply evidence NEVER changes the stage directly: it becomes a pending proposal."""
    app = q.get_application_by_job(conn, job_id, profile_id)
    if app is None or app["stage"] != "applied":
        raise StageError("reply evidence only applies to jobs in stage 'applied'")
    return q.create_approval_request(
        conn, "update_stage", f"Possible recruiter reply: move job {job_id} to 'replied'?", run_id=run_id,
        profile_id=profile_id, job_id=job_id, application_id=app["id"], evidence=evidence,
        payload={"job_id": job_id, "new_stage": "replied", "reason": "recruiter reply detected in mailbox"})
