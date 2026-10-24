"""LLM result cache: validated requirement extraction and skill coverage are stored per profile and reused while the
inputs are unchanged. Offline (ScriptedLLM). A cache hit must give the same scores as a miss."""
import itertools
import json

from jobledger.db import queries as q
from jobledger.db import schema
from jobledger.llm import ScriptedLLM
from jobledger.tools import matcher as m
from jobledger.tools.profile_reader import read_full_profile

CV_TEXT = ("Built retrieval pipelines (RAG) over LLMs. Trained PyTorch models in production. "
           "Python services for six years.")
CV_SKILLS = ["Python", "PyTorch", "LLMs", "RAG"]
JD = "Requirements: Python, Machine Learning/AI. Full-time, hybrid in London."
EXTRACT_OK = json.dumps({"required_skills": [{"skill": "Python", "evidence": "Python"},
                                             {"skill": "Machine Learning/AI", "evidence": "Machine Learning/AI"}],
                         "preferred_skills": []})
COVER_OK = json.dumps({"judgments": [{"skill": "Machine Learning/AI", "verdict": "covered",
                                      "cv_evidence": "PyTorch", "why": "Trained models in production."}]})
BAD = "this is not json"
_seq = itertools.count(1)


def _llm(model="model-a", extraction=EXTRACT_OK, coverage=COVER_OK):
    def pick(item):
        if isinstance(item, Exception):
            raise item
        return item

    def route(system, user):
        if system == m.SYSTEM_PROMPT:
            return pick(extraction)
        if system == m.COVERAGE_SYSTEM_PROMPT:
            return pick(coverage)
        raise AssertionError("unexpected LLM call: the job and profile are in London, so no distance call")
    return ScriptedLLM(model=model).set_default(route)


def _calls(llm, system):
    return sum(1 for c in llm.calls if c["system"] == system)


def _extract_calls(llm):
    return _calls(llm, m.SYSTEM_PROMPT)


def _cover_calls(llm):
    return _calls(llm, m.COVERAGE_SYSTEM_PROMPT)


def _profile(conn, email, cv_text=CV_TEXT, skills=CV_SKILLS):
    pid = q.create_profile(conn, "Cache Tester", email, None, "London")
    q.add_cv_version(conn, pid, "cv-cache", cv_text, ",".join(skills), 6, "MSc")
    return pid


def _job(conn, pid, jd=JD, company="Acme Cache"):
    n = next(_seq)
    url = f"https://cache{n}.example/job"
    jid, _ = q.upsert_job(conn, pid, None, "ML Engineer", company, location="London", arrangement="hybrid",
                          employment_type="fulltime", apply_url=url)
    sid = q.save_jd_snapshot(conn, jid, url, url, "ok", content_text=jd, verification_status="verified")
    snap = conn.execute("SELECT * FROM jd_snapshots WHERE id=?", (sid,)).fetchone()
    return jid, snap


def _score(conn, pid, job, llm):
    jid, snap = job
    return m.evaluate_job(conn, read_full_profile(conn, pid), q.get_job(conn, jid), snap, llm=llm)


def _change_prefs(conn, pid, value):
    """A preference change: a new prefs version, so the evaluation is recomputed and written as a new row."""
    q.update_profile_fields(conn, pid, {"preferred_arrangements": value}, "test preference change")


def _rows(conn, kind=None, pid=None):
    sql, args = "SELECT COUNT(*) FROM llm_cache WHERE 1=1", []
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    if pid is not None:
        sql += " AND profile_id = ?"
        args.append(pid)
    return conn.execute(sql, args).fetchone()[0]


def _events(outcome):
    return [e.get("event") for e in outcome.llm_events]


# ---------------------------------------------------------------- reuse

