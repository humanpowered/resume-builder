"""
The handover to the pipeline, per person: their record becomes the profile
the pipeline scores against, and their settings decide its limits, letter
and threshold. Nobody else's record or settings ever reach their run.
"""
import json
import os
import tempfile
import types
import unittest

import helpers  # noqa: F401

from resume_builder import matching
from resume_builder import record as mr
from resume_builder.store import MemoryStore

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

RECORD = """# R

## Contact

- **Name:** Sam Rivera
- **Email:** sam@example.com

## Roles

### Mercy Hospital — Charge Nurse

- **Dates:** 2019 - Present

#### Cut falls

- **Problem:** Falls were high
- **Actions:** Hourly rounding
- **Results:** Falls down 30%
- **Evidence:** metric
- **Bullet:** Cut patient falls 30% by introducing hourly rounding

## Skills

### Clinical

- Telemetry (Expert) — evidence: Cut falls
"""


def response(obj):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=json.dumps(obj))],
        stop_reason="end_turn", usage=types.SimpleNamespace(output_tokens=10))


class Pipeline:
    """Stands in for the pipeline's API client: answers each call from a
    queue and keeps the prompts, so a test can see what the pipeline sent."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.prompts.append(" ".join(b["text"] for m in kw["messages"]
                                     if isinstance(m["content"], list)
                                     for b in m["content"]))
        return response(self.replies.pop(0))


LETTER = {"greeting": "x", "opening": "Please consider my qualifications for Charge Nurse.",
          "groups": [{"header": h, "bullets": ["Cut patient falls 30% at Mercy Hospital"]}
                     for h in ("Safety", "Leadership", "Care")],
          "sign_off": "x", "name": "", "contact": ""}
RESUME = {"name": "Sam Rivera", "summary": "Nurse.", "skills": [],
          "experience": [{"company": "Mercy Hospital", "title": "Charge Nurse",
                          "bullets": ["Cut patient falls 30%"]}]}
POSTING = {"title": "Nurse Manager", "company": "St. Luke's", "location": "Remote",
           "description": "Lead a unit."}


class Base(unittest.TestCase):
    def use(self, client):
        st, _, _ = matching._pipeline()
        saved = st.client
        st.client = client
        self.addCleanup(setattr, st, "client", saved)
        return client


class Match(Base):
    def store(self):
        return MemoryStore(mr.parse(RECORD))

    def test_the_record_is_the_profile(self):
        p = matching.profile_for(self.store())
        self.assertEqual(p["name"], "Sam Rivera")
        self.assertIn("Cut patient falls 30% by introducing hourly rounding",
                      json.dumps(p["work_history"]))
        self.assertEqual(p["skills_by_category"], {"Clinical": ["Telemetry (Expert) [evidence: Cut falls]"]})

    def test_below_threshold_no_documents_are_drafted(self):
        c = self.use(Pipeline({"score": 4, "reasoning": "r", "overqualification_risk": False}))
        out = matching.match(self.store(), POSTING)
        self.assertEqual(len(c.prompts), 1)
        self.assertNotIn("resume", out)
        self.assertIn("Sam Rivera", c.prompts[0])

    def test_the_persons_settings_decide(self):
        s = self.store()
        matching.save_settings(s, {"tuning": {"score_threshold": 3},
                                   "letter": {"frame": {"positioning": "I keep patients safe."}}})
        c = self.use(Pipeline({"score": 4, "reasoning": "r", "overqualification_risk": False},
                              RESUME, LETTER))
        out = matching.match(s, POSTING)
        self.assertEqual(out["threshold"], 3)
        self.assertEqual(out["cover_letter"]["positioning"], "I keep patients safe.")
        self.assertIn("I keep patients safe.", c.prompts[2])

    def test_bad_settings_are_refused_and_not_stored(self):
        s = self.store()
        with self.assertRaises(ValueError):
            matching.save_settings(s, {"tuning": {"salary_floor": "high"}})
        self.assertIsNone(s.load_state(matching.SETTINGS))


@unittest.skipIf(TestClient is None, "fastapi not installed")
class OverHttp(Base):
    def setUp(self):
        import importlib
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["RB_DB"] = os.path.join(self.tmp.name, "t.sqlite3")
        os.environ["RB_DEV"] = "1"
        import web.app as appmod
        self.appmod = importlib.reload(appmod)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("RB_DEV", None)

    def client(self, email):
        c = TestClient(self.appmod.app)
        c.post("/dev/login", json={"email": email})
        return c

    def test_settings_are_per_person_and_validated(self):
        a, b = self.client("a@example.com"), self.client("b@example.com")
        r = a.put("/api/settings", json={"tuning": {"salary_floor": 90000},
                                         "boards": {"greenhouse": ["mercy"]}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(a.get("/api/settings").json()["effective"]["tuning"]["salary_floor"],
                         90000)
        self.assertIsNone(b.get("/api/settings").json()["effective"]["tuning"]["salary_floor"])
        self.assertNotIn("model", a.get("/api/settings").json()["effective"]["tuning"])
        bad = a.put("/api/settings", json={"tuning": {"model": "claude-opus-5"}})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("set by the service", bad.json()["detail"])

    def test_match_needs_a_record_then_scores_it(self):
        c = self.client("a@example.com")
        self.assertEqual(c.post("/api/match", json=POSTING).status_code, 409)
        c.put("/api/record", json={"markdown": RECORD})
        self.use(Pipeline({"score": 5, "reasoning": "r", "overqualification_risk": False}))
        out = c.post("/api/match", json=POSTING).json()
        self.assertEqual(out["score"]["score"], 5)


if __name__ == "__main__":
    unittest.main()
