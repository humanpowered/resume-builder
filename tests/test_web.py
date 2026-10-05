"""
The web service: per-user isolation, the interview over HTTP, the user's
right to download and delete, and refusing to run without sign-in.
"""
import importlib
import os
import tempfile
import unittest

import helpers  # noqa: F401

try:
    from fastapi.testclient import TestClient
except ImportError:          # the web layer is optional for the engine
    TestClient = None

from fakes import FakeBackend
from resume_builder import llm


def turn(status="asking", say="?", **draft):
    d = {"title": "", "problem": "", "actions": "", "results": "",
         "evidence": "", "skills": []}
    d.update(draft)
    return {"say": say, "draft": d, "status": status}


@unittest.skipIf(TestClient is None, "fastapi not installed")
class Web(unittest.TestCase):

    def setUp(self):
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

    def seed(self, c, employer="Riverside"):
        md = f"# R\n\n## Roles\n\n### {employer} — RN\n\n- **Dates:** 2019 - Present\n"
        self.assertEqual(c.put("/api/record", json={"markdown": md}).status_code, 200)

    def test_one_user_cannot_see_another_users_record(self):
        a, b = self.client("a@example.com"), self.client("b@example.com")
        self.seed(a, "Riverside")
        self.assertIn("Riverside", a.get("/api/record").json()["markdown"])
        self.assertNotIn("Riverside", b.get("/api/record").json()["markdown"])
        self.assertFalse(b.get("/api/record").json()["exists"])

    def test_signed_out_requests_are_refused(self):
        c = TestClient(self.appmod.app)
        self.assertEqual(c.get("/api/record").status_code, 401)

    def test_without_dev_mode_the_service_refuses_to_run(self):
        """No real sign-in configured means no service, not an open one."""
        os.environ.pop("RB_DEV")
        self.appmod = importlib.reload(self.appmod)
        c = TestClient(self.appmod.app)
        c.cookies.set("rb_user", "a@example.com")
        self.assertEqual(c.get("/api/record").status_code, 503)
        self.assertEqual(c.post("/dev/login", json={"email": "a@example.com"}).status_code, 404)

    def test_interview_over_http_resumes_across_requests(self):
        c = self.client("a@example.com")
        self.seed(c)
        llm.use_backend(FakeBackend(
            turn(say="What was the unit like when you started?"),
            turn("complete", title="Cut falls", problem="Falls were high",
                 actions="Hourly rounding", results="Falls down 30%", evidence="metric"),
            turn("role_done")))
        p = c.post("/api/interview/step", json={"answer": None}).json()
        self.assertTrue(p["text"].startswith("In a sentence"))       # role context first
        for _ in range(7):
            if p["text"].startswith("What was the unit"):
                break
            p = c.post("/api/interview/step", json={"answer": ""}).json()
        self.assertEqual(p["text"], "What was the unit like when you started?")
        p = c.post("/api/interview/step", json={"answer": "Lots of falls"}).json()
        self.assertTrue(any("recorded: Cut falls" in n for n in p["notes"]))
        self.assertEqual(c.get("/api/health").json()["quantified"], 1)

    def test_education_over_http_needs_no_model(self):
        c = self.client("a@example.com")
        self.seed(c)
        step = lambda a: c.post("/api/interview/step",
                                json={"answer": a, "role": "background"}).json()
        self.assertTrue(step(None)["text"].startswith("Your next qualification"))
        for a in ("High school diploma", "", "Lakeside High", "2010"):
            step(a)
        self.assertIn("recorded: High school diploma, Lakeside High, 2010", step("")["notes"])
        step("")
        self.assertEqual(step("")["kind"], "done")
        self.assertIn("High school diploma, Lakeside High, 2010",
                      c.get("/api/record").json()["markdown"])

    def test_suggested_skill_confirm_and_reject(self):
        c = self.client("a@example.com")
        md = ("# R\n\n## Roles\n\n### Riverside — RN\n\n#### Cut falls\n\n"
              "- **Problem:** p\n- **Actions:** a\n- **Results:** Falls down 30%\n")
        c.put("/api/record", json={"markdown": md})
        llm.use_backend(FakeBackend({"profession": "nurse", "skills": [
            {"name": "Fall prevention", "category": "Patient Safety", "because": ["Cut falls"]},
            {"name": "Telemetry", "category": "Clinical", "because": ["Cut falls"]}]}))
        self.assertEqual(len(c.post("/api/skills/suggest").json()["added"]), 2)
        c.post("/api/skills/Fall prevention/confirm", json={"level": "Advanced"})
        c.post("/api/skills/Telemetry/confirm", json={"level": None})
        skills = c.get("/api/record").json()["summary"]["skills"]
        self.assertEqual([(s["name"], s["category"], s["pending"]) for s in skills],
                         [("Fall prevention", "Patient Safety", False)])
        csv_text = c.get("/api/export/skills_inventory.csv").text
        self.assertIn("Patient Safety,Fall prevention,yes,Advanced", csv_text)

    def test_delete_removes_everything(self):
        c = self.client("a@example.com")
        self.seed(c)
        self.seed(c, "Second Employer")        # creates a saved version too
        c.delete("/api/me")
        c2 = self.client("a@example.com")
        self.assertFalse(c2.get("/api/record").json()["exists"])

    def test_import_rejects_unsupported_files(self):
        c = self.client("a@example.com")
        r = c.post("/api/import", files={"file": ("x.exe", b"MZ" * 100)})
        self.assertEqual(r.status_code, 415)


if __name__ == "__main__":
    unittest.main()
