"""
The end of the first build and after it: the career story, the summary,
the finish line, home's suggestions, welcome back, and a later upload shown
as a review that never quietly brings back what the person deleted.
"""
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import helpers  # noqa: F401

from fakes import FakeBackend
from resume_builder import finish, llm
from resume_builder.store import MemoryStore
from test_journey import SOURCE, TestClient, extract_reply

NEWER = SOURCE.replace("EXPERIENCE\n", "EXPERIENCE\nLakeside Health\nNurse Manager   2025 - Present\n"
                       "• Ran a 30-bed unit\n") + "• Cut overtime on nights by 12%\nSKILLS\nEpic EHR\n"


def newer_reply():
    r = extract_reply()
    r["roles"] = [{"employer": "Lakeside Health", "title": "Nurse Manager", "dates": "2025 - Present",
                   "location": "", "company_description": "", "bullets": ["Ran a 30-bed unit"]}] + r["roles"]
    r["roles"][1]["bullets"].append("Cut overtime on nights by 12%")
    r["skills"] = ["Epic EHR"]
    return r


@unittest.skipIf(TestClient is None, "fastapi not installed")
class Finish(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["RB_DB"] = os.path.join(self.tmp.name, "t.sqlite3")
        os.environ["RB_DEV"] = "1"
        import web.app as appmod
        self.c = TestClient(importlib.reload(appmod).app)
        self.c.post("/dev/login", json={"email": "pat@example.com"})

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("RB_DEV", None)

    def state(self):
        return self.c.get("/api/j/state").json()

    def post(self, path, **body):
        r = self.c.post(path, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def upload(self, text=SOURCE, reply=None):
        llm.use_backend(FakeBackend(reply or extract_reply()))
        r = self.c.post("/api/j/import", files={"file": ("resume.txt", text.encode())})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def user(self):
        return _conn().execute("SELECT user_id FROM records").fetchone()[0]

    def finish_jobs(self):
        for j in self.state()["work"]["jobs"]:
            self.post("/api/j/job/mark", job=j["key"], what="done")

    # -- the career story and the summary

    def test_story_answers_go_into_the_record_and_can_be_changed(self):
        self.upload()
        r = self.c.put("/api/j/story", json={"fields": {"best": "Calming a chaotic night shift",
                                                         "next": "Nurse manager"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([q["answer"] for q in r.json()["story"]["questions"]],
                         ["Calming a chaotic night shift", "", "Nurse manager"])
        md = self.c.get("/api/record").json()["markdown"]
        self.assertIn("Best at: Calming a chaotic night shift", md)
        self.c.put("/api/j/story", json={"fields": {"best": ""}})
        md = self.c.get("/api/record").json()["markdown"]
        self.assertNotIn("Best at:", md)
        self.assertIn("Next move: Nurse manager", md)

    def test_a_draft_with_a_figure_the_record_lacks_is_never_shown(self):
        self.upload()
        llm.use_backend(FakeBackend({"summary": "Charge nurse who led 8 nurses and cut errors 40%."},
                                    {"summary": "Charge nurse who led a team of 8 nurses on nights."}))
        s = self.post("/api/j/summary/draft")
        self.assertEqual(s["summary"]["drafts"], ["Charge nurse who led a team of 8 nurses on nights."])
        llm.use_backend(FakeBackend({"summary": "Led 50 nurses."}, {"summary": "Saved $2M."}))
        s = self.post("/api/j/summary/draft")
        self.assertTrue(s["result"]["failed"])
        self.assertEqual(len(s["summary"]["drafts"]), 1)

    def test_approving_saves_the_summary_and_marks_the_section(self):
        self.upload()
        s = self.post("/api/j/summary/approve", text="  Charge nurse with   eight years. ")
        self.assertEqual(s["summary"]["current"], "Charge nurse with eight years.")
        self.assertTrue(s["summary"]["approved"])
        self.assertTrue(s["sections"]["summary"])
        r = self.c.post("/api/j/summary/approve", json={"text": "  "})
        self.assertEqual(r.status_code, 400)

    # -- the finish line

    def test_the_record_is_complete_once_every_position_and_section_is_done(self):
        self.upload()
        f = self.state()["finish"]
        self.assertEqual((f["positions"], f["complete"], f["record_complete"]), (3, 0, False))
        self.assertEqual(f["sections_left"], ["edu", "skills", "summary"])
        self.assertGreater(f["minutes_left"], 0)
        self.finish_jobs()
        for name in ("edu", "skills"):
            self.post("/api/j/section", name=name)
        self.assertFalse(self.state()["finish"]["record_complete"])
        self.post("/api/j/summary/approve", text="Charge nurse.")
        f = self.state()["finish"]
        self.assertTrue(f["record_complete"])
        self.assertEqual(f["minutes_left"], 0)
        self.c.post("/api/j/jobs", json={"employer": "Lakeside Health", "title": "Nurse Manager",
                                         "dates": "2025 - Present"})
        f = self.state()["finish"]
        self.assertEqual((f["record_complete"], f["first_build_done"]), (False, True))

    # -- home

    def test_suggestions_are_specific_at_most_three_and_dismissable(self):
        self.upload()
        s = self.state()["suggestions"]
        self.assertEqual(len(s), 3)
        self.assertEqual(s[0]["text"], "1 line at Riverside Hospital doesn't have a number yet.")
        self.assertEqual((s[0]["screen"], s[0]["at"]), ("lines", "riversidehospital|chargenurse"))
        self.assertEqual(s[1]["key"], "target")
        r = self.post("/api/j/dismiss", name=s[0]["key"])
        self.assertNotIn(s[0]["key"], [x["key"] for x in r["suggestions"]])
        self.assertTrue(self.state()["updated"])

    def test_a_new_target_after_approval_asks_to_review_the_summary(self):
        self.upload()
        self.post("/api/j/summary/approve", text="Charge nurse.")
        self.c.put("/api/j/targets", json={"titles": ["Nurse Manager"], "where": "", "modes": []})
        self.assertIn("target-summary", [x["key"] for x in self.state()["suggestions"]])

    # -- coming back

    def test_the_page_learns_how_long_the_person_was_away(self):
        self.upload()
        self.assertEqual(self.state()["away_hours"], 0)
        from resume_builder.store import SqlStore
        store = SqlStore(_conn(), self.user())
        step = store.load_state("journey.step")
        step["seen_at"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        store.save_state("journey.step", step)
        s = self.state()
        self.assertGreaterEqual(s["away_hours"], 29.9)
        self.assertEqual(self.state()["away_hours"], 0)

    # -- a later upload

    def test_a_later_upload_is_a_review_and_deleted_things_are_offered_not_added(self):
        s = self.upload()
        self.c.put("/api/j/step", json={"screen": "check"})
        intern = next(j for j in s["jobs"] if j["title"] == "Nurse Intern")
        r = self.c.delete(f"/api/j/jobs/{intern['index']}", params={"expect": intern["label"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.post("/api/j/jobs/0/lines/1/delete", expect=s["jobs"][0]["label"], kind="resume",
                  expect_text="Cut handoff errors on the unit")
        s = self.upload(NEWER, newer_reply())
        self.assertTrue(s["reviewing"])
        self.assertEqual(len(s["jobs"]), 2)                        # nothing saved yet
        items = {x["text"]: x for x in s["review"]["items"]}
        self.assertEqual((items["Nurse Manager, Lakeside Health"]["group"],
                          items["Nurse Manager, Lakeside Health"]["pick"]), ("job", True))
        self.assertEqual((items["Nurse Intern, Mercy Clinic"]["group"],
                          items["Nurse Intern, Mercy Clinic"]["pick"]), ("deleted_job", False))
        self.assertEqual(items["Cut handoff errors on the unit"]["group"], "deleted_line")
        self.assertEqual(items["Cut overtime on nights by 12%"]["group"], "line")
        self.assertEqual(items["Epic EHR"]["group"], "skill")
        picked = [x["id"] for x in s["review"]["items"] if x["pick"]]
        picked.append(items["Nurse Intern, Mercy Clinic"]["id"])
        r = self.post("/api/j/review/apply", ids=picked)
        self.assertEqual(r["added"], {"jobs": 2, "lines": 1, "other": 1})
        self.assertIsNone(r["review"])
        self.assertEqual([j["title"] for j in r["jobs"]],
                         ["Nurse Manager", "Charge Nurse", "Registered Nurse", "Nurse Intern"])
        self.assertNotIn("Cut handoff errors on the unit", [l["text"] for l in r["jobs"][1]["lines"]])
        # The job they took back is no longer remembered as deleted.
        s = self.upload(NEWER, newer_reply())
        groups = {x["text"]: x["group"] for x in s["review"]["items"]}
        self.assertNotIn("Nurse Intern, Mercy Clinic", groups)
        self.assertEqual(groups, {"Cut handoff errors on the unit": "deleted_line"})
        r = self.post("/api/j/review/drop")
        self.assertIsNone(r["review"])

    def test_the_first_upload_before_checking_is_still_added_directly(self):
        s = self.upload()
        self.assertNotIn("reviewing", s)
        self.assertEqual(len(s["jobs"]), 3)


def _conn():
    from resume_builder import store
    return store.connect(store.database())


class Engine(unittest.TestCase):

    def test_stamp_reports_hours_away(self):
        store = MemoryStore()
        now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
        self.assertEqual(finish.stamp(store, now), 0)
        self.assertAlmostEqual(finish.stamp(store, now + timedelta(hours=20)), 20)

    def test_minutes_left_is_rounded_to_five(self):
        job = {"open_questions": 4, "counts": {"todo": 3, "skipped": 0}, "more": False}
        self.assertEqual(finish.minutes_left([job], ["skills", "summary"]), 25)
        self.assertEqual(finish.minutes_left([], []), 0)
