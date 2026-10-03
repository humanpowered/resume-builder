"""
Matching an employer's reply to the application it answers.

This is the riskiest automation in the project: a wrongly matched rejection
closes a live application. Every rule here exists because the naive version got
it wrong on real mail, so the tests are mostly about what must NOT match.
"""
import unittest
from datetime import datetime

import helpers  # noqa: F401  -- puts src/ on the path; must come first

from check_replies import classify, company_keys, match_row, name_variants


def message(subject, body="", sender="recruiter@acme.com", when="2026-09-20"):
    return {"subject": subject, "body": body, "sender": sender,
            "date": datetime.fromisoformat(when)}


def row(company, title="Director, Marketing Analytics", url="",
        submitted="2026-09-10", status=""):
    return {"company": company, "title": title, "url": url,
            "date_submitted": submitted, "status": status, "notes": ""}


class Classify(unittest.TestCase):
    def test_rejection(self):
        self.assertEqual(classify("Update", "we are not moving forward"), "rejected")

    def test_closed_role_counts_as_a_rejection(self):
        """"The role is no longer available" is a rejection, however politely
        phrased."""
        self.assertEqual(classify("Update", "the role is no longer available"),
                         "rejected")

    def test_interview(self):
        self.assertEqual(classify("Next steps", "could you share your availability"),
                         "interview")

    def test_acknowledgement(self):
        self.assertEqual(classify("Received", "thank you for applying"), "acknowledged")

    def test_rejection_wins_over_interview_wording(self):
        """A rejection that mentions the interview process must not read as an
        invitation."""
        self.assertEqual(
            classify("Update", "after your interview we are not moving forward"),
            "rejected")

    def test_ordinary_mail_is_not_a_reply(self):
        self.assertIsNone(classify("Newsletter", "here are this week's articles"))


class NameVariants(unittest.TestCase):
    def test_connector_words_are_dropped(self):
        """The tracker stores a hyphenated ATS token, "beck-and-rowe"; the
        email says "Beck & Rowe", where the ampersand normalises to a space and
        the "and" is simply not there. Compare the two compacted and they never
        meet, so a real rejection went unmatched."""
        self.assertIn("beckrowe", name_variants("beck-and-rowe"))
        self.assertIn("beckrowe", name_variants("Beck & Rowe"))

    def test_both_spellings_meet(self):
        self.assertTrue(name_variants("beck-and-rowe") & name_variants("Beck & Rowe"))


class CompanyKeys(unittest.TestCase):
    def test_the_employer_comes_out_of_an_ats_url(self):
        """The tracker's company column is often an ATS token, so the posting
        URL is the better source."""
        keys = company_keys(row("jobs", url="https://jobs.lever.co/northwind/123"))
        self.assertIn("northwind", keys)

    def test_job_boards_are_not_employers(self):
        """"linkedin" sits in the footer of half the mail in an inbox. Left in,
        it matched a franchise-sales email to an unrelated application."""
        keys = company_keys(row("Acme Co", url="https://www.linkedin.com/jobs/view/1"))
        self.assertNotIn("linkedin", keys)

    def test_generic_tokens_are_dropped(self):
        for junk in ("jobs", "careers", "talent"):
            with self.subTest(junk=junk):
                self.assertNotIn(junk, company_keys(row(junk)))


class MatchRow(unittest.TestCase):
    # Both callers act only at 5 or above. Anything less is reported for a
    # human to look at rather than applied to the tracker.
    ACT_THRESHOLD = 5

    def test_the_sender_domain_alone_is_not_enough_to_act_on(self):
        """Suggestive, not conclusive: mail from an employer's domain may be
        about a different thread entirely, and acting on it closes a live
        application. It scores, but below the threshold."""
        rows = [row("Acme Co")]
        best, score, _runner = match_row(
            message("Your application", body="thanks", sender="hr@acmeco.com"), rows)
        self.assertIs(best, rows[0])
        self.assertGreater(score, 0)
        self.assertLess(score, self.ACT_THRESHOLD)

    def test_employer_in_the_body_and_the_sender_clears_the_threshold(self):
        rows = [row("Acme Co")]
        best, score, _runner = match_row(
            message("Your application",
                    body="Thanks for applying to Acme Co.",
                    sender="hr@acmeco.com"), rows)
        self.assertIs(best, rows[0])
        self.assertGreaterEqual(score, self.ACT_THRESHOLD)

    def test_employer_never_named_does_not_match(self):
        """Scoring on title alone matched one company's rejection to another's
        application, because half these roles share a title."""
        rows = [row("Acme Co")]
        best, score, _runner = match_row(
            message("Director, Marketing Analytics - update",
                    body="we are not moving forward",
                    sender="people@someoneelse.com"), rows)
        self.assertIsNone(best)
        self.assertEqual(score, 0)

    def test_mail_predating_the_application_is_ignored(self):
        rows = [row("Acme Co", submitted="2026-09-25")]
        best, _score, _runner = match_row(
            message("Your application", sender="hr@acmeco.com", when="2026-09-01"),
            rows)
        self.assertIsNone(best)

    def test_rows_with_no_submit_date_are_ignored(self):
        rows = [row("Acme Co", submitted="")]
        best, _score, _runner = match_row(
            message("Your application", sender="hr@acmeco.com"), rows)
        self.assertIsNone(best)

    def test_two_roles_at_one_employer_report_a_tie(self):
        """Closing the wrong one is worse than closing neither, so the caller
        needs to see that the runner-up is just as good a match."""
        rows = [row("Acme Co", title="Director, Analytics"),
                row("Acme Co", title="Director, Insights")]
        best, score, runner_up = match_row(
            message("An update on your application",
                    body="we are not moving forward",
                    sender="hr@acmeco.com"), rows)
        self.assertIsNotNone(best)
        self.assertLess(score - runner_up, 2,
                        "an ambiguous pair must not look like a clear winner")

    def test_title_disambiguates_between_roles_at_one_employer(self):
        rows = [row("Acme Co", title="Warehouse Supervisor"),
                row("Acme Co", title="Director, Marketing Analytics")]
        best, score, runner_up = match_row(
            message("Your application for Director, Marketing Analytics",
                    body="Director, Marketing Analytics at Acme Co",
                    sender="hr@acmeco.com"), rows)
        self.assertEqual(best["title"], "Director, Marketing Analytics")
        self.assertGreaterEqual(score - runner_up, 2)


if __name__ == "__main__":
    unittest.main()
