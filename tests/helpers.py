"""Puts the package on the import path and makes sure no test can reach the
API: the model layer gets a backend that fails loudly if anything calls it
without a test supplying its own."""
import os
import sys
from pathlib import Path

# The pipeline builds its API client when imported; this key lets it import
# and would fail loudly if anything ever used it.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-tests-never-call-out")

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from resume_builder import llm  # noqa: E402


class _NoNetwork:
    def create(self, **kw):
        raise AssertionError("a test reached the model without a FakeBackend")


llm.use_backend(_NoNetwork())


# RB_TEST_DATABASE_URL=postgresql://... runs every test against that Postgres
# database instead of a temporary SQLite file, emptied before each test.
if os.environ.get("RB_TEST_DATABASE_URL"):
    import unittest
    from resume_builder import store as _store

    os.environ["RB_DATABASE_URL"] = os.environ["RB_TEST_DATABASE_URL"]
    _run = unittest.TestCase.run

    def _run_on_empty_database(self, result=None):
        conn = _store.connect(os.environ["RB_DATABASE_URL"])
        _store.SqlStore.init(conn)
        _store.SqlStore.wipe_all(conn)
        conn.close()
        return _run(self, result)

    unittest.TestCase.run = _run_on_empty_database
