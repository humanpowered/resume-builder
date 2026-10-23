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

    def test_the_resume_on_screen_downloads_as_word_or_pdf(self):
        c = self.client("a@example.com")
        res = {"name": "Ann Lee", "employers": [{"employer": "Riverside", "roles": [
            {"title": "RN", "dates": "2019 - Present", "lines": ["Cut wait times 20%"]}]}]}
        r = c.post("/api/j/sample/file", json={"sample": res, "format": "docx"})
        self.assertEqual(r.status_code, 200)
        self.assertIn('filename="Ann Lee resume.docx"', r.headers["content-disposition"])
        self.assertEqual(r.content[:2], b"PK")
        r = c.post("/api/j/sample/file", json={"sample": res, "format": "pdf"})
        self.assertEqual(r.content[:5], b"%PDF-")
        self.assertEqual(c.post("/api/j/sample/file", json={"sample": res, "format": "exe"}).status_code, 422)
        self.assertEqual(TestClient(self.appmod.app).post(
            "/api/j/sample/file", json={"sample": res}).status_code, 401)

    def test_the_whole_record_downloads_as_word(self):
        c = self.client("a@example.com")
        self.seed(c)
        r = c.get("/api/me/record.docx")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content[:2], b"PK")
        self.assertIn("career record.docx", r.headers["content-disposition"])
        self.assertEqual(TestClient(self.appmod.app).get("/api/me/record.docx").status_code, 401)

    def test_the_page_gets_the_opening_card_and_phone_tip(self):
        r = self.client("a@example.com").get("/api/record").json()
        self.assertIn("15 to 20 minutes a job", r["intro"])
        self.assertIn("microphone", r["phone_tip"])

    def test_the_web_interview_leaves_job_targets_to_their_own_card(self):
        c = self.client("a@example.com")
        self.seed(c)
        p = c.post("/api/interview/step", json={"answer": None}).json()
        seen = [p["text"]]
        for _ in range(8):
            if p["text"].startswith("In a sentence"):
                break
            p = c.post("/api/interview/step", json={"answer": ""}).json()
            seen.append(p["text"])
        self.assertFalse([t for t in seen if "aiming for" in t or "industries" in t.lower()])

    def test_the_targets_card_suggests_the_persons_own_titles_and_saves(self):
        c = self.client("a@example.com")
        md = ("# R\n\n## Roles\n\n### Riverside — Charge Nurse\n\n- **Dates:** 2021 - Present\n\n"
              "### Mercy — RN\n\n- **Dates:** 2015 - 2021\n\n### Parkview — rn\n\n- **Dates:** 2012 - 2015\n")
        c.put("/api/record", json={"markdown": md})
        t = c.get("/api/target").json()
        self.assertEqual(t["Titles"], "")
        self.assertEqual(t["suggested_titles"], ["Charge Nurse", "RN"])
        t = c.put("/api/target", json={"Titles": " Nurse Manager;  Charge Nurse ;",
                                       "Locations": "Denver; remote"}).json()
        self.assertEqual(t["Titles"], "Nurse Manager; Charge Nurse")
        self.assertIn("**Locations:** Denver; remote", c.get("/api/record").json()["markdown"])
        c.put("/api/target", json={"Titles": ""})
        self.assertNotIn("Titles", c.get("/api/record").json()["markdown"])

    def test_the_page_knows_a_returning_person_from_a_new_one(self):
        # a new person sees the welcome page; anyone with an interview under way
        # goes straight back to their next question
        c = self.client("a@example.com")
        self.assertFalse(c.get("/api/record").json()["interview_started"])
        c.post("/api/interview/step", json={"answer": None})
        self.assertTrue(c.get("/api/record").json()["interview_started"])

    def test_with_invite_codes_set_only_invited_testers_get_in(self):
        os.environ["RB_INVITE_CODES"] = "maple-42, cedar-7"
        try:
            self.appmod = importlib.reload(self.appmod)
            c = TestClient(self.appmod.app)
            self.assertEqual(c.post("/dev/login", json={"email": "a@example.com"}).status_code, 403)
            self.assertEqual(c.post("/dev/login", json={"email": "a@example.com",
                                                        "code": "oak-1"}).status_code, 403)
            self.assertEqual(c.post("/dev/login", json={"email": "a@example.com",
                                                        "code": " cedar-7 "}).status_code, 200)
            self.assertEqual(c.get("/api/record").status_code, 200)
        finally:
            os.environ.pop("RB_INVITE_CODES")

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
        for _ in range(12):                 # contact and target, then any new job
            if p["text"].startswith("In a sentence"):               # role context
                break
            p = c.post("/api/interview/step", json={"answer": ""}).json()
        self.assertTrue(p["text"].startswith("In a sentence"))
        for _ in range(12):
            if p["text"].startswith("What was the unit"):
                break
            p = c.post("/api/interview/step", json={"answer": ""}).json()
        self.assertEqual(p["text"], "What was the unit like when you started?")
        p = c.post("/api/interview/step", json={"answer": "Lots of falls"}).json()
        self.assertTrue(any("recorded: Cut falls" in n for n in p["notes"]))
        self.assertEqual(c.get("/api/health").json()["quantified"], 1)

        # what was saved comes back as a card, in plain words, and can be
        # changed then and any time after
        card = p["saved"]
        self.assertEqual((card["title"], card["results"], card["label"]),
                         ("Cut falls", "Falls down 30%", "Has a number"))
        url = f"/api/roles/{card['role']}/accomplishments/{card['index']}"
        r = c.patch(url, json={"expect_title": "Cut falls", "title": "Cut ICU falls",
                               "results": "Falls down 30% in a year", "skills": ["Hourly rounding"]})
        self.assertEqual(r.status_code, 200)
        r = c.patch(url, json={"expect_title": "Cut ICU falls", "results": "Falls fell sharply"})
        self.assertEqual(r.json()["label"], "Described, no number", "the label follows the words")
        self.assertEqual(c.patch(url, json={"expect_title": "Cut falls", "title": "x"}).status_code,
                         409, "an edit made from a stale page is refused")
        acc = c.get("/api/record").json()["summary"]["roles"][0]["accomplishments"][0]
        self.assertEqual((acc["title"], acc["evidence"]), ("Cut ICU falls", "qualitative"))
        sk = {s["name"]: s["evidence"] for s in c.get("/api/record").json()["summary"]["skills"]}
        self.assertEqual(sk["Hourly rounding"], ["Cut ICU falls"])

    def test_a_job_can_be_edited_and_keeps_its_place_in_the_interview(self):
        c = self.client("a@example.com")
        self.seed(c)
        llm.use_backend(FakeBackend(turn(say="What changed?")))
        p = c.post("/api/interview/step", json={"answer": None}).json()
        while not p["text"].startswith("In a sentence"):
            p = c.post("/api/interview/step", json={"answer": ""}).json()
        r = c.patch("/api/roles/0", json={"expect": "Riverside — RN", "title": "ICU Charge Nurse",
                                          "fields": {"Reported to": "Nurse Manager", "Dates": ""}})
        self.assertEqual(r.status_code, 200)
        role = c.get("/api/record").json()["summary"]["roles"][0]
        self.assertEqual((role["title"], role["fields"]["Reported to"], role["dates"]),
                         ("ICU Charge Nurse", "Nurse Manager", ""))
        p = c.post("/api/interview/step", json={"answer": "A 30-bed hospital"}).json()
        self.assertIn("ICU Charge Nurse", p["text"], "the interview carries on with the renamed job")
        self.assertEqual(c.patch("/api/roles/0", json={"expect": "Riverside — RN", "title": "x"})
                         .status_code, 409)
        self.assertEqual(c.patch("/api/roles/0", json={"fields": {"Other titles": "x"}}).status_code, 400)

    def test_education_over_http_needs_no_model(self):
        c = self.client("a@example.com")
        self.seed(c)
        step = lambda a: c.post("/api/interview/step",
                                json={"answer": a, "role": "background"}).json()
        self.assertTrue(step(None)["text"].startswith("Your next qualification"))
        for a in ("High school diploma", "", "Lakeside High", "2010"):
            step(a)
        self.assertIn("recorded: High school diploma, Lakeside High, 2010", step("")["notes"])
        p = step("")
        while p["kind"] != "done":
            p = step("")
        self.assertIn("High school diploma, Lakeside High, 2010",
                      c.get("/api/record").json()["markdown"])

    def test_skills_list_build_answer_add_and_edit(self):
        c = self.client("a@example.com")
        md = ("# R\n\n## Roles\n\n### Riverside — RN\n\n#### Cut falls\n\n"
              "- **Problem:** p\n- **Actions:** a\n- **Results:** Falls down 30%\n")
        c.put("/api/record", json={"markdown": md})
        item = lambda name, cat, because=(), level="": {
            "name": name, "category": cat, "same_as": "", "named": False,
            "because": list(because), "level": level}
        llm.use_backend(FakeBackend({"field": "Hospital nursing", "skills": [
            item("Fall prevention", "Patient Safety", ["Cut falls"], "Advanced"),
            item("Telemetry", "Clinical")]}))
        out = c.post("/api/skills/build", json={"field": ""}).json()
        self.assertEqual(out["added"], ["Fall prevention", "Telemetry"])
        self.assertEqual(c.get("/api/skills").json()["field"], "Hospital nursing")
        self.assertEqual(c.post("/api/skills/accept-shown").json()["accepted"],
                         ["Fall prevention"])
        self.assertEqual(c.patch("/api/skills/Telemetry", json={"have": "no"}).status_code, 200)
        self.assertEqual(c.patch("/api/skills/Telemetry", json={"level": "Guru"}).status_code, 400)
        self.assertEqual(c.patch("/api/skills/Nope", json={"have": "no"}).status_code, 404)
        c.post("/api/skills", json={"name": "Wound care", "category": "Clinical",
                                    "level": "Working"})
        c.patch("/api/skills/Wound care", json={"level": "Expert"})
        rows = {s["name"]: (s["category"], s["have"], s["level"])
                for s in c.get("/api/skills").json()["skills"]}
        self.assertEqual(rows, {"Fall prevention": ("Patient Safety", "yes", "Advanced"),
                                "Telemetry": ("Clinical", "no", ""),
                                "Wound care": ("Clinical", "yes", "Expert")})
        csv_text = c.get("/api/export/skills_inventory.csv").text
        self.assertIn("Patient Safety,Fall prevention,yes,Advanced,your record", csv_text)
        self.assertIn("Clinical,Telemetry,no,,your field", csv_text)
        self.assertEqual(c.delete("/api/skills/Telemetry").status_code, 200)
        self.assertNotIn("Telemetry", c.get("/api/record").json()["markdown"])

    def test_summary_and_sections_are_editable(self):
        c = self.client("a@example.com")
        self.seed(c)
        r = c.put("/api/summary", json={"text": "  I run   ICU units. "}).json()
        self.assertEqual(r["text"], "I run ICU units.")
        self.assertTrue(r["issues"], "first person is flagged")
        self.assertEqual(c.put("/api/sections/languages",
                               json={"items": ["Spanish (fluent)", " ", "French"]}).status_code, 200)
        c.put("/api/sections/education", json={"items": ["BSN, Ohio State, 2015"]})
        secs = {x["key"]: x["items"] for x in c.get("/api/sections").json()}
        self.assertEqual(secs["languages"], ["Spanish (fluent)", "French"])
        self.assertEqual(secs["education"], ["BSN, Ohio State, 2015"])
        self.assertEqual(c.put("/api/sections/hobbies", json={"items": []}).status_code, 404)
        md = c.get("/api/record").json()["markdown"]
        self.assertIn("## Summary\n\nI run ICU units.", md)
        self.assertIn("## Languages\n\n- Spanish (fluent)\n- French", md)
        self.assertEqual(c.post("/api/interview/step",
                                json={"answer": None, "role": "background:hobbies"}).status_code, 400)

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


