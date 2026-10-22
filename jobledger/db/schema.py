"""SQLite schema migration module for JobLedger."""

import sqlite3

SCHEMA_VERSION = 1

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

MIGRATIONS: list[tuple[int, str]] = [
    (1, _MIGRATION_1_SQL),
]


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode, foreign keys, and Row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply any outstanding schema migrations in order."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        )
        """
    )
    conn.commit()

    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current_version: int = row["version"] if row else 0

    for version, sql in MIGRATIONS:
        if version <= current_version:
            continue
        with conn:
            conn.executescript(sql)
            if row is None and version == 1:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            else:
                conn.execute("UPDATE schema_version SET version = ?", (version,))
            row = True  # mark that a row now exists


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialise the database, run migrations, and return the connection."""
    conn = get_connection(db_path)
    apply_migrations(conn)
    return conn
