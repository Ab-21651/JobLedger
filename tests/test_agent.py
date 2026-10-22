import json

import pytest

from jobledger.agent import SimulatedCrash
from jobledger.db import queries as q
from jobledger.evaluation import FAULT_POINTS, baseline_sig, sig
from jobledger.llm import LLMError, ScriptedLLM
from jobledger.testing import crash_at, fixture_reader

FIX = "batch1_plain.json"


def _payload(user):
    """The allowed-actions payload of an action-selection prompt, or None for any other prompt
    (the same LLM is also asked to extract requirements; those get an unusable answer -> deterministic extractor)."""
    if "<untrusted_data>" not in user:
        return None
    return json.loads(user.split("<untrusted_data>")[1].split("</untrusted_data>")[0])


def _llm_choosing(action_fn):
    """LLM that answers with whatever ``action_fn(allowed_actions)`` returns."""
    def reply(system, user):
        payload = _payload(user)
        return "not json" if payload is None else json.dumps(action_fn(payload["allowed_actions"]))
    return ScriptedLLM().set_default(reply)


def _run(ws, llm=None, **kw):
    return ws.agent(llm=llm, mail=fixture_reader(FIX), **kw).run()


def test_deterministic_run_matches_baseline(ws):
    _run(ws)
    assert sig(ws.conn, ws.pid) == baseline_sig()


def test_rerun_is_idempotent(ws):
    _run(ws)
    n_jobs = len(q.list_jobs(ws.conn, ws.pid))
    n_eval = ws.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    res = _run(ws)
    assert len(q.list_jobs(ws.conn, ws.pid)) == n_jobs
    assert ws.conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == n_eval
    assert res.counters["jobs_new"] == 0 and res.counters["emails_processed"] == 0


def test_valid_llm_choices_are_used(ws):
    llm = _llm_choosing(lambda allowed: {"action": allowed[0]["action"], "args": allowed[0]["args"],
                                         "evidence_refs": [], "reasoning_summary": "first option"})
    res = _run(ws, llm)
    assert res.decisions["llm"] > 0 and res.status in ("completed", "completed_with_errors")
    assert sig(ws.conn, ws.pid) == baseline_sig()          # a legal choice order changes the path, never the outcome


@pytest.mark.parametrize("bad", [
    "not json at all",
    '{"action": "delete_database", "args": {}}',
    '{"action": "apply_to_job", "args": {"job_id": 1}}',
    '{"action": "send_follow_up", "args": {}}',
    '{"action": "finish", "args": {"force": true}, "evidence_refs": ["made_up_ref"]}',
    '["a", "list"]',
    '{"action": 5}',
])
def test_invalid_llm_proposals_rejected_and_fallback_equals_baseline(ws, bad):
    res = _run(ws, ScriptedLLM().set_default(lambda s, u: bad))
    assert res.decisions["fallback"] > 0
    assert sig(ws.conn, ws.pid) == baseline_sig()
    assert ws.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0     # nothing was "applied"
    assert ws.conn.execute("SELECT COUNT(*) FROM approval_requests WHERE action_type IN ('apply_to_job','send_follow_up')").fetchone()[0] == 0


def test_wrong_args_for_allowed_action_rejected(ws):
    llm = _llm_choosing(lambda allowed: {"action": allowed[0]["action"], "args": {"job_id": 99999}, "evidence_refs": []})
    res = _run(ws, llm)
    assert res.decisions["fallback"] > 0 or res.decisions["single_option"] > 0
    assert sig(ws.conn, ws.pid) == baseline_sig()


def test_one_corrective_retry_then_success(ws):
    seq = {"n": 0}

    def reply(system, user):
        payload = _payload(user)
        if payload is None:
            return "not json"
        seq["n"] += 1
        a = payload["allowed_actions"][0]
        if seq["n"] == 1:
            return "oops"
        if seq["n"] == 2:
            assert "rejected" in user                   # the corrective retry says why
        return json.dumps({"action": a["action"], "args": a["args"], "evidence_refs": []})
    res = _run(ws, ScriptedLLM().set_default(reply))
    assert res.decisions["llm"] >= 1


@pytest.mark.parametrize("kind", ["timeout", "rate_limit", "server", "network"])
def test_transient_llm_errors_degrade_gracefully(ws, kind):
    def boom(s, u):
        raise LLMError(kind, "simulated", retry_after=1 if kind == "rate_limit" else None)
    res = _run(ws, ScriptedLLM().set_default(boom))
    assert res.status in ("completed", "completed_with_errors")
    assert sig(ws.conn, ws.pid) == baseline_sig()


