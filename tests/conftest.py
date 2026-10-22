"""Shared fixtures. Everything runs offline: FakeWeb, FakeIMAP and ScriptedLLM come from jobledger.testing."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobledger.db.schema import init_db            # noqa: E402
from jobledger.testing import (FakeWeb, make_agent, make_settings, riley_profile, sam_profile)  # noqa: E402


class Ws:
    """A throw-away workspace: temp dir, settings, open SQLite connection, fake web, Sam's profile."""

    def __init__(self, tmp_path):
        self.dir = str(tmp_path)
        self.settings = make_settings(self.dir)
        self.conn = init_db(self.settings.db_path)
        self.web = FakeWeb()
        self.pid = sam_profile(self.conn)

    def riley(self):
        return riley_profile(self.conn)

    def agent(self, pid=None, **kw):
        return make_agent(self.conn, pid or self.pid, self.settings, web=kw.pop("web", self.web), **kw)

    def reopen(self):
        self.conn.close()
        self.conn = init_db(self.settings.db_path)


@pytest.fixture
def ws(tmp_path):
    w = Ws(tmp_path)
    yield w
    try:
        w.conn.close()
    except Exception:
        pass
