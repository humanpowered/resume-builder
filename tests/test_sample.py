"""
Sample resumes: a free draft, a general sample and a few written for a
posting. Every line on a sample is a line already on the record, and making
one writes no file.
"""
import importlib
import json
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401

from fakes import FakeBackend
from resume_builder import journey, llm, sample
from test_journey import SOURCE, TestClient, extract_reply

POSTING = ("Nurse Manager, Lakeside Health. We need a nurse leader to run a 30-bed unit, "
           "coach charge nurses, cut handoff errors and manage a budget. Epic experience a plus.")


def choice(**more):
    return {"jobs": [{"job": 0, "lines": ["j0l1", "j0l0", "j0l0", "j9l9"]},
                     {"job": 1, "lines": ["j1l0"]}],
            "skills": ["s0", "s7"], "posting_title": "", "posting_company": "", "gaps": [], **more}


@unittest.skipIf(TestClient is None, "fastapi not installed")
class Samples(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["RB_DB"] = os.path.join(self.tmp.name, "t.sqlite3")
        os.environ["RB_DEV"] = "1"
        import web.app as appmod
        self.c = TestClient(importlib.reload(appmod).app)
        self.c.post("/dev/login", json={"email": "pat@example.com"})
        r = extract_reply()
        llm.use_backend(FakeBackend(r))
        self.c.post("/api/j/import", files={"file": ("resume.txt", SOURCE.encode())})
        self.c.post("/api/j/skill/add", json={"name": "Epic EHR"})

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("RB_DEV", None)

    def post(self, path, **body):
        r = self.c.post(path, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_the_draft_is_free_and_stacks_titles_under_one_employer(self):
        llm.use_backend(FakeBackend())          # any model call would fail
        s = self.post("/api/j/sample/draft")["sample"]
        self.assertEqual([e["employer"] for e in s["employers"]], ["Riverside Hospital", "Mercy Clinic"])
        self.assertEqual([r["title"] for r in s["employers"][0]["roles"]], ["Charge Nurse", "Registered Nurse"])
        self.assertEqual(s["employers"][0]["roles"][0]["lines"][0], "Led a team of 8 nurses on nights")
        self.assertEqual(s["skills"], ["Epic EHR"])

    def test_the_model_only_chooses_and_only_real_lines_appear(self):
        files = set(os.listdir(self.tmp.name))
        llm.use_backend(FakeBackend(choice()))
        s = self.post("/api/j/sample/general")
        res = s["sample"]
        self.assertEqual(res["employers"][0]["roles"][0]["lines"],
                         ["Cut handoff errors on the unit", "Led a team of 8 nurses on nights"])
        self.assertEqual(res["employers"][0]["roles"][1]["lines"], ["Precepted new graduate nurses"])
        self.assertEqual(res["employers"][1]["roles"][0]["lines"], [])
        self.assertEqual(res["skills"], ["Epic EHR"])
        self.assertEqual(s["samples"]["general"]["kind"], "general")
        self.assertEqual(set(os.listdir(self.tmp.name)), files)     # nothing written

    def test_a_few_samples_for_a_posting_are_free(self):
        r = self.c.post("/api/j/sample/tailor", json={"text": "Nurse wanted"})
        self.assertEqual(r.status_code, 400)
        for n in range(3):
            llm.use_backend(FakeBackend(choice(posting_title="Nurse Manager", posting_company="Lakeside Health",
                                               gaps=["Budget management"])))
            s = self.post("/api/j/sample/tailor", text=POSTING)
        self.assertEqual(s["sample"]["posting"], {"title": "Nurse Manager", "company": "Lakeside Health", "url": ""})
        self.assertEqual(s["sample"]["gaps"], ["Budget management"])
        self.assertEqual(s["samples"]["free_left"], 0)
        r = self.c.post("/api/j/sample/tailor", json={"text": POSTING})
        self.assertEqual(r.status_code, 400)

    def test_titles_at_one_employer_read_newest_first_wherever_they_were_stored(self):
        jobs = self.c.get("/api/j/state").json()["jobs"]
        rn = next(j for j in jobs if j["title"] == "Registered Nurse")
        r = self.c.patch(f"/api/j/jobs/{rn['index']}", json={"expect": rn["label"], "dates": "Mar 2022 - Present"})
        self.assertEqual(r.status_code, 200, r.text)
        s = self.c.get("/api/j/state").json()
        self.assertEqual([s["jobs"][i]["title"] for i in s["shown"]],
                         ["Registered Nurse", "Charge Nurse", "Nurse Intern"])
        self.assertEqual([j["title"] for j in s["jobs"] if j["last_at_employer"]], ["Charge Nurse", "Nurse Intern"])
        llm.use_backend(FakeBackend())
        d = self.post("/api/j/sample/draft")["sample"]
        self.assertEqual([r["title"] for r in d["employers"][0]["roles"]], ["Registered Nurse", "Charge Nurse"])


class PostingLinks(unittest.TestCase):
    """A link is read before anything is spent, and only public addresses."""

    def test_only_a_bare_link_counts_as_a_link(self):
        from resume_builder import posting
        self.assertTrue(posting.is_link("  https://jobs.example.com/123 "))
        self.assertFalse(posting.is_link("Nurse Manager. Apply at https://jobs.example.com/123"))
        self.assertFalse(posting.is_link("ftp://example.com/job"))

    def test_private_and_odd_addresses_are_refused_before_any_request(self):
        from resume_builder import posting
        for url in ("http://127.0.0.1/admin", "http://localhost:8000/", "http://169.254.169.254/latest",
                    "http://10.0.0.5/job", "http://[::1]/", "file:///etc/passwd"):
            with self.assertRaises(posting.Unreadable, msg=url):
                posting._public(url)

    def test_the_posting_data_built_into_a_page_is_used(self):
        from resume_builder import posting
        page = ('<html><title>Jobs</title><script type="application/ld+json">'
                + json.dumps({"@type": "JobPosting", "title": "Nurse Manager",
                              "hiringOrganization": {"name": "Lakeside Health"},
                              "description": "<p>" + "Run a 30-bed unit and coach charge nurses. " * 10 + "</p>"})
                + "</script></html>")
        with mock.patch.object(posting, "_get", return_value=(page, "https://jobs.example.com/1")):
            got = posting.read("https://jobs.example.com/1")
        self.assertEqual((got["title"], got["company"]), ("Nurse Manager", "Lakeside Health"))
        self.assertTrue(posting.as_posting(got).startswith("Nurse Manager\nLakeside Health\n\nRun a 30-bed unit"))

    def test_a_page_with_too_little_text_asks_for_the_text(self):
        from resume_builder import posting
        with mock.patch.object(posting, "_get", return_value=("<html><body>Sign in to see this job</body></html>",
                                                              "https://www.linkedin.com/jobs/view/1")):
            with self.assertRaises(posting.Unreadable) as ctx:
                posting.read("https://www.linkedin.com/jobs/view/1")
        self.assertIn("linkedin.com", str(ctx.exception))


@unittest.skipIf(TestClient is None, "fastapi not installed")
class SampleFromLink(unittest.TestCase):
    setUp, tearDown, post = Samples.setUp, Samples.tearDown, Samples.post

    def test_a_link_is_read_and_kept_and_an_unreadable_one_costs_nothing(self):
        from resume_builder import posting
        with mock.patch.object(posting, "read", side_effect=posting.Unreadable("We couldn't read the posting")):
            r = self.c.post("/api/j/sample/tailor", json={"text": "https://www.linkedin.com/jobs/view/1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("couldn't read", r.json()["detail"])
        self.assertEqual(self.c.get("/api/j/state").json()["samples"]["free_left"], 3)
        got = {"title": "Nurse Manager", "company": "Lakeside Health", "text": POSTING, "url": "https://jobs.example.com/1"}
        llm.use_backend(FakeBackend(choice(posting_title="Nurse Manager", posting_company="Lakeside Health")))
        with mock.patch.object(posting, "read", return_value=got):
            s = self.post("/api/j/sample/tailor", text="https://jobs.example.com/1")
        self.assertEqual(s["sample"]["posting"]["url"], "https://jobs.example.com/1")
        self.assertEqual(s["samples"]["free_left"], 2)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class StageAndChoices(unittest.TestCase):
    """The sample's order follows the person's career stage, and what goes on
    every resume (phone, old graduation years) is theirs to choose."""
    setUp, tearDown, post = Samples.setUp, Samples.tearDown, Samples.post

    def draft(self):
        llm.use_backend(FakeBackend())          # the draft never calls the model
        return self.post("/api/j/sample/draft")["sample"]

    def put(self, path, **body):
        r = self.c.put(path, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def edu(self, section, **fields):
        return self.post("/api/j/edu", section=section, index=-1, fields=fields)

    def keys(self, s):
        return [x["key"] for x in s["sections"]]

    def test_contact_shows_the_website_and_the_phone_only_if_wanted(self):
        self.c.patch("/api/j/contact", json={"fields": {"Telephone": "614-555-0100",
                                                        "Website": "patlee.dev"}})
        self.assertEqual(self.draft()["contact"], "Columbus, OH · pat@example.com · 614-555-0100 · patlee.dev")
        s = self.put("/api/j/sample/prefs", phone=False)
        self.assertFalse(s["samples"]["prefs"]["phone"])
        self.assertEqual(self.draft()["contact"], "Columbus, OH · pat@example.com · patlee.dev")

    def test_dates_print_one_way_and_the_company_line_shows(self):
        steps = self.c.get("/api/j/state").json()["steps"]
        company = next(x for x in steps if x["kind"] == "company" and x["employer"] == "Riverside Hospital")
        self.post("/api/j/answer", id=company["id"], status="answered",
                  text="Regional hospital, 300 beds and 2,500 staff")
        s = self.draft()
        self.assertEqual(s["employers"][0]["about"], "Regional hospital, 300 beds and 2,500 staff")
        self.assertEqual(s["employers"][0]["roles"][0]["dates"], "2021 to Present")
        self.assertIn("Regional hospital, 300 beds and 2,500 staff", sample.as_text(s))

    def test_a_student_gets_education_first_with_a_good_gpa(self):
        now = journey.today()[0]
        self.edu("education", degree="BSN", field="Nursing", school="Ohio State", year=str(now), gpa="3.6")
        s = self.draft()
        self.assertEqual(self.keys(s)[:2], ["experience", "education"])
        self.assertEqual(s["education"], [f"BSN, Nursing, Ohio State, {now}, GPA 3.6"])  # recent: GPA stays
        self.put("/api/j/targets", titles=[], where="", modes=[], stage="Student or new graduate")
        self.assertEqual(self.keys(self.draft())[:2], ["education", "experience"])

    def test_a_low_gpa_and_an_old_graduation_year_are_left_off_unless_asked(self):
        old = journey.today()[0] - 20
        self.edu("education", degree="BSN", school="Ohio State", year=str(old), gpa="2.8")
        self.assertEqual(self.draft()["education"], ["BSN, Ohio State"])
        self.put("/api/j/sample/prefs", old_years=True)
        self.assertEqual(self.draft()["education"], [f"BSN, Ohio State, {old}"])
        rec_line = self.c.get("/api/j/state").json()["edu"][0]["line"]
        self.assertEqual(rec_line, f"BSN, Ohio State, {old}, GPA 2.8")     # the record keeps it all

    def test_a_licence_goes_above_experience(self):
        self.edu("certifications", name="Registered Nurse (RN)", issuer="Ohio Board of Nursing", year="2018",
                 expires="Mar 2027", multistate=True)
        s = self.draft()
        self.assertEqual(self.keys(s)[:2], ["credentials", "experience"])
        self.assertEqual(s["sections"][0]["lines"],
                         ["Registered Nurse (RN), Ohio Board of Nursing, 2018, multistate (compact) licence, expires Mar 2027"])

    def test_jobs_that_ended_long_ago_become_one_line_each(self):
        old = journey.today()[0] - 20
        jobs = self.c.get("/api/j/state").json()["jobs"]
        intern = next(j for j in jobs if j["title"] == "Nurse Intern")
        self.c.patch(f"/api/j/jobs/{intern['index']}", json={"expect": intern["label"],
                                                             "dates": f"{old - 1} - {old}"})
        s = self.draft()
        self.assertEqual([e["employer"] for e in s["employers"]], ["Riverside Hospital"])
        self.assertEqual(next(x for x in s["sections"] if x["key"] == "earlier")["lines"],
                         ["Nurse Intern, Mercy Clinic"])

    def test_languages_and_clearance_get_their_own_sections(self):
        self.post("/api/j/skill/add", name="Spanish")
        self.post("/api/j/skill", name="Spanish", have="yes", level="Fluent")
        s = self.draft()
        self.assertNotIn("Spanish", s["skills"])
        self.assertEqual(next(x for x in s["sections"] if x["key"] == "languages")["lines"], ["Spanish (Fluent)"])

    def test_choices_redraw_samples_already_made_without_a_model_call(self):
        llm.use_backend(FakeBackend(choice()))
        self.post("/api/j/sample/general")
        self.c.patch("/api/j/contact", json={"fields": {"Telephone": "614-555-0100"}})
        self.assertIn("614-555-0100", self.c.get("/api/j/state").json()["samples"]["general"]["contact"])
        llm.use_backend(FakeBackend())
        s = self.put("/api/j/sample/prefs", phone=False)
        self.assertNotIn("614-555-0100", s["samples"]["general"]["contact"])