@unittest.skipIf(TestClient is None, "fastapi not installed")
class FirebaseSignIn(unittest.TestCase):
    """Tester sign-in: Firebase proves the email, the invite list decides who
    gets in, and the service's own signed cookie keeps them signed in."""

    ENV = {"RB_FIREBASE_PROJECT": "rb-test", "RB_FIREBASE_API_KEY": "web-key",
           "RB_SESSION_SECRET": "s3cret", "RB_INVITE_EMAILS": "Ann@Example.com, bo@example.com"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["RB_DB"] = os.path.join(self.tmp.name, "t.sqlite3")
        os.environ.pop("RB_DEV", None)
        os.environ.update(self.ENV)
        import web.app as appmod
        self.appmod = importlib.reload(appmod)
        self.claims = {}
        self.appmod.verify_token = lambda token: self.claims[token]

    def tearDown(self):
        self.tmp.cleanup()
        for k in self.ENV:
            os.environ.pop(k, None)

    def signed_in(self, email, verified=True):
        c = TestClient(self.appmod.app, base_url="https://testserver")
        self.claims["tok-" + email] = {"email": email, "email_verified": verified}
        return c, c.post("/auth/session", json={"id_token": "tok-" + email})

    def test_the_page_learns_firebase_settings_but_never_the_secret(self):
        cfg = TestClient(self.appmod.app).get("/auth/config").json()
        self.assertEqual(cfg["mode"], "firebase")
        self.assertEqual(cfg["firebase"]["authDomain"], "rb-test.firebaseapp.com")
        self.assertNotIn("s3cret", str(cfg))

    def test_the_page_gets_an_analytics_id_only_when_one_is_set(self):
        self.assertIsNone(TestClient(self.appmod.app).get("/auth/config").json()["ga"])
        for value, sent in (("G-ABC123XYZ9", "G-ABC123XYZ9"), ("<script>", None)):
            os.environ["RB_GA_ID"] = value
            try:
                appmod = importlib.reload(self.appmod)
                self.assertEqual(TestClient(appmod.app).get("/auth/config").json()["ga"], sent)
            finally:
                os.environ.pop("RB_GA_ID", None)

    def test_only_invited_emails_are_sent_a_link(self):
        c = TestClient(self.appmod.app)
        self.assertEqual(c.post("/auth/invited", json={"email": " ANN@example.com "}).status_code, 200)
        r = c.post("/auth/invited", json={"email": "eve@example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("tester list", r.json()["detail"])

    def test_an_invited_tester_signs_in_and_reaches_only_their_record(self):
        ann, r = self.signed_in("ann@example.com")
        self.assertEqual(r.status_code, 200)
        md = "# R\n\n## Roles\n\n### Riverside — RN\n\n- **Dates:** 2019 - Present\n"
        self.assertEqual(ann.put("/api/record", json={"markdown": md}).status_code, 200)
        bo, _ = self.signed_in("bo@example.com")
        self.assertNotIn("Riverside", bo.get("/api/record").json()["markdown"])
        self.assertIn("Riverside", ann.get("/api/record").json()["markdown"])

    def test_uninvited_unverified_or_bad_tokens_get_no_session(self):
        _, r = self.signed_in("eve@example.com")
        self.assertEqual(r.status_code, 403)
        _, r = self.signed_in("ann@example.com", verified=False)
        self.assertEqual(r.status_code, 400)
        c = TestClient(self.appmod.app, base_url="https://testserver")
        self.assertEqual(c.post("/auth/session", json={"id_token": "forged"}).status_code, 400)
        self.assertEqual(c.get("/api/record").status_code, 401)

    def test_a_tampered_cookie_or_the_dev_login_does_not_get_in(self):
        ann, _ = self.signed_in("ann@example.com")
        cookie = ann.cookies.get("rb_session")
        enc, issued, sig = cookie.split(".")
        import base64
        bo = base64.urlsafe_b64encode(b"bo@example.com").decode()
        c = TestClient(self.appmod.app, base_url="https://testserver")
        c.cookies.set("rb_session", f"{bo}.{issued}.{sig}")
        self.assertEqual(c.get("/api/record").status_code, 401)
        c.cookies.clear()
        c.cookies.set("rb_user", "ann@example.com")
        self.assertEqual(c.get("/api/record").status_code, 401)
        self.assertEqual(c.post("/dev/login", json={"email": "ann@example.com"}).status_code, 404)

    def test_rb_dev_left_on_does_not_reopen_the_development_sign_in(self):
        os.environ["RB_DEV"] = "1"
        try:
            self.appmod = importlib.reload(self.appmod)
            c = TestClient(self.appmod.app, base_url="https://testserver")
            self.assertEqual(c.post("/dev/login", json={"email": "ann@example.com"}).status_code, 404)
            c.cookies.set("rb_user", "ann@example.com")
            self.assertEqual(c.get("/api/record").status_code, 401)
        finally:
            os.environ.pop("RB_DEV")

    def test_sessions_end_after_two_weeks_away_or_when_taken_off_the_list(self):
        ann, _ = self.signed_in("ann@example.com")
        cookie = ann.cookies.get("rb_session")
        self.assertEqual(self.appmod._read_session(cookie)[0], "ann@example.com")
        real = self.appmod.time.time
        try:
            self.appmod.time.time = lambda: real() + 15 * 86400
            self.assertEqual(self.appmod._read_session(cookie)[0], "")
        finally:
            self.appmod.time.time = real
        self.appmod.INVITED.discard("ann@example.com")
        self.assertEqual(ann.get("/api/record").status_code, 401)

    def test_signing_out_ends_the_session(self):
        ann, _ = self.signed_in("ann@example.com")
        self.assertEqual(ann.get("/api/record").status_code, 200)
        ann.post("/auth/signout")
        self.assertEqual(ann.get("/api/record").status_code, 401)

    def test_the_old_page_sends_testers_to_the_new_one(self):
        r = TestClient(self.appmod.app).get("/", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/new")
