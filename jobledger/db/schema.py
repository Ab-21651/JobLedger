"""SQLite schema + migrations for JobLedger.

Migrations are lists of single statements applied inside ONE transaction each
(SQLite DDL is transactional), so a failed migration leaves the DB untouched.

Memory map (see docs/ARCHITECTURE.md):
  long-term  : profiles, cv_versions, preferences_history, jobs, job_sources,
               jd_snapshots, evaluations, applications, application_stage_history,
               follow_ups, mailbox_state, llm_cache
  episodic   : action_logs (decisions, tool results, corrections, errors, retries)
  working    : in-process AgentContext, serialised into processing_runs.checkpoint
"""
from __future__ import annotations

import sqlite3

_MIGRATION_1_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    email                 TEXT NOT NULL UNIQUE,
    phone                 TEXT,
    location              TEXT,
    work_authorization    TEXT,
    preferred_arrangements TEXT,
    preferred_types       TEXT,
    excluded_companies    TEXT,
    excluded_keywords     TEXT,
    min_salary            REAL,
    currency              TEXT DEFAULT 'USD',
    notes                 TEXT,
    is_active             INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cv_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id       INTEGER NOT NULL REFERENCES profiles(id),
    version_label    TEXT NOT NULL,
    content_text     TEXT NOT NULL,
    skills           TEXT NOT NULL,
    experience_years REAL,
    education        TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    UNIQUE(profile_id, version_label)
);

CREATE TABLE IF NOT EXISTS preferences_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    field_name TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    reason     TEXT,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_emails (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    INTEGER NOT NULL REFERENCES profiles(id),
    message_uid   TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'imap',
    sender        TEXT,
    subject       TEXT,
    received_at   TEXT,
    processed_at  TEXT NOT NULL,
    raw_body_hash TEXT,
    job_count     INTEGER DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'processed',
    error_msg     TEXT,
    UNIQUE(profile_id, message_uid)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          INTEGER NOT NULL REFERENCES profiles(id),
    source_email_id     INTEGER REFERENCES processed_emails(id),
    stable_id           TEXT NOT NULL,
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    location            TEXT,
    arrangement         TEXT,
    employment_type     TEXT,
    apply_url           TEXT,
    salary_min          REAL,
    salary_max          REAL,
    salary_currency     TEXT,
    salary_raw          TEXT,
    description_snippet TEXT,
    jd_status           TEXT NOT NULL DEFAULT 'PENDING',
    received_at         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(profile_id, stable_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_profile_id ON jobs(profile_id);
CREATE INDEX IF NOT EXISTS idx_jobs_jd_status  ON jobs(jd_status);

CREATE TABLE IF NOT EXISTS jd_snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id               INTEGER NOT NULL REFERENCES jobs(id),
    source_url           TEXT NOT NULL,
    final_url            TEXT,
    retrieved_at         TEXT NOT NULL,
    status               TEXT NOT NULL,
    failure_reason       TEXT,
    content_text         TEXT,
    content_hash         TEXT,
    title_found          TEXT,
    company_found        TEXT,
    verification_status  TEXT,
    verification_evidence TEXT,
    response_bytes       INTEGER,
    retry_count          INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jd_snapshots_job_id ON jd_snapshots(job_id);

CREATE TABLE IF NOT EXISTS evaluations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER NOT NULL REFERENCES jobs(id),
    profile_id          INTEGER NOT NULL REFERENCES profiles(id),
    cv_version_id       INTEGER REFERENCES cv_versions(id),
    eval_version        TEXT NOT NULL DEFAULT 'v1',
    hard_filter_pass    INTEGER,
    hard_filter_details TEXT,
    skill_score         REAL,
    experience_score    REAL,
    arrangement_score   REAL,
    type_score          REAL,
    location_score      REAL,
    total_score         REAL,
    score_breakdown     TEXT,
    llm_assessment      TEXT,
    llm_model           TEXT,
    llm_provider        TEXT,
    match_rationale     TEXT,
    missing_info        TEXT,
    label_expected      INTEGER,
    label_rationale     TEXT,
    evaluated_at        TEXT NOT NULL,
    UNIQUE(job_id, profile_id, eval_version)
);

CREATE INDEX IF NOT EXISTS idx_evaluations_profile_id ON evaluations(profile_id);

CREATE TABLE IF NOT EXISTS applications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) UNIQUE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    stage      TEXT NOT NULL DEFAULT 'saved',
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_applications_profile_id ON applications(profile_id);

