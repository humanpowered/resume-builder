"""
The engine's guards. Most of these are about refusing: what the importer
will not accept from the model, what an export will not overwrite or drop,
what the interview will not lose when someone stops half-way.

Every model reply here is scripted. Nothing reaches the network.
"""
import unittest
from pathlib import Path

import helpers  # noqa: F401

from fakes import FakeBackend
from resume_builder import bullets, export, health, importer, interview, llm, skills, summary
from resume_builder import record as mr
from resume_builder.record import Accomplishment, Record, Role, Skill
from resume_builder.store import MemoryStore


def finish(iv) -> int:
    """Skip every remaining question; how many it took."""
    for n in range(1, 100):
        if iv.step("").kind == "done":
            return n
    raise AssertionError("the interview never finished")


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

    def test_volunteer_work_and_summary_land_in_their_sections(self):
        r = self.reply(other=[{"heading": "VOLUNTEER EXPERIENCE",
                               "lines": ["Volunteer: free clinic, 2020"]}],
                       summary=["PAT LEE"])
        rec, rep = self.build(r)
        self.assertEqual(rec.extras["volunteer"], ["Volunteer: free clinic, 2020"])
        self.assertEqual(rec.summary, "PAT LEE")
        self.assertNotIn("Volunteer: free clinic, 2020", rep["unplaced"])
        self.assertIn("## Volunteer work", mr.render(rec))

    def test_merge_keeps_your_summary_and_adds_new_entries(self):
        mine = Record(summary="Mine.", extras={"languages": ["Spanish (fluent)"]})
        theirs = Record(summary="Theirs.", extras={"languages": ["Spanish (fluent)", "French"]})
        importer.merge(mine, theirs)
        self.assertEqual(mine.summary, "Mine.")
        self.assertIn("Theirs.", "\n".join(mine.trailing))
        self.assertEqual(mine.extras["languages"], ["Spanish (fluent)", "French"])

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


