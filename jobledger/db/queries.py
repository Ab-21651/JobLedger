"""Database query helpers for JobLedger.

Transaction model: the connection is in autocommit mode; every write goes
through ``tx(conn)`` which opens BEGIN IMMEDIATE (or joins an enclosing tx) and
commits/rolls back as a unit. Callers that need several writes to be atomic wrap
them in one ``with tx(conn):`` block.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from jobledger.timeutil import now_iso as now


class ProfileIsolationError(LookupError):
    """Raised when a record is requested under a profile that does not own it."""


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic block. Joins an enclosing transaction if one is already open."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _dumps(value: Any) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, default=str, sort_keys=True)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

# Whitelist: update_profile_field builds SQL from the field name, so it must be validated.
PROFILE_FIELDS = (
    "name", "phone", "location", "work_authorization", "preferred_arrangements",
    "preferred_types", "excluded_companies", "excluded_keywords", "min_salary",
    "currency", "notes",
)
# Fields that count as "preferences" for versioning/evaluation hashing.
PREFERENCE_FIELDS = (
    "location", "work_authorization", "preferred_arrangements", "preferred_types",
    "excluded_companies", "excluded_keywords", "min_salary", "currency",
)


def create_profile(
    conn: sqlite3.Connection, name: str, email: str, phone: str = None,
    location: str = None, work_authorization: str = None,
    preferred_arrangements: str = None, preferred_types: str = None,
    excluded_companies: str = None, excluded_keywords: str = None,
    min_salary: float = None, currency: str = "USD", notes: str = None,
) -> int:
    ts = now()
    with tx(conn):
        cur = conn.execute(
            """INSERT INTO profiles (name, email, phone, location, work_authorization,
                   preferred_arrangements, preferred_types, excluded_companies,
                   excluded_keywords, min_salary, currency, notes, is_active,
                   created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (name, email, phone, location, work_authorization, preferred_arrangements,
             preferred_types, excluded_companies, excluded_keywords, min_salary,
             currency, notes, ts, ts),
        )
    return cur.lastrowid


