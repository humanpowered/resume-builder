"""
Shared setup and fixtures for the test suite.

Three things every test file needs:

  1. src/ on the import path, since the modules are scripts rather than an
     installed package.
  2. a dummy ANTHROPIC_API_KEY. score_and_tailor builds its API client at
     import, so importing it with no key set raises before a single test runs.
     Nothing here ever calls the API -- the one test that exercises the retry
     loop substitutes a stub client -- so the value is deliberately fake and
     would fail loudly if anything tried to use it.
  3. fixtures that do not come from anyone's real profile. The tests have to
     pass in a bare clone, where profile/ and config/ are gitignored and
     absent, so every input is built here.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-tests-never-call-out")


def profile(**overrides) -> dict:
    """A small, self-consistent profile. Numbers here are the only numbers the
    grounding lint should accept."""
    p = {
        "name": "Jordan Avery",
        "email": "jordan@example.com",
        "phone": "555-0100",
        "location": "Portland, OR",
        "years_experience": 18,
        "summary": "Analytics leader.",
        "work_history": [
            {"company": "Northwind Retail", "title": "Head of Analytics",
             "dates": "2021 - Present", "scope": "team of 9",
             "company_descriptor": "a home goods retailer serving 12 brands",
             "highlights": ["Cut acquisition cost 23% across paid search",
                            "Built the measurement practice from scratch"]},
            {"company": "Baker Media", "title": "Director, Analytics",
             "dates": "2016 - 2021", "company_descriptor": "a media agency",
             "highlights": ["Launched a marketing mix model covering 4 channels"]},
        ],
        "skills_by_category": {"Measurement": ["Marketing Mix Modeling", "Incrementality"]},
        "education": [{"school": "State University", "degree": "BA Economics"}],
    }
    p.update(overrides)
    return p


def letter(**overrides) -> dict:
    """A letter that passes every lint, so a test can break one thing at a
    time and know the failure it sees is the one it caused."""
    L = {
        "greeting": "Dear Hiring Manager,",
        "opening": "Please consider my qualifications for the Director, Analytics role at Acme.",
        "positioning": "Fixed positioning paragraph.",
        "lead_in": "Fixed lead-in paragraph.",
        "groups": [
            {"header": "Measurement",
             "bullets": ["Cut acquisition cost 23% across paid search at Northwind Retail",
                         "Launched a marketing mix model covering 4 channels at Baker Media"]},
            {"header": "Team leadership",
             "bullets": ["Led a team of 9 analysts at Northwind Retail"]},
            {"header": "Methods",
             "bullets": ["Built incrementality testing into the Northwind Retail roadmap"]},
        ],
        "closing_para": "Fixed closing paragraph.",
        "sign_off": "Sincerely,",
        "name": "Jordan Avery",
        "contact": "jordan@example.com\n555-0100",
    }
    L.update(overrides)
    return L
