"""Tool I - Approval Manager (+ follow-up drafts).

Nothing consequential happens without an explicit decision recorded here, and even an
APPROVED action never contacts the outside world:
  * update_stage   -> performs the DB stage change (the only executor with side effects);
  * apply_to_job / send_follow_up -> hand-off only. JobLedger has no code path that submits an
    application or sends an email. Approving records the decision and tells the user what to do.
"""
from __future__ import annotations

import json
from typing import Optional

from jobledger.db import queries as q
from jobledger.tools import stage_manager as sm

ACTION_TYPES = {"update_stage", "apply_to_job", "send_follow_up", "other"}


def propose(conn, profile_id: int, action_type: str, description: str, payload: Optional[dict] = None,
            job_id: Optional[int] = None, application_id: Optional[int] = None, run_id: Optional[str] = None,
            evidence=None) -> int:
    if action_type not in ACTION_TYPES:
        raise ValueError(f"unknown action_type '{action_type}'")
    if job_id is not None:
        q.require_job(conn, job_id, profile_id)
    return q.create_approval_request(conn, action_type, description, run_id=run_id, profile_id=profile_id,
                                     job_id=job_id, evidence=evidence, payload=payload, application_id=application_id)


def format_approval(row) -> str:
    st = row["status"]
    lines = [f"[{row['id']}] {row['action_type']}  status={st.upper()}  approval_required={'yes' if row['requires_approval'] else 'no'}",
             f"    proposed: {row['description']}"]
    if row["job_id"]:
        lines.append(f"    job: {row['job_id']}")
    if row["evidence"]:
        lines.append(f"    evidence: {str(row['evidence'])[:300]}")
    if row["payload"]:
        lines.append(f"    payload: {row['payload'][:200]}")
    if row["decided_at"]:
        lines.append(f"    decided: {row['decided_at']} by {row['decided_by']}")
    if row["result"]:
        lines.append(f"    result: {row['result'][:300]}")
    return "\n".join(lines)


def decide(conn, profile_id: int, approval_id: int, approve: bool, decided_by: str = "user") -> dict:
    """Decide a pending request exactly once; execute atomically when approved."""
    with q.tx(conn):
        row = q.get_approval(conn, approval_id, profile_id)
        if row is None:
            raise q.ProfileIsolationError(f"approval {approval_id} not found for profile {profile_id}")
        if not q.update_approval_status(conn, approval_id, "approved" if approve else "rejected", decided_by):
            return {"approval_id": approval_id, "status": row["status"], "executed": False,
                    "message": f"already {row['status']}; a request can be decided only once"}
        if not approve:
            return {"approval_id": approval_id, "status": "rejected", "executed": False, "message": "rejected; nothing changed"}
        result = _execute(conn, profile_id, row)
        q.mark_approval_executed(conn, approval_id, result)
    return {"approval_id": approval_id, "status": "approved", "executed": True, "message": result.get("message", ""), "result": result}


def _execute(conn, profile_id: int, row) -> dict:
    payload = json.loads(row["payload"]) if row["payload"] else {}
    kind = row["action_type"]
    if kind == "update_stage":
        res = sm.transition_stage(conn, profile_id, int(payload["job_id"]), payload["new_stage"],
                                  reason=payload.get("reason", "approved proposal"),
                                  evidence=str(row["evidence"] or "approved by user"), actor="approval")
        return {"message": f"stage {res['from']} -> {res['to']}", **res}
    if kind == "send_follow_up":
        return {"message": "Approved. JobLedger does NOT send email: copy the draft (`followup show`) and send it yourself, "
                           "then run `followup mark-sent` so the record is accurate.", "sent": False}
    if kind == "apply_to_job":
        return {"message": "Approved. JobLedger does NOT submit applications: apply yourself, then run the `applied` command.",
                "applied": False}
    return {"message": "approved (no executable side effect)"}


# ---------------------------------------------------------------------------
# Follow-up drafts (drafting is not sending)
# ---------------------------------------------------------------------------

def draft_follow_up(conn, profile, job_id: int, wait_days: int = 7, run_id: Optional[str] = None) -> dict:
    job = q.require_job(conn, job_id, profile.id)
    app = q.get_application_by_job(conn, job_id, profile.id)
    if app is None:
        return {"drafted": False, "reason": "no application record"}
    elig = sm.check_follow_up_eligibility(conn, app["id"], wait_days=wait_days)
    if not elig["eligible"]:
        return {"drafted": False, "reason": f"{elig['code']}: {elig['reason']}", "eligibility": elig}
    applied = (app["applied_at"] or "")[:10]
    text = (f"Subject: Following up - {job['title']}\n\nHello {job['company']} hiring team,\n\n"
            f"I applied for the {job['title']} position on {applied} and wanted to follow up on the status of my application. "
            f"I remain very interested in the role and would be glad to provide anything further you need.\n\n"
            f"Thank you for your time.\n\nKind regards,\n{profile.name}\n")
    with q.tx(conn):
        q.save_followup_draft(conn, app["id"], text)
        aid = q.create_approval_request(
            conn, "send_follow_up", f"Send follow-up for '{job['title']}' at {job['company']}? (draft only; you send it)",
            run_id=run_id, profile_id=profile.id, job_id=job_id, application_id=app["id"], evidence=elig["reason"],
            payload={"application_id": app["id"]})
    return {"drafted": True, "draft": text, "approval_id": aid, "sent": False}
