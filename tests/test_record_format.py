"""
The master.md format, and the interview's own logic.

The format matters more than most things here: it is the one file that
accumulates for years, three programs read or write it, and anything it loses
is someone's career history. So the tests are mostly about what survives.

Nothing here calls the API. The interview's model turn is exercised through a
stub in test_interview_turn.py.
"""
import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  -- puts the package on the path; must come first

from resume_builder import record as mr
from resume_builder.record import Accomplishment, Record, Role

SAMPLE = """# Master accomplishment record

Imported from `intake.docx` on 2026-01-01.

## Contact

- **Name:** Jordan Avery
- **Email:** jordan@example.com

## Positioning

### Core competencies

- Measurement
- Leadership

## Roles

### Northwind Retail — Head of Analytics

- **Dates:** 2021 - Present
- **Authority:** 9 analysts
- **Reported to:** CMO

> Added from elsewhere; the intake never asked about this role.

#### Paid search efficiency

- **Problem:** Acquisition cost had risen 40%.
- **Actions:** Built geo-holdout tests.
- **Results:** Cut acquisition cost 23%.
- **Evidence:** metric

#### Recorded resume bullets

From a finished resume, so each one is already compressed.

- Built the measurement practice from scratch
- Hired and coached four analysts

### Baker Media — Director, Analytics

- **Dates:** 2016 - 2021

#### Launched mix modelling

- **Problem:** Clients wanted channel ROI.
- **Actions:** Built the practice.
- **Results:** Four clients on retainer.
- **Evidence:** scope

## Peer comments

- "Jordan rebuilt how we measure marketing."
"""


class Parsing(unittest.TestCase):
    def setUp(self):
        self.rec = mr.parse(SAMPLE)

    def test_roles_and_their_headings(self):
        self.assertEqual([r.employer for r in self.rec.roles],
                         ["Northwind Retail", "Baker Media"])
        self.assertEqual(self.rec.roles[0].title, "Head of Analytics")

    def test_role_fields(self):
        fields = self.rec.roles[0].fields
        self.assertEqual(fields["Dates"], "2021 - Present")
        self.assertEqual(fields["Reported to"], "CMO")

    def test_accomplishments_with_all_four_parts(self):
        acc = self.rec.roles[0].accomplishments[0]
        self.assertEqual(acc.title, "Paid search efficiency")
        self.assertEqual(acc.evidence, "metric")
        self.assertTrue(acc.problem and acc.actions and acc.results)

    def test_recorded_bullets_are_separate_from_accomplishments(self):
        """They are finished resume lines, not problem/actions/results records,
        and conflating the two would overstate what has been captured."""
        role = self.rec.roles[0]
        self.assertEqual(len(role.accomplishments), 1)
        self.assertEqual(len(role.recorded_bullets), 2)
        self.assertIn("Built the measurement practice from scratch",
                      role.recorded_bullets)

    def test_the_bullets_preamble_is_not_mistaken_for_a_bullet(self):
        self.assertFalse(any("already compressed" in b
                             for b in self.rec.roles[0].recorded_bullets))

    def test_role_notes_survive(self):
        self.assertTrue(any("intake never asked" in n
                            for n in self.rec.roles[0].notes))

    def test_contact_and_positioning(self):
        self.assertEqual(self.rec.contact["Name"], "Jordan Avery")
        self.assertEqual(self.rec.competencies, ["Measurement", "Leadership"])

    def test_the_header_is_kept_verbatim(self):
        self.assertTrue(any("Imported from" in l for l in self.rec.header))

    def test_unknown_sections_are_kept_rather_than_dropped(self):
        """A section this module knows nothing about is still somebody's
        content."""
        self.assertTrue(any("Peer comments" in l for l in self.rec.trailing))
        self.assertTrue(any("rebuilt how we measure" in l
                            for l in self.rec.trailing))

    def test_a_fill_in_marker_counts_as_unanswered(self):
        """Treating "[FILL IN]" as content would make the interview skip exactly
        the gaps it exists to close."""
        rec = mr.parse("## Roles\n\n### Acme — Analyst\n\n"
                       "#### Did a thing\n\n"
                       "- **Problem:** [FILL IN]\n"
                       "- **Actions:** Built it.\n"
                       "- **Results:** [FILL IN -- not recorded]\n")
        acc = rec.roles[0].accomplishments[0]
        self.assertEqual(acc.problem, "")
        self.assertEqual(acc.results, "")
        self.assertEqual(acc.actions, "Built it.")


