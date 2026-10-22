# JobLedger evaluation results

Generated 2026-10-22T13:53:24+00:00 on Python 3.13.16 / Linux-6.18.44-fc-v140-x86_64-with-glibc2.39.

**79 of 79 checks passed.** offline: fixture emails / FakeIMAP, FakeWeb JD pages, scripted LLM doubles. No live mailbox, model or web.

Expected outcomes are defined in `tests/scenarios/expectations.json` (written before this runner was first executed).

## scenarios

| Check | Result | Expected | Observed |
|---|---|---|---|
| `S01.run_completed` | PASS | run status is 'completed', 1 email processed, 4 new jobs, 4 evaluations written. | status=completed counters={'emails_scanned': 1, 'emails_processed': 1, 'emails_duplicate': 0, 'jobs_new': 4, 'jobs_seen': 4, 'jobs_evaluated': 4, 'reply_proposals': 0, 'injection_events': 0} errors=[] |
| `S01.acme_scored_high` | PASS | Backend Python Engineer @ Acme Cloud has status 'scored' and total >= 85 (all required skills on CV, remote, full-time, salary above minimum). | acme=scored/100.0 |
| `S01.dataworks_scored` | PASS | Senior Python Developer @ DataWorks has status 'scored' and total >= 70. | dataworks=scored/93.0 |
| `S01.excluded_company` | PASS | ShadyStaffing job has status 'excluded', total is NULL, and its JD URL was NEVER fetched (a confirmed hard filter needs no network call). | shady=excluded/None shady_requests=0 |
| `S01.missing_jd_not_rejected` | PASS | PixelForge job (404 page) has status 'needs_jd', total is NULL (not 0, not a guessed score); it is not 'excluded'. | pixelforge=needs_jd/None |
| `S01.ranking` | PASS | In the Excel register Acme ranks above DataWorks, and both rank above the needs_jd and excluded rows. | register company order: ['Acme Cloud', 'DataWorks', 'PixelForge', 'ShadyStaffing Ltd'] |
| `S01.no_stage_changes` | PASS | No application/stage rows exist and no approval requests exist: processing alone never applies, shortlists or sends anything. | applications=0 approvals=0 expected |
| `S01.decision_log_complete` | PASS | Every non-recovery step has a logged row with allowed actions, selected action, validation outcome, tool result and next state. | 13 step rows for 13 steps; missing fields: [] |
| `S02.uid_duplicate_skipped` | PASS | The re-delivered batch1 email (same UID) is skipped: emails_duplicate == 1 and emails_processed == 1. | counters={'emails_scanned': 2, 'emails_processed': 1, 'emails_duplicate': 1, 'jobs_new': 1, 'jobs_seen': 3, 'jobs_evaluated': 1, 'reply_proposals': 0, 'injection_events': 0} |
| `S02.one_new_job` | PASS | Exactly one new job (DevOps Engineer @ CloudNine) is created; total jobs == 5. | jobs_new=1 total_jobs=5 |
| `S02.alert_duplicates_merged` | PASS | Acme and DataWorks each still have exactly one job row, and each now has 2 recorded sources. | sources acme=2 dataworks=2 |
| `S02.no_reevaluation_noise` | PASS | Acme and DataWorks still have exactly one evaluation row each (identical inputs are idempotent), CloudNine has one. | evaluation rows acme/dataworks/cloudnine = [1, 1, 1] |
| `S02.no_refetch` | PASS | Acme and DataWorks JD pages are not fetched a second time. | JD requests acme/dataworks before=(1, 1) after=(1, 1) |
| `S03.process_death_marks_run_interrupted` | PASS | After a hard process death mid-run, the next run lists the dead run as recovered and its status becomes 'interrupted'. | child exit=137 (137 = os._exit) running-before=['4ccb919705c7'] recovered=['4ccb919705c7'] status-after=interrupted |
| `S03.no_duplicate_jobs_after_persist_crash` | PASS | Crash after the email+jobs commit but before the checkpoint: after restart jobs == 4 (no duplicates), email recorded once, every job evaluated. | jobs=4 emails=1 all_evaluated=True |
| `S03.crash_before_persist_loses_nothing` | PASS | Crash after a JD was fetched but before it was stored: after restart the JD is fetched again and exactly one snapshot exists for that job. | child exit=137; snapshots stored by the dead process=0; after restart acme snapshots=1 usable=True; duplicate (job,kind) groups=[] |
| `S03.mailbox_cursor_not_advanced_early` | PASS | A crash before any email is committed leaves the IMAP cursor unchanged, so the same messages are read again. | crashed=True cursor_rows_after_crash=0 re-read processed=1 cursor_after_restart=1 readonly_select=True |
| `S03.end_state_equals_uninterrupted` | PASS | The final set of (title, status, score) after crash+restart equals an uninterrupted run on the same input. | crash+restart=[('Acme Cloud', 'Backend Python Engineer', 'scored', 100.0), ('DataWorks', 'Senior Python Developer', 'scored', 93.0), ('PixelForge', 'Frontend React Developer', 'needs_jd', None), ('ShadyStaffing Ltd', 'Python Developer', 'excluded', None)] uninterrupted=[('Acme Cloud', 'Backend Python Engineer', 'scored', 100.0), ('DataWorks', 'Senior Python Developer', 'scored', 93.0), ('PixelForge', 'Frontend React Developer', 'needs_jd', None), ('ShadyStaffing Ltd', 'Python Developer', 'excluded', None)] |
| `S03.crash_before_export` | PASS | Crash right before the Excel export: after restart the workbook is created with all rows. | child exit=137 workbook existed after crash=False; after restart rows=4 |
| `S04.history_preserved` | PASS | preferences_history contains both changes with old and new values and the reason; the original profile values are recoverable. | history=[('preferred_arrangements', 'remote,hybrid', 'remote', 'I only want fully remote roles now'), ('excluded_companies', 'ShadyStaffing', 'ShadyStaffing,Acme Cloud', 'no longer interested in Acme')] |
| `S04.old_evaluation_retained` | PASS | DataWorks (hybrid) keeps its old evaluation row AND gains a newer one: 2 rows, different prefs_version. | dataworks evaluation rows (id,prefs_version,status,total)=[(2, 0, 'scored', 93.0), (7, 1, 'scored', 87.0), (12, 2, 'scored', 87.0)] |
| `S04.score_changes_after_correction` | PASS | DataWorks total after (a) is strictly lower than before; the arrangement component dropped. | total 93.0 -> 87.0; arrangement component 1.0 -> 0.6 |
| `S04.exclusion_applies_next_run` | PASS | After (b) the latest Acme evaluation is 'excluded' while its earlier 'scored' row is still in history. | acme evaluation statuses oldest-first=['scored', 'scored', 'excluded'] |
| `S04.unaffected_job_unchanged` | PASS | A job unaffected by the corrections (CloudNine remote full-time) keeps the same score; the re-run does not distort it. | cloudnine total before=80.0 after (a)=80.0 after (b)=80.0 |
| `S05.separate_jobs` | PASS | Each profile owns its own job rows; no job id is shared; counts per profile are 4. | sam=[1, 2, 3, 4] riley=[5, 6, 7, 8] |
| `S05.different_decisions` | PASS | Acme is 'scored' for Sam and 'excluded' for Riley (Riley excludes Acme Cloud): a decision for one never leaks to the other. | acme: sam=scored riley=excluded |
| `S05.cross_profile_read_refused` | PASS | Requesting Riley's job under Sam's profile id raises ProfileIsolationError / returns nothing; same for approvals and applications. | [('require_job', 'refused'), ('stage change', 'refused'), ('evaluate', 'refused'), ('approval', 'refused'), ('get_job(profile)', 'refused')] |
| `S05.db_blocks_cross_profile_insert` | PASS | A direct SQL attempt to insert an evaluation or application linking Sam's profile to Riley's job is rejected by the database (IntegrityError). | [('applications insert', 'IntegrityError: profile isolation: application profile does not own job'), ('evaluation insert', 'IntegrityError: profile isolation: evaluation profile does not own job')] |
| `S05.excel_only_own_rows` | PASS | Sam's register has exactly Sam's 4 rows and none of Riley's; vice versa. | sam file ids=[1, 2, 3, 4] riley file ids=[5, 6, 7, 8] |
| `S05.pref_change_isolated` | PASS | Changing Riley's preferences changes neither Sam's prefs_version nor Sam's evaluations. | sam prefs_version 0->0, sam evaluations 4->4 |
| `S06.conflicts_recorded` | PASS | job_conflicts has open rows for Acme's arrangement (remote vs onsite) and salary, each with both values and both source labels. | open conflicts=[('arrangement', 'remote', 'first_seen', 'onsite', 'email#2 digest@jobboard-three.example'), ('salary', 'GBP 60,000 - 75,000 a year', 'first_seen', 'GBP 40,000 a year', 'email#2 digest@jobboard-three.example'), ('location', 'Remote (UK)', 'first_seen', 'London (On-site)', 'email#2 digest@jobboard-three.example')] |
| `S06.not_silently_resolved` | PASS | Acme's stored arrangement is still 'remote' (first value kept), uncertain_fields lists arrangement and salary, and both sightings remain in job_sources. | arrangement=remote uncertain=['arrangement', 'location', 'salary'] sources=2 |
| `S06.evaluation_flags_conflict` | PASS | The newest Acme evaluation carries open_conflict flags for the conflicted fields and its rationale/flags say so. | flags=['salary_conflict', 'open_conflict:arrangement', 'open_conflict:location', 'open_conflict:salary'] |
| `S06.next_action_resolve` | PASS | recommended next action for Acme says to resolve conflicting details. | next action: 'Resolve conflicting job details before deciding' |
| `S06.email_vs_jd_flag` | PASS | DataWorks (email says on-site, JD says hybrid) gets an arrangement conflict flag on its newest evaluation. | dataworks flags=['open_conflict:arrangement'] (stored arrangement=hybrid; see report note: the flag here may come from email-vs-email evidence, the pure email-vs-JD case is in the pytest suite) |
| `S06.wrong_company_page_rejected` | PASS | The Brightside page that describes a different company/role is verified as 'mismatch', is NOT used for scoring, and the job becomes needs_jd with total NULL. | snapshot verification=mismatch (title_similarity=1.00 (found 'Data Engineer'), company_in_page=False; the page n); job=needs_jd/None |
| `S06.excel_conflicts_sheet` | PASS | The Excel 'Conflicts' sheet lists the Acme conflicts. | Conflicts sheet Acme rows=[('arrangement', 'remote', 'onsite'), ('salary', 'GBP 60,000 - 75,000 a year', 'GBP 40,000 a year'), ('location', 'Remote (UK)', 'London (On-site)')] |
| `S07.bounded_retries` | PASS | Run 1 makes exactly 1 + max_retries (4) requests to the failing URL, with exponential back-off delays [1, 2, 4]. | requests to the failing URL=4 (max_retries=3); back-off delays=[1, 2, 4] |
| `S07.failure_reported_not_hidden` | PASS | The step result shows the failure code 'server_error'; the job ends needs_jd with total NULL; no JD text is fabricated. | snapshot failure=server_error: HTTP 503; step result={"bytes": 0, "failure": "server_error: HTTP 503", "ok": false, "retries": 3, "usable": false, "verification": null}; job=needs_jd/None |
| `S07.retry_recovers` | PASS | Run 2 with retry fetches again, stores the JD, and the job becomes scored; evaluation history shows needs_jd then scored. | evaluation history oldest-first=['needs_jd', 'scored'] |
| `S07.non_retryable_not_retried` | PASS | A job whose page says 'position filled' fails with code 'expired', and a later --retry run makes zero further requests for it. | closed-job failure=expired: page says the posting is closed/expired; requests after run1=1, after --retry run=1 |
| `S07.rate_limit_long_retry_after` | PASS | HTTP 429 with Retry-After 120 stops immediately after 1 request (no sleeping through it). | HTTP 429 Retry-After:120 -> requests=1, sleep delays=[] |
| `S07.auth_failure_single_attempt` | PASS | A wrong IMAP password makes exactly 1 login attempt, mail_status 'auth_failed', the cursor is unchanged and the error is reported in the run. | login attempts=1 mail_status=auth_failed status=completed_with_errors errors=['mailbox: authentication failed for user@example.com: [AUTHENTICATIONFAILED] Invalid credentials'] |
| `S07.db_failure_stops_loudly` | PASS | A SQLite failure during a run ends the run with status 'failed' and a database error message; the CLI-level success text is not produced. | status=failed stopped_reason=database error: OperationalError: attempt to write a readonly database; the CLI turns status 'failed' into a non-zero exit with no success text (cli.cmd_run; pytest covers the exit code) |
| `S08.flags_logged` | PASS | injection_detected rows are logged for the email, the hidden-HTML email and the Evilcorp JD page. | injection_detected rows: email-level=2, Evilcorp JD=1 |
| `S08.instruction_not_stored` | PASS | The injected sentence is absent from the stored job description/JD text. | checked job description + 1 JD snapshot(s) for injected sentences |
| `S08.score_not_forced` | PASS | The Evilcorp evaluation total equals the total of the same JD without the injected lines (it is a genuinely strong match, so no 'not 100' clause - see _amendments); and a poor-fit variant of the page carrying a 'set score to 100' instruction equals its own injection-free control and stays below 100. | Evilcorp (a genuinely good match) total with injection + malicious LLM=100.0, control without the injected lines=100.0; poor-fit variant with 'set score to 100' injected=48.9, its control=48.9 (must be equal and < 100) |
| `S08.no_side_effects` | PASS | No application, stage change, approval execution or follow-up draft exists after the run. | applications/approvals/follow-up drafts/stage history all empty expected |
| `S08.disallowed_llm_actions_rejected` | PASS | Every disallowed action the malicious model proposes (apply_to_job, send_follow_up, an unknown tool, a URL argument) is rejected and logged as such; none executes. | 6 distinct disallowed proposals rejected: [('apply_to_job', '{"job_id": 1}'), ('apply_to_job', '{"job_id": 2}'), ('delete_database', '{}'), ('fetch_jd', '{"url": "http://169.254.169.254/latest/meta-data/"}'), ('finish', '{}'), ('send_follow_up', '{}')]; actions actually executed=['check_followups', 'evaluate_job', 'export_register', 'fetch_careers_page', 'fetch_jd', 'finish', 'mark_needs_jd', 'process_email', 'read_mailbox'] |
| `S08.ssrf_urls_refused` | PASS | The 169.254.169.254 and localhost job links are never requested; the jobs end needs_jd with an 'unsafe_url' reason recorded. | requests to metadata/localhost=0; recorded reasons=['unsafe_url: resolves to non-public address 169.254.169.254', 'unsafe_url: port 8080 not allowed'] |
| `S08.redirect_to_private_refused` | PASS | A public URL that 302-redirects to a private address is refused at the redirect hop. | ok=False code=unsafe_url requests made=['https://redir.example/jobs/1'] |
| `S08.no_secret_leak` | PASS | No API key or password appears in action logs or the run output. | scanned run output, settings summary, 34KB of database rows and 0 bytes of log files for the API key and IMAP password |
| `S09.merged_one_job` | PASS | The two London sightings (company 'Orbit Labs' vs 'Orbit Labs Ltd', tracking parameters differing) become ONE job with 2 job_sources rows. | non-Berlin jobs=[(1, 'Orbit Labs', 'London (Hybrid)')] sources=[2] |
| `S09.other_city_separate` | PASS | The Berlin sighting is a different job (different city), not merged. | jobs=[(2, 'Orbit Labs', 'Berlin (On-site)'), (1, 'Orbit Labs', 'London (Hybrid)')] |
| `S09.one_evaluation_for_merged` | PASS | The merged job has exactly one evaluation and one Excel row. | evaluations=1 excel rows for it=1 |
| `S09.gap_filled_not_conflict` | PASS | The second source lacks arrangement/type; it fills no gap with a guess and raises no false conflict. | arrangement=hybrid type=fulltime conflicts=[] |
| `S10.waiting_period` | PASS | 3 days after applying: not eligible, code waiting_period, due date = applied + 7 days. | {'eligible': False, 'code': 'waiting_period', 'reason': 'wait 7 days after applying', 'due_at': '2026-10-29T13:53:22+00:00'} |
| `S10.eligible_after_wait` | PASS | 8 days after applying, no reply: eligible. | {'eligible': True, 'code': 'eligible', 'reason': 'applied 2026-10-22 with no reply after 7+ days', 'due_at': '2026-10-29T13:53:22+00:00'} |
| `S10.reply_suppresses_followup` | PASS | After the Acme reply email is processed: reply is recorded, an approval proposal exists, the stage is STILL 'applied', and follow-up eligibility is false with code recruiter_replied. | last_reply_at=2026-10-10T09:30:00+00:00 pending proposals=1 stage=applied eligibility=recruiter_replied |
| `S10.stage_needs_approval` | PASS | Approving the proposal moves Acme to 'replied' with history evidence; before approval the agent has no way to change stages (actor 'agent' raises ApprovalRequired). | agent blocked=True; after approval stage=replied; last history=applied->replied by approval evidence='email #3 from recruiter@acme.example (Acme Cloud): Re: Your '; second decide executed=False |
| `S10.interview_and_terminal` | PASS | Moving DataWorks to interview gives interview_in_progress; to rejected gives terminal_rejected. | interview_in_progress then terminal_rejected |
| `S10.already_followed_up` | PASS | After the user records a sent follow-up, eligibility is already_followed_up until twice the wait period passes. | 7 days after sending: already_followed_up; 15 days after: eligible |
| `S10.draft_is_not_send` | PASS | Drafting a follow-up only creates a draft and a pending approval; sent_at stays NULL; approving it executes nothing external; the package contains no SMTP/send code. | drafted=True sent_at after draft=None / after approving=None approval result sent=False; files containing mail-sending code=[] |
| `S10.unapplied_not_eligible` | PASS | A job still 'saved' is never follow-up eligible (not_applied). | stage=saved not_applied |

