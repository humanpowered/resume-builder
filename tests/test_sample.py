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
from resume_builder import llm
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

    def test_twenty_samples_for_a_posting_are_free_unless_set(self):
        self.assertEqual(self.c.get("/api/j/state").json()["samples"]["free"], 20)

    @mock.patch.dict(os.environ, {"RB_FREE_TAILORED": "3"})
    def test_a_few_samples_for_a_posting_are_free(self):
        self.assertEqual(self.c.get("/api/j/state").json()["samples"]["free_left"], 3)
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
        self.assertEqual(self.c.get("/api/j/state").json()["samples"]["free_left"], 20)
        got = {"title": "Nurse Manager", "company": "Lakeside Health", "text": POSTING, "url": "https://jobs.example.com/1"}
        llm.use_backend(FakeBackend(choice(posting_title="Nurse Manager", posting_company="Lakeside Health")))
        with mock.patch.object(posting, "read", return_value=got):
            s = self.post("/api/j/sample/tailor", text="https://jobs.example.com/1")
        self.assertEqual(s["sample"]["posting"]["url"], "https://jobs.example.com/1")
        self.assertEqual(s["samples"]["free_left"], 19)