def test_repeat_after_preference_change_makes_no_extraction_or_coverage_call(ws):
    pid = _profile(ws.conn, "cache.pref@example.com")
    job = _job(ws.conn, pid)
    llm = _llm()
    first = _score(ws.conn, pid, job, llm)
    assert _extract_calls(llm) == 1 and _cover_calls(llm) == 1
    assert "llm_cache_hit" not in _events(first)

    _change_prefs(ws.conn, pid, "onsite")
    second = _score(ws.conn, pid, job, llm)
    assert second.inserted                                          # a new evaluation row, as before
    assert _extract_calls(llm) == 1 and _cover_calls(llm) == 1      # ZERO new LLM calls
    assert second.components["skill"] == first.components["skill"] and second.components["skill"] > 0
    assert second.scoring_mode == "llm"
    assert {"event": "llm_cache_hit", "kind": "extraction"} in second.llm_events
    assert {"event": "llm_cache_hit", "kind": "coverage"} in second.llm_events
    stored = ws.conn.execute("SELECT scoring_mode FROM evaluations WHERE id = ?", (second.evaluation_id,)).fetchone()
    assert stored["scoring_mode"] == "llm"


def test_cache_hit_gives_identical_scores_and_breakdown(ws):
    pid = _profile(ws.conn, "cache.same@example.com")
    job = _job(ws.conn, pid)
    llm = _llm()
    miss = _score(ws.conn, pid, job, llm)
    hit = _score(ws.conn, pid, job, llm)                            # same inputs: served from the cache
    assert _extract_calls(llm) == 1 and _cover_calls(llm) == 1
    assert "llm_cache_hit" in _events(hit) and "llm_cache_hit" not in _events(miss)
    assert hit.total_score == miss.total_score and hit.components == miss.components
    assert hit.flags == miss.flags and hit.missing_info == miss.missing_info
    assert miss.components["skill"] > 0
    # same evaluation row, so the stored breakdown is the same object either way
    assert hit.evaluation_id == miss.evaluation_id and hit.inserted is False


def test_cv_change_reasks_extraction_and_coverage(ws):
    pid = _profile(ws.conn, "cache.cv@example.com")
    job = _job(ws.conn, pid)
    llm = _llm()
    _score(ws.conn, pid, job, llm)
    q.add_cv_version(ws.conn, pid, "cv-java", "Designed Java services for banks for eight years.", "Java", 8, "BSc")
    _score(ws.conn, pid, job, llm)
    assert _extract_calls(llm) == 2 and _cover_calls(llm) == 2


def test_changed_jd_text_reasks_extraction(ws):
    pid = _profile(ws.conn, "cache.jd@example.com")
    llm = _llm()
    _score(ws.conn, pid, _job(ws.conn, pid), llm)
    _score(ws.conn, pid, _job(ws.conn, pid, jd=JD + " Friendly remote-first team."), llm)
    assert _extract_calls(llm) == 2
    # coverage is keyed on the validated requirement names and the CV, not on the JD text: same names, reused
    assert _cover_calls(llm) == 1


def test_different_model_reasks_both_steps(ws):
    pid = _profile(ws.conn, "cache.model@example.com")
    job = _job(ws.conn, pid)
    _score(ws.conn, pid, job, _llm(model="model-a"))
    llm_b = _llm(model="model-b")
    out = _score(ws.conn, pid, job, llm_b)
    assert _extract_calls(llm_b) == 1 and _cover_calls(llm_b) == 1
    assert out.scoring_mode == "llm"
    models = {r["model"] for r in ws.conn.execute("SELECT model FROM llm_cache WHERE profile_id = ?", (pid,))}
    assert models == {"model-a", "model-b"}


def test_cache_rows_survive_reopen(ws):
    pid = _profile(ws.conn, "cache.reopen@example.com")
    job = _job(ws.conn, pid)
    _score(ws.conn, pid, job, _llm())
    ws.reopen()
    _change_prefs(ws.conn, pid, "onsite")
    llm = _llm()
    out = _score(ws.conn, pid, job, llm)
    assert _extract_calls(llm) == 0 and _cover_calls(llm) == 0
    assert out.scoring_mode == "llm"


def test_other_profile_does_not_hit_this_profiles_cache(ws):
    pa = _profile(ws.conn, "cache.a@example.com")
    pb = _profile(ws.conn, "cache.b@example.com")
    _score(ws.conn, pa, _job(ws.conn, pa), _llm())
    llm_b = _llm()
    _score(ws.conn, pb, _job(ws.conn, pb), llm_b)
    assert _extract_calls(llm_b) == 1 and _cover_calls(llm_b) == 1
    assert _rows(ws.conn, pid=pa) == 2 and _rows(ws.conn, pid=pb) == 2


