# Architecture

```
            ┌────────────── CLI (cli.py) ──────────────┐
            │ profile · cv · prefs · run · jobs · stage │
            └───────────────────┬───────────────────────┘
                                │
                   ┌────────────▼─────────────┐
                   │   JobLedgerAgent.run()   │  bounded action loop (agent.py)
                   └─┬──────────┬───────────┬─┘
        LLM (optional)│          │           │ Python (always)
   chooses ONE action │          │           └ computes allowed actions, validates, executes, logs
                      ▼          ▼
   tools/: email_reader · email_parser · jd_retriever · matcher · stage_manager · approval_manager · excel_exporter
                                │
                     SQLite (db/schema.py, db/queries.py)  ← source of truth
                                │
                     Excel register (generated view)
```

## The action loop (a genuine, bounded agent loop)

Each step:

1. **Python computes the allowed actions** from database state: `read_mailbox`, `process_email`, `fetch_jd`,
   `fetch_careers_page`, `evaluate_job`, `mark_needs_jd`, `check_followups`, `export_register`, `finish`
   — each with exact arguments (a `fetch_jd` option carries only a `job_id`; the URL comes from the DB).
2. One option → executed directly (`single_option`, no LLM call). No LLM or breaker open → deterministic pick
   (fixed priority order). Otherwise the LLM receives the options plus an evidence summary (untrusted text
   wrapped and instruction-like lines withheld) and must answer
   `{"action", "args", "evidence_refs", "reasoning_summary"}`.
3. Python **validates**: known action, currently allowed, args identical to an offered option, evidence refs
   exist. A rejected answer gets **one** corrective retry, then Python picks the deterministic option.
4. Python executes the tool, logs everything to `action_logs`, saves a checkpoint to `processing_runs`.
5. Hard caps: max steps per run, ≤ 2 failures per (action, args), circuit breaker after 3 consecutive LLM
   failures (auth errors open it immediately).

Run statuses: `completed`, `completed_with_errors`, `failed` (e.g. database write failure), `stopped_max_steps`.

Actions the LLM can **never** reach, because they are not in the action set: applying, sending email, changing
a stage, approving, editing preferences. `send_follow_up` / `apply_to_job` exist only as approval-gate action
types that hand work back to you.

## Memory

| Kind | Where | Contents |
|---|---|---|
| Working | `AgentContext` (process memory) | current step, pending emails, per-run failure counts, breaker state. **Lost on crash, rebuilt from the database** |
| Long-term | SQLite | profile, CV versions, preferences + history, jobs, sources, conflicts, JD snapshots, evaluations, applications, follow-ups, mailbox cursor, approvals |
| Episodic | `action_logs`, `processing_runs`, stage/preference/evaluation history | what the agent saw, was allowed, chose, and what happened (`jobledger logs`) |

## Data integrity

* Autocommit connection + explicit `tx()` (BEGIN IMMEDIATE; nested use joins the outer transaction), WAL, FKs on.
* Profile isolation: every query takes a profile id; `ProfileIsolationError` in Python **and** `BEFORE INSERT`
  triggers (applications, evaluations, job_sources, job_conflicts, jobs↔emails) in SQLite.
* Evaluations append-only (UPDATE/DELETE triggers). Preference corrections keep history with reason.
* Duplicates: `(profile, message uid)` for emails; normalised title+company+city for jobs across sources; every
  sighting kept in `job_sources`; disagreeing values kept in `job_conflicts`, never overwritten.

## Crash recovery

Checkpoints are written after each step; every fault point (`mail_read`, `email_parsed_before_persist`,
`email_persisted_before_checkpoint`, `jd_fetched_before_persist`, `evaluated_before_checkpoint`,
`step_logged_before_checkpoint`, `before_export`) is exercised: crash, restart, and the end state must equal an
uninterrupted run. The mailbox cursor only advances after the messages it covers are committed. Runs left in
`running` by a dead process are marked `interrupted` on the next start. Three scenarios kill a **real child
process** (`os._exit(137)`); the rest use an in-process `SimulatedCrash`.