class Rendering(unittest.TestCase):
    def test_render_is_idempotent(self):
        once = mr.render(mr.parse(SAMPLE))
        twice = mr.render(mr.parse(once))
        self.assertEqual(once, twice)

    def test_nothing_is_lost_in_a_round_trip(self):
        out = mr.render(mr.parse(SAMPLE))
        for must_survive in ("Jordan Avery", "Northwind Retail", "Baker Media",
                             "Paid search efficiency", "Launched mix modelling",
                             "Built the measurement practice from scratch",
                             "Hired and coached four analysts",
                             "Peer comments", "intake never asked",
                             "Reported to", "9 analysts"):
            with self.subTest(must_survive=must_survive):
                self.assertIn(must_survive, out)

    def test_an_unanswered_field_renders_as_a_marker(self):
        rec = Record(roles=[Role(employer="Acme", title="Analyst",
                                 accomplishments=[Accomplishment(title="A thing")])])
        out = mr.render(rec)
        self.assertIn("- **Problem:** [FILL IN]", out)

    def test_evidence_is_omitted_when_unset_rather_than_marked(self):
        """An unset evidence tier is not a gap for the user to fill in by hand;
        the interview sets it."""
        rec = Record(roles=[Role(employer="Acme", title="Analyst",
                                 accomplishments=[Accomplishment(
                                     title="A", problem="p", actions="a",
                                     results="r")])])
        self.assertNotIn("Evidence", mr.render(rec))

    def test_an_empty_record_still_renders(self):
        self.assertIn("## Roles", mr.render(Record()))


class Coverage(unittest.TestCase):
    def role(self, accs=(), bullets=()):
        return Role(employer="Acme", title="Analyst",
                    accomplishments=list(accs), recorded_bullets=list(bullets))

    def test_counts_both_kinds(self):
        r = self.role([Accomplishment(title="A", problem="p", actions="a",
                                      results="Cut cost 23%", evidence="metric")],
                      ["a finished bullet"])
        c = r.coverage()
        self.assertEqual((c["accomplishments"], c["bullets"], c["total"]), (1, 1, 2))

    def test_quantified_counts_metric_and_derived(self):
        for tier in ("metric", "derived"):
            with self.subTest(tier=tier):
                r = self.role([Accomplishment(title="A", problem="p", actions="a",
                                              results="no digits here",
                                              evidence=tier)])
                self.assertEqual(r.coverage()["quantified"], 1)

    def test_a_number_in_the_result_counts_even_without_a_tier(self):
        r = self.role([Accomplishment(title="A", problem="p", actions="a",
                                      results="Saved 3 days a month")])
        self.assertEqual(r.coverage()["quantified"], 1)

    def test_qualitative_is_not_counted_as_quantified(self):
        r = self.role([Accomplishment(title="A", problem="p", actions="a",
                                      results="Ended the disputes",
                                      evidence="qualitative")])
        self.assertEqual(r.coverage()["quantified"], 0)

    def test_empty_accomplishments_do_not_inflate_the_count(self):
        r = self.role([Accomplishment(), Accomplishment(title="A", problem="p",
                                                        actions="a", results="r")])
        self.assertEqual(r.coverage()["accomplishments"], 1)


class CoverageNote(unittest.TestCase):
    def test_the_encouragement_is_off_by_default(self):
        """Six roles each carrying the same nudge reads as nagging, which is the
        failure this approach exists to avoid."""
        role = Role(employer="Acme", title="Analyst",
                    accomplishments=[Accomplishment(title="A", problem="p",
                                                    actions="a", results="r")])
        self.assertNotIn("most people", mr.coverage_note(role))
        self.assertIn("most people", mr.coverage_note(role, prompt=True))

    def test_no_nudge_once_the_target_is_met(self):
        accs = [Accomplishment(title=f"A{i}", problem="p", actions="a",
                               results="r") for i in range(10)]
        role = Role(employer="Acme", title="Analyst", accomplishments=accs)
        self.assertNotIn("most people", mr.coverage_note(role, prompt=True))

    def test_an_empty_role_says_so(self):
        self.assertIn("nothing recorded", mr.coverage_note(Role(employer="Acme")))


class EmployerMatching(unittest.TestCase):
    def test_punctuation_and_case_are_ignored(self):
        """Two sources spell the same employer differently -- "Beck Rowe" against
        "Beck & Rowe." -- and an exact match reports a recorded role as missing."""
        rec = Record(roles=[Role(employer="Beck & Rowe, Ltd.",
                                 title="Manager")])
        self.assertIsNotNone(rec.role_by_employer("Beck & Rowe Ltd"))
        self.assertIsNotNone(rec.role_by_employer("beck & rowe, ltd."))
        self.assertIsNone(rec.role_by_employer("Someone Else"))


