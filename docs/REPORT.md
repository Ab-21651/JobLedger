# JobLedger – implementation report

Date: 2026-10-22. Environment of every run reported here: Python 3.13, Linux sandbox, **offline** (no mailbox,
no LLM provider, no DNS/internet for job sites).

## 1. Verdict in one paragraph

All required functionality is implemented and passes its **offline** tests: `pytest` 127 passed, and the
scenario runner 79 of 79 pre-defined checks (`reports/EVALUATION_RESULTS.md`). Nothing has been run against a
real mailbox, a real LLM, real job sites or Windows. Those four are the things you still need to test; the
steps are in `docs/DEMO.md`. "Passes offline" is evidence the logic is right, not evidence the integrations work.

## 2. What was run, exactly

| Run | Result |
|---|---|
| `python -m pytest tests -q` | 127 passed |
| `python -m jobledger eval all` | 79/79 (scenarios S01–S10: 64 checks, memory M1–M5: 5, labeled LS.*: 5, failures F01–F05: 5) |
| Real child-process crashes (`os._exit(137)`) | S03 a, b, e |
| In-process crash at all 7 fault points, then restart == uninterrupted baseline | F01 and `test_agent.py` |
| Real loopback HTTP server through the actual `requests` stack | one test (validator contract; see limits) |
| CLI end-to-end in a scratch directory (init → profile → cv → run → jd → run → prefs → applied → followup → logs) | worked; see DEMO |
| Live public fetch via the real retriever | **failed to run**: the sandbox has no DNS, the SSRF guard refused (`unsafe_url … DNS resolution failed`). Not evidence for or against live behaviour |

## 3. Requirements → where they are

| Requirement | Implementation | Evidence |
|---|---|---|
| Real read-only IMAP | `tools/email_reader.py` (readonly select, `BODY.PEEK`, UID search, cursor) | FakeIMAP records commands and raises on any write verb; S03, S07 auth, `test_imap.py` |
| JD retrieval + SSRF protection | `tools/jd_retriever.py` | 17-case URL table, per-hop redirect validation, size/type/time/retry bounds, S07, S08 |
| Hybrid scoring, LLM never does arithmetic | `tools/matcher.py` | evidence-quote verification, malformed/injected output rejected, `test_matcher.py` |
| Stage tracking, follow-up eligibility | `tools/stage_manager.py` | S10, `test_db_stage_approval.py` |
| Approval gate, no auto apply/send | `tools/approval_manager.py`, agent action set | S01/S08/S10, grep test: no SMTP/send code |
| Excel register from SQLite | `tools/excel_exporter.py` | formula-injection prefix, locked-file fallback, per-profile file, `test_excel.py`, F05 |
| Bounded LLM action loop | `agent.py` | S08 (6 distinct bad proposals rejected), F02, F03, `test_agent.py` |
| Working / long-term / episodic memory | see `ARCHITECTURE.md` | M1–M5 |
| Preference corrections with history, profile isolation | `db/queries.py`, DB triggers | S04, S05 |
| Retries, checkpoints, restart recovery | `agent.py`, `processing_runs` | S03, S07, F01 |
| Untrusted content, conflict preservation | parser/retriever/agent, `job_conflicts` | S06, S08, S09 |

## 4. Memory ON vs OFF (5 comparisons, one honestly "same")

| Test | Memory ON | Memory OFF | Different? |
|---|---|---|---|
| M1 saved exclusion rule | ShadyStaffing excluded, JD never fetched | scored 100.0, JD fetched | yes |
| M2 corrected preference | score 93.0 → 87.0, old evaluation + history kept | stays 93.0, no history | yes |
| M3 recruiter reply | follow-up suppressed (`recruiter_replied`) | eligible (would nag a recruiter who replied) | yes |
| **M4 first-time evaluation of Acme** | 100.0 | 100.0 | **same – memory adds nothing here** |
| M5 re-delivered email | 0 new jobs | 4 duplicate jobs | yes |

## 5. Labeled 10-job set (diagnostic only, weights not tuned)

Labels were written before scoring (`tests/fixtures/labeled_jobs.json`).

| Job | Label | Result |
|---|---|---|
| L01 Acme backend | good | 100.0 |
| L02 DataWorks senior | good | 93.0 |
| L03 PixelForge React | poor | **60.0** |
| L04 FinServe contract | poor | 68.5 |
| L05 Insightly junior | poor | excluded |
| L06 CloudNine devops | borderline | 80.0 |
| L07 ShadyStaffing | poor | excluded (excluded company, perfect skills) |
| L08 Brightside full-stack | borderline | 80.0 |
| L09 NeuralWorks ML | poor | 66.0 |
| L10 Quill and Co | unknown JD | needs_jd (no score) |

