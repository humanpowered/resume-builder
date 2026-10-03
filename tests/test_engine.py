"""
The engine's guards. Most of these are about refusing: what the importer
will not accept from the model, what an export will not overwrite or drop,
what the interview will not lose when someone stops half-way.

Every model reply here is scripted. Nothing reaches the network.
"""
import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from fakes import FakeBackend
from resume_builder import bullets, export, health, importer, interview, llm, skills
from resume_builder import record as mr
from resume_builder.record import Accomplishment, Record, Role, Skill


def use(*replies) -> FakeBackend:
    fake = FakeBackend(*replies)
    llm.use_backend(fake)
    return fake


def acc(title, problem="p", actions="a", results="r", evidence="", bullet=""):
    return Accomplishment(title=title, problem=problem, actions=actions,
                          results=results, evidence=evidence, bullet=bullet)


# --------------------------------------------------------------------------


class RecordSections(unittest.TestCase):

    def test_skills_education_certifications_target_round_trip(self):
        rec = Record(header=["# R"], roles=[Role(employer="Acme", title="Nurse")])
        rec.skills = [Skill("Critical care", "Clinical", "Expert", ["Cut falls"]),
                      Skill("SQL (BigQuery, Snowflake)", "Data")]
        rec.education = ["BSN, State University, 2012"]
        rec.certifications = ["CCRN"]
        rec.target = {"Titles": "Charge Nurse"}
        text = mr.render(rec)
        back = mr.parse(text)
        self.assertEqual(mr.render(back), text)
        self.assertEqual(back.skill("critical care").evidence, ["Cut falls"])
        self.assertEqual(back.skill("critical care").level, "Expert")

    def test_a_parenthesis_in_a_skill_name_is_not_a_level(self):
        """'SQL (BigQuery, Snowflake)' read as SQL at level 'BigQuery,
        Snowflake' would lose half the name."""
        rec = mr.parse("## Skills\n\n- SQL (BigQuery, Snowflake)\n")
        self.assertEqual(rec.skills[0].name, "SQL (BigQuery, Snowflake)")
        self.assertEqual(rec.skills[0].level, "")


class Importer(unittest.TestCase):
    SOURCE = ("PAT LEE\nColumbus, OH | pat@example.com\nEXPERIENCE\n"
              "Riverside Hospital\nRegistered Nurse, ICU   2019 - Present\n"
              "• Responsible for care of 2-3 critically ill patients per shift\n"
              "• Precepted new graduate nurses\nSKILLS\nVentilator management, CRRT\n"
              "Volunteer: free clinic, 2020\n")

    def reply(self, **over):
        r = {"contact": {"Name": "PAT LEE", "Location": "Columbus, OH",
                         "Email": "pat@example.com", "Telephone": "", "Linkedin": "",
                         "Website": ""},
             "summary": [],
             "roles": [{"employer": "Riverside Hospital", "title": "Registered Nurse, ICU",
                        "dates": "2019 - Present", "location": "", "company_description": "",
                        "bullets": ["Responsible for care of 2-3 critically ill patients per shift",
                                    "Precepted new graduate nurses"]}],
             "skills": ["Ventilator management", "CRRT"], "education": [],
             "certifications": [], "other": []}
        r.update(over)
        return r

    def build(self, reply):
        use(reply)
        return importer.to_record(importer.extract(self.SOURCE), self.SOURCE, "x.txt", "2026-10-03")

    def test_a_line_not_in_the_source_is_set_aside_not_used(self):
        """The importer files lines; it must never write one. A reworded or
        invented bullet would reach a resume as the person's own claim."""
        r = self.reply()
        r["roles"][0]["bullets"].append("Reduced patient falls by 40%")
        rec, rep = self.build(r)
        self.assertNotIn("Reduced patient falls by 40%", rec.roles[0].recorded_bullets)
        self.assertIn("Reduced patient falls by 40%", rep["rejected"])
        self.assertIn("Not found in the source", mr.render(rec))

    def test_a_reworded_bullet_is_rejected(self):
        r = self.reply()
        r["roles"][0]["bullets"] = ["Delivered expert care to 2-3 critically ill ICU patients each shift"]
        rec, rep = self.build(r)
        self.assertEqual(rec.roles[0].recorded_bullets, [])
        self.assertEqual(len(rep["rejected"]), 1)

    def test_a_source_line_nobody_placed_is_kept(self):
        """A parser that drops half a document looks exactly like one that works."""
        rec, rep = self.build(self.reply())
        self.assertIn("Volunteer: free clinic, 2020", rep["unplaced"])
        self.assertIn("## Unplaced", mr.render(rec))

    def test_bullet_glyphs_are_stripped(self):
        r = self.reply()
        r["roles"][0]["bullets"] = ["• Precepted new graduate nurses"]
        rec, _ = self.build(r)
        self.assertEqual(rec.roles[0].recorded_bullets, ["Precepted new graduate nurses"])

    def test_pdf_line_breaks_inside_a_bullet_still_match(self):
        src = "• Responsible for care of 2-3 critically ill\npatients per shift\n"
        self.assertTrue(importer.found_in(
            "Responsible for care of 2-3 critically ill patients per shift",
            importer._norm(" ".join(importer.source_lines(src)))))

    def test_merge_never_replaces_a_value(self):
        mine = Record(roles=[Role(employer="Riverside Hospital", title="Registered Nurse, ICU",
                                  fields={"Dates": "2019 - Present"})])
        theirs = Record(roles=[Role(employer="Riverside Hospital", title="Registered Nurse, ICU",
                                    fields={"Dates": "2018 - Present", "Location": "Columbus"},
                                    recorded_bullets=["New bullet"])])
        importer.merge(mine, theirs)
        role = mine.roles[0]
        self.assertTrue(role.fields["Dates"].startswith("2019 - Present"))
        self.assertIn("2018 - Present", role.fields["Dates"])     # kept as a note
        self.assertEqual(role.fields["Location"], "Columbus")     # blank filled
        self.assertEqual(role.recorded_bullets, ["New bullet"])


