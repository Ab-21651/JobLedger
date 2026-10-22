# Demo script (what to run yourself)

Part A needs nothing but Python. Parts B–D are the live runs that have **not** been done yet — please do them.

## A. Offline (≈ 3 minutes)

```bat
py -m pip install -r requirements.txt
py -m pytest -q
py -m jobledger eval all --report-dir reports
py -m jobledger init
py -m jobledger profile create --name "Sam Test" --email sam@example.com --location London --arrangements remote,hybrid --types fulltime --exclude-companies "ShadyStaffing" --min-salary 50000
py -m jobledger cv --label cv1 --skills "Python,Django,PostgreSQL,Docker,AWS" --years 6 --text "Backend developer, Python, Django, PostgreSQL, Docker, AWS."
py -m jobledger run --fixtures tests\fixtures\emails\batch1_plain.json --no-llm
py -m jobledger jobs list
py -m jobledger logs --runs
```

Expect (this exact sequence was run once in the build sandbox): pytest all green; `eval` "79/79 checks passed"; in the run, the ShadyStaffing job `EXCLUDED` (JD never
requested), the PixelForge job `NEEDS_JD`. **Note:** `run --fixtures` still fetches the JD URLs from the real
web (the example hostnames in the fixtures do not exist), so those jobs will land in `NEEDS_JD` with a recorded
failure reason — that is itself a good demo of the failure path. Use `py -m jobledger jd N --file somefile.txt`
to supply a description and re-run `run --no-mail` to see it scored.

## B. Memory and corrections

```bat
py -m jobledger prefs set --set preferred_arrangements=remote --reason "only fully remote now"
py -m jobledger prefs history
py -m jobledger run --no-mail --no-llm
py -m jobledger jobs show 1
```

Old evaluations remain; new ones carry a higher `prefs_version`.

## C. Approval gate

```bat
py -m jobledger applied 1 --evidence "applied on company site"
py -m jobledger followup check 1
py -m jobledger followup draft 1
py -m jobledger approvals list
```

Right after `applied` the check says `waiting_period` and `followup draft` refuses ("no draft: waiting_period"):
that is the 7-day rule working. Once 7 days have passed (or after back-dating `applied_at` in a scratch DB), a
draft is produced together with a pending approval request; **nothing is ever sent** — you send it yourself and
then record it with `followup mark-sent 1`.

## D. Live services (not yet verified by the author)

1. Fill `.env` with a Gmail **App Password**; `py -m jobledger doctor`; `py -m jobledger run --no-llm`.
   Check: messages stay **unread** in Gmail afterwards (BODY.PEEK), and the register opens in Excel.
2. Add an LLM key, `py -m jobledger run`, then `py -m jobledger logs --run <id>` to read each step's allowed
   actions, the model's answer, the validation outcome and the fallback (if any).
3. Try a real public JD URL in a test email to see the retriever against a live site.
4. Open the register in Excel, run `export` again, and confirm the locked-file fallback message.

Please report anything that differs from this document; those are real findings, not noise.
