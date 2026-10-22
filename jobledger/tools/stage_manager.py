"""
Tool G: Application Stage Manager
Manages application lifecycle with full history preservation.
"""
from __future__ import annotations

from typing import Optional
import sqlite3

from jobledger.db import queries as q
from jobledger.logging_setup import get_logger

logger = get_logger("stage_manager")

VALID_STAGES = [
    "saved", "shortlisted", "applied", "replied",
    "interview", "rejected", "withdrawn", "closed",
]
TERMINAL_STAGES = {"rejected", "withdrawn", "closed"}
FOLLOW_UP_SUPPRESSED_STAGES = {"replied", "rejected", "withdrawn", "closed"}


def transition_stage(
    conn: sqlite3.Connection,
    job_id: int,
    profile_id: int,
    new_stage: str,
    reason: str = "",
    evidence: str = "",
    changed_by: str = "user",
) -> tuple[bool, str]:
    """
    Transition application to new_stage.
    Returns (success, message).
    Records full history. Updates follow-up eligibility.
    """
    if new_stage not in VALID_STAGES:
        return False, f"Invalid stage '{new_stage}'. Valid: {VALID_STAGES}"

    try:
        app_id, current_stage = q.get_or_create_application(conn, job_id, profile_id)
        q.update_application_stage(conn, app_id, new_stage, reason, evidence, changed_by)

        # Update follow-up eligibility
        if new_stage in FOLLOW_UP_SUPPRESSED_STAGES:
            suppression_reason = _get_suppression_reason(new_stage)
            q.update_followup_eligibility(conn, app_id, False, suppression_reason)
        elif new_stage == "applied":
            # Opening follow-up eligibility on apply
            q.update_followup_eligibility(conn, app_id, True, "")

        logger.info(f"Stage transition: job={job_id} {current_stage} -> {new_stage} by={changed_by}")
        return True, f"Stage updated: {current_stage} → {new_stage}"

    except Exception as e:
        logger.error(f"Stage transition failed for job {job_id}: {e}")
        return False, f"Error: {e}"


def _get_suppression_reason(stage: str) -> str:
    reasons = {
        "replied": "Recruiter has already responded - no follow-up needed",
        "rejected": "Application rejected - follow-up not appropriate",
        "withdrawn": "Application withdrawn by candidate",
        "closed": "Position closed",
    }
    return reasons.get(stage, f"Stage is {stage}")


def check_follow_up_eligibility(
    conn: sqlite3.Connection,
    application_id: int,
) -> tuple[bool, Optional[str]]:
    """
    Returns (eligible, suppressed_reason).
    Suppressed if stage in FOLLOW_UP_SUPPRESSED_STAGES.
    """
    app = q.get_application(conn, application_id)
    if not app:
        return False, "Application not found"

    stage = app["stage"]
    if stage in FOLLOW_UP_SUPPRESSED_STAGES:
        reason = _get_suppression_reason(stage)
        return False, reason

    # Check follow_ups table
    fu = conn.execute(
        "SELECT eligible, suppressed_reason FROM follow_ups WHERE application_id=?",
        (application_id,),
    ).fetchone()
    if fu:
        if not fu["eligible"]:
            return False, fu["suppressed_reason"]
        return True, None

    # Default: eligible if applied
    if stage == "applied":
        return True, None

    return False, f"Stage '{stage}' does not warrant follow-up"


def get_recommended_next_action(
    stage: str,
    days_since_applied: Optional[float] = None,
) -> str:
    """Return a human-readable recommended next action based on stage."""
    recommendations = {
        "saved": "Review job details and decide whether to apply",
        "shortlisted": "Prepare application materials and apply",
        "applied": "Wait for response (follow up after 7-14 days if no reply)",
        "replied": "Respond to recruiter message promptly",
        "interview": "Prepare thoroughly for the interview",
        "rejected": "Consider requesting feedback and move forward",
        "withdrawn": "No action needed - you withdrew this application",
        "closed": "Position is no longer available",
    }
    rec = recommendations.get(stage, "Review application status")
    if stage == "applied" and days_since_applied is not None and days_since_applied > 14:
        rec = "Consider sending a polite follow-up enquiry"
    return rec