class InterviewEngine(unittest.TestCase):
    """The interview one turn at a time, as a web page would drive it."""

    def setUp(self):
        self.store = MemoryStore(Record(header=["# R"], roles=[Role(
            employer="Riverside", title="RN",
            fields={k: "x" for k, _ in interview.CONTEXT_QUESTIONS},
            recorded_bullets=["Precepted new graduate nurses"])]))
        # Contact, target and story are asked once; these tests are about the jobs.
        self.store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS + interview.STORY_QUESTIONS]})

    @staticmethod
    def turn(status="asking", say="?", **draft):
        d = {"title": "", "problem": "", "actions": "", "results": "",
             "evidence": "", "skills": []}
        d.update(draft)
        return {"say": say, "draft": d, "status": status}

    def test_expanding_a_bullet_replaces_it_and_links_skills(self):
        use(self.turn(say="How many?"),
            self.turn("complete", title="Precepted new graduate nurses",
                      problem="High first-year turnover", actions="Precepted 12 new grads",
                      results="10 of 12 stayed past a year", evidence="metric",
                      skills=["Precepting"]),
            self.turn("role_done"))
        iv = interview.Interview(self.store)
        self.assertTrue(iv.step().text.startswith("A new job"))
        p = iv.step("")
        self.assertEqual(p.kind, "choice")                 # open the bullet?
        self.assertEqual(iv.step("").text, "How many?")
        p = iv.step("12 over two years")
        self.assertTrue(any("recorded" in n for n in p.notes))
        self.assertTrue(p.text.startswith("One more angle"))
        self.assertEqual(iv.step("").text, interview.DUTIES_QUESTION, "one accomplishment: thin")
        self.assertTrue(iv.step("").text.startswith("Work outside a paid job"))
        self.assertTrue(iv.step("").text.startswith("Your next qualification"))
        # no degree recorded, so training on the job is asked next
        self.assertTrue(iv.step("").text.startswith("Did any of your jobs give you formal training"))
        self.assertTrue(iv.step("").text.startswith("Your next licence"))
        # one Enter per remaining section; a nurse is never asked about a clearance,
        # and volunteering was asked with work outside paid jobs
        self.assertEqual(finish(iv), len(interview.SECTIONS) - 4, "one Enter per section")
        rec = self.store.load_record()
        self.assertEqual(rec.roles[0].recorded_bullets, [], "the expanded bullet is replaced")
        self.assertEqual(rec.roles[0].accomplishments[0].evidence, "metric")
        self.assertEqual(rec.skill("Precepting").evidence, ["Precepted new graduate nurses"])

    def test_a_team_result_keeps_the_persons_own_part(self):
        fake = use(self.turn("complete", title="Cut unit falls",
                             problem="Falls above benchmark", actions="We ran hourly rounding",
                             contribution="I designed the rounding checklist and trained 30 staff",
                             results="Our falls dropped 30%", evidence="metric"),
                   self.turn("role_done"))
        iv = interview.Interview(self.store)
        iv.step()
        iv.step("")                                        # no new job
        iv.step("")
        acc_ = self.store.load_record().roles[0].accomplishments[0]
        self.assertEqual(acc_.contribution, "I designed the rounding checklist and trained 30 staff")
        self.assertFalse(acc_.team_unclear())
        self.assertIn("what their own part was", fake.requests[0]["messages"][0]["content"])

    def test_skills_used_are_kept_on_the_accomplishment_and_explained_once(self):
        fake = use(self.turn("complete", title="Automated weekly reporting",
                             problem="p", actions="Built a pipeline", results="6 hours a week saved",
                             evidence="metric", skills=["Python", "SQL", "dbt", "python"]),
                   self.turn("role_done"))
        iv = interview.Interview(self.store)
        iv.step()
        iv.step("")                                        # no new job
        iv.step("")
        rec = self.store.load_record()
        a = rec.roles[0].accomplishments[0]
        self.assertEqual(a.skill_names(), ["Python", "SQL", "dbt"], "kept once each")
        self.assertEqual(rec.skill("dbt").evidence, ["Automated weekly reporting"])
        first = fake.requests[0]["messages"][0]["content"]
        self.assertIn("What tools, methods or know-how did that take?", first)
        self.assertIn('"explain_skills": true', first)
        self.assertTrue(iv.state["skills_explained"])
        self.assertFalse(interview.Interview(self.store)._explain_skills(),
                         "a record that already has skills used is not told again")

    def test_the_expanded_bullet_is_not_its_own_duplicate(self):
        use(self.turn("complete", title="Precepted new graduate nurses",
                      problem="p", actions="a", results="r", evidence="qualitative"),
            self.turn("role_done"))
        iv = interview.Interview(self.store)
        iv.step()
        iv.step("")                                        # no new job
        iv.step("")
        self.assertEqual(len(self.store.load_record().roles[0].accomplishments), 1)

    def test_a_new_process_picks_up_mid_conversation(self):
        """The page closes, the server restarts: the next turn carries on with
        the same conversation, not a fresh one."""
        fake = use(self.turn(say="What was it like before?", title="Night staffing"),
                   self.turn("complete", title="Night staffing", problem="p",
                             actions="a", results="r", evidence="scope"),
                   self.turn("role_done"))
        iv = interview.Interview(self.store)
        iv.step()
        iv.step("")                                        # no new job
        iv.step("skip")                                    # leave the bullet
        self.assertEqual(iv.step(None).text, "What was it like before?")

        again = interview.Interview(self.store)            # a new process
        self.assertEqual(again.step(None).text, "What was it like before?",
                         "re-shows the open question without a model call")
        self.assertEqual(len(fake.requests), 1)
        again.step("It was chaos")
        self.assertEqual(len(fake.requests[1]["messages"]), 3,
                         "the second call continues the saved conversation")
        self.assertEqual(self.store.load_record().roles[0].accomplishments[0].title,
                         "Night staffing")

    def test_one_more_angle_is_offered_once(self):
        use(self.turn("role_done"), self.turn("role_done"))
        rec = self.store.load_record()
        rec.roles[0].recorded_bullets = []
        self.store.save_record(rec)
        asked = []
        interview.run(interview.Interview(self.store),
                      ask=lambda q: asked.append(q) or "skip", say=lambda *a: None)
        self.assertEqual(len([q for q in asked if q.startswith("One more angle")]), 1)

    def test_context_asks_only_for_blank_fields(self):
        store = MemoryStore(Record(roles=[Role(employer="Acme", title="Teacher",
                                               fields={"Company": "A school"})]))
        asked = []
        use(self.turn("role_done"))
        interview.run(interview.Interview(store),
                      ask=lambda q: asked.append(q) or "", say=lambda *a: None)
        firsts = tuple(qs[0][1] for _, qs in interview.SECTIONS)
        once = tuple(q[2] for q in interview.PROFILE_QUESTIONS + interview.STORY_QUESTIONS)
        context = [q for q in asked if not q.startswith(("One more angle", "A new job",
                                                         interview.DUTIES_QUESTION,
                                                         "Work outside a paid job",
                                                         *firsts, *once))]
        self.assertEqual(len(context), len(interview.CONTEXT_QUESTIONS) - 1)

    def test_education_and_certifications_on_their_own(self):
        rec = self.store.load_record()
        rec.education = ["BSN in Nursing, Ohio State University, 2015"]
        self.store.save_record(rec)
        iv = interview.Interview(self.store, only=interview.BACKGROUND)
        p = iv.step()
        self.assertTrue(any("Ohio State" in n for n in p.notes), "says what is there")
        self.assertTrue(p.text.startswith("Your next qualification"))
        p = iv.step("MSN")
        self.assertTrue(p.text.startswith("MSN: field of study"))
        for a in ("Nursing Leadership", "Duke University", "2021"):
            iv.step(a)
        p = iv.step("")                                    # no honours
        self.assertIn("recorded: MSN in Nursing Leadership, Duke University, 2021", p.notes)
        for a in ("BSN", "Nursing", "Ohio State University", "2015"):
            iv.step(a)
        p = iv.step("")                                    # already there: not added twice
        self.assertFalse(any(n.startswith("recorded") for n in p.notes))
        p = iv.step("")                                    # no more education
        self.assertTrue(p.text.startswith("Your next licence"))
        for a in ("CCRN", "AACN", "2020"):
            iv.step(a)
        iv.step("2026")
        p = iv.step("")
        while p.kind != "done":
            p = iv.step("")
        self.assertIn("2 other entries", p.text)
        rec = self.store.load_record()
        self.assertEqual(rec.education, ["BSN in Nursing, Ohio State University, 2015",
                                         "MSN in Nursing Leadership, Duke University, 2021"])
        self.assertEqual(rec.certifications, ["CCRN, AACN, 2020 (expires 2026)"])
        ex = export.Export(rec, {}, {})
        profile = ex.build()
        self.assertIn("CCRN, AACN, 2020 (expires 2026)", profile["certifications"])
        self.assertEqual(len(profile["education"]), 2)

    def test_jumping_to_education_keeps_the_place_in_a_job(self):
        use(self.turn(say="How many?"))
        iv = interview.Interview(self.store)
        iv.step()
        iv.step("")                                        # no new job
        self.assertEqual(iv.step("").text, "How many?")
        bg = interview.Interview(self.store, only=interview.BACKGROUND)
        self.assertTrue(bg.step().text.startswith("Your next qualification"))
        finish(bg)
        self.assertEqual(interview.Interview(self.store).step(None).text, "How many?")

    def test_an_employer_filter_skips_education(self):
        use(self.turn("role_done"))
        iv = interview.Interview(self.store, only="Riverside")
        iv.step()
        p = iv.step("skip")
        while p.kind != "done":
            self.assertFalse(p.text.startswith("Your next"))
            p = iv.step("")

    def test_a_clearance_is_recorded_as_one_line(self):
        iv = interview.Interview(self.store, only="background:clearance")
        p = iv.step()
        self.assertTrue(p.text.startswith("Do you hold, or have you held, a security clearance"))
        iv.step("Top Secret/SCI")
        iv.step("active")
        iv.step("Department of Defense")
        p = iv.step("CI polygraph")
        self.assertIn("recorded: Top Secret/SCI, active, Department of Defense, CI polygraph",
                      p.notes)
        self.assertEqual(iv.step("").kind, "done")
        rec = self.store.load_record()
        self.assertEqual(rec.extras["clearance"],
                         ["Top Secret/SCI, active, Department of Defense, CI polygraph"])
        text = mr.render(rec)
        self.assertIn("## Security clearance", text)
        self.assertEqual(mr.parse(text).extras["clearance"], rec.extras["clearance"])
        self.assertEqual(export.Export(rec, {}, {}).build()["clearance"], rec.extras["clearance"])

    def asked_in_background(self, rec) -> list:
        """The first question of every background section, in order."""
        iv = interview.Interview(MemoryStore(rec), only="background")
        seen, p = [], iv.step()
        while p.kind != "done":
            seen.append(p.text)
            p = iv.step("")
        return seen

    def test_the_clearance_question_is_skipped_without_signs_of_cleared_work(self):
        rec = Record(header=["# R"], roles=[Role(employer="Riverside Hospital", title="ICU RN")])
        self.assertFalse(any("security clearance" in q for q in self.asked_in_background(rec)))

    def test_the_clearance_question_is_asked_for_defence_and_federal_work(self):
        for role in (Role(employer="US Army", title="Logistics Officer"),
                     Role(employer="Leidos", title="Systems Engineer"),
                     Role(employer="Acme", title="Analyst", fields={"Company": "Federal IT contractor"})):
            rec = Record(header=["# R"], roles=[role])
            asked = self.asked_in_background(rec)
            self.assertTrue(any("security clearance" in q for q in asked), role.employer)
        self.assertFalse(interview.clearance_relevant(
            Record(roles=[Role(employer="Acme", title="Fundraiser")])), "'fund' is not 'defense'")

    def test_a_resume_clearance_heading_is_filed_as_clearance(self):
        rec = mr.parse("## Clearance\n\n- Secret, active\n")
        self.assertEqual(rec.extras["clearance"], ["Secret, active"])

    def test_one_optional_section_on_its_own(self):
        iv = interview.Interview(self.store, only="background:languages")
        p = iv.step()
        self.assertIn("Languages (optional; Enter skips it).", p.notes)
        self.assertTrue(p.text.startswith("A language"))
        iv.step("Spanish")
        p = iv.step("professional")
        self.assertIn("recorded: Spanish (professional)", p.notes)
        p = iv.step("")
        self.assertEqual(p.kind, "done", "only that section is asked")
        self.assertEqual(self.store.load_record().extras["languages"], ["Spanish (professional)"])
        with self.assertRaises(ValueError):
            interview.Interview(self.store, only="background:hobbies")

    def test_every_section_formats_its_answers(self):
        for key, questions in interview.SECTIONS:
            with self.subTest(key=key):
                line = interview.FORMAT[key]({k: f"x{k}" for k, *_ in questions})
                for k, *_ in questions:
                    self.assertIn(f"x{k}", line)

    def test_listing_jobs_from_nothing(self):
        store = MemoryStore()
        store.save_state(interview.ASKED_STATE, {"asked": ["Name", "Email", "Telephone",
                                                          "Location", "Linkedin", "Titles",
                                                          "Industries", "Locations"]})
        iv = interview.Interview(store)
        self.assertTrue(iv.step().text.startswith("Employer"))
        iv.step("Lakeside High School")
        iv.step("Science Teacher")
        self.assertTrue(iv.step("2014 - Present").text.startswith("Any other title at Lakeside"))
        self.assertTrue(iv.step("").text.startswith("Employer"))
        use(self.turn("role_done"))
        p = iv.step("")                                    # done listing
        self.assertIn("Lakeside High School", " ".join(p.notes))
        self.assertEqual(store.load_record().roles[0].fields["Dates"], "2014 - Present")

    def test_state_is_cleared_when_finished(self):
        use(self.turn("role_done"))
        rec = self.store.load_record()
        rec.roles[0].recorded_bullets = []
        self.store.save_record(rec)
        interview.run(interview.Interview(self.store), ask=lambda q: "", say=lambda *a: None)
        self.assertIsNone(self.store.load_state(interview.STATE))


