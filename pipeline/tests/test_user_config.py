"""
One person's settings in a pipeline that serves many.

What matters: a person's settings change what the pipeline does for them and
for no one else, bad settings are refused with a reason, and with no person
set the command line behaves as it always has.
"""
import json
import threading
import types
import unittest

import helpers  # noqa: F401  -- puts src/ on the path; must come first
from helpers import letter, profile

import scraper
import score_and_tailor as st
import user_config
from settings import ConfigError
from user_config import UserConfig, using


def response(obj):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=json.dumps(obj))],
        stop_reason="end_turn", usage=types.SimpleNamespace(output_tokens=10))


class Recorder:
    """Answers every call with the same JSON and keeps the prompts it saw."""

    def __init__(self, obj):
        self.obj, self.prompts = obj, []
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.prompts.append(" ".join(b["text"] for m in kw["messages"]
                                     if isinstance(m["content"], list)
                                     for b in m["content"]))
        return response(self.obj)


class WithClient(unittest.TestCase):
    def use(self, client):
        saved = st.client
        st.client = client
        self.addCleanup(setattr, st, "client", saved)
        return client


POSTING = {"title": "Charge Nurse", "company": "Mercy", "location": "Remote",
           "description_html": "<p>Lead a 30-bed unit. Pay $70,000 - $95,000 per year.</p>"}


class Validation(unittest.TestCase):
    def test_empty_settings_are_the_defaults(self):
        cfg = UserConfig.from_dict({})
        self.assertIsNone(cfg.tuning["salary_floor"])
        self.assertEqual(cfg.letter["greeting"], "Dear Hiring Committee:")

    def test_each_kind_of_mistake_names_its_section(self):
        bad = [({"tuning": {"salary_floor": "lots"}}, "tuning"),
               ({"tuning": {"max_bullet_words": 0}}, "tuning"),
               ({"letter": {"frame": {"motto": "x"}}}, "letter"),
               ({"boards": {"greenhouse": ["../../etc"]}}, "board token"),
               ({"boards": {"apify_linkedin": {"enabled": True}}}, "cannot be set per person"),
               ({"boards": {"workday": [{"tenant": "a", "host": "evil.com", "site": "x"}]}},
                "Workday host"),
               ({"titles": ["nurse"]}, "titles"),
               ({"colour": "blue"}, "unknown section")]
        for data, expect in bad:
            with self.subTest(data=data), self.assertRaises(ConfigError) as cm:
                UserConfig.from_dict(data)
            self.assertIn(expect, str(cm.exception))

    def test_the_model_is_the_services_choice(self):
        """The service pays for the model, so a person cannot pick a dearer one."""
        with self.assertRaises(ConfigError):
            UserConfig.from_dict({"tuning": {"model": "claude-opus-5"}})

    def test_too_many_companies_is_refused(self):
        with self.assertRaises(ConfigError):
            UserConfig.from_dict({"boards": {"greenhouse": [f"co{i}" for i in range(501)]}})

    def test_stored_form_is_what_the_person_set(self):
        data = {"tuning": {"salary_floor": 90000}}
        self.assertEqual(UserConfig.from_dict(data).to_dict(), data)


class ScopedToThePerson(unittest.TestCase):
    def test_bullet_caps_follow_the_person(self):
        resume = {"experience": [{"bullets": [f"b{i}" for i in range(9)]},
                                 {"bullets": [f"c{i}" for i in range(9)]}]}
        with using(UserConfig.from_dict({"tuning": {"bullets_by_position": [2],
                                                    "bullets_tail": 1}})):
            out = st.fit_to_two_pages(json.loads(json.dumps(resume)))
        self.assertEqual([len(j["bullets"]) for j in out["experience"]], [2, 1])

    def test_outside_using_the_files_apply(self):
        self.assertIsNone(user_config.current())
        caps = st._TUNING["bullets_by_position"]
        out = st.fit_to_two_pages({"experience": [{"bullets": [f"b{i}" for i in range(9)]}]})
        self.assertEqual(len(out["experience"][0]["bullets"]), min(9, caps[0]))

    def test_two_requests_at_once_each_see_their_own_person(self):
        seen = {}

        def run(name, groups):
            with using(UserConfig.from_dict({"tuning": {"min_groups": groups}})):
                barrier.wait()
                seen[name] = st.find_style_issues(letter(groups=[]), profile())

        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=run, args=("a", 2)),
                   threading.Thread(target=run, args=("b", 5))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertIn("want 2", seen["a"][0])
        self.assertIn("want 5", seen["b"][0])


