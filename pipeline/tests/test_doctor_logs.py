"""
The two checks that read the run logs.

Both exist to catch a failure that is reported correctly and then scrolled past:
a board token that 404s every night, and a retry that has stopped being
transient. Both have to stay quiet about ordinary bad luck, or they get ignored
like the warnings they replaced.

Each test builds its own log directory in a temp folder, so nothing real is
read or written.
"""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import helpers  # noqa: F401  -- puts src/ on the path; must come first

import doctor

DEAD_TOKEN = ("[warn] greenhouse/acme failed: 404 Client Error: Not Found for "
              "url: https://boards-api.greenhouse.io/v1/boards/acme/jobs\n")
TIMEOUT = ("[warn] greenhouse/other failed: HTTPSConnectionPool(host='boards-api."
           "greenhouse.io', port=443): Read timed out.\n")
SOURCE_DOWN = "[warn] apify_linkedin failed: RuntimeError: Apify returned 402\n"
CLEAN = "  found 50 postings matching your keyword/location filters\n"

SCORED = "[8/10] Director, Marketing Analytics @ Acme\n"
RESUME = "    -> resume drafted: Acme_Director_2026-09-29.json\n"
COVER = "    -> cover letter drafted: Acme_Director_2026-09-29_cover.json\n"
JSON_RETRY = ("    [json] resume response did not parse (Expecting ',' delimiter: "
              "line 43 column 6 (char 4338)); asking again\n")
STYLE_RETRY = "    [style] rewriting cover letter, found: unsupported metric\n"
CLEAN_RUN = (SCORED + RESUME + COVER) * 5          # 15 parsed responses


class LogCheck(unittest.TestCase):
    """Runs a doctor check over synthetic logs, newest run first."""

    def check(self, fn, runs, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            now = datetime.now()
            for i, content in enumerate(runs):      # index 0 is the newest
                when = now - timedelta(days=i)
                path = logs / f"pipeline_{when:%Y-%m-%d}.log"
                path.write_text(content, encoding="utf-8")
                os.utime(path, (when.timestamp(), when.timestamp()))

            saved_logs, saved_output = doctor.LOGS, doctor.OUTPUT
            doctor.LOGS = logs
            doctor.OUTPUT = logs                    # no scored_postings.json here
            doctor._problems.update(fail=0, warn=0)
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stdout(buffer):
                    fn(**kwargs)
            finally:
                doctor.LOGS, doctor.OUTPUT = saved_logs, saved_output
            return buffer.getvalue(), dict(doctor._problems)


class DeadSources(LogCheck):
    def test_a_token_failing_every_recent_run_is_flagged(self):
        """A removed board token 404s from that day on. Two sat unnoticed for
        weeks."""
        out, problems = self.check(doctor.check_dead_sources,
                                   [DEAD_TOKEN, DEAD_TOKEN, DEAD_TOKEN, CLEAN, CLEAN])
        self.assertIn("greenhouse/acme", out)
        self.assertEqual(problems["fail"], 1, "a 404 streak is a FAIL, not a warning")

    def test_two_bad_runs_are_not_yet_a_pattern(self):
        _out, problems = self.check(doctor.check_dead_sources,
                                    [DEAD_TOKEN, DEAD_TOKEN, CLEAN, CLEAN, CLEAN])
        self.assertEqual(problems["fail"], 0)

    def test_a_one_off_timeout_is_ignored(self):
        out, problems = self.check(doctor.check_dead_sources,
                                   [TIMEOUT, CLEAN, CLEAN, CLEAN, CLEAN])
        self.assertEqual(problems, {"fail": 0, "warn": 0})
        self.assertIn("no source has failed", out)

    def test_an_intermittent_board_is_a_warning_not_a_failure(self):
        _out, problems = self.check(doctor.check_dead_sources,
                                    [TIMEOUT, CLEAN, TIMEOUT, CLEAN, TIMEOUT])
        self.assertEqual(problems["fail"], 0)
        self.assertGreaterEqual(problems["warn"], 1)

    def test_a_whole_source_down_is_reported(self):
        out, _problems = self.check(doctor.check_dead_sources,
                                    [SOURCE_DOWN, SOURCE_DOWN, SOURCE_DOWN, CLEAN, CLEAN])
        self.assertIn("apify_linkedin", out)

    def test_too_little_history_says_so(self):
        out, problems = self.check(doctor.check_dead_sources, [CLEAN, CLEAN])
        self.assertIn("not enough history", out)
        self.assertEqual(problems, {"fail": 0, "warn": 0})


class RetryRate(LogCheck):
    def test_no_retries_is_quiet(self):
        out, problems = self.check(doctor.check_retries, [CLEAN_RUN] * 5, threshold=6)
        self.assertEqual(problems, {"fail": 0, "warn": 0})
        self.assertIn("no malformed JSON", out)

    def test_one_retry_is_bad_luck(self):
        _out, problems = self.check(doctor.check_retries,
                                   [CLEAN_RUN + JSON_RETRY] + [CLEAN_RUN] * 4,
                                   threshold=6)
        self.assertEqual(problems["fail"], 0)

    def test_a_retry_in_every_run_is_a_pattern(self):
        _out, problems = self.check(doctor.check_retries,
                                   [CLEAN_RUN + JSON_RETRY] * 5, threshold=6)
        self.assertGreaterEqual(problems["warn"], 1)

    def test_mostly_malformed_is_a_failure(self):
        """At this rate the prompt or the schema is wrong and the retry is just
        paying twice."""
        run = (SCORED + JSON_RETRY + RESUME + JSON_RETRY + COVER) * 5
        _out, problems = self.check(doctor.check_retries, [run] * 4, threshold=6)
        self.assertGreaterEqual(problems["fail"], 1)

    def test_a_bad_night_is_not_hidden_by_a_clean_week(self):
        """Averaging the window dilutes a failure that started last night --
        the same arithmetic that let the dead tokens sit. The newest run is
        judged on its own too."""
        bad_night = (SCORED + JSON_RETRY + RESUME + JSON_RETRY + COVER) * 3
        _out, problems = self.check(doctor.check_retries,
                                   [bad_night] + [CLEAN_RUN] * 6, threshold=6)
        self.assertGreaterEqual(problems["fail"], 1)

    def test_the_style_lint_rewriting_most_letters_is_reported(self):
        _out, problems = self.check(doctor.check_retries,
                                   [CLEAN_RUN + STYLE_RETRY * 5] * 4, threshold=6)
        self.assertGreaterEqual(problems["warn"], 1)

    def test_too_little_history_says_so(self):
        out, problems = self.check(doctor.check_retries, [CLEAN_RUN, CLEAN_RUN],
                                   threshold=6)
        self.assertIn("not enough history", out)
        self.assertEqual(problems, {"fail": 0, "warn": 0})


if __name__ == "__main__":
    unittest.main()
