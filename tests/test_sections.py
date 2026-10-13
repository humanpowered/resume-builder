"""
The guided build after the jobs: education and licences, skills, and work
outside paid jobs.
"""
import importlib
import os
import tempfile
import unittest

import helpers  # noqa: F401

from fakes import FakeBackend
from resume_builder import llm
from test_journey import SOURCE, TestClient, extract_reply

MORE = SOURCE + ("SKILLS\nEpic EHR\nEDUCATION\nBachelor of Science in Nursing, Ohio State University, 2017\n"
                 "LICENSES\nRN License, Ohio Board of Nursing\nAWARDS\nDAISY Award, 2022\n")


def reply():
    r = extract_reply()
    r["skills"] = ["Epic EHR"]
    r["education"] = ["Bachelor of Science in Nursing, Ohio State University, 2017"]
    r["certifications"] = ["RN License, Ohio Board of Nursing"]
    r["other"] = [{"heading": "Awards", "lines": ["DAISY Award, 2022"]}]
    return r


def build_reply(*extra):
    return {"field": "Nursing", "skills": [
        {"name": "Epic EHR", "category": "Systems", "same_as": "Epic EHR", "named": True,
         "because": [], "level": "Expert"},
        {"name": "Staff training", "category": "Leadership", "same_as": "", "named": True,
         "because": [], "level": "Advanced"},
        {"name": "Vitals", "category": "Clinical", "same_as": "", "named": True,
         "because": [], "level": "Working"},
        {"name": "Triage", "category": "Clinical", "same_as": "", "named": False,
         "because": [], "level": ""},
        *extra]}


