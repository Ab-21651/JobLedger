import json

import pytest

from jobledger.db import queries as q
from jobledger.llm import ScriptedLLM
from jobledger.tools import matcher as m

JD = "Requirements: Python, Django. Nice to have: Docker. We want 5 years of Python experience. Hybrid in London. Full-time."


def test_weights_sum_to_one():
    assert abs(sum(m.WEIGHTS.values()) - 1.0) < 1e-9


def test_validate_requirements_accepts_grounded_evidence():
    obj = {"required_skills": [{"skill": "Python", "evidence": "5 years of Python"}], "preferred_skills": [],
           "min_years": {"value": 5, "evidence": "5 years of Python"}}
    ok, problems = m.validate_requirements(obj, JD, {"python"})
    assert ok and not problems


def test_validate_requirements_rejects_invented_evidence():
    obj = {"required_skills": [{"skill": "Kubernetes", "evidence": "deep Kubernetes expertise required"}]}
    ok, problems = m.validate_requirements(obj, JD, {"python"})
    assert not (ok and ok.get("required_skills")) and problems


@pytest.mark.parametrize("junk", [{"score": 100, "verdict": "perfect"}, {}, [], "text", None, 7])
def test_validate_requirements_rejects_non_extractions(junk):
    ok, problems = m.validate_requirements(junk, JD, set())
    assert ok is None and problems


def test_component_scores():
    assert m.score_experience({"value": 5}, 7)[0] == 1.0
    assert m.score_experience({"value": 10}, 2)[0] < 0.5
    assert m.score_experience(None, 7)[0] is None
    assert m.score_arrangement("remote", ["remote", "hybrid"], False)[0] == 1.0
    assert m.score_arrangement("onsite", ["remote"], False)[0] < 0.5
    assert m.score_arrangement(None, ["remote"], False)[0] is None
    assert m.score_type("fulltime", ["fulltime"], False)[0] == 1.0
    assert m.score_location("London", "hybrid", "London", False)[0] == 1.0
    assert m.score_location("Leeds", "onsite", "London", False)[0] < 1.0


def test_deterministic_requirements_find_skills():
    r = m.deterministic_requirements(JD)
    names = {s["skill"].lower() for s in r.get("required_skills", [])}
    assert "python" in names


def _eval(ws, jd, llm=None, **job):
    from jobledger.tools.profile_reader import read_full_profile as load_full_profile
    jid, _ = q.upsert_job(ws.conn, ws.pid, None, job.get("title", "Backend Python Developer"), job.get("company", "Acme Cloud"),
                          location="London", arrangement="hybrid", employment_type="fulltime", apply_url="https://x.example/j")
    snap = None
    if jd:
        sid = q.save_jd_snapshot(ws.conn, jid, "https://x.example/j", "https://x.example/j", "ok", content_text=jd,
                                 verification_status="verified")
        snap = ws.conn.execute("SELECT * FROM jd_snapshots WHERE id=?", (sid,)).fetchone()
    return m.evaluate_job(ws.conn, load_full_profile(ws.conn, ws.pid), q.get_job(ws.conn, jid), snap, llm=llm), jid


def test_no_jd_means_needs_jd_not_a_score(ws):
    out, _ = _eval(ws, None)
    assert out.status == "needs_jd" and out.total_score is None


def test_total_is_weighted_sum_computed_in_python(ws):
    out, _ = _eval(ws, JD * 3)
    assert out.total_score is not None and 0 <= out.total_score <= 100
    comps = out.components
    got = sum(m.WEIGHTS[k] * (comps[k] if comps[k] is not None else m.NEUTRAL) for k in m.WEIGHTS)
    assert abs(got * 100 - out.total_score) < 0.6


def test_idempotent_same_inputs(ws):
    out1, jid = _eval(ws, JD * 3)
    out2, _ = _eval(ws, JD * 3)
    n = ws.conn.execute("SELECT COUNT(*) c FROM evaluations WHERE job_id=?", (jid,)).fetchone()["c"]
    assert n == 1 and out1.evaluation_id == out2.evaluation_id and not out2.inserted


def test_llm_cannot_inject_a_score(ws):
    evil = ScriptedLLM(['{"score": 100, "match": "perfect", "required_skills": []}'] * 3)
    out, _ = _eval(ws, "We need Rust, Go and Haskell. On-site Leeds. Contract. " * 5, llm=evil)
    assert out.total_score != 100 or out.scoring_mode != "llm"
    assert out.scoring_mode in ("deterministic_fallback", "llm", "deterministic")


def test_llm_garbage_falls_back_deterministically(ws):
    out, _ = _eval(ws, JD * 3, llm=ScriptedLLM(["not json", "still not json"] * 2))
    assert out.scoring_mode == "deterministic_fallback" and out.total_score is not None


def test_email_vs_jd_arrangement_conflict_is_flagged_not_resolved(ws):
    jid, _ = q.upsert_job(ws.conn, ws.pid, None, "Dev", "Acme", location="London", arrangement="onsite", employment_type="fulltime")
    jd = "Requirements: Python. This is a fully remote position. Full-time. 3 years of experience. " * 3
    sid = q.save_jd_snapshot(ws.conn, jid, "u", "u", "ok", content_text=jd, verification_status="verified")
    snap = ws.conn.execute("SELECT * FROM jd_snapshots WHERE id=?", (sid,)).fetchone()
    from jobledger.tools.profile_reader import read_full_profile
    out = m.evaluate_job(ws.conn, read_full_profile(ws.conn, ws.pid), q.get_job(ws.conn, jid), snap)
    assert any(f.startswith("arrangement_conflict_email_vs_jd") for f in out.flags)
    assert out.components["arrangement"] is None            # unknown, not silently picked
    assert q.get_job(ws.conn, jid)["arrangement"] == "onsite"
