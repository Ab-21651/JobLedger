import pytest

from jobledger.testing import FakeIMAP, fixture_imap, imap_reader, make_raw
from jobledger.tools.email_reader import fetch_job_alert_emails


def _fetch(fake, **kw):
    kw.setdefault("filter_subjects", ["job alert"])
    kw.setdefault("since_days", 3650)
    return fetch_job_alert_emails("imap.example", 993, "u@example.com", "pw", imap_factory=fake.factory, **kw)


def _raw(subject="Job alert: new roles", body="1. Dev\nCo - Remote\nApply: https://co.example/j\n"):
    return make_raw(subject, "alerts@jobs.example", body, date="Thu, 01 Oct 2026 09:00:00 +0000")


def test_reader_is_read_only():
    fake = FakeIMAP({1: _raw(), 2: _raw("Job alert two")})
    out = _fetch(fake)
    assert out.ok and len(out.messages) == 2
    assert fake.selected_readonly is True
    fetches = [c for c in fake.commands if c[:2] == ("UID", "FETCH")]
    assert fetches and all("BODY.PEEK" in c[3] for c in fetches)
    verbs = {c[1] for c in fake.commands if c[0] == "UID"}
    assert verbs <= {"SEARCH", "FETCH"}


def test_non_matching_subjects_are_skipped():
    out = _fetch(FakeIMAP({1: _raw("Your invoice"), 2: _raw("Job alert ok")}))
    assert [m.subject for m in out.messages] == ["Job alert ok"]


def test_cursor_resumes_after_last_uid():
    fake = FakeIMAP({1: _raw("Job alert 1"), 2: _raw("Job alert 2"), 3: _raw("Job alert 3")})
    out = _fetch(fake, last_uid=2)
    assert [m.uid for m in out.messages] == ["3"]


def test_cursor_does_not_pass_a_failed_message():
    fake = FakeIMAP({1: _raw("Job alert 1"), 2: _raw("Job alert 2"), 3: _raw("Job alert 3")}, fail_uids=(2,))
    out = _fetch(fake)
    assert out.errors
    assert out.cursor_uid <= 1          # never advances past the message that failed


def test_auth_failure_single_login_clear_error():
    fake = FakeIMAP({1: _raw()}, auth_ok=False)
    out = _fetch(fake)
    assert not out.ok and fake.logins == 1
    assert "pw" not in " ".join(out.errors)


def test_connect_failure_and_timeout():
    def boom(h, p, t):
        raise ConnectionRefusedError("no")
    out = fetch_job_alert_emails("h", 993, "u", "p", imap_factory=boom)
    assert out.status == "connect_failed"

    def slow(h, p, t):
        raise TimeoutError("t")
    assert fetch_job_alert_emails("h", 993, "u", "p", imap_factory=slow).status == "timeout"


def test_missing_credentials_run_does_not_connect_and_is_reported(ws):
    assert not ws.settings.has_email()
    res = ws.agent().run(read_mail=True)
    assert res.status in ("completed", "completed_with_errors")
    assert res.mail_status in ("no_mailbox_configured",), res.mail_status


def test_fixture_imap_helper_round_trip():
    out = imap_reader(fixture_imap("batch1_plain.json"))(0)
    assert out.ok and out.messages