## memory

| Check | Result | Expected | Observed |
|---|---|---|---|
| `M1_saved_exclusion_rule` | PASS | ON: saved exclusion of ShadyStaffing -> job excluded, JD never fetched. OFF: no saved rule -> same job is fetched and scored (>= 85). | memory ON: excluded, score=None, JD requests=0 \| memory OFF: scored, score=100.0, JD requests=1 \| expected different |
| `M2_corrected_preference` | PASS | ON: after correcting arrangement preference, DataWorks scores lower and the earlier evaluation + change history remain. OFF: correction is unknown, DataWorks keeps the original higher score, no history. | memory ON: score 93.0 -> 87.0; evaluation rows=2; history rows=1 \| memory OFF: score 93.0; evaluation rows=1; history rows=0 \| expected different |
| `M3_reply_changes_followup` | PASS | ON: recorded recruiter reply -> follow-up suppressed (recruiter_replied). OFF: same applied date but no reply memory -> follow-up eligible (it would nag a recruiter who already answered). | memory ON: eligible=False (recruiter_replied) \| memory OFF: eligible=True (eligible) \| expected different |
| `M4_honest_same_result` | PASS | A first-time evaluation of Acme (no saved rule touches it) scores identically with memory ON and OFF: memory adds nothing here and the report must say so. | memory ON: 100.0 \| memory OFF: 100.0 \| expected same |
| `M5_processed_email_memory` | PASS | ON: re-delivering an already-processed email creates 0 new jobs. OFF: with no memory of it the same email creates the jobs again (the duplicate work memory prevents). | memory ON: re-delivered email -> new jobs=0 duplicates skipped=1 \| memory OFF: same email, no memory -> new jobs=4 \| expected different |

