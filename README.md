# JobLedger

A single-user, persistent-memory job-search agent. It reads job-alert emails (read-only IMAP), fetches
job descriptions safely, scores each job against **your** profile with a hybrid LLM + Python method,
tracks application stages and follow-up eligibility, and keeps an Excel register that is regenerated
from SQLite (the source of truth).

**It never applies to a job, never sends an email, and never changes an application stage by itself.**
Those steps are hand-offs: the agent drafts or proposes, you decide.

> **Read this first: what has and has not been run.**
> Everything was developed and tested **offline on Linux** (fixture emails, an in-memory fake IMAP server,
> saved JD pages, scripted LLM doubles). **Not yet run by anyone:** a real mailbox, a real LLM provider,
> real job sites over the internet, and Windows. You are expected to do those runs; the exact steps are in
> [`docs/DEMO.md`](docs/DEMO.md) and the honest list of gaps is in [`docs/REPORT.md`](docs/REPORT.md).

## Quick start (Windows, PowerShell or cmd)

Python 3.11+ should work; it was developed and tested only on 3.13 (Linux).

```bat
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
copy .env.example .env
py -m jobledger init
py -m jobledger doctor
```

Create your profile and CV (they are stored in SQLite and every later correction keeps its history):

```bat
py -m jobledger profile create --name "Your Name" --email you@example.com --location London ^
   --arrangements hybrid,remote --types fulltime --exclude-companies "Some Recruiter Ltd" --min-salary 50000
py -m jobledger cv --label cv-2026 --skills "Python,Django,PostgreSQL,Docker" --years 5 --file cv.txt
```

