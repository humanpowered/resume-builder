"""
The deterministic reshaping applied to a drafted resume.

Nothing here asks the model to behave. The model's output is taken as given and
corrected, which is why these are the cheapest guardrails in the project to
test and the ones most likely to break quietly.
"""
import unittest

from helpers import profile

from score_and_tailor import normalize_skills, order_experience


class OrderExperience(unittest.TestCase):
    def test_reverse_chronological_is_restored(self):
        """The model promotes whichever role matches the posting. It put a
        2010-2015 job above a 2020-2022 one on every resume in a run."""
        resume = {"experience": [{"company": "Baker Media", "title": "Director"},
                                 {"company": "Northwind Retail", "title": "Head"}]}
        got = order_experience(resume, profile())["experience"]
        self.assertEqual([e["company"] for e in got],
                         ["Northwind Retail", "Baker Media"])

    def test_descriptor_is_stripped_from_the_company_name(self):
        """The descriptor is context for writing bullets, not resume content.
        It reached the page as "Northwind Retail (a home goods retailer...)"."""
        resume = {"experience": [
            {"company": "Northwind Retail (a home goods retailer serving 12 brands)",
             "title": "Head of Analytics"}]}
        got = order_experience(resume, profile())["experience"]
        self.assertEqual(got[0]["company"], "Northwind Retail")

    def test_unrecognized_employer_sinks_rather_than_disappears(self):
        resume = {"experience": [{"company": "Someone Else Ltd", "title": "Analyst"},
                                 {"company": "Northwind Retail", "title": "Head"}]}
        got = order_experience(resume, profile())["experience"]
        self.assertEqual([e["company"] for e in got],
                         ["Northwind Retail", "Someone Else Ltd"])
        self.assertEqual(len(got), 2, "nothing should be dropped")

    def test_empty_experience_is_left_alone(self):
        self.assertEqual(order_experience({"experience": []}, profile()),
                         {"experience": []})


class NormalizeSkills(unittest.TestCase):
    def test_parentheses_are_promoted_to_siblings(self):
        """Some ATS parsers drop parenthesised text, taking the contents with
        it."""
        got = normalize_skills({"skills": ["Marketing Mix Modeling (MMM)"]})["skills"]
        self.assertNotIn("(", " ".join(got))
        self.assertIn("MMM", " ".join(got))

    def test_a_flat_list_is_not_turned_into_categories(self):
        """A few stray colons in a flat list are not structure. Inferring
        headings from them produced three bogus categories."""
        flat = ["Unit economics: CAC, LTV", "SQL", "Experiment design"]
        got = normalize_skills({"skills": list(flat)})["skills"]
        self.assertTrue(any("SQL" in s for s in got))

    def test_no_skills_is_not_an_error(self):
        self.assertEqual(normalize_skills({"skills": []}), {"skills": []})
        self.assertEqual(normalize_skills({}), {})


if __name__ == "__main__":
    unittest.main()