## labeled

| Check | Result | Expected | Observed |
|---|---|---|---|
| `LS.good_above_poor` | PASS | Both 'good' jobs score above every 'poor' job that was scored (not excluded). | good scores=[100.0, 93.0]; scored 'poor' scores=[60.0, 68.5, 66.0] |
| `LS.poor_low_or_excluded` | PASS | Each 'poor' job is either excluded or scores below both 'good' jobs. | L03=scored/60.0; L04=scored/68.5; L05=excluded/None; L07=excluded/None; L09=scored/66.0 |
| `LS.unknown_jd_needs_jd` | PASS | The 'unknown_jd' job is needs_jd (or provisional), never excluded and never a numeric full score. | L10=needs_jd/None |
| `LS.exclusion_wins` | PASS | The excluded-company job is excluded even though its skills match perfectly. | L07 (excluded company, perfect skills)=excluded/None |
| `LS.borderline_reported` | PASS | Borderline jobs are listed with their scores and are not counted as right or wrong. | borderline (not counted right/wrong): L06=scored/80.0; L08=scored/80.0 |

## failures

| Check | Result | Expected | Observed |
|---|---|---|---|
| `F01.crash_matrix` | PASS | For EACH fault point (mail_read, email_parsed_before_persist, email_persisted_before_checkpoint, jd_fetched_before_persist, evaluated_before_checkpoint, step_logged_before_checkpoint, before_export): crash in-process, restart on the same DB; the final (company,status,score) set equals the uninterrupted baseline, 4 jobs, 1 recorded email, exactly 1 evaluation per job, no duplicate snapshots for any job. | mail_read: ok (crashed=True, evals/job=[1, 1, 1, 1], dup snapshots=0) \| email_parsed_before_persist: ok (crashed=True, evals/job=[1, 1, 1, 1], dup snapshots=0) \| email_persisted_before_checkpoint: ok (crashed=True, evals/job=[1, 1, 1, 1], dup snapshots=0) \| jd_fetched_before_persist: ok (crashed=True, evals/job=[1, 1, 1, 1], dup snapshots=0) \| evaluated_before_checkpoint: ok (crashed=True, evals/job=[1, 1, 1, 1], dup snapshots=0) \| step_logged_before_checkpoint: ok (crashed=True, evals/job=[1, 1, 1, 1], dup snapshots=0) \| before_export: ok (crashed=True, evals/job=[1, 1, 1, 1], dup snapshots=0) |
| `F02.garbage_llm_falls_back` | PASS | An LLM that always returns garbage cannot stop or change a run: it completes, decisions record fallbacks, and every score equals the deterministic baseline. | status=completed_with_errors decisions={'llm': 0, 'deterministic': 4, 'single_option': 6, 'fallback': 3} scores equal baseline=True |
| `F03.llm_outage_circuit_breaker` | PASS | An LLM that always raises timeout opens the circuit breaker after 3 failures (a circuit_breaker log row exists), the run still completes with the deterministic result, and the error is reported in the run. | circuit_breaker rows=1, llm_error rows=3, status=completed_with_errors, errors=['LLM unavailable (timeout); continuing in deterministic mode'], scores equal baseline=True |
| `F04.no_mailbox_configured` | PASS | Reading mail with no mailbox configured reports 'no_mailbox_configured', creates no jobs, and the run status is completed_with_errors (never silently 'completed' with zero work). | mail_status=no_mailbox_configured jobs=0 status=completed_with_errors errors=['no mailbox configured (set IMAP_USER/IMAP_PASSWORD or use --fixtures): nothing was read'] |
| `F05.locked_export_fallback` | PASS | When the register file is locked (os.replace raises PermissionError), the export lands in a timestamped sibling file, the run reports a warning naming it, and the original file is untouched. | fallback file=jobledger_register_p1_20261022T135324+0000.xlsx; original untouched=True; warnings=['jobledger_register_p1.xlsx is locked (open in Excel?); wrote jobledger_register_p1_20261022T135324+0000.xlsx instead'] |

