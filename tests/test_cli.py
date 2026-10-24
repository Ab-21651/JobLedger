import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from jobledger import cli
from jobledger.config import reset_settings
from jobledger.testing import FIXTURES

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    for k in ("IMAP_USER", "IMAP_PASSWORD", "LLM_PROVIDER", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.chdir(tmp_path)
    reset_settings()
    yield tmp_path
    reset_settings()


def run(capsys, *argv):
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_init_reports_schema_version(cli_env, capsys):
    code, out = run(capsys, "init")
    assert code == 0 and "schema v5" in out


def test_doctor_never_prints_secrets(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("IMAP_USER", "me@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "hunter2-super-secret")
    reset_settings()
    code, out = run(capsys, "doctor")
    assert code == 0 and "hunter2" not in out and "mailbox_configured" in out


def test_end_to_end_cli_flow(cli_env, capsys):
    assert run(capsys, "profile", "create", "--name", "Sam Test", "--email", "sam@example.com", "--location", "London",
               "--arrangements", "hybrid,remote", "--types", "fulltime")[0] == 0
    assert run(capsys, "cv", "--label", "cv1", "--skills", "Python,Django,PostgreSQL", "--years", "5", "--text", "Python developer")[0] == 0
    code, out = run(capsys, "run", "--fixtures", str(FIXTURES / "emails" / "batch1_plain.json"), "--no-llm")
    assert code == 0 and "deterministic mode" in out and "register:" in out
    code, out = run(capsys, "jobs", "list")
    assert code == 0 and "Acme" in out
    code, out = run(capsys, "export")
    assert code == 0 and ".xlsx" in out


def test_run_without_profile_is_a_clean_error(cli_env, capsys):
    code, out = run(capsys, "run", "--no-mail")
    assert code == 1 and "profile" in out and "Traceback" not in out


def test_unknown_profile_is_a_clean_error(cli_env, capsys):
    run(capsys, "init")
    code, out = run(capsys, "--profile", "42", "jobs", "list")
    assert code == 1 and "does not exist" in out


def test_database_error_exits_nonzero_with_clean_message(cli_env, capsys, monkeypatch):
    run(capsys, "profile", "create", "--name", "Sam Test", "--email", "sam@example.com")

    def boom(self, read_mail=True):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr("jobledger.agent.JobLedgerAgent.run", boom)
    code, out = run(capsys, "run", "--no-mail")
    assert code == 1 and "database error" in out and "Traceback" not in out


def test_failed_run_status_is_a_nonzero_exit_with_no_success_text(cli_env, capsys, monkeypatch):
    run(capsys, "profile", "create", "--name", "Sam Test", "--email", "sam@example.com")
    from jobledger.agent import RunResult
    fake = RunResult(run_id="x", status="failed", steps=1, counters={}, decisions={}, errors=["disk full"], mail_status=None,
                     export=None, recovered_runs=[], stopped_reason="database write failed")
    monkeypatch.setattr("jobledger.agent.JobLedgerAgent.run", lambda self, read_mail=True: fake)
    code, out = run(capsys, "run", "--no-mail")
    assert code == 1 and "did not finish" in out and "completed" not in out


def test_module_entrypoint_runs():
    p = subprocess.run([sys.executable, "-m", "jobledger", "--help"], cwd=str(ROOT), env=dict(os.environ, PYTHONPATH=str(ROOT)),
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "JobLedger" in p.stdout