class Skills(unittest.TestCase):

    @staticmethod
    def item(name, category="Clinical", same_as="", named=False, because=(), level=""):
        return {"name": name, "category": category, "same_as": same_as, "named": named,
                "because": list(because), "level": level}

    def record(self):
        rec = Record(roles=[Role(employer="A", title="RN", accomplishments=[acc("Cut falls")],
                                 recorded_bullets=["Charted in Epic for a 30-bed unit"])],
                     skills=[Skill("CRRT", "Imported", source="resume"),
                             Skill("Telemetry", "Clinical", "Expert", have="yes"),
                             Skill("ECMO", "Clinical", have="no")])
        return rec

    def test_build_fills_in_what_the_record_shows_and_asks_the_rest(self):
        rec = self.record()
        use({"field": "ICU nursing", "skills": [
            self.item("Continuous Renal Replacement Therapy, CRRT", same_as="CRRT",
                      level="Advanced"),
            self.item("Telemetry", level="Working"),               # the person said Expert
            self.item("ECMO", level="Familiar"),                  # the person said no
            self.item("Epic", "Systems", named=True, level="Working"),
            self.item("Fall prevention", "Patient Safety", because=["Cut falls"],
                      level="Advanced"),
            self.item("Sepsis protocols", because=["Ran the sepsis unit"], level="Expert"),
            self.item("Wound care")]})
        out = skills.build(rec, "ICU nursing")
        self.assertEqual(out["field"], "ICU nursing")
        got = {s.name: (s.category, s.have, s.level, s.source) for s in rec.skills}
        self.assertEqual(got["CRRT"], ("Clinical", "yes", "Advanced", "resume"),
                         "an imported skill is sorted and given an estimated level")
        self.assertEqual(got["Telemetry"], ("Clinical", "yes", "Expert", ""),
                         "a level the person set is never changed")
        self.assertEqual(got["ECMO"], ("Clinical", "no", "", ""))
        self.assertEqual(got["Epic"], ("Systems", "yes", "Working", "resume"))
        self.assertEqual(got["Fall prevention"],
                         ("Patient Safety", "verify", "Advanced", "your record"))
        self.assertEqual(got["Sepsis protocols"], ("Clinical", "verify", "", "your field"),
                         "evidence that isn't in the record is dropped, and its level with it")
        self.assertEqual(got["Wound care"], ("Clinical", "verify", "", "your field"))
        self.assertEqual(len(rec.skills), 7)

    def test_a_skill_the_record_does_not_name_is_not_taken_as_yes(self):
        rec = self.record()
        use({"field": "nursing", "skills": [self.item("Ventilator management", named=True)]})
        skills.build(rec)
        self.assertEqual(rec.skill("Ventilator management").have, "verify")

    def test_answering_on_the_command_line(self):
        rec = Record(skills=[Skill("Fall prevention", "Patient Safety", "Advanced",
                                   ["Cut falls"], have="verify"),
                             Skill("Telemetry", "Clinical", have="verify"),
                             Skill("Wound care", "Clinical", have="verify")])
        answers = iter(["y", "n", "w"])
        skills.verify(rec, lambda q: next(answers), say=lambda *a: None)
        self.assertEqual([(s.name, s.have, s.level) for s in rec.skills],
                         [("Fall prevention", "yes", "Advanced"), ("Telemetry", "no", ""),
                          ("Wound care", "yes", "Working")])

    def test_editing_a_skill(self):
        rec = self.record()
        skills.set_skill(rec, "ECMO", level="Working")
        self.assertEqual((rec.skill("ECMO").have, rec.skill("ECMO").level), ("yes", "Working"))
        rec.skill("ECMO").have = "verify"
        skills.set_skill(rec, "ECMO", level="Advanced")
        self.assertEqual(rec.skill("ECMO").have, "yes", "giving a level is a yes")
        skills.set_skill(rec, "ECMO", have="no")
        self.assertEqual(rec.skill("ECMO").level, "")
        skills.set_skill(rec, "CRRT", rename="CRRT, Continuous Renal Replacement",
                         category="Renal")
        self.assertEqual(rec.skill("CRRT, Continuous Renal Replacement").category, "Renal")
        with self.assertRaises(ValueError):
            skills.set_skill(rec, "Telemetry", rename="ECMO")
        with self.assertRaises(ValueError):
            skills.set_skill(rec, "Telemetry", level="Guru")
        with self.assertRaises(LookupError):
            skills.set_skill(rec, "Nothing", have="yes")

    def test_adding_a_skill_the_list_missed(self):
        rec = self.record()
        skills.add_skill(rec, "Wound vac", "Clinical", "Expert")
        self.assertEqual((rec.skill("Wound vac").have, rec.skill("Wound vac").source),
                         ("yes", "you"))
        skills.add_skill(rec, "ecmo")                       # already listed as no
        self.assertEqual(rec.skill("ECMO").have, "yes")
        self.assertEqual(len(rec.skills), 4)

    def test_yes_to_everything_the_record_shows(self):
        rec = Record(skills=[Skill("A", have="verify", evidence=["x"], level="Working"),
                             Skill("B", have="verify")])
        self.assertEqual(skills.accept_shown(rec), ["A"])
        self.assertEqual([s.have for s in rec.skills], ["yes", "verify"])

    def test_export_matches_the_pipelines_inventory(self):
        rec = Record(skills=[Skill("Telemetry", "Clinical", "Working", have="verify"),
                             Skill("CRRT", "Imported", source="resume"),
                             Skill("ECMO", "Clinical", have="no", source="your field"),
                             Skill("Epic", "Systems", "Expert")])
        rows = {r["skill"]: r for r in export.skills_rows(rec)}
        self.assertEqual(rows["Telemetry"]["have_it"], "verify")
        self.assertEqual(rows["Telemetry"]["proficiency"], "", "an estimate is not an answer")
        self.assertEqual((rows["CRRT"]["category"], rows["CRRT"]["have_it"],
                          rows["CRRT"]["source"]), ("Other", "yes", "resume"))
        self.assertEqual(rows["ECMO"]["have_it"], "no")
        self.assertEqual(rows["Epic"]["proficiency"], "Expert")
        self.assertEqual(list(rows["Epic"]), export.CSV_COLUMNS)

    def test_the_older_to_verify_layout_still_reads(self):
        rec = mr.parse("## Skills\n\n### To verify: Clinical\n\n- Telemetry — evidence: A\n")
        self.assertEqual((rec.skills[0].category, rec.skills[0].have), ("Clinical", "verify"))
        text = mr.render(rec)
        self.assertIn("## Skills to check", text)
        self.assertEqual(mr.render(mr.parse(text)), text)