class InterviewLogic(unittest.TestCase):
    """The parts of the interview that make no API call."""

    def setUp(self):
        from resume_builder import interview
        self.iv = interview

    def test_thinnest_role_comes_first(self):
        """That is where an hour of someone's time is worth most."""
        full = Role(employer="Full", title="x", accomplishments=[
            Accomplishment(title=f"A{i}", problem="p", actions="a", results="r")
            for i in range(5)])
        thin = Role(employer="Thin", title="x")
        rec = Record(roles=[full, thin])
        self.assertEqual([r.employer for r in self.iv.by_need(rec)],
                         ["Thin", "Full"])

    def test_near_duplicate_catches_a_rephrasing(self):
        role = Role(employer="Acme", title="x", accomplishments=[
            Accomplishment(title="Automated the board reporting", problem="p",
                           actions="a", results="r")])
        self.assertIsNotNone(
            self.iv.near_duplicate(role, "Board reporting automated"))
        self.assertIsNone(
            self.iv.near_duplicate(role, "Hired and trained the night shift"))

    def test_near_duplicate_also_checks_recorded_bullets(self):
        role = Role(employer="Acme", title="x",
                    recorded_bullets=["Built the measurement practice from scratch"])
        self.assertIsNotNone(
            self.iv.near_duplicate(role, "Built measurement practice scratch"))

    def test_near_duplicate_on_an_empty_title(self):
        self.assertIsNone(self.iv.near_duplicate(Role(employer="Acme"), ""))

    def test_the_lens_offered_is_one_the_record_does_not_cover(self):
        """A second question that rephrases the first is nagging. One that comes
        from a direction they have not been thinking about is worth asking."""
        role = Role(employer="Acme", title="x", accomplishments=[
            Accomplishment(title="Automated the monthly reporting",
                           problem="it took days of manual work",
                           actions="automated the manual process",
                           results="hours saved, much faster")])
        name, question = self.iv.pick_lens(role)
        self.assertNotEqual(name, "speed",
                            "speed is what they already described at length")
        self.assertTrue(question.endswith("?"))

    def test_the_lens_shifts_with_what_is_already_recorded(self):
        people = Role(employer="A", title="x", accomplishments=[
            Accomplishment(title="Hired the team", problem="no staff",
                           actions="hired and trained six", results="team of six")])
        money = Role(employer="B", title="x", accomplishments=[
            Accomplishment(title="Cut spend", problem="budget overrun",
                           actions="renegotiated cost", results="saved revenue")])
        self.assertNotEqual(self.iv.pick_lens(people)[0], "people")
        self.assertNotEqual(self.iv.pick_lens(money)[0], "money")

    def test_every_lens_has_keywords_and_a_question(self):
        names = set()
        for name, keys, question in self.iv.LENSES:
            with self.subTest(lens=name):
                self.assertTrue(keys and all(k.islower() for k in keys))
                self.assertTrue(question.strip().endswith("?"))
                names.add(name)
        self.assertEqual(len(names), len(self.iv.LENSES), "lens names must be unique")

    def test_an_empty_role_still_gets_a_lens(self):
        name, question = self.iv.pick_lens(Role(employer="Acme", title="x"))
        self.assertIn(name, {n for n, _k, _q in self.iv.LENSES})
        self.assertTrue(question)

    def test_give_up_words_cover_the_obvious_ways_of_saying_no(self):
        for word in ("done", "no", "nothing", "that's all", "skip"):
            with self.subTest(word=word):
                self.assertIn(word, self.iv.GIVE_UP)

    def test_the_response_schema_is_valid_json(self):
        """It is sent to the API as a JSON Schema; a Python-only value in it
        would be rejected at request time rather than here."""
        json.dumps(self.iv.INTERVIEW_SCHEMA)
        props = self.iv.INTERVIEW_SCHEMA["properties"]
        self.assertEqual(set(props), {"say", "draft", "status"})
        self.assertEqual(set(props["status"]["enum"]),
                         {"asking", "complete", "role_done"})
        for tier in mr.EVIDENCE_TIERS:
            self.assertIn(tier, props["draft"]["properties"]["evidence"]["enum"])

    def test_every_evidence_tier_has_a_plain_english_gloss(self):
        """The tier is shown to the user when an accomplishment is recorded."""
        self.assertEqual(set(mr.EVIDENCE_TIERS), set(mr.EVIDENCE_HELP))


if __name__ == "__main__":
    unittest.main()
