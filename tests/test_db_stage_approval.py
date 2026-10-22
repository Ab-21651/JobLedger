import sqlite3
from datetime import timedelta

import pytest

from jobledger.db import queries as q
from jobledger.db.schema import init_db
from jobledger.testing import riley_profile
from jobledger.timeutil import parse_iso, utcnow
from jobledger.tools import approval_manager as am
from jobledger.tools import stage_manager as sm


def _job(ws, pid=None, title="Backend Python Developer", company="Acme Cloud", **kw):
    jid, new = q.upsert_job(ws.conn, pid or ws.pid, None, title, company, location=kw.pop("location", "London"),
                            arrangement=kw.pop("arrangement", "hybrid"), employment_type="fulltime", **kw)
    return jid


def test_migrations_idempotent_and_wal(ws):
    ws.conn.close()
    c = init_db(ws.settings.db_path)
    c2 = init_db(ws.settings.db_path)
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    c.close(), c2.close()
    ws.reopen()


def test_cross_source_dedup_merges_one_job(ws):
    a = _job(ws, source_label="alert-a")
    b = _job(ws, title="Backend Python Developer", company="Acme Cloud Ltd", location="London (Hybrid)", source_label="alert-b")
    assert a == b
    assert len(q.list_job_sources(ws.conn, a)) == 2


def test_other_city_is_a_separate_job(ws):
    assert _job(ws, location="London") != _job(ws, location="Leeds", arrangement="onsite")


def test_norm_city_strips_decorations():
    assert q.norm_city("London (Hybrid)") == q.norm_city("London, UK") == q.norm_city("London")
    assert q.norm_city("Leeds") != q.norm_city("London")


def test_conflicting_fields_preserved_not_overwritten(ws):
    a = _job(ws, arrangement="hybrid", salary_min=60000, salary_max=70000, salary_currency="GBP", source_label="a")
    _job(ws, arrangement="remote", salary_min=80000, salary_max=90000, salary_currency="GBP", source_label="b")
    fields = {c["field_name"] for c in q.list_job_conflicts(ws.conn, a)}
    assert "arrangement" in fields and any("salary" in f for f in fields)
    assert q.get_job(ws.conn, a)["arrangement"] == "hybrid"       # first value kept, second recorded


def test_profile_isolation_is_enforced_in_python_and_in_sql(ws):
    rid = ws.riley()
    jid = _job(ws)
    with pytest.raises(q.ProfileIsolationError):
        q.require_job(ws.conn, jid, rid)
    with pytest.raises(q.ProfileIsolationError):
        sm.transition_stage(ws.conn, rid, jid, "applied")
    # bypass python: a raw insert pointing at another profile's job must be refused by a DB trigger
    with pytest.raises(sqlite3.IntegrityError):
        ws.conn.execute("INSERT INTO applications (job_id, profile_id, stage) VALUES (?,?,?)", (jid, rid, "saved"))


def test_preference_correction_keeps_history_and_bumps_version(ws):
    v0 = q.prefs_version(ws.conn, ws.pid)
    q.update_profile_fields(ws.conn, ws.pid, {"excluded_companies": "Shady Recruiters"}, reason="user correction")
    assert q.prefs_version(ws.conn, ws.pid) > v0
    from jobledger.tools.profile_reader import read_preferences_history
    hist = read_preferences_history(ws.conn, ws.pid)
    assert any("Shady" in str(h["new_value"]) for h in hist) and len(hist) >= 1


def test_evaluations_are_append_only(ws):
    jid = _job(ws)
    eid, inserted = q.save_evaluation(ws.conn, jid, ws.pid, None, 1, {}, 0.5, 0.5, 0.5, 0.5, 0.5, 70.0, {}, None, "", "",
                                      "r", [], eval_version="v1", input_hash="hash1", prefs_version=1,
                                      status="scored", scoring_mode="deterministic")
    assert inserted
    with pytest.raises(sqlite3.IntegrityError):
        ws.conn.execute("UPDATE evaluations SET total_score=1 WHERE id=?", (eid,))
    with pytest.raises(sqlite3.IntegrityError):
        ws.conn.execute("DELETE FROM evaluations WHERE id=?", (eid,))