@unittest.skipIf(TestClient is None, "fastapi not installed")
class Sections(unittest.TestCase):

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

    def upload(self, *replies):
        llm.use_backend(FakeBackend(*(replies or [reply()])))
        r = self.c.post("/api/j/import", files={"file": ("resume.txt", MORE.encode())})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # -- education

    def test_education_shows_its_parts_and_saving_rebuilds_the_line(self):
        s = self.upload()
        deg, lic = s["edu"]
        self.assertEqual((deg["fields"]["degree"], deg["fields"]["field"], deg["fields"]["school"],
                          deg["fields"]["year"]),
                         ("Bachelor of Science", "Nursing", "Ohio State University", "2017"))
        self.assertEqual(lic["fields"]["name"], "RN License")
        r = self.post("/api/j/edu", section="certifications", index=0, expect=lic["line"],
                      fields={"name": "RN License", "issuer": "Ohio Board of Nursing", "year": "2018",
                              "current": "No, it lapsed"})
        self.assertEqual(r["edu"][1]["line"], "RN License, Ohio Board of Nursing, 2018 (lapsed)")
        self.assertEqual(r["edu"][1]["fields"]["current"], "No, it lapsed")
        r = self.post("/api/j/edu", section="education", index=-1, fields={
            "degree": "MSN", "field": "Nursing leadership", "school": "", "year": "2027",
            "studying": True, "extra": "paying my own way working nights"})
        self.assertEqual(r["edu"][1]["line"],
                         "MSN, Nursing leadership, expected 2027. Paying my own way working nights")

    def test_a_deleted_entry_can_be_undone_and_isnt_brought_back_by_the_same_file(self):
        s = self.upload()
        d = self.post("/api/j/item/delete", section="education", index=0, expect=s["edu"][0]["line"])
        self.assertEqual(len(d["edu"]), 1)
        r = self.post("/api/j/item/restore", **{k: d["result"][k] for k in ("section", "index", "text")})
        self.assertEqual(len(r["edu"]), 2)
        self.post("/api/j/item/delete", section="education", index=0, expect=s["edu"][0]["line"])
        self.assertEqual(len(self.upload()["edu"]), 1)

    def test_a_stale_edit_is_refused(self):
        self.upload()
        r = self.c.post("/api/j/edu", json={"section": "education", "index": 0, "expect": "something else",
                                            "fields": {"degree": "BA"}})
        self.assertEqual(r.status_code, 409)

    # -- skills

    def test_skills_build_names_only_what_the_lines_show_and_never_sets_a_level(self):
        self.upload()
        llm.use_backend(FakeBackend(build_reply()))
        s = self.post("/api/j/skills/build")
        items = {k["name"]: k for g in s["skills"]["groups"] for k in g["items"]}
        self.assertEqual(items["Epic EHR"]["state"], "yes")
        self.assertEqual(items["Epic EHR"]["tag"], "From your resume")
        self.assertEqual(items["Vitals"]["state"], "yes")              # a line names it
        self.assertEqual(items["Vitals"]["shown"],
                         "Shown by: \u201cTook vitals for 20 patients a day\u201d at Mercy Clinic")
        self.assertEqual(items["Vitals"]["use"], "About 2 years · last used 2018")
        self.assertEqual(items["Staff training"]["state"], "ask")      # named nowhere in the lines
        self.assertEqual(items["Triage"]["shown"], "Common for Charge Nurse")
        self.assertTrue(all(not k["level"] for k in items.values()))
        self.assertFalse(s["skills"]["needs_build"])

    def test_a_line_names_a_skill_in_any_word_form_or_by_its_other_name(self):
        self.upload()
        self.post("/api/j/skill/add", name="Patient handoff")
        llm.use_backend(FakeBackend(build_reply(
            {"name": "Precepting", "category": "Leadership", "same_as": "", "named": True,
             "because": [], "level": ""},
            {"name": "Handoffs", "category": "Clinical", "same_as": "Patient handoff", "named": True,
             "because": [], "level": ""},
            {"name": "People management", "category": "Leadership", "same_as": "", "named": False,
             "because": [], "level": ""})))
        s = self.post("/api/j/skills/build")
        items = {k["name"]: k for g in s["skills"]["groups"] for k in g["items"]}
        self.assertEqual(items["Precepting"]["state"], "yes")        # "Precepted new graduate nurses"
        self.assertIn("Precepted new graduate nurses", items["Precepting"]["shown"])
        self.assertNotIn("Handoffs", items)                          # one skill, not two
        self.assertIn("Cut handoff errors on the unit", items["Patient handoff"]["shown"])
        self.assertEqual(items["People management"]["state"], "ask")   # implied only

    def test_a_new_target_only_adds_and_no_always_wins(self):
        self.upload()
        llm.use_backend(FakeBackend(build_reply()))
        self.post("/api/j/skills/build")
        self.post("/api/j/skill", name="Triage", have="no")
        self.c.put("/api/j/targets", json={"titles": ["Nurse Manager"], "where": "", "modes": []})
        s = self.state()
        self.assertTrue(s["skills"]["needs_build"])
        llm.use_backend(FakeBackend(build_reply(
            {"name": "Budgeting", "category": "Leadership", "same_as": "", "named": False,
             "because": [], "level": "Expert"})))
        s = self.post("/api/j/skills/build")
        items = {k["name"]: k for g in s["skills"]["groups"] for k in g["items"]}
        self.assertEqual((items["Triage"]["state"], items["Budgeting"]["state"]), ("no", "ask"))
        self.assertEqual(s["skills"]["built_for"], ["Charge Nurse", "Nurse Manager"])

    def test_levels_years_and_added_skills(self):
        self.upload()
        s = self.post("/api/j/skill", name="Epic EHR", level="Advanced")
        s = self.post("/api/j/skill", name="Epic EHR", years="6", last_used="2024")
        k = s["skills"]["groups"][0]["items"][0]
        self.assertEqual((k["level"], k["use"]), ("Advanced", "6 years · last used 2024"))
        s = self.post("/api/j/skill/add", name="Power BI")
        self.assertEqual(s["skills"]["groups"][-1]["cat"], "Added by you")
        self.assertEqual(s["skills"]["mine"], 2)
        r = self.c.post("/api/j/skill", json={"name": "Nope", "level": "Expert"})
        self.assertEqual(r.status_code, 404)

    def test_a_language_has_its_own_levels(self):
        self.upload()
        s = self.post("/api/j/skill/add", name="Spanish")
        g = next(g for g in s["skills"]["groups"] if g["cat"] == "Languages")
        self.assertEqual(g["items"][0]["levels"], ["Conversational", "Fluent", "Native"])
        s = self.post("/api/j/skill", name="Spanish", level="Fluent")
        self.assertEqual(next(g for g in s["skills"]["groups"] if g["cat"] == "Languages")["items"][0]["level"],
                         "Fluent")
        self.assertIn("- Spanish (Fluent)", self.c.get("/api/record").json()["markdown"])
        r = self.c.post("/api/j/skill", json={"name": "Spanish", "level": "Expert"})
        self.assertEqual(r.status_code, 400)
        self.post("/api/j/skill", name="Epic EHR", level="Expert")    # other skills keep theirs

    # -- beyond paid work

    def test_beyond_items_are_added_edited_and_deleted(self):
        s = self.upload()
        self.assertEqual([(i["kind"], i["text"]) for i in s["beyond"]["items"]],
                         [("Award", "DAISY Award, 2022")])
        s = self.post("/api/j/tile", name="break", fields={"from": "2015", "to": "2016",
                                                           "reason": "Caring for a parent"})
        self.assertEqual(s["result"], "Career break, 2015 to 2016: Caring for a parent")
        s = self.post("/api/j/item/edit", section="career_breaks", index=0,
                      expect=s["result"], text="Career break, 2015 to 2016")
        self.assertIn("Career break, 2015 to 2016", [i["text"] for i in s["beyond"]["items"]])
        s = self.post("/api/j/item/delete", section="awards", index=0, expect="DAISY Award, 2022")
        self.assertEqual(len(s["beyond"]["items"]), 1)

    def test_volunteering_becomes_a_job_outside_paid_work(self):
        self.upload()
        s = self.post("/api/j/joblike", name="volunteer", fields={
            "title": "Shift lead", "org": "Eastside Food Bank", "dates": "2019 - Present"})
        j = s["work"]["jobs"][s["result"]]
        self.assertTrue(j["outside"])
        self.assertEqual(s["beyond"]["jobs"][0]["employer"], "Eastside Food Bank")
        self.assertEqual(s["work"]["years"]["total"], 10)        # outside work isn't counted

    def test_sections_are_marked_done(self):
        self.upload()
        s = self.post("/api/j/section", name="edu")
        self.assertEqual(s["sections"], {"edu": True, "skills": False, "beyond": False, "summary": False})
        self.c.put("/api/j/step", json={"screen": "skills"})
        self.assertEqual(self.state()["screen"], "skills")

