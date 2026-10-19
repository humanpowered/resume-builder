"""
Spending limits and usage events on the test site: a person who reaches a
limit keeps their record and is told why, and events never carry an email
address or record text.
"""
import contextlib
import importlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401

from fakes import FakeBackend
from resume_builder import llm, usage
from test_journey import SOURCE, TestClient, extract_reply
from test_sample import choice

# Each fake call costs 10 tokens in and 5 out: $0.00007 at the default rate.
ONE_CALL = llm.cost(llm.model(), 10, 5)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class Limits(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "RB_DB": os.path.join(self.tmp.name, "t.sqlite3"), "RB_DEV": "1", "RB_LIMITS": "1"})
        self.env.start()
        self.out = io.StringIO()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def app(self, **settings):
        os.environ.update({k: str(v) for k, v in settings.items()})
        import web.app as appmod
        self.appmod = importlib.reload(appmod)

    def sign_in(self, email):
        c = TestClient(self.appmod.app)
        c.post("/dev/login", json={"email": email})
        llm.use_backend(FakeBackend(extract_reply()))
        with contextlib.redirect_stdout(self.out):
            self.assertEqual(c.post("/api/j/import", files={
                "file": ("resume.txt", SOURCE.encode())}).status_code, 200)
        return c

    def sample(self, c):
        llm.use_backend(FakeBackend(choice()))
        with contextlib.redirect_stdout(self.out):
            return c.post("/api/j/sample/general")

    def events(self):
        return [json.loads(l) for l in self.out.getvalue().splitlines() if l.startswith("{")]

    def test_a_tester_over_their_cap_is_told_and_keeps_their_record(self):
        self.app(RB_CAP_TESTER_USD=ONE_CALL / 2)
        c = self.sign_in("pat@example.com")
        backend = FakeBackend(choice())
        llm.use_backend(backend)
        with contextlib.redirect_stdout(self.out):
            r = c.post("/api/j/sample/general")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["detail"], usage.TESTER_DONE)
        self.assertEqual(backend.requests, [])            # the call was never made
        self.assertEqual(c.get("/api/j/state").status_code, 200)
        self.assertTrue(c.get("/api/j/state").json()["jobs"])
        self.assertIn({"cap": "tester"}, [{"cap": e.get("cap")} for e in self.events()
                                          if e["rb_event"] == "cap_reached"])

    def test_under_the_cap_the_call_runs_and_its_cost_is_kept(self):
        self.app()
        c = self.sign_in("pat@example.com")
        self.assertEqual(self.sample(c).status_code, 200)
        with contextlib.closing(usage_conn()) as conn:
            rows, total = usage.report(conn)
        self.assertEqual(rows[0][0], "pat@example.com")
        self.assertEqual(rows[0][2], 2)
        self.assertAlmostEqual(total, 2 * ONE_CALL)

    def test_the_monthly_budget_covers_everyone(self):
        self.app(RB_CAP_MONTH_USD=ONE_CALL * 1.5)
        self.sign_in("pat@example.com")
        c = self.sign_in("lee@example.com")               # the second import takes it over
        r = self.sample(c)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["detail"], usage.MONTH_DONE)

    def test_too_many_calls_in_an_hour_waits(self):
        self.app(RB_CAP_CALLS_HOUR=1)
        c = self.sign_in("pat@example.com")
        r = self.sample(c)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["detail"], usage.TOO_FAST)

    def test_one_person_can_be_given_more(self):
        self.app(RB_CAP_TESTER_USD=ONE_CALL / 2,
                 RB_CAP_OVERRIDES="other@example.com=1, Pat@Example.com=1")
        c = self.sign_in("pat@example.com")
        self.assertEqual(self.sample(c).status_code, 200)

    def test_events_name_a_tester_id_never_an_email_or_record_text(self):
        self.app()
        c = self.sign_in("pat@example.com")
        self.sample(c)
        with contextlib.redirect_stdout(self.out):
            c.post("/api/j/sample/file", json={"sample": {"name": "Pat", "employers": []},
                                                "format": "docx"})
        ev = self.events()
        names = [e["rb_event"] for e in ev]
        for name in ("resume_uploaded", "model_call", "sample_made", "download"):
            self.assertIn(name, names)
        self.assertEqual(len({e["tester"] for e in ev}), 1)
        self.assertTrue(ev[0]["tester"].startswith("t_"))
        text = self.out.getvalue()
        self.assertNotIn("pat@example.com", text)
        self.assertNotIn("Riverside", text)

    def test_deleting_my_data_removes_my_spend_and_tester_id(self):
        self.app()
        c = self.sign_in("pat@example.com")
        with contextlib.redirect_stdout(self.out):
            self.assertEqual(c.delete("/api/me").status_code, 200)
        with contextlib.closing(usage_conn()) as conn:
            self.assertEqual(usage.report(conn)[0], [])
            self.assertIsNone(conn.execute("SELECT 1 FROM testers").fetchone())

    def test_a_local_copy_has_no_limits_or_events(self):
        os.environ.pop("RB_LIMITS")
        self.app(RB_CAP_TESTER_USD=0)
        c = self.sign_in("pat@example.com")
        self.assertEqual(self.sample(c).status_code, 200)
        self.assertEqual(self.events(), [])

    def test_the_review_notice_shows_once_on_the_test_site(self):
        self.app()
        c = self.sign_in("pat@example.com")
        self.assertTrue(c.get("/api/j/state").json()["review_notice"])
        self.assertEqual(c.post("/api/j/notice/seen").status_code, 200)
        self.assertFalse(c.get("/api/j/state").json()["review_notice"])
        page = c.get("/new").text
        self.assertIn("developer may review what you create", page)

    def test_a_local_copy_shows_no_review_notice(self):
        os.environ.pop("RB_LIMITS")
        self.app()
        c = self.sign_in("pat@example.com")
        self.assertFalse(c.get("/api/j/state").json()["review_notice"])

    def test_the_export_writes_a_testers_record_and_samples_as_word(self):
        from web import export_tester
        self.app()
        c = self.sign_in("pat@example.com")
        self.assertEqual(self.sample(c).status_code, 200)
        out = os.path.join(self.tmp.name, "exports")
        with contextlib.redirect_stdout(io.StringIO()):
            export_tester.main(["--all", "--out", out])
        names = sorted(os.listdir(os.path.join(out, "pat@example.com")))
        self.assertEqual(names, ["career record.docx", "resume - general.docx"])
        from docx import Document
        doc = Document(os.path.join(out, "pat@example.com", "career record.docx"))
        self.assertIn("Riverside Hospital", "\n".join(p.text for p in doc.paragraphs))


def usage_conn():
    from resume_builder import store
    return store.connect(store.database())


if __name__ == "__main__":
    unittest.main()