class SkillUse(unittest.TestCase):
    """Years used and last used, worked out from the roles that prove a skill."""

    def record(self):
        return Record(roles=[
            Role(employer="Clinic", title="RN", fields={"Dates": "Jan 2022 - Present"},
                 accomplishments=[acc("Ran the telemetry unit")]),
            Role(employer="Mercy", title="RN", fields={"Dates": "Jan 2015 - Dec 2022"},
                 accomplishments=[acc("Cut falls"), acc("Charted on paper")])],
            skills=[Skill(name="Telemetry", evidence=["Ran the telemetry unit", "Cut falls"]),
                    Skill(name="Paper charting", evidence=["Charted on paper"]),
                    Skill(name="Epic", evidence=["Cut falls"], years="3", last_used="2019"),
                    Skill(name="Wound care"),
                    Skill(name="Dialysis", evidence=["Cut falls"], have="no")])

    def test_overlapping_roles_count_once_and_present_is_current(self):
        rec = self.record()
        dated = skills.estimate_use(rec, today=(2026, 10))
        tele = rec.skill("Telemetry")
        self.assertEqual((tele.years, tele.last_used), ("~12", "~current"))   # 2015-01 .. 2026-10
        paper = rec.skill("Paper charting")
        self.assertEqual((paper.years, paper.last_used), ("~8", "~2022"))
        self.assertEqual(dated, ["Telemetry", "Paper charting"])

    def test_typed_values_unproven_and_absent_skills_are_left_alone(self):
        rec = self.record()
        skills.estimate_use(rec, today=(2026, 10))
        self.assertEqual((rec.skill("Epic").years, rec.skill("Epic").last_used), ("3", "2019"))
        self.assertEqual(rec.skill("Wound care").years, "")
        self.assertEqual(rec.skill("Dialysis").years, "")

    def test_years_round_trip_through_the_record(self):
        rec = self.record()
        skills.estimate_use(rec, today=(2026, 10))
        text = mr.render(rec)
        self.assertIn("- Telemetry — years: ~12 — last used: ~current — evidence:", text)
        back = mr.parse(text)
        self.assertEqual((back.skill("Telemetry").years, back.skill("Telemetry").last_used),
                         ("~12", "~current"))
        self.assertEqual(back.skill("Telemetry").evidence, ["Ran the telemetry unit", "Cut falls"])
        self.assertEqual(back.skill("Epic").years, "3")

    def test_the_csv_carries_use_in_its_notes_column(self):
        rec = self.record()
        skills.estimate_use(rec, today=(2026, 10))
        row = next(r for r in export.skills_rows(rec) if r["skill"] == "Telemetry")
        self.assertEqual(row["notes"],
                         "Used 12 yrs, last used current. evidence: Ran the telemetry unit; Cut falls")
        self.assertEqual(set(row), {"category", "skill", "have_it", "proficiency", "source", "notes"})

    def test_a_person_can_correct_the_estimate(self):
        rec = self.record()
        skills.estimate_use(rec, today=(2026, 10))
        skills.set_skill(rec, "Telemetry", years="10")
        self.assertEqual(rec.skill("Telemetry").years, "10")


class Summary(unittest.TestCase):
    def test_a_draft_flags_figures_the_record_does_not_hold(self):
        rec = Record(roles=[Role(employer="A", title="RN", accomplishments=[
            acc("Cut falls", results="Falls down 30% in a year")])])
        use({"summary": "ICU nurse who cut falls 30% and saved $2M."})
        out = summary.draft(rec)
        self.assertEqual(out["unsupported"], ["2"])

    def test_check_flags_first_person_and_length(self):
        self.assertEqual(summary.check("ICU nurse with ten years."), [])
        self.assertEqual(len(summary.check("I am a nurse. " + "word " * 100)), 2)

    def test_summary_and_sections_reach_the_profile(self):
        rec = Record(header=["# R"], contact={"Name": "Pat"}, summary="ICU nurse.",
                     extras={"languages": ["Spanish (fluent)"], "volunteer": ["Food bank"]})
        p = export.Export(rec, {}, {}).build()
        self.assertEqual(p["summary"], "ICU nurse.")
        self.assertEqual(p["languages"], ["Spanish (fluent)"])
        self.assertEqual(p["volunteer"], ["Food bank"])


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


class TeamResults(unittest.TestCase):
    """Most results are shared; the record says which part was the person's."""

    def test_contribution_round_trips_and_only_shows_when_filled(self):
        a = acc("Cut falls", actions="We ran rounding", results="Falls down 30%")
        a.contribution = "Wrote the checklist"
        role = Role(employer="Mercy", title="RN", accomplishments=[a, acc("Solo work")])
        text = mr.render(Record(contact={"Name": "x"}, roles=[role]))
        self.assertEqual(text.count("**Contribution:**"), 1)
        back = mr.parse(text).roles[0].accomplishments
        self.assertEqual(back[0].contribution, "Wrote the checklist")
        self.assertEqual(back[1].contribution, "")

    def test_a_team_result_with_no_part_is_flagged(self):
        team = acc("Cut falls", actions="We ran hourly rounding", results="Falls down 30%")
        mine = acc("Wrote policy", actions="Drafted the falls policy", results="Adopted hospital-wide")
        self.assertTrue(team.team_unclear())
        self.assertFalse(mine.team_unclear())
        rec = Record(contact={"Name": "x", "Email": "y"}, education=["e"],
                     roles=[Role(employer="Mercy", title="RN", accomplishments=[team, mine])])
        issues = health.check(rec).roles[0].issues
        self.assertEqual(issues, ["'Cut falls' reads as a team result; what was your part?"])

    def test_figures_in_the_persons_part_are_grounded(self):
        a = acc("Cut falls", actions="We ran rounding", results="Falls down 30%")
        a.contribution = "Trained 30 staff"
        self.assertEqual(bullets.ungrounded("Trained 30 staff in rounding that cut falls 30%",
                                            a.source_text()), set())

    def test_the_bullet_prompt_carries_the_part(self):
        fake = use({"bullet": "Trained 30 staff in hourly rounding, part of a unit effort that cut falls 30%"})
        a = acc("Cut falls", actions="We ran rounding", results="Falls down 30%")
        a.contribution = "Trained 30 staff"
        bullets.compile_bullet(a)
        prompt = fake.requests[0]["messages"][0]["content"]
        self.assertIn("My part: Trained 30 staff", prompt)
        self.assertIn("never to\n  them alone", prompt)