class Bullets(unittest.TestCase):

    def test_an_invented_figure_is_redrafted_then_refused(self):
        """Two drafts with a figure the source lacks, and the person's own
        words are used instead. Dull beats wrong."""
        use({"bullet": "Cut falls 40% on a 30-bed unit"},
            {"bullet": "Cut falls 45% unit-wide"})
        a = acc("Falls", problem="Falls were frequent", actions="Hourly rounding",
                results="Falls dropped by a third")
        self.assertEqual(bullets.compile_bullet(a), "Falls dropped by a third")

    def test_a_grounded_bullet_is_kept(self):
        use({"bullet": "Cut unit falls 30% by introducing hourly rounding"})
        a = acc("Falls", results="Falls down 30%", actions="Hourly rounding")
        self.assertEqual(bullets.compile_bullet(a),
                         "Cut unit falls 30% by introducing hourly rounding")

    def test_figures_compare_without_separators(self):
        self.assertEqual(bullets.ungrounded("Saved $1,200", "saved 1200 dollars"), set())

    def test_lint_catches_duties_and_first_person(self):
        self.assertIn("opens with a duty, not a result",
                      bullets.lint("Responsible for scheduling 40 staff"))
        self.assertIn("first person", bullets.lint("Cut my unit's falls 30%"))
        self.assertEqual(bullets.lint("Cut unit falls 30% with hourly rounding"), [])