# ------------------------------------------------------------------ stages
def test_stage_flow_and_history(ws):
    jid = _job(ws)
    sm.transition_stage(ws.conn, ws.pid, jid, "shortlisted")
    r = sm.transition_stage(ws.conn, ws.pid, jid, "applied", reason="sent via site")
    assert r["from"] == "shortlisted" and r["to"] == "applied"
    app = q.get_application_by_job(ws.conn, jid, ws.pid)
    assert app["applied_at"] and len(q.list_stage_history(ws.conn, app["id"])) >= 2


def test_invalid_transition_and_unknown_stage_rejected(ws):
    jid = _job(ws)
    with pytest.raises(sm.StageError):
        sm.transition_stage(ws.conn, ws.pid, jid, "interview")
    with pytest.raises(sm.StageError):
        sm.transition_stage(ws.conn, ws.pid, jid, "hired")


def test_agent_actor_cannot_change_stage(ws):
    jid = _job(ws)
    with pytest.raises(sm.ApprovalRequired):
        sm.transition_stage(ws.conn, ws.pid, jid, "applied", actor="agent")


def _applied(ws, days_ago):
    jid = _job(ws)
    sm.transition_stage(ws.conn, ws.pid, jid, "applied")
    app = q.get_application_by_job(ws.conn, jid, ws.pid)
    past = (utcnow() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    ws.conn.execute("UPDATE applications SET applied_at=? WHERE id=?", (past, app["id"]))
    return jid, app["id"]


def test_followup_rules(ws):
    _, a_new = _applied(ws, 2)
    assert sm.check_follow_up_eligibility(ws.conn, a_new)["code"] == "waiting_period"
    _, a_old = _applied(ws, 10)
    assert sm.check_follow_up_eligibility(ws.conn, a_old)["eligible"] is True
    jid = _job(ws, title="Saved Only", company="Nobody")
    sm.transition_stage(ws.conn, ws.pid, jid, "saved")
    app = q.get_application_by_job(ws.conn, jid, ws.pid)
    assert sm.check_follow_up_eligibility(ws.conn, app["id"])["code"] == "not_applied"


def test_followup_suppressed_after_reply_and_terminal(ws):
    jid, aid = _applied(ws, 10)
    q.set_application_reply(ws.conn, aid, utcnow().isoformat(timespec="seconds"))
    assert sm.check_follow_up_eligibility(ws.conn, aid)["code"] == "recruiter_replied"
    jid2, aid2 = _applied(ws, 10)
    ws.conn.execute("UPDATE jobs SET title='Other Role' WHERE id=?", (jid2,))
    sm.transition_stage(ws.conn, ws.pid, jid2, "rejected", evidence="rejection email")
    assert sm.check_follow_up_eligibility(ws.conn, aid2)["code"] == "terminal_rejected"


def test_reply_evidence_becomes_pending_proposal_not_stage_change(ws):
    jid, aid = _applied(ws, 3)
    ap = sm.apply_reply_evidence(ws.conn, ws.pid, jid, "Thanks for applying, can we talk Tuesday?")
    assert q.get_application(ws.conn, aid)["stage"] == "applied"
    assert q.get_approval(ws.conn, ap)["status"] == "pending"
    res = am.decide(ws.conn, ws.pid, ap, True)
    assert res["executed"] and q.get_application(ws.conn, aid)["stage"] == "replied"


def test_approval_decide_once_and_reject_changes_nothing(ws):
    jid, aid = _applied(ws, 3)
    ap = sm.apply_reply_evidence(ws.conn, ws.pid, jid, "reply")
    assert am.decide(ws.conn, ws.pid, ap, False)["status"] == "rejected"
    again = am.decide(ws.conn, ws.pid, ap, True)
    assert not again["executed"] and "only once" in again["message"]
    assert q.get_application(ws.conn, aid)["stage"] == "applied"


def test_approval_cross_profile_refused(ws):
    jid, _ = _applied(ws, 3)
    ap = sm.apply_reply_evidence(ws.conn, ws.pid, jid, "reply")
    rid = ws.riley()
    with pytest.raises(q.ProfileIsolationError):
        am.decide(ws.conn, rid, ap, True)


def test_unknown_action_type_refused(ws):
    with pytest.raises(ValueError):
        am.propose(ws.conn, ws.pid, "wire_money", "nope")


def test_no_outbound_send_code_in_package():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "jobledger"
    bad = re.compile(r"smtplib|\.sendmail\(|\.send_message\(|SMTP\(|webbrowser\.open|selenium|playwright")
    hits = [str(p) for p in root.rglob("*.py") if p.name != "evaluation.py" and bad.search(p.read_text(encoding="utf-8"))]
    assert hits == [], hits