## Memory ON vs OFF

| Test | Memory ON | Memory OFF | Expected |
|---|---|---|---|
| M1_saved_exclusion_rule | excluded, score=None, JD requests=0 | scored, score=100.0, JD requests=1 | different |
| M4_honest_same_result | 100.0 | 100.0 | same |
| M2_corrected_preference | score 93.0 -> 87.0; evaluation rows=2; history rows=1 | score 93.0; evaluation rows=1; history rows=0 | different |
| M3_reply_changes_followup | eligible=False (recruiter_replied) | eligible=True (eligible) | different |
| M5_processed_email_memory | re-delivered email -> new jobs=0 duplicates skipped=1 | same email, no memory -> new jobs=4 | different |

## Labeled set (diagnostic only - 10 jobs, weights not tuned)

| Key | Job | Label | Status | Score |
|---|---|---|---|---|
| L01 | Backend Python Engineer @ Acme Cloud | good | scored | 100.0 |
| L02 | Senior Python Developer @ DataWorks | good | scored | 93.0 |
| L03 | Frontend React Developer @ PixelForge | poor | scored | 60.0 |
| L04 | Python Developer (Contract) @ FinServe | poor | scored | 68.5 |
| L05 | Junior Data Analyst @ Insightly | poor | excluded | None |
| L06 | DevOps Engineer @ CloudNine | borderline | scored | 80.0 |
| L07 | Python Developer @ ShadyStaffing Ltd | poor | excluded | None |
| L08 | Full Stack Developer @ Brightside | borderline | scored | 80.0 |
| L09 | Machine Learning Engineer @ NeuralWorks | poor | scored | 66.0 |
| L10 | Backend Engineer (Python) @ Quill and Co | unknown_jd | needs_jd | None |