class Scoring(WithClient):
    def test_no_floor_judges_seniority_both_ways(self):
        c = self.use(Recorder({"score": 7, "reasoning": "ok", "overqualification_risk": True}))
        with using(UserConfig.from_dict({})):
            out = st.score_posting(POSTING, profile())
        prompt = c.prompts[0]
        self.assertIn("both", prompt)
        self.assertIn("most recently as Head of Analytics", prompt)
        self.assertNotIn("analytics functions", prompt)
        self.assertTrue(out["overqualification_risk"])

    def test_a_persons_floor_overrides_the_risk(self):
        self.use(Recorder({"score": 7, "reasoning": "ok", "overqualification_risk": True}))
        with using(UserConfig.from_dict({"tuning": {"salary_floor": 90000}})):
            out = st.score_posting(POSTING, profile())
        self.assertFalse(out["overqualification_risk"])

    def test_a_new_graduate_is_not_called_senior(self):
        c = self.use(Recorder({"score": 3, "reasoning": "ok", "overqualification_risk": False}))
        grad = profile(years_experience=None, work_history=[])
        with using(UserConfig.from_dict({})):
            st.score_posting(POSTING, grad)
        self.assertIn("the experience shown in the profile", c.prompts[0])
        self.assertNotIn("OVERqualified", c.prompts[0])


class Letter(WithClient):
    def test_the_persons_frame_is_used_verbatim(self):
        drafted = letter(positioning="model paraphrase", greeting=None)
        drafted.pop("greeting")
        c = self.use(Recorder(drafted))
        frame = {"positioning": "I run intensive care units.", "greeting": "Dear Ms. Lee:"}
        with using(UserConfig.from_dict({"letter": {"frame": frame}})):
            out = st.draft_cover_letter(POSTING, profile())
        self.assertEqual(out["positioning"], "I run intensive care units.")
        self.assertEqual(out["greeting"], "Dear Ms. Lee:")
        self.assertIn("I run intensive care units.", c.prompts[0])

    def test_the_persons_banned_phrases_apply(self):
        L = letter(opening="Please consider my qualifications. I thrive on teamwork.")
        self.assertEqual(st.find_ai_tells(L), [])
        with using(UserConfig.from_dict({"letter": {"avoid": ["thrive on"]}})):
            self.assertEqual(st.find_ai_tells(L), ['"thrive on"'])

    def test_named_methods_come_from_the_persons_skills(self):
        nurse = profile(work_history=[],
                        skills_by_category={"Clinical": ["Telemetry (Expert)"]})
        self.assertTrue(st.specific_pattern(nurse).search("Ran telemetry for the unit"))
        self.assertFalse(st.specific_pattern(nurse).search("Ran geo-holdout tests"))
        with using(UserConfig.from_dict({"methods": ["wound vac"]})):
            self.assertTrue(st.specific_pattern(nurse).search("Introduced wound vac care"))


class Search(unittest.TestCase):
    def setUp(self):
        saved = scraper.fetch_greenhouse
        self.addCleanup(setattr, scraper, "fetch_greenhouse", saved)
        self.asked = []

        def fake(token):
            self.asked.append(token)
            return [{"source": "greenhouse", "company": token, "title": t, "location": "Remote",
                     "url": f"https://x/{token}/{i}", "posting_id": str(i),
                     "description_html": ""}
                    for i, t in enumerate(["Charge Nurse", "Nurse Intern", "Accountant"])]
        scraper.fetch_greenhouse = fake

    def test_only_the_persons_companies_and_titles(self):
        cfg = UserConfig.from_dict({"titles": {"include": ["nurse"], "exclude": ["intern"]},
                                    "boards": {"greenhouse": ["mercy"]}})
        with using(cfg):
            got = scraper.collect_all_postings()
        self.assertEqual(self.asked, ["mercy"])
        self.assertEqual([j["title"] for j in got], ["Charge Nurse"])

    def test_no_titles_is_a_message_not_an_empty_run(self):
        with using(UserConfig.from_dict({"boards": {"greenhouse": ["mercy"]}})):
            with self.assertRaises(ConfigError):
                scraper.collect_all_postings()

    def test_shared_paid_sources_are_off(self):
        cfg = scraper.person_config(UserConfig.from_dict({}))
        for source in ("apify_linkedin", "jooble", "email", "jobicy", "remotive"):
            self.assertFalse(cfg[source]["enabled"], source)


class ProfileFromRows(unittest.TestCase):
    def test_rows_replace_the_profiles_own_skills(self):
        rows = [{"category": "Clinical", "skill": "Telemetry", "have_it": "yes",
                 "proficiency": "Expert"},
                {"category": "Clinical", "skill": "Dialysis", "have_it": "verify"},
                {"category": "Certifications", "skill": "CCRN", "have_it": "yes"}]
        p = st.with_skills(profile(skills=["old"]), st.skills_from_rows(rows))
        self.assertEqual(p["skills_by_category"], {"Clinical": ["Telemetry (Expert)"]})
        self.assertEqual(p["certifications"], ["CCRN"])
        self.assertNotIn("skills", p)


if __name__ == "__main__":
    unittest.main()
