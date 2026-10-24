"""Location notes show readable city names ('Düsseldorf', not 'd sseldorf'). Comparisons, cache keys and
scores stay on norm_city(). Offline."""
import pytest

from jobledger.db import queries as q
from jobledger.tools import matcher as m
from jobledger.tools.profile_reader import read_full_profile

CV_TEXT = "Python developer, five years building web services."
JD = "Requirements: Python. Onsite role. Full-time."
@pytest.mark.parametrize("city", ["Düsseldorf", "Wrocław", "Gdańsk", "Frankfurt am Main"])
def test_job_city_is_shown_with_its_real_name(city):
    score, note = m.score_location(city, "onsite", "Islamabad", False)
    assert score == 0.1 and note == f"job in {city}, candidate in Islamabad"
    assert "d sseldorf" not in note and "wroc aw" not in note
    _, note_km = m.score_location(city, "onsite", "Islamabad", False, distance_km=5600)
    assert note_km == f"job in {city}, candidate in Islamabad (~5600 km, AI-estimated)"


def test_candidate_city_is_shown_with_its_real_name():
    _, note = m.score_location("Islamabad", "onsite", "Düsseldorf", False)
    assert note == "job in Islamabad, candidate in Düsseldorf"
    _, note = m.score_location("Islamabad", "onsite", "Wrocław", False, distance_km=5600)
    assert note == "job in Islamabad, candidate in Wrocław (~5600 km, AI-estimated)"


def test_same_city_note_shows_the_readable_name():
    assert m.score_location("Düsseldorf", "onsite", "düsseldorf, Germany", False) == (1.0, "same city (Düsseldorf)")
    assert m.score_location("Wrocław", "hybrid", "Wrocław", False) == (1.0, "same city (Wrocław)")


def test_london_variants_still_compare_equal():
    assert m.score_location("London (Hybrid)", "hybrid", "London, UK", False) == (1.0, "same city (London)")
    assert m.score_location("London, UK", "hybrid", "London (Hybrid)", False)[0] == 1.0


# Expected values come from the existing rule: same city 1.0; <=30 km 1.0; <=100 km 0.6; otherwise
# hybrid 0.25 / onsite 0.1 / other 0.3. Remote, no preference, unknown and conflicted cases as before.
SCORE_CASES = [
    ("Düsseldorf", "onsite", "Islamabad", None, 0.1),
    ("Wrocław", "hybrid", "Islamabad", None, 0.25),
    ("Gdańsk", None, "Islamabad", None, 0.3),
    ("Frankfurt am Main", "onsite", "Frankfurt", 20, 1.0),
    ("Frankfurt am Main", "onsite", "Frankfurt", 30.5, 0.6),
    ("Düsseldorf", "hybrid", "Köln", 60, 0.6),
    ("Gdańsk", "onsite", "Warszawa", 100, 0.6),
    ("Gdańsk", "onsite", "Warszawa", 100.5, 0.1),
    ("Gdańsk", "hybrid", "Warszawa", 400, 0.25),
    ("London (Hybrid)", "hybrid", "London, UK", None, 1.0),
    ("Wrocław", "remote", "Islamabad", 5000, 1.0),
    ("Düsseldorf", "onsite", "Düsseldorf", None, 1.0),
    ("Düsseldorf", "onsite", None, None, 1.0),
    ("", "onsite", "Islamabad", None, None),
]


@pytest.mark.parametrize("job_loc, arr, my_loc, km, expected", SCORE_CASES)
def test_scores_do_not_change(job_loc, arr, my_loc, km, expected):
    assert m.score_location(job_loc, arr, my_loc, False, distance_km=km)[0] == expected


def test_open_conflict_still_has_no_score():
    assert m.score_location("Düsseldorf", "onsite", "Islamabad", True)[0] is None


def _candidate(conn, city, email):
    pid = q.create_profile(conn, "Ali City", email, None, city)
    q.add_cv_version(conn, pid, "cv-city", CV_TEXT, "Python", 5, "BSc")
    return pid


def test_evaluation_rationale_shows_the_readable_city(ws):
    pid = _candidate(ws.conn, "Islamabad", "ali.city@example.com")
    url = "https://city.example/job"
    jid, _ = q.upsert_job(ws.conn, pid, None, "Backend Engineer", "City Co", location="Düsseldorf",
                          arrangement="onsite", employment_type="fulltime", apply_url=url)
    sid = q.save_jd_snapshot(ws.conn, jid, url, url, "ok", content_text=JD, verification_status="verified")
    snap = ws.conn.execute("SELECT * FROM jd_snapshots WHERE id=?", (sid,)).fetchone()
    out = m.evaluate_job(ws.conn, read_full_profile(ws.conn, pid), q.get_job(ws.conn, jid), snap, llm=None)
    assert "Location: job in Düsseldorf, candidate in Islamabad." in out.rationale
    assert "d sseldorf" not in out.rationale