def test_circuit_breaker_opens_and_stops_calling_llm(ws):
    llm = ScriptedLLM().set_default(lambda s, u: (_ for _ in ()).throw(LLMError("server", "down")))
    res = _run(ws, llm)
    assert any("deterministic mode" in e for e in res.errors)
    assert len(llm.calls) <= 8, len(llm.calls)         # 3 failures x (call + corrective retry) is the ceiling


def test_auth_failure_breaks_circuit_immediately(ws):
    llm = ScriptedLLM().set_default(lambda s, u: (_ for _ in ()).throw(LLMError("auth", "bad key")))
    res = _run(ws, llm)
    assert len(llm.calls) <= 2 and res.status in ("completed", "completed_with_errors")


def test_prompt_injection_in_email_is_data_not_instruction(ws):
    prompts = []

    def reply(system, user):
        prompts.append(user)
        payload = _payload(user)
        if payload is None:
            return "not json"
        a = payload["allowed_actions"][0]
        return json.dumps({"action": a["action"], "args": a["args"], "evidence_refs": []})
    ws.agent(llm=ScriptedLLM().set_default(reply), mail=fixture_reader("injection.json")).run()
    blob = "\n".join(prompts).lower()
    assert "ignore previous instructions" not in blob and "ignore all previous" not in blob
    assert ws.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


@pytest.mark.parametrize("point", FAULT_POINTS)
def test_restart_after_crash_at_every_boundary_converges_to_baseline(ws, point):
    agent = ws.agent(mail=fixture_reader(FIX), fault=crash_at(point))
    try:
        agent.run()
    except SimulatedCrash:
        pass
    ws.reopen()
    res = ws.agent(mail=fixture_reader(FIX)).run()
    assert res.status in ("completed", "completed_with_errors")
    assert sig(ws.conn, ws.pid) == baseline_sig()
    ids = ws.conn.execute("SELECT COUNT(*) c, COUNT(DISTINCT message_uid) d FROM processed_emails WHERE profile_id=?", (ws.pid,)).fetchone()
    assert ids["c"] == ids["d"]                                    # no email processed twice
    dup = ws.conn.execute("SELECT COUNT(*) FROM (SELECT job_id, input_hash FROM evaluations GROUP BY 1,2 HAVING COUNT(*)>1)").fetchone()[0]
    assert dup == 0


def test_dead_running_run_is_marked_interrupted_on_restart(ws):
    ws.conn.execute("INSERT INTO processing_runs (run_id, profile_id, status, started_at) VALUES ('dead1', ?, 'running', '2026-10-01T00:00:00+00:00')", (ws.pid,))
    res = ws.agent().run(read_mail=False)
    assert "dead1" in res.recovered_runs and q.get_run(ws.conn, "dead1")["status"] == "interrupted"


def test_profile_isolation_across_runs(ws):
    rid = ws.riley()
    _run(ws)
    before = sig(ws.conn, rid)
    assert before == []
    ws.agent(pid=rid, mail=fixture_reader("batch2_new_and_dupes.json")).run()
    assert sig(ws.conn, ws.pid) == baseline_sig()                   # Riley's run did not touch Sam's results
    sam_jobs = {j["id"] for j in q.list_jobs(ws.conn, ws.pid)}
    assert sam_jobs.isdisjoint({j["id"] for j in q.list_jobs(ws.conn, rid)})


def test_pref_change_applies_only_to_new_evaluations_and_keeps_old(ws):
    _run(ws)
    old = ws.conn.execute("SELECT COUNT(*) FROM evaluations WHERE profile_id=?", (ws.pid,)).fetchone()[0]
    q.update_profile_fields(ws.conn, ws.pid, {"excluded_companies": "Acme Cloud"}, reason="user correction")
    ws.agent(mail=fixture_reader(FIX)).run()
    assert ws.conn.execute("SELECT COUNT(*) FROM evaluations WHERE profile_id=?", (ws.pid,)).fetchone()[0] >= old
    acme = next(j for j in q.list_jobs(ws.conn, ws.pid) if "Acme" in j["company"])
    hist = q.list_evaluations(ws.conn, acme["id"], ws.pid)
    assert hist[0]["status"] != "excluded" and hist[-1]["status"] == "excluded"       # oldest-first; history preserved


def test_database_write_failure_mid_run_is_failed_not_completed(ws):
    def hook(point, ctx):
        if point == "mail_read":
            ws.conn.execute("PRAGMA query_only=ON")           # every later write now fails
    res = ws.agent(mail=fixture_reader(FIX), fault=hook).run()
    ws.conn.execute("PRAGMA query_only=OFF")
    assert res.status == "failed" and res.errors
    assert ws.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
