"""
The filters that decide what reaches the scorer at all.

This is the cost control: everything that survives here is paid for. Both
directions matter, so each group tests what must pass as well as what must not.
"""
import re
import unittest

import helpers  # noqa: F401  -- puts src/ on the path; must come first

import scraper
from scraper import _title_excluded, _title_matches, is_application_subject


class TitleKeywords(unittest.TestCase):
    KEYWORDS = ["marketing analytics", "marketing measurement", "data scien",
                "marketing scien"]

    def match(self, title):
        return _title_matches(title, self.KEYWORDS)

    def test_stem_catches_the_longer_title(self):
        self.assertTrue(self.match("Director, Marketing Analytics"))
        self.assertTrue(self.match("Senior Manager, Marketing Analytics"))

    def test_data_scien_catches_both_words(self):
        """"data science" does not match "Data Scientist", which is why the
        stem is "data scien". Every Staff Data Scientist role was invisible
        until someone noticed."""
        self.assertTrue(self.match("Staff Data Scientist, Ads"))
        self.assertTrue(self.match("Director, Data Science"))

    def test_unrelated_titles_do_not_match(self):
        for title in ("Senior Account Director", "Warehouse Associate",
                      "Creative Director", "Registered Nurse"):
            with self.subTest(title=title):
                self.assertFalse(self.match(title))

    def test_case_and_punctuation(self):
        self.assertTrue(self.match("DIRECTOR - MARKETING ANALYTICS"))


class TitleExclusions(unittest.TestCase):
    EXCLUDES = ["intern", "entry level", "junior ", "account director"]

    def test_junior_roles_are_dropped(self):
        self.assertTrue(_title_excluded("Marketing Analytics Intern", self.EXCLUDES))
        self.assertTrue(_title_excluded("Junior Data Scientist", self.EXCLUDES))

    def test_associate_director_survives(self):
        """Exclusions are phrases on purpose. A bare "associate" would delete
        Associate Director, which is a real target level."""
        self.assertFalse(_title_excluded("Associate Director, Marketing Analytics",
                                         self.EXCLUDES))

    def test_sales_director_is_dropped_but_analytics_director_is_not(self):
        self.assertTrue(_title_excluded("Account Director", self.EXCLUDES))
        self.assertFalse(_title_excluded("Director, Marketing Analytics",
                                         self.EXCLUDES))


class ApplicationMailIsNotAJob(unittest.TestCase):
    """Replies about applications already sent were being scored as openings.
    One interview invitation reached 8/10 and had documents drafted for it."""

    REPLIES = [
        "Your application for Director, Marketing Measurement & Testing",
        "Thank you for your application to Senior Director, Analytics and Insights",
        "Head of Data and Analytics (Remote) - Confirmation of your application",
        "Thank you for applying to Acme!",
        "Thank You for Applying at Beck & Rowe",
        "Thank you for Your Interest in Northwind!",
        "Follow up regarding your Data Scientist application to Acme Corp",
        "Security code for your application to Acme",
        "Re: Acme: Scheduling the interview - October 5th",
        "Track Your Application: Redwood School Lead Head of Growth",
        "Interview confirmation: Director, Analytics",
        # A reply about a conversation that already happened. One of these
        # scored 8/10 and had a resume drafted for it, because it mentions
        # neither an application nor an interview:
        # "RE: [EXTERNAL] Thank you for the call today - <role> - <name>"
        "Thank you for the call today",
        "Thanks for your time yesterday",
        "Thank you for the conversation - next steps",
        "Thank you for our meeting",
    ]

    # Mail that mentions interviews or applications and is still not a reply.
    NOT_REPLIES = [
        "Job Alerts - How to land a job interview!",
        "You have six seconds to get a job interview!",
        "Introducing - AI Interview Buddy",
        "New Radio and Podcast Interview Guest Requests",
        "Stop spending nights on applications",
        "Acme is hiring a Director of Marketing Analytics",
        "Director, Marketing Analytics at Acme: up to $189K/year",
        "94+ Lead/Senior Data Scientist jobs (Remote)",
    ]

    def test_replies_are_recognised(self):
        for subject in self.REPLIES:
            with self.subTest(subject=subject):
                self.assertTrue(is_application_subject(subject))

    def test_postings_and_newsletters_are_left_alone(self):
        for subject in self.NOT_REPLIES:
            with self.subTest(subject=subject):
                self.assertFalse(is_application_subject(subject))

    def test_empty_subject(self):
        self.assertFalse(is_application_subject(""))
        self.assertFalse(is_application_subject(None))


class CandidateNameInAnInterviewSubject(unittest.TestCase):
    """An interview subject carries your own full name. Nothing else in an
    inbox does, which is what makes it usable."""

    def setUp(self):
        self.saved = scraper._CANDIDATE_INTERVIEW
        # Build it with the production function rather than restating the
        # pattern here. The first version of this test kept its own copy, so it
        # went on passing when the real rule grew a branch it did not have.
        scraper._CANDIDATE_INTERVIEW = scraper._candidate_name_pattern("Jordan Avery")

    def tearDown(self):
        scraper._CANDIDATE_INTERVIEW = self.saved

    def test_name_beside_interview_is_dropped(self):
        self.assertTrue(is_application_subject(
            "Acme Interview | Jordan Avery | Director Marketing Analytics"))

    def test_name_without_interview_is_kept(self):
        """A recruiter pitching a new role uses your name too."""
        self.assertFalse(is_application_subject(
            "Jordan Avery - Director Marketing Analytics opportunity"))

    def test_name_after_a_reply_prefix_is_dropped(self):
        """The shape that leaked: a thread you are already in, carrying your
        full name, about a call rather than an application."""
        for subject in (
                "RE: [EXTERNAL] Thank you for the call today - Director, "
                "Marketing Analytics - Jordan Avery",
                "Re: Director, Analytics - Jordan Avery",
                "FWD: Jordan Avery resume",
                "fw: next steps for Jordan Avery"):
            with self.subTest(subject=subject):
                self.assertTrue(is_application_subject(subject))

    def test_a_reply_prefix_without_your_name_is_kept(self):
        """Recruiters reply into threads about genuinely new roles."""
        self.assertFalse(is_application_subject(
            "Re: Director, Marketing Analytics opening at Acme"))

    def test_interview_without_the_name_is_kept(self):
        self.assertFalse(is_application_subject("How to land a job interview"))

    def test_no_profile_name_disables_only_this_rule(self):
        scraper._CANDIDATE_INTERVIEW = None
        self.assertFalse(is_application_subject(
            "Acme Interview | Jordan Avery | Director Marketing Analytics"))
        self.assertTrue(is_application_subject("Thank you for applying to Acme!"))


if __name__ == "__main__":
    unittest.main()