class SkillsInContext(unittest.TestCase):
    """An accomplishment names the skills it took; the skills list points back."""

    def record(self):
        a = acc("Automated weekly reporting", results="6 hours a week saved")
        a.skills_used = "Python; SQL; dbt"
        b = acc("Cut month-end close", results="Close down 2 days")
        return Record(contact={"Name": "x"}, roles=[Role(employer="Acme", title="Analyst",
                                                         accomplishments=[a, b])],
                      skills=[Skill("Excel", evidence=["Cut month-end close"]),
                              Skill("Tableau", evidence=["Cut month-end close"], have="verify"),
                              Skill("dbt", have="no")])

    def test_round_trip_and_hidden_when_empty(self):
        rec = self.record()
        text = mr.render(rec)
        self.assertEqual(text.count("**Skills used:**"), 1)
        self.assertIn("- **Skills used:** Python; SQL; dbt", text)
        self.assertEqual(mr.parse(text).roles[0].accomplishments[0].skill_names(),
                         ["Python", "SQL", "dbt"])

    def test_links_run_both_ways_for_confirmed_skills_only(self):
        rec = self.record()
        skills.link_both_ways(rec)
        a, b = rec.roles[0].accomplishments
        self.assertEqual(rec.skill("Python").evidence, ["Automated weekly reporting"])
        self.assertEqual(rec.skill("Python").have, "yes", "the person named it")
        self.assertEqual(b.skill_names(), ["Excel"], "an unconfirmed skill is not added")

    def test_export_names_skills_per_accomplishment_and_drops_declined_ones(self):
        rec = self.record()
        for x in rec.roles[0].accomplishments:
            x.bullet = x.title
        entry = export.Export(rec, {}, {}).build()["work_history"][0]
        self.assertEqual(entry["skills_in_context"], [
            {"accomplishment": "Automated weekly reporting", "skills": ["Python", "SQL"]},
            {"accomplishment": "Cut month-end close", "skills": ["Excel"]}])

    def test_adding_skills_does_not_redraft_a_bullet(self):
        a = acc("Automated weekly reporting")
        before = export.fingerprint(a)
        a.skills_used = "Python"
        self.assertEqual(export.fingerprint(a), before)


