"""
The lints on a drafted cover letter.

These are the guardrails the whole project argues for: the prompt states the
rule, and these decide whether it held. Each test below names a failure that
reached a finished document before the lint existed.
"""
import unittest

from helpers import letter, profile

# Read the limits from the module rather than restating them: they are
# configurable, and a test that assumes 42 words fails for whoever changed it.
from score_and_tailor import (MAX_BULLET_WORDS, MIN_GROUPS, find_ai_tells,
                              find_style_issues, find_ungrounded_claims)


class CleanLetterPasses(unittest.TestCase):
    """If this fails, every other test in this file is meaningless."""

    def test_the_fixture_is_clean(self):
        p = profile()
        L = letter()
        self.assertEqual(find_ai_tells(L), [])
        self.assertEqual(find_style_issues(L, p), [])
        self.assertEqual(find_ungrounded_claims(L, p), [])


class AiTells(unittest.TestCase):
    def test_em_dash(self):
        L = letter(opening="I lead analytics teams — and I measure what they ship.")
        self.assertIn("em/en dash", find_ai_tells(L))

    def test_not_just_x_but_y(self):
        L = letter(opening="This is not just a measurement problem but a culture one.")
        self.assertIn('"not just"', find_ai_tells(L))

    def test_inverted_contrast_flourish(self):
        """", not X" is the same rhetorical move, which is why it is banned too."""
        L = letter(opening="We need judgement, not dashboards.")
        self.assertIn('", not X" contrast flourish', find_ai_tells(L))

    def test_not_only_is_allowed(self):
        """", not only" is ordinary English and must not trip the flourish rule."""
        L = letter(opening="The work covers measurement, not only reporting.")
        self.assertEqual(find_ai_tells(L), [])

    def test_filler_intensifiers(self):
        L = letter(opening="This is really exciting and truly important.")
        found = find_ai_tells(L)
        self.assertIn('filler "really"', found)
        self.assertIn('filler "truly"', found)

    def test_case_insensitive(self):
        L = letter(opening="PASSIONATE ABOUT measurement.")
        self.assertIn('"passionate about"', find_ai_tells(L))


class StyleIssues(unittest.TestCase):
    def test_too_few_groups(self):
        L = letter(groups=letter()["groups"][:MIN_GROUPS - 1])
        self.assertTrue(any("achievement group" in i for i in find_style_issues(L, profile())))

    def test_missing_header(self):
        groups = letter()["groups"]
        groups[0] = {"header": "", "bullets": ["Cut acquisition cost 23% at Northwind Retail"]}
        self.assertIn("a group is missing its header",
                      find_style_issues(letter(groups=groups), profile()))

    def test_duplicate_header(self):
        groups = letter()["groups"]
        groups[1]["header"] = groups[0]["header"]
        issues = find_style_issues(letter(groups=groups), profile())
        self.assertTrue(any("duplicate group header" in i for i in issues))

    def test_group_with_no_bullets(self):
        groups = letter()["groups"]
        groups[2]["bullets"] = []
        issues = find_style_issues(letter(groups=groups), profile())
        self.assertTrue(any("has no bullets" in i for i in issues))

    def test_overlong_bullet(self):
        groups = letter()["groups"]
        groups[0]["bullets"][0] = ("Cut acquisition cost 23% at Northwind Retail "
                                   + "and then again " * MAX_BULLET_WORDS)
        issues = find_style_issues(letter(groups=groups), profile())
        self.assertTrue(any("bullet of" in i for i in issues))

    def test_a_bullet_at_the_limit_is_allowed(self):
        """The check is "more than", not "at", and an off-by-one here rejects
        letters that are fine."""
        groups = letter()["groups"]
        words = ["Cut", "acquisition", "cost", "23%", "at", "Northwind", "Retail"]
        words += ["more"] * (MAX_BULLET_WORDS - len(words))
        groups[0]["bullets"][0] = " ".join(words)
        issues = find_style_issues(letter(groups=groups), profile())
        self.assertFalse(any("bullet of" in i for i in issues), issues)

    def test_mostly_vague_bullets(self):
        """One bullet without a number is fine. A letter of them is the failure."""
        groups = [
            {"header": "A", "bullets": ["Drove transformation", "Partnered widely"]},
            {"header": "B", "bullets": ["Improved outcomes"]},
            {"header": "C", "bullets": ["Delivered value"]},
        ]
        issues = find_style_issues(letter(groups=groups), profile())
        self.assertTrue(any("carry no metric" in i for i in issues))

    def test_one_vague_bullet_among_specifics_is_allowed(self):
        groups = letter()["groups"]
        groups[1]["bullets"].append("Built the practice from the ground up")
        self.assertEqual(find_style_issues(letter(groups=groups), profile()), [])


class UngroundedClaims(unittest.TestCase):
    def test_invented_number(self):
        groups = letter()["groups"]
        groups[0]["bullets"][0] = "Cut acquisition cost 47% across paid search"
        issues = find_ungrounded_claims(letter(groups=groups), profile())
        self.assertTrue(issues, "a number absent from the profile should be flagged")

    def test_derived_number_is_still_invented(self):
        """23% and 4 channels are in the profile; 92 is arithmetic on them."""
        groups = letter()["groups"]
        groups[0]["bullets"][0] = "Drove 92% of the measurement roadmap at Northwind Retail"
        self.assertTrue(find_ungrounded_claims(letter(groups=groups), profile()))

    def test_vague_quantifier(self):
        groups = letter()["groups"]
        groups[0]["bullets"][0] = "Ran measurement across dozens of verticals"
        issues = find_ungrounded_claims(letter(groups=groups), profile())
        self.assertTrue(any("dozens" in i.lower() for i in issues))

    def test_number_present_in_the_profile_passes(self):
        groups = letter()["groups"]
        groups[0]["bullets"][0] = "Cut acquisition cost 23% across paid search"
        self.assertEqual(find_ungrounded_claims(letter(groups=groups), profile()), [])


if __name__ == "__main__":
    unittest.main()
