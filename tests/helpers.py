"""Puts the package on the import path and makes sure no test can reach the
API: the model layer gets a backend that fails loudly if anything calls it
without a test supplying its own."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from resume_builder import llm  # noqa: E402


class _NoNetwork:
    def create(self, **kw):
        raise AssertionError("a test reached the model without a FakeBackend")


llm.use_backend(_NoNetwork())
