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
from resume_builder import llm, sample, usage
from test_journey import SOURCE, TestClient, extract_reply
from test_sample import choice

# Each fake call costs 10 tokens in and 5 out: $0.00007 at the default rate.
ONE_CALL = llm.cost(llm.model(), 10, 5)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class Site(unittest.TestCase):
    """The test site with limits and events on, its output captured."""

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



class Limits(Site):

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


class FeedbackEvents(Site):
    """The events that measure how well drafts and questions work. Each
    carries counts, fixed choices and random ids; never record text."""

    def setUp(self):
        super().setUp()
        self.app()
        self.c = self.sign_in("pat@example.com")

    def post(self, path, **body):
        with contextlib.redirect_stdout(self.out):
            r = self.c.post(path, json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def named(self, name):
        return [e for e in self.events() if e["rb_event"] == name]

    def test_a_sample_and_its_download_share_an_id_and_name_the_prompt(self):
        made = self.sample(self.c).json()["sample"]
        self.assertTrue(made["id"].startswith("smp_"))
        ev = self.named("sample_made")[-1]
        self.assertEqual((ev["sample_id"], ev["kind"], ev["prompt_version"]),
                         (made["id"], "general", sample.PROMPT_VERSION))
        self.assertEqual(ev["bullets"], sample.bullet_count(made))
        calls = [e for e in self.named("model_call") if e["what"] == "sample"]
        self.assertEqual(calls[-1]["prompt_version"], sample.PROMPT_VERSION)
        with contextlib.redirect_stdout(self.out):
            r = self.c.post("/api/j/sample/file", json={"sample": made, "format": "docx",
                                                         "sample_id": made["id"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.named("download")[-1]["sample_id"], made["id"])

    def test_a_download_without_an_id_still_works(self):
        with contextlib.redirect_stdout(self.out):
            r = self.c.post("/api/j/sample/file", json={"sample": {"name": "Pat", "employers": []},
                                                         "format": "pdf"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("sample_id", self.named("download")[-1])

    def test_a_skipped_question_names_the_question_never_the_job(self):
        steps = self.c.get("/api/j/state").json()["steps"]
        self.post("/api/j/answer", id=steps[0]["id"], status="skipped")
        self.post("/api/j/answer", id=steps[2]["id"], status="answered", text="  ")
        self.post("/api/j/answer", id=steps[3]["id"], status="na")
        self.post("/api/j/answer", id=steps[4]["id"], status="answered", text="Hit every target")
        got = [(e["question_id"], e["reason"]) for e in self.named("question_skipped")]
        self.assertEqual(got, [("Company", "skipped"), ("Challenge", "doesnt_apply"),
                               ("Authority", "doesnt_apply")])
        self.assertNotIn("Riverside", self.out.getvalue())
        self.assertNotIn("Hit every target", self.out.getvalue())

    def stronger(self, line=1):
        w = self.c.get("/api/j/state").json()["work"]["jobs"][0]
        key = w["lines"][line]["key"]
        self.post("/api/j/line/answers", job=w["key"], line=key, answers={
            "result": "Errors dropped", "number": "from 9 a month to 2"})
        llm.use_backend(FakeBackend({"line": "Cut handoff errors from 9 a month to 2"}))
        self.post("/api/j/line/stronger", job=w["key"], line=key)
        return w["key"], key

    def test_a_stronger_line_taken_as_written(self):
        job, key = self.stronger()
        self.post("/api/j/line/approve", job=job, line=key, text="Cut handoff errors from 9 a month to 2")
        self.assertEqual([e["result"] for e in self.named("stronger_used")], ["taken"])

    def test_a_stronger_line_edited_before_approving(self):
        job, key = self.stronger()
        self.post("/api/j/line/approve", job=job, line=key, text="Cut handoff errors from 9 to 2 a month")
        self.assertEqual([e["result"] for e in self.named("stronger_used")], ["edited"])
        self.assertNotIn("9 to 2 a month", self.out.getvalue())

    def test_a_stronger_line_turned_down_by_keeping_the_original(self):
        job, key = self.stronger()
        self.post("/api/j/line/status", job=job, line=key, status="kept")
        self.assertEqual([e["result"] for e in self.named("stronger_used")], ["turned_down"])

    def test_keeping_a_line_with_no_stronger_version_logs_nothing(self):
        w = self.c.get("/api/j/state").json()["work"]["jobs"][0]
        self.post("/api/j/line/status", job=w["key"], line=w["lines"][0]["key"], status="kept")
        self.post("/api/j/line/status", job=w["key"], line=w["lines"][1]["key"], status="skipped")
        self.assertEqual(self.named("stronger_used"), [])

    def review(self):
        from test_finish import NEWER, newer_reply
        self.c.put("/api/j/step", json={"screen": "check"})
        llm.use_backend(FakeBackend(newer_reply()))
        with contextlib.redirect_stdout(self.out):
            s = self.c.post("/api/j/import", files={"file": ("newer.txt", NEWER.encode())}).json()
        self.assertTrue(s["reviewing"])
        return s["review"]["items"]

    def test_a_reviewed_upload_counts_the_lines_offered_and_kept(self):
        items = self.review()
        lines = [x["id"] for x in items if x["id"].startswith("line:")]
        self.post("/api/j/review/apply", ids=lines[:1])
        ev = self.named("import_reviewed")[-1]
        self.assertEqual((ev["lines_found"], ev["lines_kept"], ev["lines_removed"]),
                         (len(lines), 1, len(lines) - 1))

    def test_a_dropped_review_keeps_none_and_nothing_waiting_logs_nothing(self):
        items = self.review()
        found = sum(1 for x in items if x["id"].startswith("line:"))
        self.post("/api/j/review/drop")
        self.post("/api/j/review/drop")
        self.assertEqual([(e["lines_found"], e["lines_kept"]) for e in self.named("import_reviewed")],
                         [(found, 0)])


def usage_conn():
    from resume_builder import store
    return store.connect(store.database())


if __name__ == "__main__":
    unittest.main()