class DegreeSubstitutes(unittest.TestCase):
    """For people without a degree: training on the job, and promotions."""

    def test_what_counts_as_a_degree(self):
        yes = ["BSN, Ohio State University, 2015", "Bachelor of Arts in History", "MBA, Wharton",
               "B.S. Accounting", "PhD in Chemistry", "Master's in Education"]
        no = ["High school diploma, 2008", "Associate degree in Nursing", "Welding certificate",
              "Excel and MS Office course", "GED"]
        for e in yes:
            self.assertTrue(mr.has_degree(Record(education=[e])), e)
        for e in no:
            self.assertFalse(mr.has_degree(Record(education=[e])), e)

    def asked(self, rec) -> list:
        iv = interview.Interview(MemoryStore(rec), only="background")
        seen, p = [], iv.step()
        while p.kind != "done":
            seen.append(p.text)
            p = iv.step("")
        return seen

    def test_training_is_asked_only_without_a_degree(self):
        q = "Did any of your jobs give you formal training"
        none = Record(header=["# R"], roles=[Role(employer="Acme", title="Operator")],
                      education=["High school diploma"])
        grad = Record(header=["# R"], roles=[Role(employer="Acme", title="Operator")],
                      education=["BS Mechanical Engineering, Purdue, 2012"])
        self.assertTrue(any(x.startswith(q) for x in self.asked(none)))
        self.assertFalse(any(x.startswith(q) for x in self.asked(grad)))

    def test_a_degree_given_earlier_in_the_same_session_skips_training(self):
        store = MemoryStore(Record(header=["# R"], roles=[Role(employer="Acme", title="Operator")]))
        iv = interview.Interview(store, only="background")
        iv.step()
        for answer in ("BS", "Mechanical Engineering", "Purdue", "2012", ""):
            p = iv.step(answer)
        p = iv.step("")                     # no more qualifications
        self.assertTrue(p.text.startswith("Your next licence"), p.text)

    def test_training_is_recorded_and_proves_the_skills_it_taught(self):
        store = MemoryStore(Record(header=["# R"], roles=[Role(employer="Acme Foods", title="Operator")],
                                   skills=[Skill("Lean", have="yes")]))
        iv = interview.Interview(store, only="background:job_training")
        iv.step()
        for answer in ("Green Belt programme", "Acme Foods", "2019", "40 hours"):
            iv.step(answer)
        p = iv.step("Lean; Root cause analysis")
        line = "Green Belt programme (Acme Foods, 2019, 40 hours). Skills: Lean; Root cause analysis"
        self.assertIn(f"recorded: {line}", p.notes)
        rec = store.load_record()
        self.assertEqual(rec.extras["training"], [line])
        self.assertEqual(rec.skill("Lean").evidence, ["training: Green Belt programme"])
        self.assertEqual(rec.skill("Root cause analysis").evidence, ["training: Green Belt programme"])
        self.assertIn("## Training and courses", mr.render(rec))

    def test_a_trade_programme_is_asked_its_hours_and_what_it_taught(self):
        store = MemoryStore(Record(header=["# R"], roles=[Role(employer="Salon", title="Stylist")]))
        iv = interview.Interview(store, only="background:education")
        iv.step()
        for answer in ("Cosmetology diploma", "", "Paul Mitchell School", "2019", ""):
            p = iv.step(answer)
        self.assertIn("how long was the programme", p.text)
        p = iv.step("1,500 hours")
        self.assertIn("what did it teach you", p.text)
        p = iv.step("Hair color; Cutting; Chemical services")
        line = ("Cosmetology diploma, Paul Mitchell School, 2019 (1,500 hours). "
                "Skills: Hair color; Cutting; Chemical services")
        self.assertIn(f"recorded: {line}", p.notes)
        rec = store.load_record()
        self.assertEqual(rec.education, [line])
        self.assertEqual(rec.skill("Cutting").evidence, ["education: Cosmetology diploma"])

    def test_degrees_and_diplomas_skip_the_programme_questions(self):
        for first in ("BSN", "High school diploma", "Associate of Applied Science", "AAS"):
            store = MemoryStore(Record(header=["# R"], roles=[Role(employer="A", title="B")]))
            iv = interview.Interview(store, only="background:education")
            iv.step()
            for answer in (first, "", "", "", ""):
                p = iv.step(answer)
            self.assertTrue(p.text.startswith("Your next qualification"), (first, p.text))

    def test_a_new_job_is_added_to_an_existing_record_and_asked_first(self):
        store = MemoryStore(Record(header=["# R"], roles=[Role(
            employer="Mercy", title="RN", fields={"Dates": "2015 - 2024"},
            recorded_bullets=["Cut falls", "Precepted"])]))
        store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS]})
        iv = interview.Interview(store)
        p = iv.step()
        self.assertTrue(p.text.startswith("A new job"), p.text)
        self.assertIn("Mercy — RN (2015 - 2024)", " ".join(p.notes))
        iv.step("Clinic")
        iv.step("Nurse Manager")
        self.assertTrue(iv.step("2024 - Present").text.startswith("Any other title at Clinic"))
        self.assertTrue(iv.step("no").text.startswith("A new job"))
        iv.step("Mercy")
        iv.step("RN")
        p = iv.step("2015 - 2024")
        self.assertIn("already on your record", " ".join(p.notes))
        p = iv.step("no")
        self.assertIn("Clinic", " ".join(p.notes), "the new, empty job comes first")
        self.assertEqual([r.employer for r in store.load_record().roles], ["Mercy", "Clinic"])

    def test_contact_and_target_are_asked_once(self):
        store = MemoryStore()
        iv = interview.Interview(store)
        self.assertTrue(iv.step().text.startswith("Your name"))
        iv.step("Pat Lee")
        iv.step("pat@example.com")
        for _ in range(3):                                 # phone, location, link
            iv.step("")
        iv.step("Charge Nurse; Nurse Manager")
        iv.step("")
        p = iv.step("")
        self.assertTrue(p.text.startswith("Employer"), p.text)
        rec = store.load_record()
        self.assertEqual(rec.contact, {"Name": "Pat Lee", "Email": "pat@example.com"})
        self.assertEqual(rec.target["Titles"], "Charge Nurse; Nurse Manager")
        store.clear_state(interview.STATE)
        self.assertTrue(interview.Interview(store).step().text.startswith("Employer"),
                        "skipped questions are not asked again")

    def test_the_career_story_is_asked_after_the_jobs_and_exported(self):
        store = MemoryStore(Record(header=["# R"], contact={"Name": "Pat"},
                                   education=["BSN"], roles=[Role(employer="A", title="B")]))
        store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS]})
        use(InterviewEngine.turn("role_done"))
        asked = []
        answers = {"What are you best at": "Calming a chaotic unit",
                   "What do you want": "A manager role, because I already do the work"}
        interview.run(interview.Interview(store), say=lambda *a: None,
                      ask=lambda q: asked.append(q) or next(
                          (v for k, v in answers.items() if q.startswith(k)), ""))
        self.assertTrue(any(q.startswith("Looking across your jobs") for q in asked))
        rec = store.load_record()
        self.assertEqual(rec.sets_apart, ["Best at: Calming a chaotic unit"])
        self.assertEqual(interview.fact(rec, "sets_apart", "Best at"), "Calming a chaotic unit")
        profile = export.Export(rec, {}, {}).build()
        self.assertEqual(profile["differentiator"], "Best at: Calming a chaotic unit")
        self.assertEqual(profile["next_move"], "A manager role, because I already do the work")

    def test_results_against_targets_are_asked_and_exported(self):
        labels = [q[0] for q in interview.CONTEXT_QUESTIONS]
        self.assertIn("Results against targets", labels)
        self.assertNotIn("Responsibilities", labels, "duties are asked only for thin jobs")
        rec = Record(roles=[Role(employer="Acme", title="Rep", fields={
            "Responsibilities": "Managed 60 accounts",
            "Results against targets": "112% of quota in 2023"})])
        rec = mr.parse(mr.render(rec))
        entry = export.Export(rec, {}, {}).build()["work_history"][0]
        self.assertEqual(entry["responsibilities"], "Managed 60 accounts")
        self.assertEqual(entry["results_against_targets"], "112% of quota in 2023")

    def test_duties_are_asked_only_for_a_thin_job(self):
        def run_for(bullets):
            store = MemoryStore(Record(header=["# R"], contact={"Name": "x"}, roles=[Role(
                employer="Acme", title="Clerk", recorded_bullets=bullets,
                fields={k: "x" for k, _ in interview.CONTEXT_QUESTIONS})]))
            store.save_state(interview.ASKED_STATE, {"asked": [
                q[1] for q in interview.PROFILE_QUESTIONS + interview.STORY_QUESTIONS]})
            use(InterviewEngine.turn("role_done"))
            asked = []
            interview.run(interview.Interview(store), say=lambda *a: None,
                          ask=lambda q: asked.append(q) or (
                              "Filed 200 claims a week" if q.startswith("What did a normal week")
                              else "skip"))
            return asked, store.load_record().roles[0]
        asked, role = run_for(["Filed claims"])
        self.assertTrue(any(q.startswith("What did a normal week") for q in asked))
        self.assertEqual(role.fields["Responsibilities"], "Filed 200 claims a week")
        asked, role = run_for(["a", "b", "c"])
        self.assertFalse(any(q.startswith("What did a normal week") for q in asked))

    def test_employment_type_is_a_choice(self):
        self.assertEqual(interview.pick("3", mr.PAID_TYPES), "Contract")
        self.assertEqual(interview.pick("part", mr.PAID_TYPES), "Part-time")
        self.assertEqual(interview.pick("Locum", mr.PAID_TYPES), "Locum", "typed is kept")
        store = MemoryStore(Record(header=["# R"], contact={"Name": "x"},
                                   roles=[Role(employer="Acme", title="Analyst")]))
        store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS + interview.STORY_QUESTIONS]})
        iv = interview.Interview(store)
        iv.step()
        p = iv.step("")                                    # no new job
        self.assertEqual(p.text, "What kind of job was Analyst at Acme?")
        self.assertEqual(p.options, list(mr.PAID_TYPES))
        iv.step("3")
        self.assertEqual(store.load_record().roles[0].fields["Employment type"], "Contract")

    def test_work_outside_paid_jobs_gets_a_full_conversation_and_stays_off_the_job_list(self):
        store = MemoryStore(Record(header=["# R"], contact={"Name": "x"}, education=["BSN"],
                                   roles=[Role(employer="Mercy", title="RN",
                                               fields={"Dates": "2015 - 2018",
                                                       **{k: "x" for k, _ in interview.CONTEXT_QUESTIONS}},
                                               recorded_bullets=["a", "b", "c"])]))
        store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS + interview.STORY_QUESTIONS]})
        use(InterviewEngine.turn("role_done"),
            InterviewEngine.turn(say="How much did you raise?"),
            InterviewEngine.turn("complete", title="Ran the spring fundraiser",
                                 problem="Library short of funds", actions="Organised a book fair",
                                 results="Raised $4,200", evidence="metric"),
            InterviewEngine.turn("role_done"))
        iv = interview.Interview(store)
        iv.step()
        iv.step("")                                        # no new job
        for _ in range(3):                                 # the three resume bullets
            p = iv.step("skip")
        p = iv.step("")                                    # no more at Mercy; one more angle
        self.assertTrue(p.text.startswith("Work outside a paid job"), p.text)
        self.assertIn("what came of it", " ".join(p.notes))
        iv.step("Treasurer")
        p = iv.step("Lakeside PTA")
        self.assertEqual(p.options, list(mr.OUTSIDE_TYPES))
        iv.step("1")
        p = iv.step("2021 - 2023")
        self.assertEqual(p.text, "How much did you raise?", "straight into its accomplishments")
        p = iv.step("4,200 dollars")
        self.assertTrue(any("recorded" in n for n in p.notes))
        rec = store.load_record()
        pta = rec.roles[1]
        self.assertEqual((pta.employer, pta.title, pta.fields["Employment type"]),
                         ("Lakeside PTA", "Treasurer", "Volunteer"))
        self.assertTrue(pta.outside())
        self.assertNotIn("Company", pta.fields, "no job questions for volunteer work")
        pta.accomplishments[0].bullet = "Raised $4,200 running the spring book fair"
        profile = export.Export(rec, {}, {}).build()
        self.assertEqual([e["company"] for e in profile["work_history"]], ["Mercy"])
        self.assertEqual(profile["outside_work"][0]["organisation"], "Lakeside PTA")
        self.assertEqual(len(profile["outside_work"][0]["highlights"]), 1)
        rep = health.check(rec)
        self.assertFalse(any("gap" in i for i in rep.record_issues),
                         "volunteering is not a job, so it neither fills nor makes a gap")

    def test_confidential_figures_are_warned_about_and_never_pressed_for(self):
        store = MemoryStore(Record(header=["# R"], roles=[Role(employer="A", title="B")]))
        self.assertIn(interview.CONFIDENTIAL_NOTE, interview.Interview(store).step().notes)
        bg = interview.Interview(MemoryStore(), only=interview.BACKGROUND)
        self.assertNotIn(interview.CONFIDENTIAL_NOTE, bg.step().notes, "no figures asked there")
        self.assertIn("Do not ask for figures an employer or client would treat as "
                      "confidential", interview.COACH)
        self.assertIn("record it as given", interview.COACH, "what they give is theirs to share")

    def test_a_job_retyped_in_other_words_is_not_added_twice(self):
        rec = Record(roles=[Role(employer="Lakeshore Medical Center",
                                 title="Registered Nurse, Medical ICU")])
        self.assertIs(interview.same_job(rec, "Lakeshore Medical Center",
                                         "Registered Nurse, Medical ICU (Relief Charge Nurse "
                                         "since 2021)"), rec.roles[0])
        self.assertIs(interview.same_job(rec, "Lakeshore Medical", "RN, Medical ICU"),
                      rec.roles[0], "a shortened employer name and title")
        self.assertIsNone(interview.same_job(rec, "Lakeshore Medical Center", "Charge Nurse"),
                          "a promotion is a new job")
        self.assertIsNone(interview.same_job(rec, "Mercy", "Registered Nurse, Medical ICU"))

    def test_non_answers_are_not_saved_and_choices_are_read_from_sentences(self):
        self.assertTrue(interview.blank("I don't remember that detail."))
        self.assertTrue(interview.blank("Not sure"))
        self.assertFalse(interview.blank("I don't recall exactly, about 30%"))
        self.assertFalse(interview.blank("The VP of Nursing"))
        self.assertEqual(interview.pick("2. Part-time. I worked it while finishing my BSN.",
                                        mr.PAID_TYPES), "Part-time")
        self.assertEqual(interview.pick("I was on a contract", mr.PAID_TYPES), "Contract")
        self.assertEqual(interview.pick("full time", mr.PAID_TYPES), "Full-time")
        store = MemoryStore(Record(header=["# R"], contact={"Name": "x"},
                                   roles=[Role(employer="Acme", title="Analyst")]))
        store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS + interview.STORY_QUESTIONS]})
        iv = interview.Interview(store)
        iv.step()
        iv.step("")
        iv.step("I don't remember that detail.")           # employment type
        self.assertNotIn("Employment type", store.load_record().roles[0].fields)

    def test_what_they_could_not_recall_is_not_asked_again_in_that_job(self):
        fake = use(InterviewEngine.turn(say="Tell me about the DAISY award patient?",
                                        title="DAISY award"),
                   InterviewEngine.turn("role_done"))
        store = MemoryStore(Record(header=["# R"], contact={"Name": "x"}, roles=[Role(
            employer="Mercy", title="RN", fields={k: "x" for k, _ in interview.CONTEXT_QUESTIONS})]))
        store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS + interview.STORY_QUESTIONS]})
        iv = interview.Interview(store)
        iv.step()
        iv.step("")                                        # no new job
        iv.step("skip")                                    # can't recall it
        iv.step("We cut overtime")                         # the one more angle
        self.assertIn('"not_remembered": [\n  "DAISY award"\n ]',
                      fake.requests[1]["messages"][0]["content"])

    def test_promotions_are_asked_for_and_kept_as_titles_with_dates(self):
        store = MemoryStore()
        store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS]})
        iv = interview.Interview(store)
        p = iv.step()
        self.assertIn("Promoted, or changed title, at one employer?", " ".join(p.notes))
        iv.step("Mercy")
        iv.step("Charge Nurse")
        p = iv.step("2021 - Present")
        self.assertTrue(p.text.startswith("Any other title at Mercy, before or after Charge Nurse"))
        iv.step("Staff RN")
        self.assertEqual(iv.step("2018 - 2021").text[:24], "Any other title at Mercy")
        self.assertTrue(iv.step("").text.startswith("Employer"))
        rec = store.load_record()
        self.assertEqual([(r.title, r.fields["Dates"]) for r in rec.roles],
                         [("Charge Nurse", "2021 - Present"), ("Staff RN", "2018 - 2021")])
        self.assertEqual(rec.roles[0].fields["Other titles"], "Staff RN")
        rec.roles[0].accomplishments.append(acc("Ran nights", bullet="Ran nights"))
        rec.roles[1].accomplishments.append(acc("Precepted", bullet="Precepted"))
        work = export.Export(rec, {}, {}).build()["work_history"]
        self.assertEqual(work[0]["promoted_from"], "Staff RN, after 36 months")
        self.assertNotIn("promoted_from", work[1])

    def test_an_imported_job_is_asked_for_other_titles_and_reuses_the_employer_answers(self):
        use(InterviewEngine.turn("role_done"), InterviewEngine.turn("role_done"))
        store = MemoryStore(Record(header=["# R"], contact={"Name": "x"}, roles=[Role(
            employer="Acme", title="Senior Analyst", recorded_bullets=["a", "b", "c"],
            fields={"Dates": "2020 - Present", "Company": "A grocer, 900 staff",
                    "Employment type": "Full-time"})]))
        store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS + interview.STORY_QUESTIONS]})
        iv = interview.Interview(store)
        iv.step()
        p = iv.step("")                                    # no new job
        self.assertTrue(p.text.startswith("Did you hold any other title at Acme"), p.text)
        p = iv.step("Analyst, 2017 - 2020")
        self.assertIn("Added Acme — Analyst (2017 - 2020).", p.notes)
        rec = store.load_record()
        self.assertEqual(rec.roles[1].fields["Company"], "A grocer, 900 staff",
                         "the employer isn't described twice")
        self.assertEqual(interview.split_titles("Staff RN, 2018 - 2021; Intern (2017 - 2018)"),
                         [("Staff RN", "2018 - 2021"), ("Intern", "2017 - 2018")])
        self.assertIsNone(interview.same_job(rec, "Acme", "Analyst II", "2015 - 2017"),
                          "a title in other years is another title, not a duplicate")

    def _imported(self, title, **fields):
        store = MemoryStore(Record(header=["# R"], contact={"Name": "x"}, roles=[
            Role(employer="Fairhaven", title=title, fields={
                "Dates": "2015 - 2016", "Employment type": "Full-time",
                "Other titles": "None", **fields})]))
        store.save_state(interview.ASKED_STATE, {"asked": [
            q[1] for q in interview.PROFILE_QUESTIONS + interview.STORY_QUESTIONS]})
        return store

    def test_a_job_the_person_disowns_is_removed_after_asking(self):
        """Eval 02: a Student Teacher job on the old resume was denied at every
        question, and the denials were saved as its details."""
        store = self._imported("Student Teacher")
        iv = interview.Interview(store)
        iv.step()
        iv.step("")                                        # no new job
        p = iv.step("I don't have a Student Teacher role at Fairhaven.")
        self.assertIn("Remove it from your record?", p.text)
        self.assertEqual(p.options, list(interview.DISOWN_OPTIONS))
        iv.step("Yes, remove it")
        self.assertEqual(store.load_record().roles, [])

    def test_a_plain_negative_is_an_answer_not_a_denial(self):
        role = Role(employer="Fairhaven", title="Student Teacher")
        self.assertFalse(interview.disowns("I didn't have direct reports in that role", role))
        self.assertFalse(interview.disowns("No, I didn't have a budget", role))
        self.assertTrue(interview.disowns("I never worked at Fairhaven", role))

    def test_a_story_that_runs_out_of_details_is_still_kept(self):
        """Eval 05: the curbside pickup launch was told twice and never
        recorded, because "I don't remember the number" ended the talk."""
        use(InterviewEngine.turn(say="Tell me about one."),
            InterviewEngine.turn("role_done", title="Launched curbside pickup",
                                 actions="Set up the pickup lane and trained 8 associates",
                                 results=""),
            InterviewEngine.turn(say="Another one?"))
        store = self._imported("Store Manager",
                               **{k: "x" for k, _ in interview.CONTEXT_QUESTIONS})
        iv = interview.Interview(store)
        iv.step()
        iv.step("")                                        # no new job
        while iv.state["phase"] != "talk":
            iv.step("skip")
        iv.step("I don't remember how many orders")
        accs = [a.title for r in store.load_record().roles for a in r.accomplishments]
        self.assertIn("Launched curbside pickup", accs)

    def test_a_promotion_mentioned_only_in_recognition_is_flagged(self):
        rec = Record(contact={"Name": "x", "Email": "y"}, education=["e"], roles=[Role(
            employer="Mercy", title="Charge Nurse", fields={"Recognition": "Promoted in 2021"})])
        self.assertTrue(any("mentions a promotion" in i for i in health.check(rec).roles[0].issues))

    def test_promotions_are_listed_with_how_long_they_took(self):
        rec = Record(contact={"Name": "x", "Email": "y"}, education=["e"], roles=[
            Role(employer="Mercy", title="Charge Nurse", fields={"Dates": "Jul 2020 - Present"}),
            Role(employer="Mercy", title="Staff RN", fields={"Dates": "Jan 2019 - Jun 2020"}),
            Role(employer="Clinic", title="LPN", fields={"Dates": "2015 - 2018"})])
        rep = health.check(rec)
        self.assertEqual(rep.progression, ["Mercy: Staff RN to Charge Nurse in 18 months"])
        self.assertIn("Promotions worth showing:", health.render(rep))

    def test_no_degree_changes_the_advice(self):
        rec = Record(contact={"Name": "x", "Email": "y"}, education=["High school diploma"],
                     roles=[Role(employer="Acme", title="Operator")])
        self.assertTrue(any(s.startswith("No degree on your record") for s in health.check(rec).next_steps))
        rec.education = ["BA Economics"]
        self.assertFalse(any(s.startswith("No degree") for s in health.check(rec).next_steps))