# ---------------------------------------------------------------- what is never cached

def test_invalid_extraction_is_not_cached_and_is_retried(ws):
    pid = _profile(ws.conn, "cache.badx@example.com")
    job = _job(ws.conn, pid)
    bad = _llm(extraction=BAD)
    first = _score(ws.conn, pid, job, bad)
    assert first.scoring_mode == "deterministic_fallback"
    assert _extract_calls(bad) == 2                                 # both attempts, then the fallback
    assert _rows(ws.conn, kind="extraction") == 0 and _cover_calls(bad) == 0

    _change_prefs(ws.conn, pid, "onsite")
    good = _llm()
    second = _score(ws.conn, pid, job, good)
    assert _extract_calls(good) == 1 and second.scoring_mode == "llm"
    assert _rows(ws.conn, kind="extraction") == 1


def test_failed_coverage_is_not_cached_and_is_retried(ws):
    pid = _profile(ws.conn, "cache.badc@example.com")
    job = _job(ws.conn, pid)
    bad = _llm(coverage=BAD)
    first = _score(ws.conn, pid, job, bad)
    assert _cover_calls(bad) == 2 and "coverage_llm_unavailable" in first.flags
    assert _rows(ws.conn, kind="coverage") == 0 and _rows(ws.conn, kind="extraction") == 1

    _change_prefs(ws.conn, pid, "onsite")
    good = _llm()
    second = _score(ws.conn, pid, job, good)
    assert _extract_calls(good) == 0                                # the valid extraction was reused
    assert _cover_calls(good) == 1 and "coverage_llm_unavailable" not in second.flags
    assert _rows(ws.conn, kind="coverage") == 1


def test_no_llm_writes_nothing(ws):
    pid = _profile(ws.conn, "cache.none@example.com")
    out = _score(ws.conn, pid, _job(ws.conn, pid), None)
    assert out.scoring_mode == "deterministic"
    assert _rows(ws.conn) == 0


# ---------------------------------------------------------------- storage and keys

def test_migration_five_creates_llm_cache_and_init_reports_v5(ws):
    assert schema.SCHEMA_VERSION == 6 and schema.current_version(ws.conn) == 6
    cols = {r["name"] for r in ws.conn.execute("PRAGMA table_info(llm_cache)")}
    assert cols == {"id", "profile_id", "kind", "cache_key", "payload", "provider", "model", "created_at"}


def test_save_is_insert_or_ignore_and_keeps_the_first_result(ws):
    pid = _profile(ws.conn, "cache.ignore@example.com")
    assert q.save_llm_cache(ws.conn, pid, "coverage", "k1", {"judgments": {}, "dropped": []}, model="a") is True
    assert q.save_llm_cache(ws.conn, pid, "coverage", "k1", {"judgments": {"x": 1}, "dropped": []}, model="b") is False
    assert q.get_llm_cache(ws.conn, pid, "coverage", "k1") == {"judgments": {}, "dropped": []}
    assert q.get_llm_cache(ws.conn, pid, "coverage", "missing") is None


def test_keys_follow_inputs_provider_and_model():
    llm_a, llm_b = _llm(model="model-a"), _llm(model="model-b")
    k = m.extraction_cache_key(JD, "ML Engineer", "Acme", ["Python", "RAG"], llm_a)
    assert k == m.extraction_cache_key(JD, "ML Engineer", "Acme", ["RAG", "Python"], llm_a)   # order-free skills
    assert k != m.extraction_cache_key(JD, "ML Engineer", "Acme", ["Python", "RAG"], llm_b)
    assert k != m.extraction_cache_key(JD + "!", "ML Engineer", "Acme", ["Python", "RAG"], llm_a)
    c = m.coverage_cache_key(["Python"], ["Python"], CV_TEXT, llm_a)
    assert c != m.coverage_cache_key(["Python"], ["Python"], CV_TEXT, llm_b)
    assert c != m.coverage_cache_key(["Python"], ["Python"], CV_TEXT + " More.", llm_a)
