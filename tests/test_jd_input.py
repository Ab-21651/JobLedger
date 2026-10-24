"""Job descriptions from the clipboard (`jd N --clip`, `jd watch`) and from a public URL (`jd N --url`).

Offline: the clipboard is scripted, web pages come from FakeWeb, and nothing touches the real clipboard
or the network.
"""
import subprocess
from types import SimpleNamespace

import pytest

from jobledger import cli
from jobledger.config import reset_settings
from jobledger.db import queries as q
from jobledger.db.schema import init_db
from jobledger.testing import FakeWeb, Page, Sleeper, public_resolver, sam_profile
from jobledger.tools import jd_input, matcher

FILLER = "We are hiring an engineer to build reliable data pipelines and services for our customers. "
ACME_PAGE = ("<html><head><title>Senior Python Developer | Acme Corp</title></head><body>"
             "<h1>Senior Python Developer</h1><p>Acme Corp is hiring in London. "
             + "We own our Python platform end to end. " * 8 + "</p></body></html>")


def jd_text(title, company, location="London", filler=400):
    body = (FILLER * (filler // len(FILLER) + 1))[:filler]
    return f"{title}\n{company} · {location} · Hybrid\n\nAbout the role\n{body}\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("IMAP_USER", "IMAP_PASSWORD", "LLM_PROVIDER", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    db = str(tmp_path / "jd.db")
    monkeypatch.setenv("DB_PATH", db)
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.chdir(tmp_path)
    reset_settings()
    conn = init_db(db)
    pid = sam_profile(conn)
    yield SimpleNamespace(conn=conn, pid=pid, dir=tmp_path)
    conn.close()
    reset_settings()


def add_job(env, title, company, location="London", workflow=None):
    jid, _ = q.upsert_job(env.conn, env.pid, None, title, company, location=location, source_label="test")
    if workflow:
        q.set_job_workflow(env.conn, jid, workflow, "test")
    return jid


def match(env, text):
    return jd_input.match_job_by_text(env.conn, env.pid, text)


def run(capsys, *argv):
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def snapshots(env, jid):
    return q.list_jd_snapshots(env.conn, jid)


def status_of(env, jid):
    return q.get_job(env.conn, jid)["jd_status"]


class ScriptedClipboard:
    """Returns scripted items in order (an exception item is raised instead), then keeps returning the
    last text; with interrupt=True it raises KeyboardInterrupt once the script is used up, like Ctrl+C."""

    def __init__(self, items, interrupt=False):
        self.items = list(items)
        self.interrupt = interrupt
        self.last = ""

    def __call__(self):
        if self.items:
            item = self.items.pop(0)
            if isinstance(item, BaseException):
                raise item
            self.last = item
            return item
        if self.interrupt:
            raise KeyboardInterrupt
        return self.last


def use_web(monkeypatch, pages):
    fake = FakeWeb(pages)
    monkeypatch.setattr(cli, "_jd_fetch_deps",
                        lambda: {"session": fake, "resolver": public_resolver, "sleep": Sleeper()})
    return fake


# ------------------------------------------------------------------ match_job_by_text
def test_clear_match(env):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    kind, found = match(env, jd_text("Senior Python Developer", "Acme Corp"))
    assert kind == "match" and found["id"] == jid


def test_linkedin_style_page_picks_the_target_not_the_similar_jobs(env):
    target = add_job(env, "Senior Python Developer", "Acme Corp")
    add_job(env, "Data Engineer", "Globex")
    page = ("Senior Python Developer\nAcme Corp · London (Hybrid)\n" + FILLER * 3
            + "\n" + "About the job details. " * 120 + "\nPeople also viewed\nData Engineer\nGlobex · London\n")
    assert len(page) > 2500
    kind, found = match(env, page)
    assert kind == "match" and found["id"] == target


def test_same_role_in_two_cities_is_broken_by_location(env):
    add_job(env, "Backend Engineer", "Initech", "London")
    berlin = add_job(env, "Backend Engineer", "Initech", "Berlin")
    kind, found = match(env, jd_text("Backend Engineer", "Initech", "Berlin"))
    assert kind == "match" and found["id"] == berlin


def test_same_role_in_two_cities_still_tied_is_ambiguous(env):
    a = add_job(env, "Backend Engineer", "Initech", "London")
    b = add_job(env, "Backend Engineer", "Initech", "Berlin")
    kind, found = match(env, jd_text("Backend Engineer", "Initech", "Paris"))
    assert kind == "ambiguous" and sorted(j["id"] for j in found) == sorted([a, b])


def test_decorated_company_matches_the_bare_name_on_the_page(env):
    jid = add_job(env, "Senior AI Engineer", "Apertera (formerly Alexa Translations)")
    kind, found = match(env, jd_text("Senior AI Engineer", "Apertera"))
    assert kind == "match" and found["id"] == jid


def test_decorated_title_matches_the_bare_title_on_the_page(env):
    jid = add_job(env, "Senior AI Engineer (f/m/d)", "Acme Corp")
    kind, found = match(env, jd_text("Senior AI Engineer", "Acme Corp"))
    assert kind == "match" and found["id"] == jid


def test_company_that_is_only_a_parenthesised_part_keeps_its_full_string(env):
    add_job(env, "Data Engineer", "(Remote)")
    assert match(env, jd_text("Data Engineer", "Globex") + " Remote working")[0] == "none"
    kind, found = match(env, jd_text("Data Engineer", "Globex") + " (Remote)")
    assert kind == "match"


def test_same_title_and_decorated_companies_are_told_apart_by_location(env):
    uk = add_job(env, "AI Engineer", "Acme (UK)", "London")
    us = add_job(env, "AI Engineer", "Acme (US)", "Berlin")
    kind, found = match(env, jd_text("AI Engineer", "Acme", "Berlin"))
    assert kind == "match" and found["id"] == us
    kind, found = match(env, jd_text("AI Engineer", "Acme", "London"))
    assert kind == "match" and found["id"] == uk
    kind, found = match(env, jd_text("AI Engineer", "Acme", "Paris"))
    assert kind == "ambiguous" and sorted(j["id"] for j in found) == sorted([uk, us])


SIDEBAR = "AI Engineer\nCreatio\nPoland\nAI Engineer\nOpera\nWroclaw\nApplied AI Engineer - Remote\ndexter health\n"


def probe_page(title, company, place, sidebar=SIDEBAR):
    """Shape of a copied job page: title, company and place at the top, the real description, then a sidebar."""
    return (f"{title}\n{company}\n{place}\n\nAbout the job "
            + "We build generative AI products for enterprise customers. " * 8
            + "\n\nSimilar jobs\n" + sidebar)


@pytest.fixture
def probe(env):
    ids = {}
    ids[1] = add_job(env, "Senior Generative AI Software Engineer", "Apertera (formerly Alexa Translations)", "Islamabad")
    ids[2] = add_job(env, "AI Engineer – GenAI & Cloud (f/m/x)", "Sii Poland", "Warsaw")
    ids[3] = add_job(env, "AI Engineer – GenAI & Cloud (f/m/x)", "Sii Poland", "Gdańsk, Pomeranian, Poland")
    ids[4] = add_job(env, "AI Engineer", "Creatio Poland", "Poland")
    ids[5] = add_job(env, "AI Engineer", "Opera", "Wrocław")
    ids[6] = add_job(env, "Applied AI Engineer - Remote", "dexter health", "Germany")
    ids[7] = add_job(env, "AI Developer (LLM Products) - Remote", "dexter health", "Germany")
    return ids


def test_probe_warsaw_page_picks_the_warsaw_job(env, probe):
    kind, found = match(env, probe_page("AI Engineer – GenAI & Cloud", "Sii Poland", "Warsaw"))
    assert kind == "match" and found["id"] == probe[2]


def test_probe_gdansk_page_picks_the_gdansk_job(env, probe):
    kind, found = match(env, probe_page("AI Engineer – GenAI & Cloud", "Sii Poland", "Gdańsk, Pomeranian, Poland"))
    assert kind == "match" and found["id"] == probe[3]


def test_probe_same_company_with_only_a_country_is_ambiguous_between_the_two_sii_jobs(env, probe):
    kind, found = match(env, probe_page("AI Engineer – GenAI & Cloud", "Sii Poland", "Poland"))
    assert kind == "ambiguous" and sorted(j["id"] for j in found) == [2, 3]


def test_probe_creatio_page_ignores_opera_in_the_sidebar(env, probe):
    kind, found = match(env, probe_page("AI Engineer", "Creatio", "Poland"))
    assert kind == "match" and found["id"] == probe[4]


def test_probe_applied_ai_engineer_remote_is_not_the_developer_job(env, probe):
    kind, found = match(env, probe_page("Applied AI Engineer - Remote", "dexter health", "Germany"))
    assert kind == "match" and found["id"] == probe[6]


def test_probe_bare_company_name_matches_the_decorated_company(env, probe):
    kind, found = match(env, probe_page("Senior Generative AI Software Engineer", "Apertera", "Islamabad"))
    assert kind == "match" and found["id"] == probe[1]


def test_probe_short_snippet_and_unrelated_page_match_nothing(env, probe):
    assert match(env, "Sii Poland, AI Engineer – GenAI & Cloud")[0] == "none"
    barista = ("Barista\nStarbucks\nLondon\n\nMake coffee and serve customers. " * 10)
    assert match(env, barista) == ("none", None)


def test_no_match_for_unrelated_text(env):
    add_job(env, "Senior Python Developer", "Acme Corp")
    assert match(env, jd_text("Florist", "Petal Shop"))[0] == "none"


def test_title_beyond_the_first_2500_characters_does_not_match(env):
    add_job(env, "Senior Python Developer", "Acme Corp")
    text = "Lorem ipsum " * 250 + "Senior Python Developer at Acme Corp"
    assert match(env, text) == ("none", None)


def test_text_under_200_characters_is_ignored(env):
    add_job(env, "Senior Python Developer", "Acme Corp")
    assert match(env, "Senior Python Developer at Acme Corp, London. Apply now!") == ("none", None)


@pytest.mark.parametrize("state", ["closed", "expired"])
def test_closed_or_expired_jobs_are_ignored(env, state):
    add_job(env, "Data Engineer", "Globex", workflow=state)
    assert match(env, jd_text("Data Engineer", "Globex"))[0] == "none"


# ------------------------------------------------------------------ jd N --clip
def test_clip_stores_matching_text(env, capsys, monkeypatch):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    text = jd_text("Senior Python Developer", "Acme Corp")
    monkeypatch.setattr(jd_input, "read_clipboard", lambda: text)
    code, out = run(capsys, "jd", str(jid), "--clip")
    assert code == 0, out
    snaps = snapshots(env, jid)
    assert len(snaps) == 1
    assert snaps[0]["source_kind"] == "user_provided" and snaps[0]["content_text"] == text
    assert snaps[0]["verification_evidence"] == "copied from clipboard"
    assert status_of(env, jid) == "RETRIEVED"


def test_clip_accepts_a_page_that_shows_the_decorated_company_bare(env, capsys, monkeypatch):
    jid = add_job(env, "Senior AI Engineer (f/m/d)", "Apertera (formerly Alexa Translations)")
    text = jd_text("Senior AI Engineer", "Apertera")
    monkeypatch.setattr(jd_input, "read_clipboard", lambda: text)
    code, out = run(capsys, "jd", str(jid), "--clip")
    assert code == 0, out
    assert len(snapshots(env, jid)) == 1 and "--force" not in snapshots(env, jid)[0]["verification_evidence"]


def test_clip_refuses_text_that_is_not_for_this_job(env, capsys, monkeypatch):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    monkeypatch.setattr(jd_input, "read_clipboard", lambda: jd_text("Data Engineer", "Globex"))
    code, out = run(capsys, "jd", str(jid), "--clip")
    assert code == 1 and "--force" in out and "Traceback" not in out
    assert snapshots(env, jid) == []


def test_clip_force_stores_text_that_is_not_for_this_job(env, capsys, monkeypatch):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    monkeypatch.setattr(jd_input, "read_clipboard", lambda: jd_text("Data Engineer", "Globex"))
    code, out = run(capsys, "jd", str(jid), "--clip", "--force")
    assert code == 0, out
    snaps = snapshots(env, jid)
    assert len(snaps) == 1 and "--force" in snaps[0]["verification_evidence"]


def test_clip_refuses_text_under_200_characters(env, capsys, monkeypatch):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    monkeypatch.setattr(jd_input, "read_clipboard", lambda: "Senior Python Developer at Acme Corp, London")
    code, out = run(capsys, "jd", str(jid), "--clip")
    assert code == 1 and "200" in out and snapshots(env, jid) == []


def test_clip_reports_an_unreadable_clipboard_cleanly(env, capsys, monkeypatch):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")

    def broken():
        raise jd_input.ClipboardUnavailable("no clipboard tool found: install wl-clipboard")
    monkeypatch.setattr(jd_input, "read_clipboard", broken)
    code, out = run(capsys, "jd", str(jid), "--clip")
    assert code == 1 and "no clipboard tool found" in out and "Traceback" not in out


# ------------------------------------------------------------------ jd watch
def test_watch_stores_each_new_text_once(env, capsys, monkeypatch):
    a = add_job(env, "Senior Python Developer", "Acme Corp")
    b = add_job(env, "Data Engineer", "Globex")
    c = add_job(env, "Backend Engineer", "Initech")
    ta, tb, tc = (jd_text("Senior Python Developer", "Acme Corp"), jd_text("Data Engineer", "Globex"),
                  jd_text("Backend Engineer", "Initech"))
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([ta, ta, tb, tc]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.5", "--interval", "0")
    assert code == 0, out
    assert [len(snapshots(env, j)) for j in (a, b, c)] == [1, 1, 1]
    lines = out.splitlines()
    assert sum(line.startswith("stored:") for line in lines) == 3
    assert not any(line.startswith("ignored:") or line.startswith("already stored:") for line in lines)
    assert "run `py -m jobledger run --no-mail` to score" in out


def test_watch_recopying_an_old_description_says_already_stored(env, capsys, monkeypatch):
    a = add_job(env, "Senior Python Developer", "Acme Corp")
    b = add_job(env, "Data Engineer", "Globex")
    ta, tb = jd_text("Senior Python Developer", "Acme Corp"), jd_text("Data Engineer", "Globex")
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([ta, tb, ta]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.5", "--interval", "0")
    assert code == 0, out
    assert len(snapshots(env, a)) == 1 and len(snapshots(env, b)) == 1
    assert sum(line.startswith("already stored:") for line in out.splitlines()) == 1


def test_watch_reports_ambiguous_and_ignored_text(env, capsys, monkeypatch):
    a = add_job(env, "Backend Engineer", "Initech", "London")
    b = add_job(env, "Backend Engineer", "Initech", "Berlin")
    ambiguous = jd_text("Backend Engineer", "Initech", "Paris")
    unrelated = jd_text("Florist", "Petal Shop")
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([ambiguous, unrelated]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert code == 0, out
    assert f"ambiguous: copied text matches jobs {a}, {b}" in out and "jd N --clip" in out
    assert any(line.startswith("ignored:") for line in out.splitlines())
    assert snapshots(env, a) == [] and snapshots(env, b) == []


def test_watch_survives_a_busy_clipboard_and_prints_a_summary_on_ctrl_c(env, capsys, monkeypatch):
    a = add_job(env, "Senior Python Developer", "Acme Corp")
    text = jd_text("Senior Python Developer", "Acme Corp")
    monkeypatch.setattr(jd_input, "read_clipboard",
                        ScriptedClipboard([jd_input.ClipboardError("PowerShell exited with 1"), text], interrupt=True))
    code, out = run(capsys, "jd", "watch", "--interval", "0")
    assert code == 0, out
    assert len(snapshots(env, a)) == 1
    assert "still watching" in out and "summary: 1 stored" in out
    assert "run `py -m jobledger run --no-mail` to score" in out


def test_watch_without_a_clipboard_tool_stops_with_an_error(env, capsys, monkeypatch):
    def broken():
        raise jd_input.ClipboardUnavailable("no clipboard tool found")
    monkeypatch.setattr(jd_input, "read_clipboard", broken)
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.1")
    assert code == 1 and "no clipboard tool found" in out and "Traceback" not in out


# ------------------------------------------------------------------ clipboard reading (no real clipboard)
def test_windows_clipboard_command_and_utf8_decoding(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="Café — Python".encode("utf-8"), stderr=b"")
    monkeypatch.setattr(jd_input.sys, "platform", "win32")
    monkeypatch.setattr(jd_input.subprocess, "run", fake_run)
    assert jd_input.read_clipboard() == "Café — Python"
    assert seen["cmd"][:3] == ["powershell", "-NoProfile", "-Command"]
    assert "Get-Clipboard -Raw" in seen["cmd"][3]


def test_linux_without_clipboard_tools_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(jd_input.sys, "platform", "linux")
    monkeypatch.setattr(jd_input.shutil, "which", lambda name: None)
    with pytest.raises(jd_input.ClipboardUnavailable, match="wl-clipboard"):
        jd_input.read_clipboard()


# ------------------------------------------------------------------ jd N --url
def test_url_happy_path_stores_a_verified_page(env, capsys, monkeypatch):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    use_web(monkeypatch, {"https://careers.acme.example/jobs/42": Page(200, ACME_PAGE)})
    code, out = run(capsys, "jd", str(jid), "--url", "https://careers.acme.example/jobs/42")
    assert code == 0, out
    snap = snapshots(env, jid)[0]
    assert snap["source_kind"] == "user_url" and snap["status"] == "ok"
    assert snap["verification_status"] == "verified"
    assert status_of(env, jid) == "RETRIEVED"


def test_url_refuses_linkedin_and_stores_nothing(env, capsys, monkeypatch):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    fake = use_web(monkeypatch, {})
    code, out = run(capsys, "jd", str(jid), "--url", "https://www.linkedin.com/jobs/view/3988")
    assert code == 1 and "blocked by policy" in out and "Nothing was stored" in out
    assert snapshots(env, jid) == [] and status_of(env, jid) == "PENDING" and fake.calls == []


def test_url_fetch_failure_marks_the_job_failed(env, capsys, monkeypatch):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    use_web(monkeypatch, {"https://careers.acme.example/jobs/9": Page(404, "not found")})
    code, out = run(capsys, "jd", str(jid), "--url", "https://careers.acme.example/jobs/9")
    assert code == 1 and "could not fetch" in out and "FAILED" in out
    assert status_of(env, jid) == "FAILED" and snapshots(env, jid)[0]["status"] == "failed"


# ------------------------------------------------------------------ argument rules and the existing --file path
def test_jd_needs_exactly_one_source(env, capsys):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    code, out = run(capsys, "jd", str(jid))
    assert code == 1 and "exactly one" in out
    code, out = run(capsys, "jd", str(jid), "--text", "x" * 60, "--clip")
    assert code == 1 and "exactly one" in out and snapshots(env, jid) == []
    code, out = run(capsys, "jd", str(jid), "--text", "x" * 60, "--force")
    assert code == 1 and "--clip" in out and snapshots(env, jid) == []


def test_job_argument_must_be_an_id_or_watch(env, capsys):
    add_job(env, "Senior Python Developer", "Acme Corp")
    code, out = run(capsys, "jd", "abc", "--text", "x" * 60)
    assert code == 1 and "job id" in out and "Traceback" not in out


def test_jd_file_still_works(env, capsys):
    jid = add_job(env, "Senior Python Developer", "Acme Corp")
    path = env.dir / "jd.txt"
    text = jd_text("Senior Python Developer", "Acme Corp")
    path.write_text(text, encoding="utf-8")
    code, out = run(capsys, "jd", str(jid), "--file", str(path))
    assert code == 0, out
    snap = snapshots(env, jid)[0]
    assert snap["source_kind"] == "user_provided" and snap["verification_evidence"] == "supplied by the user"
    assert snap["content_text"] == text and status_of(env, jid) == "RETRIEVED"


# ------------------------------------------------------------------ explain_no_match
def test_explain_title_beyond_the_window_says_so(env):
    add_job(env, "Senior Python Developer", "Acme Corp")
    text = "Lorem ipsum " * 250 + "Senior Python Developer at Acme Corp"
    [item] = jd_input.explain_no_match(env.conn, env.pid, text)
    assert item["title_at"] == 3000 and item["company_at"] >= 0
    assert "only the first 2500" in item["problem"]


def test_explain_title_with_the_wrong_company(env):
    add_job(env, "Senior Python Developer", "Acme Corp")
    [item] = jd_input.explain_no_match(env.conn, env.pid, jd_text("Senior Python Developer", "Globex"))
    assert item["title_at"] >= 0 and item["company_at"] == -1
    assert 'company "Acme Corp" was not found' in item["problem"]


def test_explain_company_only_says_the_title_was_not_found(env):
    add_job(env, "Senior Python Developer", "Acme Corp")
    [item] = jd_input.explain_no_match(env.conn, env.pid, jd_text("Florist", "Acme Corp"))
    assert item["title_at"] == -1 and item["company_at"] >= 0
    assert 'the title "Senior Python Developer" was not found' in item["problem"]


def test_explain_unrelated_text_lists_nothing(env):
    add_job(env, "Senior Python Developer", "Acme Corp")
    assert jd_input.explain_no_match(env.conn, env.pid, jd_text("Florist", "Petal Shop")) == []


def test_explain_ranks_title_only_before_company_only_and_respects_limit(env):
    add_job(env, "Florist", "Nobody Ltd")
    companies = ["Acme Corp", "Globex", "Initech", "Hooli", "Umbrella"]
    titles = ["Chef de Cuisine", "Pilot Trainee", "Lighthouse Keeper", "Baker", "Bus Driver"]
    for title, company in zip(titles, companies):
        add_job(env, title, company)
    text = jd_text("Florist", "Petal Shop") + "Also seen: Acme Corp, Globex, Initech, Hooli, Umbrella."
    items = jd_input.explain_no_match(env.conn, env.pid, text, limit=3)
    assert len(items) == 3
    assert [i["title"] for i in items] == ["Florist", "Chef de Cuisine", "Pilot Trainee"]
    assert 'company "Nobody Ltd" was not found' in items[0]["problem"]
    assert len(jd_input.explain_no_match(env.conn, env.pid, text, limit=1)) == 1


# ------------------------------------------------------------------ jd watch explanations
def test_watch_ignored_text_shows_its_start_and_the_closest_job(env, capsys, monkeypatch):
    a = add_job(env, "Senior Python Developer", "Acme Corp")
    text = "Lorem ipsum " * 250 + "Acme Corp is hiring in London"
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([text]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert code == 0, out
    lines = out.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("ignored:"))
    assert lines[i + 1].startswith('  text starts: "Lorem ipsum Lorem ipsum')
    assert (f'  closest: job {a} "Senior Python Developer" @ Acme Corp - company found but the title '
            f'"Senior Python Developer" was not found') in lines


def test_watch_unrelated_text_says_no_job_appears(env, capsys, monkeypatch):
    add_job(env, "Senior Python Developer", "Acme Corp")
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([jd_text("Florist", "Petal Shop")]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert code == 0, out
    assert "no job title or company from this profile appears in the text" in out
    assert "closest: job" not in out


def test_watch_ambiguous_lists_each_candidate(env, capsys, monkeypatch):
    a = add_job(env, "Backend Engineer", "Initech", "London")
    b = add_job(env, "Backend Engineer", "Initech", "Berlin")
    monkeypatch.setattr(jd_input, "read_clipboard",
                        ScriptedClipboard([jd_text("Backend Engineer", "Initech", "Paris")]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert code == 0, out
    lines = out.splitlines()
    assert f'  job {a} "Backend Engineer" @ Initech (London)' in lines
    assert f'  job {b} "Backend Engineer" @ Initech (Berlin)' in lines


# ------------------------------------------------------------------ weaker match from the description alone
DOLBY_BOX = ("About the job Join the leader in entertainment innovation. Dolby is looking for a software engineer "
             "to help build our agentic AI platform. You will own services from design to production and work with "
             "research and product teams across three offices. Benefits include flexible hours and a learning budget.")
# The title phrase itself ('AI Engineer', 'Backend Engineer') must not appear in these, or the strict matcher takes them.
STATWORX_BOX = ("About the job Über uns statworx ist ein führendes Beratungsunternehmen für Künstliche Intelligenz. "
                "Als Engineer entwickelst du AI-Projekte und KI-Lösungen für unsere Kunden in Frankfurt und begleitest "
                "sie vom Konzept bis zum Betrieb. Wir bieten flexible Arbeitszeiten und ein starkes Team.")
STATWORX_TRAINEE_BOX = ("About the job Über uns statworx ist ein führendes Beratungsunternehmen für Künstliche Intelligenz. "
                        "Als Trainee entwickelst du AI-Projekte als Engineer in Frankfurt und begleitest unsere Kunden "
                        "vom Konzept bis zum Betrieb. Wir bieten flexible Arbeitszeiten und ein starkes Team.")
ACME_NO_TITLE = ("Acme Corp builds data platforms for retailers across Europe. Our team writes Python every day to "
                 "ship features, and we value curiosity, ownership and clear communication with customers and colleagues.")
NO_COMPANY_BOX = ("Software engineer agentic AI platform role: you will own services from design to production, "
                  "with flexible hours, a learning budget and a relaxed culture for every engineer on the team "
                  "in London, working on tools that our creative teams use every single day.")
INITECH_BOX = ("Initech builds backend services for banks and insurers. You will be an experienced engineer on our "
               "platform team, shipping reliable APIs, owning production systems end to end and mentoring colleagues "
               "who join from other teams across the company.")


def test_description_only_text_matches_by_company_and_title_words(env):
    jid = add_job(env, "Software Engineer Agentic AI Platform", "Dolby Laboratories")
    kind, job, detail = jd_input.match_job_by_description(env.conn, env.pid, DOLBY_BOX)
    assert kind == "weak" and job["id"] == jid
    assert detail == "company Dolby Laboratories; 5 of 5 title words"
    assert match(env, DOLBY_BOX) == ("none", None)            # the strict matcher still needs the title line


def test_statworx_text_with_trainee_picks_the_trainee_job(env):
    ai = add_job(env, "AI Engineer (m/w/d)", "statworx", "Frankfurt")
    trainee = add_job(env, "Trainee AI Engineer (m/w/d)", "statworx", "Frankfurt")
    kind, job, _ = jd_input.match_job_by_description(env.conn, env.pid, STATWORX_TRAINEE_BOX)
    assert kind == "weak" and job["id"] == trainee
    assert ai != trainee


def test_statworx_text_without_trainee_picks_the_ai_engineer_job(env):
    ai = add_job(env, "AI Engineer (m/w/d)", "statworx", "Frankfurt")
    add_job(env, "Trainee AI Engineer (m/w/d)", "statworx", "Frankfurt")
    kind, job, detail = jd_input.match_job_by_description(env.conn, env.pid, STATWORX_BOX)
    assert kind == "weak" and job["id"] == ai
    assert detail == "company statworx; 2 of 2 title words"


def test_company_only_text_with_too_few_title_words_matches_nothing(env):
    add_job(env, "Senior Python Developer", "Acme Corp")       # only 'python' of 3 title words is in the text
    assert jd_input.match_job_by_description(env.conn, env.pid, ACME_NO_TITLE) == ("none", None, None)


def test_description_without_any_company_name_matches_nothing(env, capsys, monkeypatch):
    add_job(env, "Software Engineer Agentic AI Platform", "Dolby Laboratories")
    assert jd_input.match_job_by_description(env.conn, env.pid, NO_COMPANY_BOX) == ("none", None, None)
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([NO_COMPANY_BOX]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert code == 0, out
    assert any(line.startswith("ignored:") for line in out.splitlines())
    assert "stored:" not in out and "stored (weaker match)" not in out


def test_two_identical_jobs_with_a_description_only_text_are_ambiguous(env):
    a = add_job(env, "Backend Engineer", "Initech", "London")
    b = add_job(env, "Backend Engineer", "Initech", "Berlin")
    kind, jobs, detail = jd_input.match_job_by_description(env.conn, env.pid, INITECH_BOX)
    assert kind == "ambiguous" and sorted(j["id"] for j in jobs) == [a, b] and detail is None


@pytest.mark.parametrize("state", ["closed", "expired"])
def test_closed_jobs_are_not_weaker_matches(env, state):
    add_job(env, "Software Engineer Agentic AI Platform", "Dolby Laboratories", workflow=state)
    assert jd_input.match_job_by_description(env.conn, env.pid, DOLBY_BOX) == ("none", None, None)


def test_weaker_match_stores_partial_with_evidence_and_says_provisional(env, capsys, monkeypatch):
    a = add_job(env, "AI Engineer (m/w/d)", "statworx", "Frankfurt")
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([STATWORX_BOX]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert code == 0, out
    assert (f'stored (weaker match): job {a} "AI Engineer (m/w/d)" @ statworx (' in out
            and "it will score as PROVISIONAL" in out and f"`jd {a} --clip`" in out)
    assert "summary: 1 stored" in out
    (snap,) = snapshots(env, a)
    assert snap["verification_status"] == "partial"
    assert snap["verification_evidence"] == ("copied from clipboard; weaker match: company statworx; 2 of 2 title "
                                             "words; the job title line was not in the copied text")
    assert snap["content_text"] == STATWORX_BOX
    assert status_of(env, a) == "RETRIEVED"


def test_weaker_match_is_usable_for_scoring(env, capsys, monkeypatch):
    a = add_job(env, "AI Engineer (m/w/d)", "statworx", "Frankfurt")
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([STATWORX_BOX]))
    run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert matcher.usable_jd_text(snapshots(env, a)[-1]) == STATWORX_BOX


def test_recopying_a_weaker_match_says_already_stored(env, capsys, monkeypatch):
    a = add_job(env, "AI Engineer (m/w/d)", "statworx", "Frankfurt")
    monkeypatch.setattr(jd_input, "read_clipboard",
                        ScriptedClipboard([STATWORX_BOX, jd_text("Florist", "Petal Shop"), STATWORX_BOX]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.5", "--interval", "0")
    assert code == 0, out
    assert len(snapshots(env, a)) == 1
    assert sum(line.startswith("already stored: job") for line in out.splitlines()) == 1


def test_trainee_copy_is_stored_against_the_trainee_job_only(env, capsys, monkeypatch):
    ai = add_job(env, "AI Engineer (m/w/d)", "statworx", "Frankfurt")
    trainee = add_job(env, "Trainee AI Engineer (m/w/d)", "statworx", "Frankfurt")
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([STATWORX_TRAINEE_BOX]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert code == 0, out
    assert len(snapshots(env, trainee)) == 1 and snapshots(env, ai) == []


def test_ambiguous_description_lists_each_job_with_its_location(env, capsys, monkeypatch):
    a = add_job(env, "Backend Engineer", "Initech", "London")
    b = add_job(env, "Backend Engineer", "Initech", "Berlin")
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([INITECH_BOX]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert code == 0, out
    assert (f'ambiguous: this looks like a description for one of: job {a} "Backend Engineer" @ Initech (London), '
            f'job {b} "Backend Engineer" @ Initech (Berlin); copy the page including the job title line, '
            f'or use `jd N --clip`') in out
    assert "0 stored, 0 ignored, 1 ambiguous" in out
    assert snapshots(env, a) == [] and snapshots(env, b) == []


def test_strict_match_still_stores_as_verified(env, capsys, monkeypatch):
    a = add_job(env, "Senior Python Developer", "Acme Corp")
    monkeypatch.setattr(jd_input, "read_clipboard", ScriptedClipboard([jd_text("Senior Python Developer", "Acme Corp")]))
    code, out = run(capsys, "jd", "watch", "--max-seconds", "0.3", "--interval", "0")
    assert code == 0, out
    (snap,) = snapshots(env, a)
    assert snap["verification_status"] == "verified"
    assert snap["verification_evidence"] == "copied from clipboard; matched on title+company"
    assert "stored (weaker match)" not in out


# ------------------------------------------------------------------ two-part copy: title line, then description
MOSS_DESC = ("Moss builds AI-powered software for retailers and banks across Europe. You will join the platform team "
             "in Warsaw, own services from design to production and work closely with research and product colleagues "
             "on reliable systems. Benefits include flexible hours, a learning budget and a generous leave policy.")
MOSS_HEADER = "Applied AI Engineer\nMoss\nWarsaw"


def feed(env, capsys, monkeypatch, steps):
    """Runs (text, seconds) steps through cli._watch_event with a fake clock; returns (output, counts, pending)."""
    clock = {"t": 0.0}
    monkeypatch.setattr(cli, "_now", lambda: clock["t"])
    counts = {"stored": 0, "ignored": 0, "ambiguous": 0, "already stored": 0}
    pending = {}
    for text, at in steps:
        clock["t"] = at
        cli._watch_event(env.conn, env.pid, text, counts, pending)
    return capsys.readouterr().out, counts, pending


def test_title_line_then_description_is_one_verified_match(env, capsys, monkeypatch):
    a = add_job(env, "Applied AI Engineer", "Moss", "Warsaw")
    out_, counts, pending = feed(env, capsys, monkeypatch, [(MOSS_HEADER, 0.0), (MOSS_DESC, 1.0)])
    (snap,) = snapshots(env, a)
    assert snap["verification_status"] == "verified"
    assert snap["verification_evidence"] == ("copied from clipboard in two parts: title line, then description; "
                                             "matched on title+company")
    assert snap["content_text"] == MOSS_HEADER + "\n" + MOSS_DESC
    assert f'stored (2-part copy): job {a} "Applied AI Engineer" @ Moss (' in out_
    assert counts == {"stored": 1, "ignored": 0, "ambiguous": 0, "already stored": 0}
    assert pending == {}


def test_title_line_alone_prints_nothing_and_counts_nothing(env, capsys, monkeypatch):
    add_job(env, "Applied AI Engineer", "Moss", "Warsaw")
    out_, counts, pending = feed(env, capsys, monkeypatch, [(MOSS_HEADER, 0.0)])
    assert out_ == ""
    assert counts == {"stored": 0, "ignored": 0, "ambiguous": 0, "already stored": 0}
    assert pending["header"] == MOSS_HEADER


def test_title_line_older_than_120_seconds_is_not_used(env, capsys, monkeypatch):
    a = add_job(env, "Applied AI Engineer", "Moss", "Warsaw")
    out_, counts, _ = feed(env, capsys, monkeypatch, [(MOSS_HEADER, 0.0), (MOSS_DESC, 120.5)])
    assert snapshots(env, a) == []
    assert "2-part" not in out_ and "ignored:" in out_
    assert counts["ignored"] == 1


def test_title_line_exactly_120_seconds_old_is_still_used(env, capsys, monkeypatch):
    a = add_job(env, "Applied AI Engineer", "Moss", "Warsaw")
    out_, _, _ = feed(env, capsys, monkeypatch, [(MOSS_HEADER, 0.0), (MOSS_DESC, 120.0)])
    assert len(snapshots(env, a)) == 1 and "stored (2-part copy)" in out_


def test_newer_short_copy_replaces_the_older_title_line(env, capsys, monkeypatch):
    a = add_job(env, "Applied AI Engineer", "Moss", "Warsaw")
    _, _, pending = feed(env, capsys, monkeypatch,
                         [("Old Title Here Corp", 0.0), (MOSS_HEADER, 10.0), (MOSS_DESC, 20.0)])
    (snap,) = snapshots(env, a)
    assert snap["content_text"].startswith("Applied AI Engineer\nMoss\nWarsaw\n")
    assert "Old Title" not in snap["content_text"]
    assert pending == {}


def test_title_line_is_cleared_after_a_store(env, capsys, monkeypatch):
    a = add_job(env, "Applied AI Engineer", "Moss", "Warsaw")
    out_, counts, _ = feed(env, capsys, monkeypatch,
                           [(MOSS_HEADER, 0.0), (MOSS_DESC, 1.0), (jd_text("Florist", "Petal Shop"), 2.0)])
    assert len(snapshots(env, a)) == 1
    assert out_.count("stored (2-part copy)") == 1
    assert counts["ignored"] == 1 and counts["stored"] == 1


def test_title_line_over_300_characters_is_not_a_title_line(env, capsys, monkeypatch):
    a = add_job(env, "Applied AI Engineer", "Moss", "Warsaw")
    too_long = "Applied AI Engineer " + "Warsaw " * 45
    assert len(too_long) > 300
    _, _, pending = feed(env, capsys, monkeypatch, [(too_long, 0.0), (MOSS_DESC, 1.0)])
    assert pending == {}
    assert snapshots(env, a) == []


def test_firm_two_part_match_beats_a_weaker_candidate(env, capsys, monkeypatch):
    firm = add_job(env, "Applied AI Engineer", "Moss", "Warsaw")
    other = add_job(env, "AI Engineer", "Moss", "Warsaw")
    feed(env, capsys, monkeypatch, [(MOSS_HEADER, 0.0), (MOSS_DESC, 1.0)])
    (snap,) = snapshots(env, firm)
    assert snap["verification_status"] == "verified"
    assert snapshots(env, other) == []


def test_title_line_without_the_company_uses_the_weaker_match_as_partial(env, capsys, monkeypatch):
    a = add_job(env, "Applied AI Engineer", "Moss", "Warsaw")
    header = "Applied AI, Engineer"                    # the title words, but not the title as one phrase, and no company
    out_, counts, _ = feed(env, capsys, monkeypatch, [(header, 0.0), (MOSS_DESC, 1.0)])
    (snap,) = snapshots(env, a)
    assert snap["verification_status"] == "partial"
    assert snap["verification_evidence"] == ("copied from clipboard; weaker match: company Moss; 3 of 3 title words; "
                                             "the job title line was not in the copied text")
    assert snap["content_text"] == header + "\n" + MOSS_DESC
    assert f'stored (weaker match): job {a} "Applied AI Engineer" @ Moss (' in out_
    assert counts["stored"] == 1