def get_profile(conn: sqlite3.Connection, profile_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()


def get_profile_by_email(conn: sqlite3.Connection, email: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM profiles WHERE email = ?", (email,)).fetchone()


def list_profiles(conn: sqlite3.Connection) -> list:
    return conn.execute("SELECT * FROM profiles WHERE is_active = 1 ORDER BY id").fetchall()


def update_profile_fields(
    conn: sqlite3.Connection, profile_id: int, changes: dict, reason: str = "",
) -> list[dict]:
    """Atomically apply several field changes, writing one history row per real change.

    Unknown fields are rejected (the name is interpolated into SQL). Setting a
    field to its current value is a no-op and writes no history.
    """
    bad = [f for f in changes if f not in PROFILE_FIELDS]
    if bad:
        raise ValueError(f"Unknown or protected profile field(s): {bad}. Allowed: {list(PROFILE_FIELDS)}")
    applied: list[dict] = []
    ts = now()
    with tx(conn):
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise ProfileIsolationError(f"Profile {profile_id} not found")
        for field, new_value in changes.items():
            old = row[field]
            old_s = None if old is None else str(old)
            new_s = None if new_value is None else str(new_value)
            if old_s == new_s:
                continue
            conn.execute(
                """INSERT INTO preferences_history
                   (profile_id, field_name, old_value, new_value, reason, changed_at)
                   VALUES (?,?,?,?,?,?)""",
                (profile_id, field, old_s, new_s, reason, ts),
            )
            conn.execute(f"UPDATE profiles SET {field} = ?, updated_at = ? WHERE id = ?",
                         (new_value, ts, profile_id))
            applied.append({"field": field, "old": old_s, "new": new_s})
    return applied


def update_profile_field(conn, profile_id: int, field: str, new_value: Any, reason: str = "") -> None:
    update_profile_fields(conn, profile_id, {field: new_value}, reason)


def prefs_version(conn: sqlite3.Connection, profile_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS v FROM preferences_history WHERE profile_id = ?",
        (profile_id,)).fetchone()
    return int(row["v"])


def deactivate_profile(conn: sqlite3.Connection, profile_id: int) -> None:
    with tx(conn):
        conn.execute("UPDATE profiles SET is_active = 0, updated_at = ? WHERE id = ?",
                     (now(), profile_id))


# ---------------------------------------------------------------------------
# CV versions (never overwritten; old versions stay, only is_active flips)
# ---------------------------------------------------------------------------

def add_cv_version(
    conn: sqlite3.Connection, profile_id: int, version_label: str, content_text: str,
    skills: str, experience_years: float = None, education: str = None,
) -> int:
    with tx(conn):
        conn.execute("UPDATE cv_versions SET is_active = 0 WHERE profile_id = ? AND is_active = 1",
                     (profile_id,))
        cur = conn.execute(
            """INSERT INTO cv_versions (profile_id, version_label, content_text, skills,
                   experience_years, education, is_active, created_at)
               VALUES (?,?,?,?,?,?,1,?)""",
            (profile_id, version_label, content_text, skills, experience_years, education, now()),
        )
    return cur.lastrowid


def get_active_cv(conn: sqlite3.Connection, profile_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cv_versions WHERE profile_id = ? AND is_active = 1", (profile_id,)).fetchone()


def list_cv_versions(conn: sqlite3.Connection, profile_id: int) -> list:
    return conn.execute(
        "SELECT * FROM cv_versions WHERE profile_id = ? ORDER BY id DESC", (profile_id,)).fetchall()


# ---------------------------------------------------------------------------
# Emails + mailbox cursor
# ---------------------------------------------------------------------------

def record_email(
    conn: sqlite3.Connection, profile_id: int, message_uid: str, source: str, sender: str,
    subject: str, received_at: str, raw_body_hash: str, job_count: int = 0,
    status: str = "processed", error_msg: str = None,
) -> Optional[int]:
    """INSERT OR IGNORE; returns the row id, or None if this email was already recorded."""
    with tx(conn):
        cur = conn.execute(
            """INSERT OR IGNORE INTO processed_emails
               (profile_id, message_uid, source, sender, subject, received_at, processed_at,
                raw_body_hash, job_count, status, error_msg)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (profile_id, message_uid, source, sender, subject, received_at, now(),
             raw_body_hash, job_count, status, error_msg),
        )
    return cur.lastrowid if cur.rowcount else None


def get_email(conn: sqlite3.Connection, email_id: int, profile_id: int = None) -> Optional[sqlite3.Row]:
    if profile_id is None:
        return conn.execute("SELECT * FROM processed_emails WHERE id = ?", (email_id,)).fetchone()
    return conn.execute("SELECT * FROM processed_emails WHERE id = ? AND profile_id = ?",
                        (email_id, profile_id)).fetchone()


def email_already_processed(conn: sqlite3.Connection, profile_id: int, message_uid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM processed_emails WHERE profile_id = ? AND message_uid = ?",
        (profile_id, message_uid)).fetchone() is not None


def get_mailbox_cursor(conn, profile_id: int, folder: str, uidvalidity: str) -> int:
    row = conn.execute(
        "SELECT last_uid FROM mailbox_state WHERE profile_id=? AND folder=? AND uidvalidity=?",
        (profile_id, folder, str(uidvalidity))).fetchone()
    return int(row["last_uid"]) if row else 0


def set_mailbox_cursor(conn, profile_id: int, folder: str, uidvalidity: str, last_uid: int) -> None:
    """Advance only (never move backwards), so replays can't regress the cursor."""
    with tx(conn):
        conn.execute(
            """INSERT INTO mailbox_state (profile_id, folder, uidvalidity, last_uid, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(profile_id, folder, uidvalidity)
               DO UPDATE SET last_uid = MAX(last_uid, excluded.last_uid), updated_at = excluded.updated_at""",
            (profile_id, folder, str(uidvalidity), int(last_uid), now()))


# ---------------------------------------------------------------------------
# Normalisation helpers (dedup keys)
# ---------------------------------------------------------------------------

_CORP_SUFFIX = re.compile(r"\b(ltd|limited|inc|incorporated|llc|plc|gmbh|corp|corporation|co|company|pvt|llp)\b\.?")
_TRACKING_PARAMS = re.compile(r"^(utm_.*|ref|refid|trk|trackingid|tracking|gclid|fbclid|mc_.*|source|src|from|campaign|cid|eid|midtoken|midsig|trkemail|lipi|licu|otptoken|rcm|alid)$", re.I)


def norm_text(value: Optional[str]) -> str:
    s = (value or "").lower()
    s = re.sub(r"[^a-z0-9+#]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_company(value: Optional[str]) -> str:
    s = _CORP_SUFFIX.sub(" ", (value or "").lower().replace("&", " and "))
    return norm_text(s)


def is_remote_location(value: Optional[str]) -> bool:
    return bool(value) and bool(re.search(r"\bremote\b|\bwork from home\b|\banywhere\b", value, re.I))


def norm_city(value: Optional[str]) -> str:
    """City part of a location ("London (Hybrid)" and "London, UK" -> "london"); '' for unknown or remote-only."""
    if not value or is_remote_location(value):
        return ""
    city = re.sub(r"\([^)]*\)", " ", value.split(",")[0])
    city = re.sub(r"\b(hybrid|on[- ]?site|in[- ]office)\b", " ", city, flags=re.I)
    return norm_text(city)


def tc_key_for(title: str, company: str) -> str:
    return hashlib.sha256(f"{norm_text(title)}|{norm_company(company)}".encode()).hexdigest()[:24]


def compute_stable_id(title: str, company: str, location: str = None) -> str:
    """Identity of a job posting: normalised title + company + city (not tracking URLs)."""
    raw = f"{norm_text(title)}|{norm_company(company)}|{norm_city(location)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonicalize_url(url: Optional[str]) -> Optional[str]:
    """Drop fragments and tracking params; lowercase scheme/host. None for unusable input."""
    if not url:
        return None
    try:
        p = urlparse(url.strip())
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING_PARAMS.match(k)]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(query), ""))


def _num_differs(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) > 0.5


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _record_conflict(conn, job_id, profile_id, field, val_a, src_a, val_b, src_b) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO job_conflicts
           (job_id, profile_id, field_name, value_a, source_a, value_b, source_b, detected_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (job_id, profile_id, field, str(val_a), src_a, str(val_b), src_b, now()))
    return cur.rowcount > 0


def _mark_uncertain(conn, job_id: int, field: str) -> None:
    row = conn.execute("SELECT uncertain_fields FROM jobs WHERE id=?", (job_id,)).fetchone()
    fields = set(json.loads(row["uncertain_fields"]) if row and row["uncertain_fields"] else [])
    if field not in fields:
        fields.add(field)
        conn.execute("UPDATE jobs SET uncertain_fields=?, updated_at=? WHERE id=?",
                     (json.dumps(sorted(fields)), now(), job_id))


def upsert_job(
    conn: sqlite3.Connection, profile_id: int, source_email_id: Optional[int], title: str,
    company: str, location: str = None, arrangement: str = None, employment_type: str = None,
    apply_url: str = None, salary_min: float = None, salary_max: float = None,
    salary_currency: str = None, salary_raw: str = None, description_snippet: str = None,
    received_at: str = None, source_label: str = None,
) -> tuple:
    """Insert or merge a job. Returns (job_id, is_new).

    The same posting arriving from another alert/source is MERGED (same normalised
    title+company, compatible city). Every sighting is stored in job_sources; fields
    that disagree are stored in job_conflicts and flagged uncertain, never overwritten.
    """
    if not title or not company:
        raise ValueError("title and company are required to store a job")
    ts = now()
    tc = tc_key_for(title, company)
    city = norm_city(location)
    canon = canonicalize_url(apply_url)
    src_label = source_label or (f"email:{source_email_id}" if source_email_id else "manual")

    with tx(conn):
        existing = None
        for cand in conn.execute("SELECT * FROM jobs WHERE profile_id=? AND tc_key=? ORDER BY id",
                                 (profile_id, tc)).fetchall():
            cc = norm_city(cand["location"])
            if cc == city or not cc or not city:
                existing = cand
                break

        if existing is None:
            stable = compute_stable_id(title, company, location)
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs (profile_id, source_email_id, stable_id, tc_key, title,
                       company, location, arrangement, employment_type, apply_url, salary_min,
                       salary_max, salary_currency, salary_raw, description_snippet, jd_status,
                       received_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',?,?,?)""",
                (profile_id, source_email_id, stable, tc, title, company, location, arrangement,
                 employment_type, apply_url, salary_min, salary_max, salary_currency, salary_raw,
                 description_snippet, received_at, ts, ts))
            is_new = cur.rowcount == 1
            job_id = conn.execute("SELECT id FROM jobs WHERE profile_id=? AND stable_id=?",
                                  (profile_id, stable)).fetchone()["id"]
        else:
            is_new = False
            job_id = existing["id"]
            _merge_sighting(conn, existing, profile_id, src_label, location, arrangement,
                            employment_type, salary_min, salary_max, salary_currency, salary_raw,
                            apply_url, description_snippet)

        conn.execute(
            """INSERT OR IGNORE INTO job_sources
               (job_id, profile_id, email_id, source_label, url, canonical_url, title_seen,
                company_seen, location_seen, arrangement_seen, type_seen, salary_seen, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, profile_id, source_email_id, src_label, apply_url, canon or "", title, company,
             location, arrangement, employment_type, salary_raw, ts))
    return (job_id, is_new)


def _merge_sighting(conn, existing, profile_id, src_label, location, arrangement, employment_type,
                    salary_min, salary_max, salary_currency, salary_raw, apply_url, snippet) -> None:
    job_id = existing["id"]
    src_a = "first_seen"
    updates: dict[str, Any] = {}

    # fill gaps (unknown -> known) without touching existing known values
    for col, new in (("location", location), ("arrangement", arrangement),
                     ("employment_type", employment_type), ("apply_url", apply_url),
                     ("salary_currency", salary_currency), ("salary_raw", salary_raw),
                     ("description_snippet", snippet)):
        if new and not existing[col]:
            updates[col] = new
    if existing["salary_min"] is None and salary_min is not None:
        updates["salary_min"] = salary_min
    if existing["salary_max"] is None and salary_max is not None:
        updates["salary_max"] = salary_max

    # disagreements are preserved, not resolved
    for col, new in (("arrangement", arrangement), ("employment_type", employment_type)):
        old = existing[col]
        if old and new and old.lower() != new.lower():
            _record_conflict(conn, job_id, profile_id, col, old, src_a, new, src_label)
            _mark_uncertain(conn, job_id, col)
    if _num_differs(existing["salary_min"], salary_min) or _num_differs(existing["salary_max"], salary_max):
        _record_conflict(conn, job_id, profile_id, "salary", existing["salary_raw"], src_a, salary_raw, src_label)
        _mark_uncertain(conn, job_id, "salary")
    old_loc = existing["location"]
    if old_loc and location and (is_remote_location(old_loc) != is_remote_location(location)):
        _record_conflict(conn, job_id, profile_id, "location", old_loc, src_a, location, src_label)
        _mark_uncertain(conn, job_id, "location")

    if updates:
        sets = ", ".join(f"{c} = ?" for c in updates)
        conn.execute(f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?",
                     (*updates.values(), now(), job_id))


def get_job(conn: sqlite3.Connection, job_id: int, profile_id: int = None) -> Optional[sqlite3.Row]:
    """Fetch a job. Pass profile_id to enforce ownership (returns None for other profiles' jobs)."""
    if profile_id is None:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return conn.execute("SELECT * FROM jobs WHERE id = ? AND profile_id = ?",
                        (job_id, profile_id)).fetchone()


def require_job(conn: sqlite3.Connection, job_id: int, profile_id: int) -> sqlite3.Row:
    row = get_job(conn, job_id, profile_id)
    if row is None:
        raise ProfileIsolationError(f"Job {job_id} not found for profile {profile_id}")
    return row


def list_jobs(conn: sqlite3.Connection, profile_id: int, stage: str = None, limit: int = 500,
              workflow_status: str = None) -> list:
    sql = "SELECT j.* FROM jobs j"
    where, params = ["j.profile_id = ?"], [profile_id]
    if stage:
        sql += " JOIN applications a ON a.job_id = j.id"
        where.append("a.stage = ?")
        params.append(stage)
    if workflow_status:
        where.append("j.workflow_status = ?")
        params.append(workflow_status)
    sql += " WHERE " + " AND ".join(where) + " ORDER BY j.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def update_job_jd_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    with tx(conn):
        conn.execute("UPDATE jobs SET jd_status = ?, updated_at = ? WHERE id = ?", (status, now(), job_id))


def set_job_workflow(conn: sqlite3.Connection, job_id: int, status: str, reason: str = None) -> None:
    with tx(conn):
        conn.execute("UPDATE jobs SET workflow_status=?, workflow_reason=?, updated_at=? WHERE id=?",
                     (status, reason, now(), job_id))


def list_job_sources(conn, job_id: int) -> list:
    return conn.execute("SELECT * FROM job_sources WHERE job_id=? ORDER BY id", (job_id,)).fetchall()


def list_job_conflicts(conn, job_id: int, only_open: bool = True) -> list:
    sql = "SELECT * FROM job_conflicts WHERE job_id=?" + (" AND status='open'" if only_open else "") + " ORDER BY id"
    return conn.execute(sql, (job_id,)).fetchall()


# ---------------------------------------------------------------------------
# JD snapshots
# ---------------------------------------------------------------------------

def save_jd_snapshot(
    conn: sqlite3.Connection, job_id: int, source_url: str, final_url: str, status: str,
    retrieved_at: str = None, failure_reason: str = None, content_text: str = None,
    title_found: str = None, company_found: str = None, verification_status: str = None,
    verification_evidence: str = None, response_bytes: int = None, retry_count: int = 0,
    source_kind: str = "url", injection_flags: Any = None,
) -> int:
    content_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest() if content_text else None
    with tx(conn):
        cur = conn.execute(
            """INSERT INTO jd_snapshots (job_id, source_url, final_url, retrieved_at, status,
                   failure_reason, content_text, content_hash, title_found, company_found,
                   verification_status, verification_evidence, response_bytes, retry_count,
                   source_kind, injection_flags)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, source_url, final_url, retrieved_at or now(), status, failure_reason,
             content_text, content_hash, title_found, company_found, verification_status,
             verification_evidence, response_bytes, retry_count, source_kind, _dumps(injection_flags)))
    return cur.lastrowid


def get_latest_jd_snapshot(conn, job_id: int, usable_only: bool = False) -> Optional[sqlite3.Row]:
    """Latest snapshot. usable_only restricts to ok snapshots with verified/partial/email text."""
    sql = "SELECT * FROM jd_snapshots WHERE job_id = ?"
    if usable_only:
        sql += " AND status='ok' AND verification_status IN ('verified','partial','email_inline')"
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()


def count_jd_attempts(conn, job_id: int, kind: str = "url") -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM jd_snapshots WHERE job_id=? AND source_kind=?",
                        (job_id, kind)).fetchone()["n"]


def list_jd_snapshots(conn, job_id: int) -> list:
    return conn.execute("SELECT * FROM jd_snapshots WHERE job_id=? ORDER BY id", (job_id,)).fetchall()


# ---------------------------------------------------------------------------
# Evaluations (history is append-only; identical inputs are idempotent)
# ---------------------------------------------------------------------------

def save_evaluation(
    conn: sqlite3.Connection, job_id: int, profile_id: int, cv_version_id: Optional[int],
    hard_filter_pass: Optional[int], hard_filter_details: Any, skill_score: float,
    experience_score: float, arrangement_score: float, type_score: float,
    location_score: float, total_score: float, score_breakdown: Any, llm_assessment: Any,
    llm_model: str, llm_provider: str, match_rationale: str, missing_info: Any,
    eval_version: str = "v1", input_hash: str = "", prefs_version: int = 0,
    status: str = "scored", scoring_mode: str = "llm",
) -> tuple[int, bool]:
    """Append an evaluation. Returns (evaluation_id, inserted). Same inputs -> no new row."""
    with tx(conn):
        cur = conn.execute(
            """INSERT OR IGNORE INTO evaluations (job_id, profile_id, cv_version_id, eval_version,
                   input_hash, prefs_version, status, scoring_mode, hard_filter_pass,
                   hard_filter_details, skill_score, experience_score, arrangement_score,
                   type_score, location_score, total_score, score_breakdown, llm_assessment,
                   llm_model, llm_provider, match_rationale, missing_info, evaluated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, profile_id, cv_version_id, eval_version, input_hash, prefs_version, status,
             scoring_mode, hard_filter_pass, _dumps(hard_filter_details), skill_score,
             experience_score, arrangement_score, type_score, location_score, total_score,
             _dumps(score_breakdown), _dumps(llm_assessment), llm_model, llm_provider,
             match_rationale, _dumps(missing_info), now()))
        if cur.rowcount:
            return cur.lastrowid, True
        row = conn.execute(
            "SELECT id FROM evaluations WHERE job_id=? AND profile_id=? AND eval_version=? AND input_hash=?",
            (job_id, profile_id, eval_version, input_hash)).fetchone()
    return row["id"], False


def get_latest_evaluation(conn, job_id: int, profile_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM evaluations WHERE job_id=? AND profile_id=? ORDER BY id DESC LIMIT 1",
        (job_id, profile_id)).fetchone()


def evaluation_exists(conn, job_id: int, profile_id: int, eval_version: str, input_hash: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM evaluations WHERE job_id=? AND profile_id=? AND eval_version=? AND input_hash=?",
        (job_id, profile_id, eval_version, input_hash)).fetchone() is not None


def list_evaluations(conn, job_id: int, profile_id: int) -> list:
    return conn.execute("SELECT * FROM evaluations WHERE job_id=? AND profile_id=? ORDER BY id",
                        (job_id, profile_id)).fetchall()


# ---------------------------------------------------------------------------
# Applications + stage history
# ---------------------------------------------------------------------------

VALID_STAGES = ["saved", "shortlisted", "applied", "replied", "interview", "rejected", "withdrawn", "closed"]


def get_or_create_application(conn: sqlite3.Connection, job_id: int, profile_id: int) -> tuple:
    """Return (application_id, current_stage). Enforces that the profile owns the job."""
    with tx(conn):
        require_job(conn, job_id, profile_id)
        row = conn.execute("SELECT id, stage FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        if row:
            return (row["id"], row["stage"])
        ts = now()
        cur = conn.execute(
            "INSERT INTO applications (job_id, profile_id, stage, created_at, updated_at) VALUES (?,?,?,?,?)",
            (job_id, profile_id, "saved", ts, ts))
        conn.execute(
            """INSERT INTO application_stage_history
               (application_id, from_stage, to_stage, reason, evidence, changed_at, changed_by)
               VALUES (?,?,?,?,?,?,?)""",
            (cur.lastrowid, None, "saved", "application record created", "", ts, "system"))
    return (cur.lastrowid, "saved")


def update_application_stage(
    conn: sqlite3.Connection, application_id: int, new_stage: str, reason: str = "",
    evidence: str = "", changed_by: str = "user",
) -> None:
    """Record history and move the stage. applied_at is set once, on the first move to 'applied'."""
    if new_stage not in VALID_STAGES:
        raise ValueError(f"Invalid stage '{new_stage}'")
    ts = now()
    with tx(conn):
        row = conn.execute("SELECT stage, applied_at FROM applications WHERE id = ?",
                           (application_id,)).fetchone()
        if row is None:
            raise LookupError(f"Application {application_id} not found")
        conn.execute(
            """INSERT INTO application_stage_history
               (application_id, from_stage, to_stage, reason, evidence, changed_at, changed_by)
               VALUES (?,?,?,?,?,?,?)""",
            (application_id, row["stage"], new_stage, reason, evidence, ts, changed_by))
        applied_at = row["applied_at"] or (ts if new_stage == "applied" else None)
        last_reply_sql = ", last_reply_at = ?" if new_stage in ("replied", "interview") else ""
        params = [new_stage, ts, applied_at] + ([ts] if last_reply_sql else []) + [application_id]
        conn.execute(
            f"UPDATE applications SET stage = ?, updated_at = ?, applied_at = ?{last_reply_sql} WHERE id = ?",
            params)


def get_application(conn, application_id: int, profile_id: int = None) -> Optional[sqlite3.Row]:
    if profile_id is None:
        return conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
    return conn.execute("SELECT * FROM applications WHERE id = ? AND profile_id = ?",
                        (application_id, profile_id)).fetchone()


def get_application_by_job(conn, job_id: int, profile_id: int = None) -> Optional[sqlite3.Row]:
    if profile_id is None:
        return conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    return conn.execute("SELECT * FROM applications WHERE job_id = ? AND profile_id = ?",
                        (job_id, profile_id)).fetchone()


def list_stage_history(conn, application_id: int) -> list:
    return conn.execute("SELECT * FROM application_stage_history WHERE application_id=? ORDER BY id",
                        (application_id,)).fetchall()


def list_applications(conn, profile_id: int) -> list:
    return conn.execute("SELECT * FROM applications WHERE profile_id=? ORDER BY id", (profile_id,)).fetchall()


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

def get_or_create_followup(conn: sqlite3.Connection, application_id: int) -> int:
    with tx(conn):
        row = conn.execute("SELECT id FROM follow_ups WHERE application_id = ?", (application_id,)).fetchone()
        if row:
            return row["id"]
        ts = now()
        cur = conn.execute(
            "INSERT INTO follow_ups (application_id, eligible, created_at, updated_at) VALUES (?,1,?,?)",
            (application_id, ts, ts))
    return cur.lastrowid


def update_followup_eligibility(conn, application_id: int, eligible: bool, reason: str,
                                due_at: str = None) -> None:
    with tx(conn):
        fid = get_or_create_followup(conn, application_id)
        conn.execute(
            """UPDATE follow_ups SET eligible = ?, suppressed_reason = ?, due_at = ?,
                   last_checked_at = ?, updated_at = ? WHERE id = ?""",
            (1 if eligible else 0, None if eligible else reason, due_at, now(), now(), fid))


def get_followup(conn, application_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM follow_ups WHERE application_id=?", (application_id,)).fetchone()


def save_followup_draft(conn, application_id: int, draft_text: str) -> None:
    with tx(conn):
        fid = get_or_create_followup(conn, application_id)
        ts = now()
        conn.execute("UPDATE follow_ups SET draft_text=?, drafted_at=?, updated_at=? WHERE id=?",
                     (draft_text, ts, ts, fid))


# ---------------------------------------------------------------------------
# Action logs (episodic memory)
# ---------------------------------------------------------------------------

def log_action(
    conn: sqlite3.Connection, run_id: str, action_type: str, profile_id: int = None,
    job_id: int = None, email_id: int = None, action_details: Any = None, llm_model: str = None,
    llm_provider: str = None, llm_input_summary: Any = None, llm_output: Any = None,
    llm_selected_action: str = None, llm_allowed_actions: Any = None,
    validation_outcome: str = None, tool_executed: str = None, tool_result_summary: Any = None,
    error: str = None, retry_count: int = 0, step_no: int = None, next_state: str = None,
    llm_evidence_refs: Any = None, llm_reasoning: str = None,
) -> int:
    with tx(conn):
        cur = conn.execute(
            """INSERT INTO action_logs (run_id, profile_id, job_id, email_id, action_type,
                   action_details, llm_model, llm_provider, llm_input_summary, llm_output,
                   llm_selected_action, llm_allowed_actions, validation_outcome, tool_executed,
                   tool_result_summary, error, retry_count, created_at, step_no, next_state,
                   llm_evidence_refs, llm_reasoning)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, profile_id, job_id, email_id, action_type, _dumps(action_details), llm_model,
             llm_provider, _dumps(llm_input_summary), _dumps(llm_output), llm_selected_action,
             _dumps(llm_allowed_actions), validation_outcome, tool_executed,
             _dumps(tool_result_summary), error, retry_count, now(), step_no, next_state,
             _dumps(llm_evidence_refs), llm_reasoning))
    return cur.lastrowid


def list_action_logs(conn, profile_id: int = None, run_id: str = None, job_id: int = None,
                     action_type: str = None, limit: int = 100) -> list:
    where, params = [], []
    for col, val in (("profile_id", profile_id), ("run_id", run_id), ("job_id", job_id),
                     ("action_type", action_type)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    sql = "SELECT * FROM action_logs" + (" WHERE " + " AND ".join(where) if where else "")
    return conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()


# ---------------------------------------------------------------------------
# Processing runs / checkpoints
# ---------------------------------------------------------------------------

def start_run(conn, run_id: str, profile_id: int, mode: str = None) -> int:
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO processing_runs (run_id, profile_id, status, started_at, mode) VALUES (?,?,'running',?,?)",
            (run_id, profile_id, now(), mode))
    return cur.lastrowid


def update_run_checkpoint(conn, run_id: str, checkpoint: dict) -> None:
    with tx(conn):
        conn.execute("UPDATE processing_runs SET checkpoint = ? WHERE run_id = ?",
                     (json.dumps(checkpoint, default=str, sort_keys=True), run_id))


def complete_run(conn, run_id: str, status: str = "completed", emails_scanned: int = 0,
                 jobs_found: int = 0, jobs_evaluated: int = 0, error: str = None) -> None:
    with tx(conn):
        conn.execute(
            """UPDATE processing_runs SET status=?, emails_scanned=?, jobs_found=?,
                   jobs_evaluated=?, completed_at=?, error=? WHERE run_id=?""",
            (status, emails_scanned, jobs_found, jobs_evaluated, now(), error, run_id))


def get_run(conn, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM processing_runs WHERE run_id = ?", (run_id,)).fetchone()


def get_last_run(conn, profile_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM processing_runs WHERE profile_id = ? ORDER BY id DESC LIMIT 1",
                        (profile_id,)).fetchone()


def list_runs(conn, profile_id: int, limit: int = 20) -> list:
    return conn.execute("SELECT * FROM processing_runs WHERE profile_id=? ORDER BY id DESC LIMIT ?",
                        (profile_id, limit)).fetchall()


def interrupted_runs(conn, profile_id: int) -> list:
    """Runs left in 'running' state - i.e. the process died before completing them."""
    return conn.execute("SELECT * FROM processing_runs WHERE profile_id=? AND status='running' ORDER BY id",
                        (profile_id,)).fetchall()


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

def create_approval_request(
    conn: sqlite3.Connection, action_type: str, description: str, run_id: str = None,
    profile_id: int = None, job_id: int = None, evidence: Any = None, payload: Any = None,
    application_id: int = None, requires_approval: bool = True,
) -> int:
    with tx(conn):
        cur = conn.execute(
            """INSERT INTO approval_requests (run_id, profile_id, job_id, application_id, action_type,
                   description, evidence, payload, requires_approval, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)""",
            (run_id, profile_id, job_id, application_id, action_type, description,
             _dumps(evidence), _dumps(payload), 1 if requires_approval else 0, now()))
    return cur.lastrowid


def get_approval(conn, approval_id: int, profile_id: int = None) -> Optional[sqlite3.Row]:
    if profile_id is None:
        return conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    return conn.execute("SELECT * FROM approval_requests WHERE id=? AND profile_id=?",
                        (approval_id, profile_id)).fetchone()


def update_approval_status(conn, approval_id: int, status: str, decided_by: str = "user") -> bool:
    """Decide a PENDING request exactly once. Returns False if it was already decided."""
    with tx(conn):
        cur = conn.execute(
            """UPDATE approval_requests SET status=?, decided_at=?, decided_by=?
               WHERE id=? AND status='pending'""",
            (status, now(), decided_by, approval_id))
    return cur.rowcount == 1


def mark_approval_executed(conn, approval_id: int, result: Any) -> None:
    with tx(conn):
        conn.execute("UPDATE approval_requests SET executed_at=?, result=? WHERE id=?",
                     (now(), _dumps(result), approval_id))


def get_pending_approvals(conn, profile_id: int = None) -> list:
    if profile_id is not None:
        return conn.execute(
            "SELECT * FROM approval_requests WHERE status='pending' AND profile_id=? ORDER BY id",
            (profile_id,)).fetchall()
    return conn.execute("SELECT * FROM approval_requests WHERE status='pending' ORDER BY id").fetchall()


def list_approvals(conn, profile_id: int, limit: int = 50) -> list:
    return conn.execute("SELECT * FROM approval_requests WHERE profile_id=? ORDER BY id DESC LIMIT ?",
                        (profile_id, limit)).fetchall()


# ---------------------------------------------------------------------------
# Additions used by the agent controller
# ---------------------------------------------------------------------------

def get_last_mailbox_state(conn, profile_id: int, folder: str):
    """Most recently updated (uidvalidity, last_uid) row for a folder, or None."""
    return conn.execute(
        "SELECT uidvalidity, last_uid FROM mailbox_state WHERE profile_id=? AND folder=? "
        "ORDER BY updated_at DESC, rowid DESC LIMIT 1", (profile_id, folder)).fetchone()


def set_application_reply(conn, application_id: int, replied_at: str) -> None:
    """Record that a reply email was SEEN (a fact). This is not a stage change."""
    with tx(conn):
        conn.execute("UPDATE applications SET last_reply_at = ? WHERE id = ? AND (last_reply_at IS NULL OR last_reply_at < ?)",
                     (replied_at, application_id, replied_at))


def mark_followup_sent(conn, application_id: int, sent_at: str = None) -> None:
    """Record that the USER sent a follow-up themselves. JobLedger never sends anything."""
    with tx(conn):
        fid = get_or_create_followup(conn, application_id)
        ts = sent_at or now()
        conn.execute("UPDATE follow_ups SET sent_at=?, updated_at=? WHERE id=?", (ts, now(), fid))


# ---------------------------------------------------------------------------
# Place distances (global: a fact about two places, not profile data). Keys are norm_city() values.
# ---------------------------------------------------------------------------

def get_place_distance(conn: sqlite3.Connection, a: str, b: str) -> Optional[float]:
    """Cached km between two city keys, looked up in both directions. None when not cached."""
    row = conn.execute(
        """SELECT km FROM place_distances
           WHERE (from_place = ? AND to_place = ?) OR (from_place = ? AND to_place = ?)
           ORDER BY id LIMIT 1""",
        (a, b, b, a)).fetchone()
    return float(row["km"]) if row else None


def save_place_distance(conn: sqlite3.Connection, a: str, b: str, km: float, source: str,
                        model: Optional[str] = None, note: Optional[str] = None) -> bool:
    """Cache one estimate. INSERT OR IGNORE: the first stored value for a pair is kept. True if inserted."""
    if not a or not b:
        raise ValueError("place keys must be non-empty normalised city names")
    with tx(conn):
        cur = conn.execute(
            """INSERT OR IGNORE INTO place_distances (from_place, to_place, km, source, model, note, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (a, b, float(km), source, model, note, now()))
    return cur.rowcount > 0