class Promotions(unittest.TestCase):
    """Two titles at one employer are two roles, and stay two roles."""

    def record(self):
        return Record(contact={"Name": "Pat"}, roles=[
            Role(employer="Mercy Hospital", title="Nurse Manager", fields={"Dates": "2021 - Present"},
                 accomplishments=[acc("Cut agency spend", results="Agency hours down 40%",
                                      bullet="Cut agency nursing hours 40%")]),
            Role(employer="Mercy Hospital", title="Staff RN", fields={"Dates": "2016 - 2021"},
                 accomplishments=[acc("Precepted new grads", results="Trained 14 new graduates",
                                      bullet="Precepted 14 new graduate nurses")])])

    def profile(self):
        return {"work_history": [
            {"company": "Mercy Hospital", "title": "Nurse Manager", "dates": "2021 - Present",
             "highlights": ["Cut agency nursing hours 40%"]},
            {"company": "Mercy Hospital", "title": "Staff Nurse", "dates": "2016 - 2021",
             "highlights": ["Precepted 14 new graduate nurses"]}]}

    def test_reimporting_the_earlier_title_adds_no_duplicate(self):
        rec = self.record()
        importer.merge(rec, Record(roles=[Role(employer="Mercy Hospital", title="Staff RN")]))
        self.assertEqual([r.title for r in rec.roles], ["Nurse Manager", "Staff RN"])

    def test_a_new_title_is_still_a_new_role(self):
        rec = self.record()
        importer.merge(rec, Record(roles=[Role(employer="Mercy Hospital", title="Charge Nurse")]))
        self.assertEqual(len(rec.roles), 3)

    def test_export_keeps_each_title_with_its_own_highlights(self):
        ex = export.Export(self.record(), self.profile())
        self.assertEqual(ex.losses(), {})
        history = ex.build()["work_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["title"], "Nurse Manager")
        self.assertEqual(history[0]["highlights"], ["Cut agency nursing hours 40%"])
        self.assertEqual(history[1]["title"], "Staff Nurse")     # matched on dates; curated title kept
        self.assertEqual(history[1]["highlights"], ["Precepted 14 new graduate nurses"])

    def test_a_lost_highlight_names_the_title(self):
        profile = self.profile()
        profile["work_history"][1]["highlights"].append("Won the Daisy Award in 2019")
        lost = export.Export(self.record(), profile).losses()
        self.assertEqual(list(lost), ["Mercy Hospital — Staff Nurse"])


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

    def test_a_side_job_inside_a_long_role_is_not_a_gap(self):
        rec = Record(contact={"Name": "x", "Email": "y"}, education=["e"], roles=[
            Role(employer="Clinic", title="RN", fields={"Dates": "2024 - Present"}),
            Role(employer="Agency", title="Per diem RN", fields={"Dates": "2012 - 2013"}),
            Role(employer="Mercy", title="RN", fields={"Dates": "2010 - 2024"})])
        rep = health.check(rec)
        self.assertFalse(any("gap" in i for i in rep.record_issues), rep.record_issues)

    def test_a_gap_after_overlapping_roles_is_measured_from_the_last_end(self):
        rec = Record(contact={"Name": "x", "Email": "y"}, education=["e"], roles=[
            Role(employer="Now", title="t", fields={"Dates": "Jan 2023 - Present"}),
            Role(employer="Side", title="t", fields={"Dates": "2012 - 2013"}),
            Role(employer="Main", title="t", fields={"Dates": "2010 - Dec 2021"})])
        rep = health.check(rec)
        self.assertIn("13-month gap between Main — t and Now — t", rep.record_issues)

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


class ImportedSkills(unittest.TestCase):
    """Skills from an old resume are believed; the person is shown them once
    and only what they untick is dropped."""

    def interview(self):
        rec = Record(header=["# R"], contact={"Name": "x"}, skills=[
            Skill(name="Python", source="resume"),
            Skill(name="Kubernetes", source="resume"),
            Skill(name="Java", source="resume"),
            Skill(name="SQL", source="resume", evidence=["Built the reporting mart"]),
            Skill(name="Excel", source="you")])
        store = MemoryStore(rec)
        iv = interview.Interview(store)
        iv.state = iv._fresh()
        iv.state.update(phase="skills", queue=[], qi=0, outside_done=True,
                        background_done=True)
        return store, iv

    def test_only_unproven_resume_skills_are_shown_all_ticked(self):
        store, iv = self.interview()
        p = iv.step(None)
        self.assertEqual(p.kind, "multi")
        self.assertEqual(p.options, ["Python", "Kubernetes", "Java"],
                         "work already shows SQL; Excel the person added themselves")
        self.assertIn("Interviewers can ask", p.text)

    def test_unticked_skills_become_no_and_kept_ones_get_last_used(self):
        store, iv = self.interview()
        iv.step(None)
        p = iv.step("drop: Kubernetes; Java")
        self.assertIn("dropped: Kubernetes; Java", p.notes)
        self.assertEqual(p.text, "When did you last use Python?")
        p = iv.step("current")
        rec = store.load_record()
        self.assertEqual({s.name: s.have for s in rec.skills}["Kubernetes"], "no")
        self.assertEqual(rec.skill("Python").have, "yes")
        self.assertEqual(rec.skill("Python").last_used, "current")
        self.assertEqual(p.kind, "done")

    def test_enter_keeps_everything_and_done_skips_the_rest(self):
        store, iv = self.interview()
        iv.step(None)
        p = iv.step("")
        self.assertEqual(p.text, "When did you last use Python?")
        p = iv.step("done")
        self.assertEqual(p.kind, "done")
        self.assertTrue(all(s.have == "yes" for s in store.load_record().skills))

    def test_the_list_is_shown_once_across_sessions(self):
        store, iv = self.interview()
        iv.step(None)
        iv.step("")
        again = interview.Interview(store)
        self.assertEqual(again._unchecked_skills(), [])

    def test_drops_in_a_sentence_or_by_number(self):
        names = ["Java", "C++", "Kubernetes"]
        self.assertEqual(interview.dropped("1, 3", names), ["Java", "Kubernetes"])
        self.assertEqual(interview.dropped("I haven't used C++ in years", names), ["C++"])
        self.assertEqual(interview.dropped("Keep them all", names), [])