Run it **without any mailbox or LLM** on the bundled fixtures (fully deterministic, works offline for emails;
JD pages are fetched from the web unless you use the test suite's fakes):

```bat
py -m jobledger run --fixtures tests\fixtures\emails\batch1_plain.json --no-llm
py -m jobledger jobs list
py -m jobledger export
```

The register is written to `exports\jobledger_register_p<profile id>.xlsx`
(sheets: **Register**, **Conflicts**, **About**). If the file is open in Excel, a new timestamped file is
written instead and the run says so.

### Connect a real mailbox (read-only)

1. Gmail: enable 2-step verification and create an **App Password** (your normal password will not work).
2. Edit `.env`: `IMAP_USER`, `IMAP_PASSWORD`, optionally `IMAP_HOST`/`IMAP_FOLDER`/`EMAIL_FILTER_SUBJECTS`.
3. `py -m jobledger doctor` shows the configuration with secrets redacted (it does not log in).
4. `py -m jobledger run` — the reader opens the folder **read-only** and fetches with `BODY.PEEK`, so
   nothing is marked as read, moved or deleted. It remembers the last UID it fully handled.

### Add an LLM (optional)

Set `LLM_PROVIDER` (`openai | anthropic | groq | gemini | ollama | none`), `LLM_MODEL` and the matching
`*_API_KEY` in `.env`. Without one, every step is deterministic and the logs say
`deterministic` — a supported mode, not an error. With one:

* the LLM **chooses the next action** from a list Python computed; Python validates and executes the choice,
  allows one corrective retry, then falls back to the deterministic pick;
* the LLM **extracts requirements** from a JD with verbatim evidence quotes; Python verifies every quote
  against the JD text and does **all** score arithmetic. The LLM never produces a number that reaches a score.
* 3 consecutive LLM failures open a circuit breaker for the rest of the run.

## Everyday commands

| Command | What it does |
|---|---|
| `py -m jobledger run [--fixtures PATH] [--no-mail] [--no-llm] [--retry]` | one bounded agent run |
| `py -m jobledger jobs list` / `jobs show N` | register in the terminal / one job with its evidence |
| `py -m jobledger jd N --file jd.txt` | supply a JD yourself for a `NEEDS_JD` job |
| `py -m jobledger jd N --clip` | same, but takes the JD from the clipboard (refused unless the title and company appear in it; `--force` overrides) |
| `py -m jobledger jd N --url https://…` | fetch one public job page through the safe fetcher (LinkedIn is refused by policy) |
| `py -m jobledger jd watch` | watch the clipboard; each copied posting is matched to one of your jobs and stored (Ctrl+C to stop) |
| `py -m jobledger prefs set --set excluded_companies="X, Y" --reason "..."` | correct a preference (history kept) |
| `py -m jobledger prefs history` | every preference change, old value → new value, reason |
| `py -m jobledger applied N` / `stage N --to interview --evidence "..."` | **you** record what happened |
| `py -m jobledger approvals list` / `approve ID` / `reject ID` | decide proposals the agent made (decide-once) |
| `py -m jobledger followup check N` / `draft N` / `show N` / `mark-sent N` | eligibility, a **draft** (never sent), record that you sent it |
| `py -m jobledger logs --runs` / `logs --run ID` | episodic memory: every step, the options, the choice, the outcome |
| `py -m jobledger export` | regenerate the Excel register |
| `py -m jobledger eval all` | run the 79 pre-defined checks and write `reports\` |
| `py -m pytest` | unit/integration tests (offline) |

Use `--profile N` when more than one profile exists; every query is scoped to one profile and the database
itself rejects cross-profile rows (triggers).

**Supplying a job description** (exactly one of `--file`, `--text`, `--clip`, `--url` per `jd N` call):

* `jd watch` polls the clipboard. Copying the whole page (including the title line) gives a firm match by title and
  company; copying only the 'About the job' box gives a weaker match, stored as PROVISIONAL. Copying the title line
  and then the description within 2 minutes is matched as one posting. Ambiguous or unmatched text is reported,
  never guessed; repeats are skipped.
* `jd N --clip` stores the clipboard for job N, but only if the posting names that job's title and company.
* `jd N --url URL` fetches one public page with the same SSRF and size limits as the agent, verifies it, and
  marks the job `FAILED` (with the reason) if the fetch fails. LinkedIn URLs are refused.
* A copy under half the length of the stored description (e.g. only the 'About the job' box after the full posting)
  is refused and nothing is stored: `jd watch` counts it as `kept longer`, and `jd N` fails. Copy the full text again,
  or add `--replace` to `jd N` (not `jd watch`) to store it anyway.

## Run the checks

```bat
py -m pytest -q
py -m jobledger eval all --report-dir reports
```

`eval` exits non-zero if any check fails. Expected outcomes live in
[`tests/scenarios/expectations.json`](tests/scenarios/expectations.json) and were written **before** the runner;
one amendment is disclosed in the file and in the report.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `mail_status=auth_failed` | wrong or normal Gmail password — use an App Password; JobLedger makes **one** login attempt and stops |
| `no_mailbox_configured` | no `IMAP_USER`/`IMAP_PASSWORD`; use `run --fixtures` or fill `.env` |
| every JD fails with `unsafe_url … DNS resolution failed` | no direct DNS from this machine (e.g. a mandatory proxy); the SSRF guard resolves hostnames itself and **refuses** unresolvable ones. See limitations |
| register "written to a new file" | the old one is open in Excel; close it |
| a job stays `NEEDS_JD` | no verified description was found — paste one with `jd N --file`; this is intentionally **not** a rejection |
| `database error … locked` | another JobLedger process is using the same DB file (no cross-process lock beyond SQLite's) |
| scores look flat | with no LLM the deterministic extractor only recognises `Requirements: a, b` style lists and bullet sections; unknown ≠ mismatch, it counts as neutral (0.5) and is listed in *Missing info* |

## Layout

```
jobledger/            package (agent.py = action loop, tools/ = the 7 tools, db/ = schema + queries)
tests/                pytest suite, fixtures, scenarios/expectations.json
docs/                 ARCHITECTURE, scoring methodology, REPORT, DEMO
reports/              generated EVALUATION_RESULTS.md + eval_results.json
```

## Safety summary

* IMAP: read-only select, `BODY.PEEK`, no store/expunge/copy/append code paths (the test double raises if called).
* Web: http/https only, ports 80/443, every DNS answer must be globally routable, redirects re-validated
  per hop, size/time/retry bounded, LinkedIn blocked by policy.
* Untrusted text (emails, JDs) is flagged, stripped of instruction-like lines before storage, and wrapped as data in prompts.
* No SMTP/send code exists in the package (a test greps for it). Secrets live only in `.env` (git-ignored) and are redacted from logs.

## AI assistance

This codebase was written by an AI coding assistant (Claude) from a specification supplied by the repository
owner, then exercised with the offline tests described above. It has had **no human code review** and no
live-service run yet. Treat it as a well-tested draft until you have done the runs in `docs/DEMO.md`.
