"""
The guided build's record page: seeing, adding, editing and deleting jobs and
lines, Undo, where a refresh lands, and a later upload never quietly bringing
back what the person deleted.
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

SOURCE = ("PAT LEE\nColumbus, OH | pat@example.com\nEXPERIENCE\n"
          "Riverside Hospital\nCharge Nurse   2021 - Present\n"
          "• Led a team of 8 nurses on nights\n• Cut handoff errors on the unit\n"
          "Registered Nurse   2018 - 2021\n• Precepted new graduate nurses\n"
          "Mercy Clinic\nWalk-in clinic\nNurse Intern   2017 - 2018\n• Took vitals for 20 patients a day\n")


def extract_reply(roles=None):
    return {"contact": {"Name": "PAT LEE", "Location": "Columbus, OH",
                        "Email": "pat@example.com", "Telephone": "", "Linkedin": "",
                        "Website": ""},
            "summary": [],
            "roles": roles or [
                {"employer": "Riverside Hospital", "title": "Charge Nurse",
                 "dates": "2021 - Present", "location": "", "company_description": "",
                 "bullets": ["Led a team of 8 nurses on nights",
                             "Cut handoff errors on the unit"]},
                {"employer": "Riverside Hospital", "title": "Registered Nurse",
                 "dates": "2018 - 2021", "location": "", "company_description": "",
                 "bullets": ["Precepted new graduate nurses"]},
                {"employer": "Mercy Clinic", "title": "Nurse Intern",
                 "dates": "2017 - 2018", "location": "", "company_description": "",
                 "bullets": ["Took vitals for 20 patients a day"]}],
            "skills": [], "education": [], "certifications": [], "other": []}


@unittest.skipIf(TestClient is None, "fastapi not installed")
class RecordPage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["RB_DB"] = os.path.join(self.tmp.name, "t.sqlite3")
        os.environ["RB_DEV"] = "1"
        import web.app as appmod
        self.appmod = importlib.reload(appmod)
        self.c = TestClient(self.appmod.app)
        self.c.post("/dev/login", json={"email": "pat@example.com"})

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("RB_DEV", None)

    def upload(self, *replies):
        llm.use_backend(FakeBackend(*(replies or [extract_reply()])))
        r = self.c.post("/api/j/import", files={"file": ("resume.txt", SOURCE.encode())})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def state(self):
        return self.c.get("/api/j/state").json()

    # -- arriving and refreshing

    def test_a_new_person_starts_at_welcome_and_a_refresh_returns_to_their_step(self):
        self.assertEqual(self.state()["screen"], "welcome")
        self.c.put("/api/j/step", json={"screen": "upload"})
        self.assertEqual(self.state()["screen"], "upload")

    def test_someone_with_a_record_from_the_old_page_lands_on_it(self):
        self.c.put("/api/record", json={"markdown": "# R\n\n## Roles\n\n### Acme — Analyst\n"})
        self.assertEqual(self.state()["screen"], "check")

    def test_an_unknown_step_is_refused(self):
        self.assertEqual(self.c.put("/api/j/step", json={"screen": "nowhere"}).status_code, 400)

    # -- what the upload gave us

    def test_the_upload_shows_every_job_with_its_lines(self):
        s = self.upload()
        self.assertEqual(s["new_jobs"], 3)
        self.assertEqual([j["title"] for j in s["jobs"]],
                         ["Charge Nurse", "Registered Nurse", "Nurse Intern"])
        self.assertEqual([l["text"] for l in s["jobs"][0]["lines"]],
                         ["Led a team of 8 nurses on nights", "Cut handoff errors on the unit"])
        self.assertEqual(s["contact"]["Email"], "pat@example.com")

    def test_titles_at_one_employer_are_marked_so_the_page_can_group_them(self):
        jobs = self.upload()["jobs"]
        self.assertEqual([j["titles_at_employer"] for j in jobs], [2, 2, 1])
        self.assertEqual([j["last_at_employer"] for j in jobs], [False, True, True])

    # -- lines

    def test_a_line_can_be_added_edited_and_deleted_with_undo(self):
        jobs = self.upload()["jobs"]
        label = jobs[2]["label"]
        s = self.c.post("/api/j/jobs/2/lines", json={"expect": label,
                                                      "text": "Logged intake for walk-ins"}).json()
        self.assertEqual(s["jobs"][2]["lines"][-1]["text"], "Logged intake for walk-ins")
        s = self.c.patch("/api/j/jobs/2/lines/1", json={
            "expect": label, "kind": "resume", "expect_text": "Logged intake for walk-ins",
            "text": "Logged intake for about 30 walk-ins a day"}).json()
        self.assertEqual(s["jobs"][2]["lines"][1]["text"], "Logged intake for about 30 walk-ins a day")
        r = self.c.post("/api/j/jobs/2/lines/0/delete", json={
            "expect": label, "kind": "resume",
            "expect_text": "Took vitals for 20 patients a day"}).json()
        self.assertEqual([l["text"] for l in r["jobs"][2]["lines"]],
                         ["Logged intake for about 30 walk-ins a day"])
        gone = r["deleted"]
        s = self.c.post("/api/j/jobs/2/lines/0/restore", json={
            "expect": label, "kind": gone["kind"], "text": gone["text"]}).json()
        self.assertEqual(s["jobs"][2]["lines"][0]["text"], "Took vitals for 20 patients a day")

    def test_an_edit_against_an_out_of_date_page_is_refused(self):
        jobs = self.upload()["jobs"]
        r = self.c.post("/api/j/jobs/0/lines/0/delete", json={
            "expect": jobs[0]["label"], "kind": "resume", "expect_text": "Something else"})
        self.assertEqual(r.status_code, 409)
        r = self.c.patch("/api/j/jobs/0", json={"expect": "Wrong — Label", "title": "X"})
        self.assertEqual(r.status_code, 409)

    def test_a_blank_line_is_refused(self):
        jobs = self.upload()["jobs"]
        r = self.c.post("/api/j/jobs/0/lines", json={"expect": jobs[0]["label"], "text": "  "})
        self.assertEqual(r.status_code, 400)

    # -- jobs

    def test_a_job_can_be_edited(self):
        jobs = self.upload()["jobs"]
        s = self.c.patch("/api/j/jobs/2", json={"expect": jobs[2]["label"],
                                                "dates": "2016 - 2018"}).json()
        self.assertEqual(s["jobs"][2]["dates"], "2016 - 2018")

    def test_an_added_job_goes_in_date_order_newest_first(self):
        self.upload()
        s = self.c.post("/api/j/jobs", json={"employer": "Riverside Hospital",
                                             "title": "Nursing Assistant",
                                             "dates": "2018 - 2018"}).json()
        self.assertEqual([j["title"] for j in s["jobs"]],
                         ["Charge Nurse", "Registered Nurse", "Nursing Assistant", "Nurse Intern"])
        s = self.c.post("/api/j/jobs", json={"employer": "City Hospital", "title": "Unit Clerk",
                                             "dates": "2014 - 2016"}).json()
        self.assertEqual(s["jobs"][-1]["title"], "Unit Clerk")
        s = self.c.post("/api/j/jobs", json={"employer": "Northside", "title": "Director",
                                             "dates": "2024 - Present"}).json()
        self.assertEqual(s["jobs"][0]["title"], "Director")

    def test_a_job_needs_an_employer_and_title_and_is_never_listed_twice(self):
        self.upload()
        self.assertEqual(self.c.post("/api/j/jobs", json={"employer": "X"}).status_code, 400)
        r = self.c.post("/api/j/jobs", json={"employer": "Mercy Clinic", "title": "Nurse Intern"})
        self.assertEqual(r.status_code, 400)

    def test_deleting_a_job_takes_its_lines_with_it(self):
        jobs = self.upload()["jobs"]
        r = self.c.delete("/api/j/jobs/2", params={"expect": jobs[2]["label"]}).json()
        self.assertEqual(len(r["jobs"]), 2)
        self.assertEqual(r["deleted"]["label"], jobs[2]["label"])

    # -- later uploads

    def test_a_later_upload_never_brings_back_what_was_deleted(self):
        jobs = self.upload()["jobs"]
        self.c.delete("/api/j/jobs/2", params={"expect": jobs[2]["label"]})
        self.c.post("/api/j/jobs/0/lines/1/delete", json={
            "expect": jobs[0]["label"], "kind": "resume",
            "expect_text": "Cut handoff errors on the unit"})
        s = self.upload()
        self.assertEqual(s["left_out_deleted"], 2)
        self.assertEqual(len(s["jobs"]), 2)
        self.assertEqual([l["text"] for l in s["jobs"][0]["lines"]],
                         ["Led a team of 8 nurses on nights"])

    def test_an_undone_delete_is_no_longer_held_back_from_uploads(self):
        jobs = self.upload()["jobs"]
        label = jobs[0]["label"]
        gone = self.c.post("/api/j/jobs/0/lines/1/delete", json={
            "expect": label, "kind": "resume",
            "expect_text": "Cut handoff errors on the unit"}).json()["deleted"]
        self.c.post("/api/j/jobs/0/lines/1/restore", json={
            "expect": label, "kind": "resume", "text": gone["text"]})
        self.assertEqual(self.upload()["left_out_deleted"], 0)

    def test_a_reworded_line_is_not_brought_back_by_the_same_file(self):
        jobs = self.upload()["jobs"]
        self.c.patch("/api/j/jobs/0/lines/0", json={
            "expect": jobs[0]["label"], "kind": "resume",
            "expect_text": "Led a team of 8 nurses on nights",
            "text": "Led a team of 8 nurses on the night shift"})
        s = self.upload()
        self.assertEqual(s["new_lines"], 0)
        self.assertEqual([l["text"] for l in s["jobs"][0]["lines"]],
                         ["Led a team of 8 nurses on the night shift",
                          "Cut handoff errors on the unit"])

    def test_a_second_file_adds_only_what_is_new(self):
        self.upload()
        s = self.upload()
        self.assertEqual((s["new_jobs"], s["new_lines"]), (0, 0))
        self.assertEqual(len(s["jobs"]), 3)

    # -- contact

    def test_contact_details_can_be_corrected(self):
        self.upload()
        s = self.c.patch("/api/j/contact", json={"fields": {"Telephone": "614-555-0100"}}).json()
        self.assertEqual(s["contact"]["Telephone"], "614-555-0100")
        self.assertEqual(self.c.patch("/api/j/contact",
                                      json={"fields": {"Salary": "x"}}).status_code, 400)

    # -- jobs you're aiming for

    def test_targets_offer_titles_from_the_record_and_hold_up_to_three(self):
        s = self.upload()
        t = s["targets"]
        self.assertEqual(t["titles"], [])
        self.assertEqual(t["suggest"], ["Charge Nurse", "Registered Nurse", "Nurse Intern"])
        s = self.c.put("/api/j/targets", json={"titles": ["Nurse Manager", "Charge Nurse"],
                                               "where": "Columbus, OH",
                                               "modes": ["Hybrid", "On site"]}).json()
        t = s["targets"]
        self.assertEqual(t["titles"], ["Nurse Manager", "Charge Nurse"])
        self.assertEqual((t["where"], t["modes"], t["status"]),
                         ("Columbus, OH", ["Hybrid", "On site"], "saved"))
        self.assertNotIn("Charge Nurse", t["suggest"])
        r = self.c.put("/api/j/targets", json={"titles": ["A", "B", "C", "D"]})
        self.assertEqual(r.status_code, 400)

    def test_targets_go_where_the_pipeline_reads_them(self):
        self.upload()
        self.c.put("/api/j/targets", json={"titles": ["Nurse Manager", "Charge Nurse"]})
        md = self.c.get("/api/record").json()["markdown"]
        self.assertIn("Nurse Manager; Charge Nurse", md)

    def test_not_sure_yet_is_remembered(self):
        self.upload()
        s = self.c.put("/api/j/targets", json={"not_sure": True}).json()
        self.assertEqual(s["targets"]["status"], "not_sure")

    # -- the background questions

    def steps(self, s=None):
        return (s or self.state())["steps"]

    def test_questions_run_by_employer_newest_first(self):
        steps = self.steps(self.upload())
        self.assertEqual([(x["kind"], x["title"], x["field"]) for x in steps[:7]], [
            ("company", "Charge Nurse", "Company"), ("titles", "Charge Nurse", "titles"),
            ("q", "Charge Nurse", "Challenge"), ("q", "Charge Nurse", "Authority"),
            ("q", "Charge Nurse", "Results against targets"), ("q", "Charge Nurse", "Recognition"),
            ("q", "Registered Nurse", "Challenge")])
        self.assertEqual(steps[0]["titles"] if "titles" in steps[0] else None, None)
        self.assertEqual([t["title"] for t in steps[1]["titles"]],
                         ["Charge Nurse", "Registered Nurse"])
        self.assertEqual((steps[6]["job_number"], steps[6]["job_total"]), (2, 3))
        self.assertEqual(len(steps), 2 + 4 + 4 + 2 + 4)

    def test_an_answer_lands_on_the_record_and_once_per_employer(self):
        steps = self.steps(self.upload())
        s = self.c.post("/api/j/answer", json={"id": steps[0]["id"], "status": "answered",
                                               "text": "Regional hospital, 300 beds"}).json()
        self.assertEqual(s["jobs"][0]["background"][0]["answer"], "Regional hospital, 300 beds")
        self.assertEqual(s["jobs"][1]["background"][0]["answer"], "Regional hospital, 300 beds")
        self.assertEqual(s["steps"][0]["status"], "answered")
        s = self.c.post("/api/j/answer", json={"id": steps[2]["id"], "status": "answered",
                                               "text": "Run the night shift"}).json()
        self.assertEqual(s["jobs"][0]["background"][1],
                         {"field": "Challenge", "question": "What were you brought in to do?",
                          "answer": "Run the night shift"})

    def test_skip_and_doesnt_apply_never_fill_anything_in(self):
        steps = self.steps(self.upload())
        self.c.post("/api/j/answer", json={"id": steps[0]["id"], "status": "skipped"})
        s = self.c.post("/api/j/answer", json={"id": steps[3]["id"], "status": "na",
                                               "text": "typed then changed my mind"}).json()
        self.assertEqual(s["jobs"][0]["background"], [])
        self.assertEqual([x["status"] for x in s["steps"][:4]], ["skipped", "todo", "todo", "na"])

    def test_an_empty_next_counts_as_skipped(self):
        steps = self.steps(self.upload())
        s = self.c.post("/api/j/answer", json={"id": steps[2]["id"], "status": "answered",
                                               "text": "  "}).json()
        self.assertEqual(s["steps"][2]["status"], "na")

    def test_what_the_upload_gave_us_is_not_asked(self):
        roles = extract_reply()["roles"]
        roles[2]["company_description"] = "Walk-in clinic"
        steps = self.steps(self.upload(extract_reply(roles)))
        self.assertNotIn(("company", "Nurse Intern"),
                         [(x["kind"], x["title"]) for x in steps])

    def test_going_back_to_an_answer_shows_it(self):
        steps = self.steps(self.upload())
        self.c.post("/api/j/answer", json={"id": steps[2]["id"], "status": "answered",
                                           "text": "Run the night shift"})
        self.assertEqual(self.steps()[2]["answer"], "Run the night shift")

    def test_a_promotion_with_one_title_on_file_asks_for_the_earlier_title(self):
        steps = self.steps(self.upload())
        rec = [x for x in steps if x["title"] == "Nurse Intern" and x["field"] == "Recognition"][0]
        r = self.c.post("/api/j/answer", json={"id": rec["id"], "status": "answered",
                                               "text": "Promoted to lead intern after 6 months"}).json()
        self.assertEqual(r["ask_earlier_title"], "titles|mercyclinic")
        rec = [x for x in steps if x["title"] == "Charge Nurse" and x["field"] == "Recognition"][0]
        r = self.c.post("/api/j/answer", json={"id": rec["id"], "status": "answered",
                                               "text": "Promoted from RN"}).json()
        self.assertNotIn("ask_earlier_title", r)

    def test_a_title_added_at_an_employer_gets_its_own_questions(self):
        self.upload()
        s = self.c.post("/api/j/jobs", json={"employer": "Mercy Clinic", "title": "Medical Assistant",
                                             "dates": "2016 - 2017"}).json()
        titles = [x["title"] for x in s["steps"] if x["kind"] == "q" and x["field"] == "Challenge"]
        self.assertEqual(titles, ["Charge Nurse", "Registered Nurse", "Nurse Intern",
                                  "Medical Assistant"])
        self.assertEqual(s["steps"][-1]["job_total"], 4)

    def test_the_question_step_survives_a_refresh(self):
        steps = self.steps(self.upload())
        self.c.put("/api/j/step", json={"screen": "bg", "at": steps[3]["id"]})
        s = self.state()
        self.assertEqual((s["screen"], s["step"]["at"]), ("bg", steps[3]["id"]))

    def test_an_answer_to_a_question_that_is_gone_is_refused(self):
        self.upload()
        r = self.c.post("/api/j/answer", json={"id": "q|nowhere|x|Challenge", "status": "na"})
        self.assertEqual(r.status_code, 404)

    def test_the_new_page_is_served_beside_the_old_one(self):
        self.assertIn("Career record", self.c.get("/new").text)
        self.assertEqual(self.c.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
