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
        p = iv.step()
        self.assertEqual(p.kind, "choice")                 # open the bullet?
        self.assertEqual(iv.step("").text, "How many?")
        p = iv.step("12 over two years")
        self.assertTrue(any("recorded" in n for n in p.notes))
        self.assertTrue(p.text.startswith("One more angle"))
        self.assertTrue(iv.step("").text.startswith("Your next qualification"))
        self.assertTrue(iv.step("").text.startswith("Your next licence"))
        self.assertEqual(finish(iv), len(interview.SECTIONS) - 1, "one Enter per section")
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
        iv.step("")
        acc_ = self.store.load_record().roles[0].accomplishments[0]
        self.assertEqual(acc_.contribution, "I designed the rounding checklist and trained 30 staff")
        self.assertFalse(acc_.team_unclear())
        self.assertIn("what their own part was", fake.requests[0]["messages"][0]["content"])

    def test_the_expanded_bullet_is_not_its_own_duplicate(self):
        use(self.turn("complete", title="Precepted new graduate nurses",
                      problem="p", actions="a", results="r", evidence="qualitative"),
            self.turn("role_done"))
        iv = interview.Interview(self.store)
        iv.step()
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
        context = [q for q in asked if not q.startswith(("One more angle", *firsts))]
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
                line = interview.FORMAT[key]({k: f"x{k}" for k, _, _ in questions})
                for k, _, _ in questions:
                    self.assertIn(f"x{k}", line)

    def test_listing_jobs_from_nothing(self):
        store = MemoryStore()
        iv = interview.Interview(store)
        self.assertTrue(iv.step().text.startswith("Employer"))
        iv.step("Lakeside High School")
        iv.step("Science Teacher")
        self.assertTrue(iv.step("2014 - Present").text.startswith("Employer"))
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