class InterviewSession(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "record.md"
        rec = Record(header=["# R"], roles=[Role(
            employer="Riverside", title="RN",
            fields={k: "x" for k, _ in interview.CONTEXT_QUESTIONS},
            recorded_bullets=["Precepted new graduate nurses"])])
        self.path.write_text(mr.render(rec), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def turn(status="asking", say="?", **draft):
        d = {"title": "", "problem": "", "actions": "", "results": "",
             "evidence": "", "skills": []}
        d.update(draft)
        return {"say": say, "draft": d, "status": status}

    def session(self, answers):
        it = iter(answers)
        return interview.Session(self.path, ask=lambda q: next(it, ""), say=lambda *a: None)

    def test_expanding_a_bullet_replaces_it_and_links_skills(self):
        use(self.turn(say="How many?"),
            self.turn("complete", title="Precepted new graduate nurses",
                      problem="High first-year turnover", actions="Precepted 12 new grads",
                      results="10 of 12 stayed past a year", evidence="metric",
                      skills=["Precepting"]),
            self.turn("role_done"))
        s = self.session(["", "12 over two years", ""])     # open it; answer; skip lens
        s.role(s.rec.roles[0])
        rec = mr.parse(self.path.read_text(encoding="utf-8"))
        role = rec.roles[0]
        self.assertEqual(role.recorded_bullets, [], "the expanded bullet is replaced")
        self.assertEqual(role.accomplishments[0].evidence, "metric")
        self.assertEqual(rec.skill("Precepting").evidence, ["Precepted new graduate nurses"])

    def test_the_expanded_bullet_is_not_its_own_duplicate(self):
        use(self.turn("complete", title="Precepted new graduate nurses",
                      problem="p", actions="a", results="r", evidence="qualitative"))
        s = self.session([])
        out = s.converse(s.rec.roles[0], bullet="Precepted new graduate nurses")
        self.assertEqual(out, "complete")

    def test_stopping_mid_conversation_keeps_the_draft(self):
        use(self.turn(say="What was it like before?", title="Night staffing"))
        s = self.session([])

        def stop(q):
            raise interview.Stop()
        s.ask = stop
        with self.assertRaises(interview.Stop):
            s.converse(s.rec.roles[0])
        state = json.loads(s.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["draft"]["title"], "Night staffing")
        self.assertEqual(state["employer"], "Riverside")

    def test_a_resumed_draft_carries_its_conversation(self):
        fake = use(self.turn(say="Q1", title="Night staffing"),
                   self.turn("complete", title="Night staffing", problem="p",
                             actions="a", results="r", evidence="scope"))
        s = self.session([])
        s.ask = lambda q: (_ for _ in ()).throw(interview.Stop())
        with self.assertRaises(interview.Stop):
            s.converse(s.rec.roles[0])
        s2 = self.session([""])                   # Enter = pick it up
        s2.load_state and s2.converse(s2.rec.roles[0], resume=s2.load_state())
        self.assertEqual(len(fake.requests[1]["messages"]), 2,
                         "the resumed call continues the saved conversation")
        self.assertFalse(s2.state_path.exists())
        self.assertEqual(s2.rec.roles[0].accomplishments[0].title, "Night staffing")

    def test_one_more_angle_is_offered_once(self):
        use(self.turn("role_done"), self.turn("role_done"))
        asked = []
        s = interview.Session(self.path, ask=lambda q: asked.append(q) or "skip",
                              say=lambda *a: None)
        s.rec.roles[0].recorded_bullets = []
        s.role(s.rec.roles[0])
        lens_questions = [q for q in asked if q.startswith("One more angle")]
        self.assertEqual(len(lens_questions), 1)

    def test_context_asks_only_for_blank_fields(self):
        role = Role(employer="Acme", title="Teacher", fields={"Company": "A school"})
        asked = []
        interview.ask_context(role, lambda q: asked.append(q) or "")
        self.assertEqual(len(asked), len(interview.CONTEXT_QUESTIONS) - 1)
        self.assertNotIn("A school", "".join(asked))


class Skills(unittest.TestCase):

    def test_a_suggestion_without_real_evidence_is_dropped(self):
        rec = Record(roles=[Role(employer="A", title="t", accomplishments=[acc("Cut falls")])])
        use({"profession": "nurse", "skills": [
            {"name": "Fall prevention", "category": "Patient Safety", "because": ["Cut falls"]},
            {"name": "Telemetry", "category": "Clinical", "because": ["Ran telemetry unit"]},
            {"name": "Quality improvement", "category": "Leadership", "because": []}]})
        out = skills.suggest(rec)
        self.assertEqual([s.name for s in out["skills"]], ["Fall prevention"])
        self.assertTrue(skills.is_pending(out["skills"][0]))

    def test_confirming_moves_a_skill_into_its_group(self):
        rec = Record(skills=[Skill("Fall prevention", "To verify: Patient Safety"),
                             Skill("Telemetry", "To verify: Clinical")])
        answers = iter(["a", "n"])
        skills.verify(rec, lambda q: next(answers), say=lambda *a: None)
        self.assertEqual([(s.name, s.category, s.level) for s in rec.skills],
                         [("Fall prevention", "Patient Safety", "Advanced")])

    def test_unconfirmed_skills_never_export_as_yes(self):
        rec = Record(skills=[Skill("Telemetry", "To verify: Clinical"),
                             Skill("CRRT", "Imported"), Skill("Epic", "Systems", "Expert")])
        rows = {r["skill"]: r for r in export.skills_rows(rec)}
        self.assertEqual(rows["Telemetry"]["have_it"], "verify")
        self.assertEqual(rows["CRRT"]["have_it"], "yes")     # it was on their own resume
        self.assertEqual(rows["Epic"]["proficiency"], "Expert")


class Export(unittest.TestCase):

    def profile(self):
        return {"name": "Pat Lee", "work_history": [{
            "company": "Riverside", "title": "Registered Nurse, ICU",
            "dates": "2019 - Present",
            "highlights": ["Cut unit falls 30% with hourly rounding",
                           "Chaired the unit practice council for two years"]}]}

    def record(self):
        role = Role(employer="Riverside", title="RN", fields={
            "Dates": "Jan 2019 - Present", "Budget": "NA",
            "Location": "Columbus  <!-- the import says: Dublin -->"},
            accomplishments=[acc("Cut unit falls", results="Falls down 30%",
                                 actions="hourly rounding",
                                 bullet="Cut unit falls 30% with hourly rounding")])
        return Record(contact={"Name": "Pat Lee"}, roles=[role])

    def test_refuses_when_a_highlight_would_be_lost(self):
        ex = export.Export(self.record(), self.profile())
        self.assertIn("Chaired the unit practice council for two years",
                      ex.losses()["Riverside"])

    def test_adopt_makes_the_export_lossless(self):
        rec = self.record()
        ex = export.Export(rec, self.profile())
        self.assertEqual(ex.adopt(), 1)
        self.assertEqual(export.Export(rec, self.profile()).losses(), {})

    def test_fills_blanks_and_never_overwrites(self):
        rec = self.record()
        export.Export(rec, self.profile()).adopt()
        ex = export.Export(rec, self.profile())
        entry = ex.build()["work_history"][0]
        self.assertEqual(entry["title"], "Registered Nurse, ICU")     # curated value kept
        self.assertEqual(entry["dates"], "2019 - Present")
        self.assertTrue(any("title" in c for c in ex.conflicts))
        self.assertEqual(entry["location"], "Columbus", "annotation stripped")
        self.assertNotIn("NA", entry.get("scope", ""))

    def test_a_hand_written_bullet_is_not_redrafted(self):
        use()                     # any model call would fail: there are no replies
        rec = self.record()
        export.Export(rec, self.profile()).adopt()
        ex = export.Export(rec, self.profile())
        ex.build()
        self.assertEqual(ex.drafted, 0)

    def test_an_edited_source_is_redrafted(self):
        rec = self.record()
        export.Export(rec, self.profile()).adopt()
        ex = export.Export(rec, self.profile())
        ex.build()
        rec.roles[0].accomplishments[0].results = "Falls down 35%"
        use({"bullet": "Cut unit falls 35% with hourly rounding"})
        ex2 = export.Export(rec, self.profile(), prints=ex.prints)
        ex2.build()
        self.assertEqual(ex2.drafted, 1)

    def test_a_role_only_in_the_profile_is_carried_over(self):
        p = self.profile()
        p["work_history"].append({"company": "Old Job", "highlights": ["Did things"]})
        rec = self.record()
        export.Export(rec, p).adopt()
        built = export.Export(rec, p).build()
        self.assertIn("Old Job", [e["company"] for e in built["work_history"]])

    def test_csv_merge_never_changes_an_existing_row(self):
        existing = [{"category": "Clinical", "skill": "CRRT", "have_it": "no",
                     "proficiency": "", "source": "me", "notes": ""}]
        rows, added = export.merge_csv(existing, export.skills_rows(Record(skills=[
            Skill("CRRT", "Clinical", "Expert"), Skill("Epic", "Systems")])))
        self.assertEqual(added, 1)
        self.assertEqual(rows[0]["have_it"], "no", "a person's 'no' stands")


class Health(unittest.TestCase):

    def test_date_forms_people_write(self):
        cases = {"Jan 2023 - Present": ((2023, 1), (2026, 10)),
                 "11/2020 – 12/2022": ((2020, 11), (2022, 12)),
                 "Nov. 2005 – Oct. 2010": ((2005, 11), (2010, 10)),
                 "2016-2019": ((2016, 1), (2019, 12)),
                 "June 2015 to Aug 2019": ((2015, 6), (2019, 8))}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(health.parse_range(text), want)

    def test_a_gap_over_six_months_is_reported(self):
        rec = Record(contact={"Name": "x", "Email": "y"}, education=["e"], roles=[
            Role(employer="New", title="t", fields={"Dates": "2021 - Present"}),
            Role(employer="Old", title="t", fields={"Dates": "2015 - Jan 2020"})])
        rep = health.check(rec)
        self.assertTrue(any("gap" in i for i in rep.record_issues))

    def test_unsettled_notes_and_blanks_are_flagged(self):
        rec = Record(roles=[Role(employer="A", title="t", fields={
            "Dates": "2019 - 2020 <!-- other says 2018 -->"})])
        rep = health.check(rec)
        self.assertTrue(rep.roles[0].issues)

    def test_next_steps_point_at_the_thinnest_recent_role(self):
        rec = Record(contact={"Name": "x", "Email": "y"}, education=["e"], roles=[
            Role(employer="Current", title="t", fields={"Dates": "2022 - Present"})])
        self.assertTrue(any("Current" in s for s in health.check(rec).next_steps))


if __name__ == "__main__":
    unittest.main()
