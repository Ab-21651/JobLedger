# Scoring methodology

The weights and rules below were fixed **before** the labeled 10-job set was run, and have **not** been tuned
against it. The labeled set is a diagnostic (see `REPORT.md`), not a training set.

## Who does what

| Step | Done by | Why |
|---|---|---|
| Extract required/preferred skills, years, arrangement, employment type from a JD | LLM (optional) or the deterministic extractor | language understanding |
| Check every extracted item has a **verbatim** evidence quote that occurs in the JD | Python | LLM claims are not trusted |
| Reject malformed / non-extraction output (e.g. `{"score": 100}`) | Python | an LLM must not be able to inject a result |
| Hard filters | Python | confirmed facts only |
| Component scores, weighted total, status | Python | all arithmetic is reproducible |

If the LLM output is unusable after one corrective retry, or no LLM is configured, the deterministic extractor
is used and the evaluation is labelled `deterministic_fallback` / `deterministic` (column `scoring_mode`).
Once the agent's LLM circuit breaker is open, no further extraction calls are attempted in that run.

## Components and weights (`matcher.WEIGHTS`)

| Component | Weight | Scoring |
|---|---|---|
| skill | 0.40 | required skills: 1.0 (on CV list or named in CV text), 0.5 (related, e.g. Flask↔Django, capped), 0 (missing). Preferred skills contribute 15 % of the skill score if any exist |
| experience | 0.20 | `min(1, CV years / required years)`; unknown if the JD or CV states none |
| arrangement | 0.15 | 1.0 if preferred, otherwise a fixed table (e.g. hybrid vs wanting remote 0.6, onsite vs remote 0.1) |
| employment type | 0.10 | 1.0 if preferred, 0.1 otherwise |
| location | 0.15 | 1.0 remote / same city, 0.25 hybrid elsewhere, 0.1 onsite elsewhere |

`total = 100 × Σ weight × component`, rounded to one decimal.

### Skill coverage judgment (eval version `v2`)

The deterministic skill credit is brittle: a CV with PyTorch, LLMs and RAG is "missing" a requirement such as
*Machine Learning/AI*. When an LLM is configured **and** requirement extraction succeeded, one extra call asks
the model to judge each already-validated required or preferred skill against the CV as `covered`, `partial`
or `none`. Guards, all in Python (`matcher.validate_coverage`):

* Only skills that passed extraction can be judged. An extra requirement in the reply is dropped.
* `covered` and `partial` need a `cv_evidence` that is either a CV skill name or a quote found in the CV text
  (case and whitespace normalised). Otherwise that claim is dropped.
* Verdict must be one of the three values. At most 60 judgments; duplicates ignored; `why` cut to 160 characters.
* Credit is `max(deterministic, LLM)`: `covered` gives 1.0, `partial` gives 0.5, `none` changes nothing. The LLM
  can raise a credit but never lower a literal match, and it never sets a number.
* The CV is passed as data inside `<candidate_cv>` tags, capped at 8000 characters. One corrective retry, as for
  extraction. If the reply is still unusable or the call fails, deterministic credit is used and the evaluation
  carries the flag `coverage_llm_unavailable`.
* No call is made without a usable JD or when extraction fell back to the deterministic extractor. A successful
  evaluation makes exactly one coverage call.

LLM-credited skills are shown in the rationale as *covered by CV evidence (AI-judged)* with the quoted evidence.
Dropped claims are listed under `dropped_llm_items` in the score breakdown.

## Unknowns are not mismatches

A component that cannot be determined (no years stated, arrangement missing, **or an open source conflict
on that field**) counts as **0.5 (neutral)**, is listed under *Missing info*, and never as 0. If ≥ 25 % of the
weight is unknown, no skills could be extracted, or the JD page was only a *partial* match, the status is
`provisional` rather than `scored`.

## Statuses

* `excluded` – a **confirmed** hard filter failed (excluded company/keyword, stated salary below minimum in
  the same currency). No score. Unknown/uncomparable values never exclude.
* `needs_jd` – no verified description. No score (NULL, not 0). **Not a rejection.**
* `provisional` – scored with material unknowns.
* `scored` – scored with the evidence available.

Strict arrangement/type filters exist but are **off by default**; the work-authorisation check only raises
"needs confirmation", it never rejects.

## Evaluations are history

Rows in `evaluations` are append-only (enforced by DB triggers since schema v3). Re-running with identical
inputs (`UNIQUE(job_id, profile_id, eval_version, input_hash)`) writes nothing. The hash covers profile
preferences, CV, job fields, JD snapshot, weights and whether an LLM was used, so a preference correction or
new JD produces a **new** row and the old one stays.

## Known weakness (stated, not hidden)

Logistics components are generous. A job whose required skills **do not match the CV at all** can still reach
roughly 60 if arrangement, type and location fit (labeled job L03 scored 60.0). It still ranks well below the
good matches (93–100), but a "skills gate" (cap the total when the skill component is 0) would be the obvious
improvement. It was **not** added because that is a tuning decision made after seeing the labeled set; it needs
your approval.