CREATE TABLE IF NOT EXISTS application_stage_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    from_stage     TEXT,
    to_stage       TEXT NOT NULL,
    reason         TEXT,
    evidence       TEXT,
    changed_at     TEXT NOT NULL,
    changed_by     TEXT NOT NULL DEFAULT 'user'
);

CREATE INDEX IF NOT EXISTS idx_stage_history_application_id ON application_stage_history(application_id);

CREATE TABLE IF NOT EXISTS follow_ups (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id   INTEGER NOT NULL REFERENCES applications(id),
    eligible         INTEGER NOT NULL DEFAULT 1,
    suppressed_reason TEXT,
    due_at           TEXT,
    drafted_at       TEXT,
    draft_text       TEXT,
    sent_at          TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    profile_id          INTEGER,
    job_id              INTEGER,
    email_id            INTEGER,
    action_type         TEXT NOT NULL,
    action_details      TEXT,
    llm_model           TEXT,
    llm_provider        TEXT,
    llm_input_summary   TEXT,
    llm_output          TEXT,
    llm_selected_action TEXT,
    llm_allowed_actions TEXT,
    validation_outcome  TEXT,
    tool_executed       TEXT,
    tool_result_summary TEXT,
    error               TEXT,
    retry_count         INTEGER DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_logs_run_id     ON action_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_action_logs_profile_id ON action_logs(profile_id);

CREATE TABLE IF NOT EXISTS processing_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL UNIQUE,
    profile_id      INTEGER,
    status          TEXT NOT NULL DEFAULT 'running',
    checkpoint      TEXT,
    emails_scanned  INTEGER DEFAULT 0,
    jobs_found      INTEGER DEFAULT 0,
    jobs_evaluated  INTEGER DEFAULT 0,
    started_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS approval_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    profile_id  INTEGER,
    job_id      INTEGER,
    action_type TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    decided_at  TEXT,
    decided_by  TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_status ON approval_requests(status);
"""

# Migration 2: history-preserving evaluations, cross-source dedup + conflicts,
# approval payloads, mailbox state, and DB-level profile-isolation triggers.
_MIGRATION_2_STATEMENTS = [
    # --- evaluations: keep EVERY evaluation (history) instead of replacing ---
    """CREATE TABLE evaluations_v2 (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id              INTEGER NOT NULL REFERENCES jobs(id),
        profile_id          INTEGER NOT NULL REFERENCES profiles(id),
        cv_version_id       INTEGER REFERENCES cv_versions(id),
        eval_version        TEXT NOT NULL DEFAULT 'v1',
        input_hash          TEXT NOT NULL DEFAULT '',
        prefs_version       INTEGER NOT NULL DEFAULT 0,
        status              TEXT NOT NULL DEFAULT 'scored',
        scoring_mode        TEXT NOT NULL DEFAULT 'llm',
        hard_filter_pass    INTEGER,
        hard_filter_details TEXT,
        skill_score         REAL,
        experience_score    REAL,
        arrangement_score   REAL,
        type_score          REAL,
        location_score      REAL,
        total_score         REAL,
        score_breakdown     TEXT,
        llm_assessment      TEXT,
        llm_model           TEXT,
        llm_provider        TEXT,
        match_rationale     TEXT,
        missing_info        TEXT,
        label_expected      INTEGER,
        label_rationale     TEXT,
        evaluated_at        TEXT NOT NULL,
        UNIQUE(job_id, profile_id, eval_version, input_hash)
    )""",
    """INSERT INTO evaluations_v2 (
        id, job_id, profile_id, cv_version_id, eval_version, hard_filter_pass,
        hard_filter_details, skill_score, experience_score, arrangement_score,
        type_score, location_score, total_score, score_breakdown, llm_assessment,
        llm_model, llm_provider, match_rationale, missing_info, label_expected,
        label_rationale, evaluated_at)
       SELECT id, job_id, profile_id, cv_version_id, eval_version, hard_filter_pass,
        hard_filter_details, skill_score, experience_score, arrangement_score,
        type_score, location_score, total_score, score_breakdown, llm_assessment,
        llm_model, llm_provider, match_rationale, missing_info, label_expected,
        label_rationale, evaluated_at FROM evaluations""",
    "DROP TABLE evaluations",
    "ALTER TABLE evaluations_v2 RENAME TO evaluations",
    "CREATE INDEX idx_evaluations_profile_id ON evaluations(profile_id)",
    "CREATE INDEX idx_evaluations_job_profile ON evaluations(job_id, profile_id, id)",

    # --- jobs: workflow state, dedup key, uncertainty ---
    "ALTER TABLE jobs ADD COLUMN tc_key TEXT",
    "ALTER TABLE jobs ADD COLUMN workflow_status TEXT NOT NULL DEFAULT 'new'",
    "ALTER TABLE jobs ADD COLUMN workflow_reason TEXT",
    "ALTER TABLE jobs ADD COLUMN uncertain_fields TEXT",
    "CREATE INDEX idx_jobs_tc_key ON jobs(profile_id, tc_key)",

    # --- every place a job was seen (different alert sources keep their own evidence) ---
    """CREATE TABLE job_sources (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id        INTEGER NOT NULL REFERENCES jobs(id),
        profile_id    INTEGER NOT NULL REFERENCES profiles(id),
        email_id      INTEGER REFERENCES processed_emails(id),
        source_label  TEXT,
        url           TEXT,
        canonical_url TEXT,
        title_seen    TEXT,
        company_seen  TEXT,
        location_seen TEXT,
        arrangement_seen TEXT,
        type_seen     TEXT,
        salary_seen   TEXT,
        seen_at       TEXT NOT NULL,
        UNIQUE(job_id, email_id, canonical_url)
    )""",
    "CREATE INDEX idx_job_sources_job ON job_sources(job_id)",

    # --- conflicting evidence is preserved, never silently resolved ---
    """CREATE TABLE job_conflicts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id          INTEGER NOT NULL REFERENCES jobs(id),
        profile_id      INTEGER NOT NULL REFERENCES profiles(id),
        field_name      TEXT NOT NULL,
        value_a         TEXT,
        source_a        TEXT,
        value_b         TEXT,
        source_b        TEXT,
        detected_at     TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'open',
        resolution      TEXT,
        UNIQUE(job_id, field_name, value_a, value_b)
    )""",

    # --- jd snapshots: where did this text come from ---
    "ALTER TABLE jd_snapshots ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'url'",
    "ALTER TABLE jd_snapshots ADD COLUMN injection_flags TEXT",

    # --- mailbox cursor: last-seen UID per (profile, folder, uidvalidity) ---
    """CREATE TABLE mailbox_state (
        profile_id  INTEGER NOT NULL REFERENCES profiles(id),
        folder      TEXT NOT NULL,
        uidvalidity TEXT NOT NULL,
        last_uid    INTEGER NOT NULL DEFAULT 0,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (profile_id, folder, uidvalidity)
    )""",

    # --- approvals carry the exact action to run once approved ---
    "ALTER TABLE approval_requests ADD COLUMN payload TEXT",
    "ALTER TABLE approval_requests ADD COLUMN application_id INTEGER",
    "ALTER TABLE approval_requests ADD COLUMN executed_at TEXT",
    "ALTER TABLE approval_requests ADD COLUMN result TEXT",
    "ALTER TABLE approval_requests ADD COLUMN requires_approval INTEGER NOT NULL DEFAULT 1",

    # --- follow-ups: one row per application ---
    "CREATE UNIQUE INDEX idx_follow_ups_application ON follow_ups(application_id)",
    "ALTER TABLE follow_ups ADD COLUMN last_checked_at TEXT",

    # --- applications: recruiter reply date feeds follow-up rules ---
    "ALTER TABLE applications ADD COLUMN last_reply_at TEXT",

    # --- action logs (episodic memory) gain step + next-state columns ---
    "ALTER TABLE action_logs ADD COLUMN step_no INTEGER",
    "ALTER TABLE action_logs ADD COLUMN next_state TEXT",
    "ALTER TABLE action_logs ADD COLUMN llm_evidence_refs TEXT",
    "ALTER TABLE action_logs ADD COLUMN llm_reasoning TEXT",
    "CREATE INDEX idx_action_logs_job ON action_logs(job_id)",

    # --- processing runs: mode + error text ---
    "ALTER TABLE processing_runs ADD COLUMN mode TEXT",
    "ALTER TABLE processing_runs ADD COLUMN error TEXT",

    # --- DB-level profile isolation: rows must agree on who owns the job ---
    """CREATE TRIGGER trg_applications_profile_match
       BEFORE INSERT ON applications
       WHEN (SELECT profile_id FROM jobs WHERE id = NEW.job_id) IS NOT NEW.profile_id
       BEGIN SELECT RAISE(ABORT, 'profile isolation: application profile does not own job'); END""",
    """CREATE TRIGGER trg_evaluations_profile_match
       BEFORE INSERT ON evaluations
       WHEN (SELECT profile_id FROM jobs WHERE id = NEW.job_id) IS NOT NEW.profile_id
       BEGIN SELECT RAISE(ABORT, 'profile isolation: evaluation profile does not own job'); END""",
    """CREATE TRIGGER trg_job_sources_profile_match
       BEFORE INSERT ON job_sources
       WHEN (SELECT profile_id FROM jobs WHERE id = NEW.job_id) IS NOT NEW.profile_id
       BEGIN SELECT RAISE(ABORT, 'profile isolation: source profile does not own job'); END""",
    """CREATE TRIGGER trg_job_conflicts_profile_match
       BEFORE INSERT ON job_conflicts
       WHEN (SELECT profile_id FROM jobs WHERE id = NEW.job_id) IS NOT NEW.profile_id
       BEGIN SELECT RAISE(ABORT, 'profile isolation: conflict profile does not own job'); END""",
    """CREATE TRIGGER trg_jobs_email_profile_match
       BEFORE INSERT ON jobs
       WHEN NEW.source_email_id IS NOT NULL
        AND (SELECT profile_id FROM processed_emails WHERE id = NEW.source_email_id) IS NOT NEW.profile_id
       BEGIN SELECT RAISE(ABORT, 'profile isolation: email belongs to another profile'); END""",
]


def _split_statements(script: str) -> list[str]:
    """Split a multi-statement script on complete statements (trigger-safe)."""
    statements, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements


# --- v3: evaluation history is append-only, enforced by the database and not only by convention ---
_MIGRATION_3_STATEMENTS = [
    """CREATE TRIGGER trg_evaluations_no_update
       BEFORE UPDATE ON evaluations
       BEGIN SELECT RAISE(ABORT, 'evaluations are append-only: insert a new evaluation instead'); END""",
    """CREATE TRIGGER trg_evaluations_no_delete
       BEFORE DELETE ON evaluations
       BEGIN SELECT RAISE(ABORT, 'evaluations are append-only: history is never deleted'); END""",
]

# --- v4: LLM-estimated distance between two places. A geographic fact, not profile data, so it is global ---
_MIGRATION_4_STATEMENTS = [
    """CREATE TABLE place_distances (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        from_place  TEXT NOT NULL,
        to_place    TEXT NOT NULL,
        km          REAL NOT NULL,
        source      TEXT NOT NULL,
        model       TEXT,
        note        TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE(from_place, to_place)
    )""",
]

# --- v5: validated LLM results (requirement extraction, skill coverage) cached per profile. Only Python-validated
# replies are stored; the key carries the inputs, prompt version, provider and model (see matcher.py). ---
_MIGRATION_5_STATEMENTS = [
    """CREATE TABLE llm_cache (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id  INTEGER NOT NULL REFERENCES profiles(id),
        kind        TEXT NOT NULL,
        cache_key   TEXT NOT NULL,
        payload     TEXT NOT NULL,
        provider    TEXT,
        model       TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE(profile_id, kind, cache_key)
    )""",
]

MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, _split_statements(_MIGRATION_1_SQL)),
    (2, _MIGRATION_2_STATEMENTS),
    (3, _MIGRATION_3_STATEMENTS),
    (4, _MIGRATION_4_STATEMENTS),
    (5, _MIGRATION_5_STATEMENTS),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open SQLite in autocommit mode (we issue explicit BEGIN/COMMIT via tx())."""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply outstanding migrations, each atomically. Returns the final version."""
    version_now = current_version(conn)
    for version, statements in MIGRATIONS:
        if version <= version_now:
            continue
        # foreign keys must be off for table rebuilds
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                bad = conn.execute("PRAGMA foreign_key_check").fetchall()
                if bad:
                    raise sqlite3.IntegrityError(f"foreign_key_check failed after migration {version}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        version_now = version
    return version_now


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialise the database, run migrations, and return the connection."""
    conn = get_connection(db_path)
    apply_migrations(conn)
    return conn
