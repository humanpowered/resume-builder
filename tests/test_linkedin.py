"""
The LinkedIn profile: text to paste into LinkedIn, written from a finished
record. Titles and dates come from the record, figures must be the record's,
exact figures are the default, and rewrites are counted.
"""
import importlib
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401

from fakes import FakeBackend
from resume_builder import llm
from test_journey import SOURCE, TestClient, extract_reply


def pair(text, soft=""):
    return {"text": text, "soft": soft}


def profile(**more):
    return {"headlines": [pair("Charge Nurse | Night shift leadership"),
                          pair("Nurse leader who cuts handoff errors"),
                          pair("Charge Nurse at Riverside Hospital")],
            "about": pair("I lead night shifts at Riverside Hospital.\n\nI led a team of 8 nurses.",
                          ""),
            "close_active": pair("I'm looking for my next nurse manager role."),
            "close_offers": pair("I'm glad to talk about nurse manager roles."),
            "jobs": [{"job": "j0", "text": "Ran nights.\n\n• Led a team of 8 nurses", "soft": ""},
                     {"job": "j1", "text": "Bedside nursing.\n\n• Precepted new graduates", "soft": ""},
                     {"job": "j9", "text": "A job that isn't there", "soft": ""}],
            **more}


@unittest.skipIf(TestClient is None, "fastapi not installed")
class LinkedIn(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["RB_DB"] = os.path.join(self.tmp.name, "t.sqlite3")
        os.environ["RB_DEV"] = "1"
        import web.app as appmod
        self.c = TestClient(importlib.reload(appmod).app)
        self.c.post("/dev/login", json={"email": "pat@example.com"})
        llm.use_backend(FakeBackend(extract_reply()))
        self.c.post("/api/j/import", files={"file": ("resume.txt", SOURCE.encode())})

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("RB_DEV", None)

    def post(self, path, **body):
        r = self.c.post(path, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def finish(self):
        for j in self.c.get("/api/j/state").json()["work"]["jobs"]:
            self.post("/api/j/job/mark", job=j["key"], what="done")
        for name in ("edu", "skills"):
            self.post("/api/j/section", name=name)
        self.post("/api/j/summary/approve", text="Charge nurse.")

    def write(self, *replies):
        self.backend = FakeBackend(*replies)
        llm.use_backend(self.backend)
        return self.post("/api/j/linkedin/write")

    def test_it_waits_for_a_finished_record(self):
        llm.use_backend(FakeBackend())          # any model call would fail
        r = self.c.post("/api/j/linkedin/write")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Finish your record", r.json()["detail"])
        self.assertIsNone(self.c.get("/api/j/state").json()["linkedin"]["profile"])

    def test_titles_and_dates_come_from_the_record_and_every_job_is_listed(self):
        self.finish()
        v = self.write(profile())
        p = v["profile"]
        self.assertEqual([(j["title"], j["dates"]) for j in p["jobs"]],
                         [("Charge Nurse", "2021 - Present"), ("Registered Nurse", "2018 - 2021"),
                          ("Nurse Intern", "2017 - 2018")])
        self.assertEqual(p["jobs"][0]["text"], "Ran nights.\n\n• Led a team of 8 nurses")
        self.assertEqual(p["jobs"][2]["text"], "")      # not written; the page offers to write it
        self.assertEqual(len(p["headlines"]), 3)
        self.assertEqual(v["rewrites_left"], 10)        # the first profile is free
        prompt = self.backend.requests[0]["messages"][0]["content"]
        self.assertIn("[j0] Charge Nurse, Riverside Hospital", prompt)
        self.assertIn("Led a team of 8 nurses on nights", prompt)

    def test_who_should_know_sets_the_end_of_about(self):
        self.finish()
        v = self.write(profile())
        self.assertTrue(v["profile"]["about"].endswith("I'm glad to talk about nurse manager roles."))
        r = self.c.put("/api/j/linkedin", json={"open": "active"}).json()
        self.assertTrue(r["profile"]["about"].endswith("I'm looking for my next nurse manager role."))
        r = self.c.put("/api/j/linkedin", json={"open": "quiet"}).json()
        self.assertTrue(r["profile"]["about"].endswith("I led a team of 8 nurses."))
        self.assertEqual(self.c.put("/api/j/linkedin", json={"open": "loud"}).status_code, 400)

    def test_exact_figures_are_the_default_and_softening_is_a_choice(self):
        self.finish()
        v = self.write(profile(about=pair("I led a team of 8 nurses on a unit with 20 beds.",
                                          "I led a team of 8 nurses on a small unit.")))
        self.assertFalse(v["soften"])
        self.assertTrue(v["profile"]["has_money"])
        self.assertIn("20 beds", v["profile"]["about"])
        r = self.c.put("/api/j/linkedin", json={"soften": True}).json()
        self.assertIn("a small unit", r["profile"]["about"])
        self.assertIn("Led a team of 8 nurses", r["profile"]["jobs"][0]["text"])   # nothing to soften

    def test_a_figure_the_record_doesnt_hold_is_asked_again_then_flagged(self):
        self.finish()
        bad = profile(about=pair("I led a team of 12 nurses."))
        v = self.write(bad, profile())
        self.assertEqual(len(self.backend.requests), 2)
        self.assertIn("12", self.backend.requests[1]["messages"][0]["content"])
        self.assertEqual(v["profile"]["unsupported"], [])
        v = self.write(bad, bad)
        self.assertEqual(v["profile"]["unsupported"], ["12"])

    def test_rewrites_are_counted_and_capped(self):
        self.finish()
        self.write(profile())
        llm.use_backend(FakeBackend({"items": [pair("Night charge nurse | Team of 8")]}))
        v = self.post("/api/j/linkedin/rewrite", section="headline")
        self.assertEqual(v["profile"]["headlines"], ["Night charge nurse | Team of 8"])
        self.assertEqual(v["rewrites_left"], 9)
        key = v["profile"]["jobs"][2]["key"]
        llm.use_backend(FakeBackend({"items": [pair("Student placement.\n\n• Took vitals for 20 patients a day")]}))
        v = self.post("/api/j/linkedin/rewrite", section="job:" + key)
        self.assertIn("20 patients", v["profile"]["jobs"][2]["text"])
        self.assertEqual(self.c.post("/api/j/linkedin/rewrite", json={"section": "job:nobody|nothing"}).status_code, 404)

    @mock.patch.dict(os.environ, {"RB_LINKEDIN_REWRITES": "1"})
    def test_once_the_rewrites_are_used_nothing_more_is_spent(self):
        self.finish()
        self.write(profile())
        llm.use_backend(FakeBackend({"items": [pair("I run nights at Riverside Hospital.")]}))
        self.assertEqual(self.post("/api/j/linkedin/rewrite", section="about")["rewrites_left"], 0)
        llm.use_backend(FakeBackend())          # any model call would fail
        r = self.c.post("/api/j/linkedin/rewrite", json={"section": "about"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("all 1 rewrites", r.json()["detail"])
        self.assertEqual(self.c.post("/api/j/linkedin/write").status_code, 400)

    def test_a_failed_call_uses_no_rewrite(self):
        self.finish()
        self.write(profile())
        llm.use_backend(FakeBackend("not json", "still not json"))
        self.assertEqual(self.c.post("/api/j/linkedin/rewrite", json={"section": "about"}).status_code, 502)
        self.assertEqual(self.c.get("/api/j/linkedin").json()["rewrites_left"], 10)

    def test_a_changed_record_marks_the_profile_out_of_date(self):
        self.finish()
        self.assertFalse(self.write(profile())["profile"]["stale"])
        self.c.post("/api/j/jobs", json={"employer": "Lakeside Health", "title": "Nurse Manager",
                                         "dates": "2025 - Present"})
        v = self.c.get("/api/j/linkedin").json()["profile"]
        self.assertTrue(v["stale"])
        self.assertEqual(v["jobs"][0]["title"], "Nurse Manager")
        self.assertEqual(v["jobs"][0]["text"], "")

    def test_the_headline_choice_and_pasted_parts_are_kept(self):
        self.finish()
        self.write(profile())
        r = self.c.put("/api/j/linkedin", json={"headline": 2, "pasted": ["headline", "skills", "x"]}).json()
        self.assertEqual(r["profile"]["headline"], 2)
        self.assertEqual(r["pasted"], ["headline", "skills"])
        self.assertEqual(self.c.put("/api/j/linkedin", json={"headline": 7}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