Both good jobs outrank every scored poor job, but **the margin for L03 is uncomfortable**: a job whose required
skills do not match the CV at all still scores 60 because location, type and arrangement fit. A skills gate
would fix that; it was deliberately not added after seeing this result (that would be tuning against the
labeled set). Your call; see `docs/scoring_methodology.md`.
Borderline jobs are reported, not counted right or wrong. With 10 jobs this is a sanity check, not a measure of accuracy.

## 6. Defects found by running it (all fixed at the root cause, none by weakening a check)

1. `norm_city` did not strip "(Hybrid)" → same posting from two alerts was not merged.
2. The JD verifier used the *claimed URL* as evidence of the right company → a page for another company passed.
3. The matcher accepted `{"score":100}` as an "empty extraction" → treated as a provisional result; now rejected.
4. After a restart, the agent looked at the wrong snapshot kind and re-fetched URL/careers pages (duplicate snapshots).
5. Evaluations were append-only only by convention → added schema v3 triggers (UPDATE/DELETE aborted).
6. After the LLM breaker opened, every job's requirement extraction still made a doomed LLM call → extraction is now skipped once the breaker is open.
7. `init` printed `schema v0` (read the wrong version source).

Plus several runner/test mistakes of mine (None-guards, evaluation ordering, wrong snapshot kind in assertions).

## 7. Disclosures about the checks themselves

* **Provenance of expectations.** `tests/scenarios/expectations.json` was written before the runner was first
  executed, in the same working session; nothing was committed before that, so git cannot prove the ordering —
  take it as my statement, not as tamper-proof evidence.
* **One amendment**, recorded in the file (`_amendments`) and here: `S08.score_not_forced` originally required
  "total is not 100". That was a specification error: the Evilcorp posting is genuinely a perfect match, so 100 is
  correct with or without the injected text. The check now requires injected == injection-free control and adds a
  poor-fit variant that must stay below 100. Stronger, not weaker, but still a change after seeing a failure.
* **S06.email_vs_jd_flag**: in the fixture the arrangement flag for DataWorks comes from two *emails* disagreeing
  (hybrid vs on-site). A pure email-vs-JD conflict is covered separately in `test_matcher.py`.
* Run 1 of the runner passed 69/79; the failures above were real. The 79/79 you see is the final state.

## 8. Limitations / not verified / risks

**Not run at all:** real IMAP server (Gmail quirks: label folders, rate limits, app-password flow), any real LLM
provider (HTTP clients exist but have never spoken to a real endpoint; JSON-mode support differs by provider),
real job sites, Windows (paths, console encoding, Excel file locking behave differently; the locked-file test
patches `os.replace`).

**Known design limits**

* DNS-rebinding TOCTOU: the URL is validated, then `requests` resolves again. Closing it needs connect-time IP pinning.
* The SSRF guard resolves hostnames itself and refuses unresolvable ones; behind a mandatory proxy with no local
  DNS every fetch fails closed (seen in the sandbox).
* The email parser is heuristic: it recognises numbered/labelled blocks and common layouts, not arbitrary HTML
  newsletters, and requires a minimum of structure (title + company + one more signal). Unrecognised alerts yield
  zero jobs plus a warning rather than guesses.
* The deterministic JD extractor only understands `Requirements: a, b` sentences and bulleted sections; with
  no LLM many real JDs will have an unknown skill component (neutral 0.5, `provisional`).
* The 1–2–4 s back-off and 3 retries are unit-tested with an injected sleeper, never against real latency.
* No cross-process locking beyond SQLite's own; two simultaneous runs on one DB file can hit "database is locked".
* LinkedIn is blocked by policy; JS-rendered career pages are not rendered (no headless browser).
* Weights are a-priori judgement calls, not validated against real outcomes.
* Only one CV is active per profile; no multi-language JD handling; salary comparison is skipped across currencies.
* The CLI leaves SQLite connections to the garbage collector; harmless for a one-shot process.

## 9. AI-assistance note

Written by Claude (Anthropic) from the owner's specification, in one extended session, including this report.
No human reviewed the code. Everything that sounds like "verified" above means "exercised by the offline tests listed".
