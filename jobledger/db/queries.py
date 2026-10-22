"""Database query helper functions for JobLedger."""

import sqlite3
import hashlib
import json
from datetime import datetime
from typing import Optional, Any


def now() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def create_profile(
    conn: sqlite3.Connection,
    name: str,
    email: str,
    phone: str = None,
    location: str = None,
    work_authorization: str = None,
    preferred_arrangements: str = None,
    preferred_types: str = None,
    excluded_companies: str = None,
    excluded_keywords: str = None,
    min_salary: float = None,
    currency: str = 'USD',
    notes: str = None,
) -> int:
    """INSERT a new profile and return its id."""
    ts = now()
    cur = conn.execute(
        """
        INSERT INTO profiles (
            name, email, phone, location, work_authorization,
            preferred_arrangements, preferred_types,
            excluded_companies, excluded_keywords,
            min_salary, currency, notes,
            is_active, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
        """,
        (
            name, email, phone, location, work_authorization,
            preferred_arrangements, preferred_types,
            excluded_companies, excluded_keywords,
            min_salary, currency, notes,
            ts, ts,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_profile(conn: sqlite3.Connection, profile_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()


def get_profile_by_email(conn: sqlite3.Connection, email: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM profiles WHERE email = ?", (email,)
    ).fetchone()


def list_profiles(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT * FROM profiles WHERE is_active = 1 ORDER BY id"
    ).fetchall()


def update_profile_field(
    conn: sqlite3.Connection,
    profile_id: int,
    field: str,
    new_value: Any,
    reason: str = '',
) -> None:
    """Read old value, record history, update profiles."""
    row = conn.execute(
        f"SELECT {field} FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    old_value = str(row[field]) if row and row[field] is not None else None
    new_value_str = str(new_value) if new_value is not None else None
    ts = now()
    conn.execute(
        """
        INSERT INTO preferences_history
            (profile_id, field_name, old_value, new_value, reason, changed_at)
        VALUES (?,?,?,?,?,?)
        """,
        (profile_id, field, old_value, new_value_str, reason, ts),
    )
    conn.execute(
        f"UPDATE profiles SET {field} = ?, updated_at = ? WHERE id = ?",
        (new_value, ts, profile_id),
    )
    conn.commit()


def deactivate_profile(conn: sqlite3.Connection, profile_id: int) -> None:
    conn.execute(
        "UPDATE profiles SET is_active = 0, updated_at = ? WHERE id = ?",
        (now(), profile_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# CV Versions
# ---------------------------------------------------------------------------

def add_cv_version(
    conn: sqlite3.Connection,
    profile_id: int,
    version_label: str,
    content_text: str,
    skills: str,
    experience_years: float = None,
    education: str = None,
) -> int:
    """Deactivate any existing active CV for this profile then insert new one."""
    conn.execute(
        "UPDATE cv_versions SET is_active = 0 WHERE profile_id = ? AND is_active = 1",
        (profile_id,),
    )
    ts = now()
    cur = conn.execute(
        """
        INSERT INTO cv_versions
            (profile_id, version_label, content_text, skills,
             experience_years, education, is_active, created_at)
        VALUES (?,?,?,?,?,?,1,?)
        """,
        (profile_id, version_label, content_text, skills, experience_years, education, ts),
    )
    conn.commit()
    return cur.lastrowid


def get_active_cv(conn: sqlite3.Connection, profile_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cv_versions WHERE profile_id = ? AND is_active = 1",
        (profile_id,),
    ).fetchone()


def list_cv_versions(conn: sqlite3.Connection, profile_id: int) -> list:
    return conn.execute(
        "SELECT * FROM cv_versions WHERE profile_id = ? ORDER BY id DESC",
        (profile_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

def record_email(
    conn: sqlite3.Connection,
    profile_id: int,
    message_uid: str,
    source: str,
    sender: str,
    subject: str,
    received_at: str,
    raw_body_hash: str,
    job_count: int = 0,
    status: str = 'processed',
    error_msg: str = None,
) -> Optional[int]:
    """INSERT OR IGNORE; return the row id, or None if it was a duplicate."""
    ts = now()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO processed_emails
            (profile_id, message_uid, source, sender, subject,
             received_at, processed_at, raw_body_hash,
             job_count, status, error_msg)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            profile_id, message_uid, source, sender, subject,
            received_at, ts, raw_body_hash,
            job_count, status, error_msg,
        ),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return cur.lastrowid


def get_email(conn: sqlite3.Connection, email_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM processed_emails WHERE id = ?", (email_id,)
    ).fetchone()


def email_already_processed(
    conn: sqlite3.Connection, profile_id: int, message_uid: str
) -> bool:
    row = conn.execute(
        "SELECT id FROM processed_emails WHERE profile_id = ? AND message_uid = ?",
        (profile_id, message_uid),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def compute_stable_id(title: str, company: str, source_url: str) -> str:
    """sha256 of lowercased, stripped, joined fields."""
    raw = "|".join([
        (title or "").lower().strip(),
        (company or "").lower().strip(),
        (source_url or "").lower().strip(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upsert_job(
    conn: sqlite3.Connection,
    profile_id: int,
    source_email_id: Optional[int],
    title: str,
    company: str,
    location: str = None,
    arrangement: str = None,
    employment_type: str = None,
    apply_url: str = None,
    salary_min: float = None,
    salary_max: float = None,
    salary_currency: str = None,
    salary_raw: str = None,
    description_snippet: str = None,
    received_at: str = None,
) -> tuple:
    """Return (job_id, is_new). Uses stable_id for dedup."""
    stable_id = compute_stable_id(title, company, apply_url or "")
    ts = now()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO jobs (
            profile_id, source_email_id, stable_id, title, company,
            location, arrangement, employment_type, apply_url,
            salary_min, salary_max, salary_currency, salary_raw,
            description_snippet, jd_status, received_at,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',?,?,?)
        """,
        (
            profile_id, source_email_id, stable_id, title, company,
            location, arrangement, employment_type, apply_url,
            salary_min, salary_max, salary_currency, salary_raw,
            description_snippet, received_at, ts, ts,
        ),
    )
    is_new = cur.rowcount == 1
    conn.commit()
    row = conn.execute(
        "SELECT id FROM jobs WHERE profile_id = ? AND stable_id = ?",
        (profile_id, stable_id),
    ).fetchone()
    return (row["id"], is_new)


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()


def list_jobs(
    conn: sqlite3.Connection,
    profile_id: int,
    stage: str = None,
    limit: int = 100,
) -> list:
    """List jobs for a profile; optionally filter by application stage."""
    if stage:
        return conn.execute(
            """
            SELECT j.* FROM jobs j
            JOIN applications a ON a.job_id = j.id
            WHERE j.profile_id = ? AND a.stage = ?
            ORDER BY j.created_at DESC
            LIMIT ?
            """,
            (profile_id, stage, limit),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM jobs WHERE profile_id = ?
        ORDER BY created_at DESC LIMIT ?
        """,
        (profile_id, limit),
    ).fetchall()


def update_job_jd_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    conn.execute(
        "UPDATE jobs SET jd_status = ?, updated_at = ? WHERE id = ?",
        (status, now(), job_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# JD Snapshots
# ---------------------------------------------------------------------------

def save_jd_snapshot(
    conn: sqlite3.Connection,
    job_id: int,
    source_url: str,
    final_url: str,
    status: str,
    retrieved_at: str = None,
    failure_reason: str = None,
    content_text: str = None,
    title_found: str = None,
    company_found: str = None,
    verification_status: str = None,
    verification_evidence: str = None,
    response_bytes: int = None,
    retry_count: int = 0,
) -> int:
    content_hash = (
        hashlib.sha256(content_text.encode("utf-8")).hexdigest()
        if content_text
        else None
    )
    ts = retrieved_at or now()
    cur = conn.execute(
        """
        INSERT INTO jd_snapshots (
            job_id, source_url, final_url, retrieved_at, status,
            failure_reason, content_text, content_hash,
            title_found, company_found,
            verification_status, verification_evidence,
            response_bytes, retry_count
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            job_id, source_url, final_url, ts, status,
            failure_reason, content_text, content_hash,
            title_found, company_found,
            verification_status, verification_evidence,
            response_bytes, retry_count,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_latest_jd_snapshot(
    conn: sqlite3.Connection, job_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM jd_snapshots WHERE job_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------

def save_evaluation(
    conn: sqlite3.Connection,
    job_id: int,
    profile_id: int,
    cv_version_id: Optional[int],
    hard_filter_pass: Optional[int],
    hard_filter_details: Any,
    skill_score: float,
    experience_score: float,
    arrangement_score: float,
    type_score: float,
    location_score: float,
    total_score: float,
    score_breakdown: Any,
    llm_assessment: Any,
    llm_model: str,
    llm_provider: str,
    match_rationale: str,
    missing_info: str,
    eval_version: str = 'v1',
) -> int:
    ts = now()
    hard_filter_details_str = (
        json.dumps(hard_filter_details)
        if not isinstance(hard_filter_details, str)
        else hard_filter_details
    )
    score_breakdown_str = (
        json.dumps(score_breakdown)
        if not isinstance(score_breakdown, str)
        else score_breakdown
    )
    llm_assessment_str = (
        json.dumps(llm_assessment)
        if not isinstance(llm_assessment, str)
        else llm_assessment
    )
    cur = conn.execute(
        """
        INSERT OR REPLACE INTO evaluations (
            job_id, profile_id, cv_version_id, eval_version,
            hard_filter_pass, hard_filter_details,
            skill_score, experience_score, arrangement_score,
            type_score, location_score, total_score,
            score_breakdown, llm_assessment, llm_model, llm_provider,
            match_rationale, missing_info, evaluated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            job_id, profile_id, cv_version_id, eval_version,
            hard_filter_pass, hard_filter_details_str,
            skill_score, experience_score, arrangement_score,
            type_score, location_score, total_score,
            score_breakdown_str, llm_assessment_str, llm_model, llm_provider,
            match_rationale, missing_info, ts,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_evaluation(
    conn: sqlite3.Connection,
    job_id: int,
    profile_id: int,
    eval_version: str = 'v1',
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM evaluations
        WHERE job_id = ? AND profile_id = ? AND eval_version = ?
        """,
        (job_id, profile_id, eval_version),
    ).fetchone()


def set_evaluation_label(
    conn: sqlite3.Connection,
    job_id: int,
    profile_id: int,
    label_expected: int,
    label_rationale: str,
    eval_version: str = 'v1',
) -> None:
    conn.execute(
        """
        UPDATE evaluations
        SET label_expected = ?, label_rationale = ?
        WHERE job_id = ? AND profile_id = ? AND eval_version = ?
        """,
        (label_expected, label_rationale, job_id, profile_id, eval_version),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

VALID_STAGES = [
    'saved', 'shortlisted', 'applied', 'replied',
    'interview', 'rejected', 'withdrawn', 'closed',
]


def get_or_create_application(
    conn: sqlite3.Connection, job_id: int, profile_id: int
) -> tuple:
    """Return (application_id, current_stage)."""
    row = conn.execute(
        "SELECT id, stage FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row:
        return (row["id"], row["stage"])
    ts = now()
    cur = conn.execute(
        """
        INSERT INTO applications (job_id, profile_id, stage, created_at, updated_at)
        VALUES (?,?,?,?,?)
        """,
        (job_id, profile_id, 'saved', ts, ts),
    )
    conn.commit()
    return (cur.lastrowid, 'saved')


def update_application_stage(
    conn: sqlite3.Connection,
    application_id: int,
    new_stage: str,
    reason: str = '',
    evidence: str = '',
    changed_by: str = 'user',
) -> None:
    """Read current stage, record history, update application."""
    row = conn.execute(
        "SELECT stage FROM applications WHERE id = ?", (application_id,)
    ).fetchone()
    from_stage = row["stage"] if row else None
    ts = now()
    conn.execute(
        """
        INSERT INTO application_stage_history
            (application_id, from_stage, to_stage, reason, evidence, changed_at, changed_by)
        VALUES (?,?,?,?,?,?,?)
        """,
        (application_id, from_stage, new_stage, reason, evidence, ts, changed_by),
    )
    applied_at_sql = ", applied_at = ?" if new_stage == 'applied' else ""
    if new_stage == 'applied':
        conn.execute(
            f"UPDATE applications SET stage = ?, updated_at = ?, applied_at = ? WHERE id = ?",
            (new_stage, ts, ts, application_id),
        )
    else:
        conn.execute(
            "UPDATE applications SET stage = ?, updated_at = ? WHERE id = ?",
            (new_stage, ts, application_id),
        )
    conn.commit()


def get_application(
    conn: sqlite3.Connection, application_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM applications WHERE id = ?", (application_id,)
    ).fetchone()


def get_application_by_job(
    conn: sqlite3.Connection, job_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

def get_or_create_followup(conn: sqlite3.Connection, application_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM follow_ups WHERE application_id = ?", (application_id,)
    ).fetchone()
    if row:
        return row["id"]
    ts = now()
    cur = conn.execute(
        """
        INSERT INTO follow_ups (application_id, eligible, created_at, updated_at)
        VALUES (?,1,?,?)
        """,
        (application_id, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def update_followup_eligibility(
    conn: sqlite3.Connection,
    application_id: int,
    eligible: bool,
    reason: str,
) -> None:
    followup_id = get_or_create_followup(conn, application_id)
    conn.execute(
        """
        UPDATE follow_ups
        SET eligible = ?, suppressed_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (1 if eligible else 0, reason, now(), followup_id),
    )
    conn.commit()


def save_followup_draft(
    conn: sqlite3.Connection, application_id: int, draft_text: str
) -> None:
    followup_id = get_or_create_followup(conn, application_id)
    ts = now()
    conn.execute(
        """
        UPDATE follow_ups
        SET draft_text = ?, drafted_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (draft_text, ts, ts, followup_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Action Logs
# ---------------------------------------------------------------------------

def log_action(
    conn: sqlite3.Connection,
    run_id: str,
    action_type: str,
    profile_id: int = None,
    job_id: int = None,
    email_id: int = None,
    action_details: Any = None,
    llm_model: str = None,
    llm_provider: str = None,
    llm_input_summary: str = None,
    llm_output: Any = None,
    llm_selected_action: str = None,
    llm_allowed_actions: str = None,
    validation_outcome: str = None,
    tool_executed: str = None,
    tool_result_summary: str = None,
    error: str = None,
    retry_count: int = 0,
) -> int:
    action_details_str = (
        json.dumps(action_details)
        if action_details is not None and not isinstance(action_details, str)
        else action_details
    )
    llm_output_str = (
        json.dumps(llm_output)
        if llm_output is not None and not isinstance(llm_output, str)
        else llm_output
    )
    cur = conn.execute(
        """
        INSERT INTO action_logs (
            run_id, profile_id, job_id, email_id, action_type,
            action_details, llm_model, llm_provider, llm_input_summary,
            llm_output, llm_selected_action, llm_allowed_actions,
            validation_outcome, tool_executed, tool_result_summary,
            error, retry_count, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, profile_id, job_id, email_id, action_type,
            action_details_str, llm_model, llm_provider, llm_input_summary,
            llm_output_str, llm_selected_action, llm_allowed_actions,
            validation_outcome, tool_executed, tool_result_summary,
            error, retry_count, now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Processing Runs
# ---------------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, run_id: str, profile_id: int) -> int:
    ts = now()
    cur = conn.execute(
        """
        INSERT INTO processing_runs (run_id, profile_id, status, started_at)
        VALUES (?,?,'running',?)
        """,
        (run_id, profile_id, ts),
    )
    conn.commit()
    return cur.lastrowid


def update_run_checkpoint(
    conn: sqlite3.Connection, run_id: str, checkpoint: dict
) -> None:
    conn.execute(
        "UPDATE processing_runs SET checkpoint = ? WHERE run_id = ?",
        (json.dumps(checkpoint), run_id),
    )
    conn.commit()


def complete_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str = 'completed',
    emails_scanned: int = 0,
    jobs_found: int = 0,
    jobs_evaluated: int = 0,
) -> None:
    conn.execute(
        """
        UPDATE processing_runs
        SET status = ?, emails_scanned = ?, jobs_found = ?,
            jobs_evaluated = ?, completed_at = ?
        WHERE run_id = ?
        """,
        (status, emails_scanned, jobs_found, jobs_evaluated, now(), run_id),
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM processing_runs WHERE run_id = ?", (run_id,)
    ).fetchone()


def get_last_run(
    conn: sqlite3.Connection, profile_id: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM processing_runs
        WHERE profile_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (profile_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

def create_approval_request(
    conn: sqlite3.Connection,
    action_type: str,
    description: str,
    run_id: str = None,
    profile_id: int = None,
    job_id: int = None,
    evidence: str = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO approval_requests
            (run_id, profile_id, job_id, action_type, description,
             evidence, status, created_at)
        VALUES (?,?,?,?,?,?,'pending',?)
        """,
        (run_id, profile_id, job_id, action_type, description, evidence, now()),
    )
    conn.commit()
    return cur.lastrowid


def update_approval_status(
    conn: sqlite3.Connection,
    approval_id: int,
    status: str,
    decided_by: str = 'user',
) -> None:
    conn.execute(
        """
        UPDATE approval_requests
        SET status = ?, decided_at = ?, decided_by = ?
        WHERE id = ?
        """,
        (status, now(), decided_by, approval_id),
    )
    conn.commit()


def get_pending_approvals(
    conn: sqlite3.Connection, profile_id: int = None
) -> list:
    if profile_id is not None:
        return conn.execute(
            """
            SELECT * FROM approval_requests
            WHERE status = 'pending' AND profile_id = ?
            ORDER BY id
            """,
            (profile_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM approval_requests WHERE status = 'pending' ORDER BY id"
    ).fetchall()
