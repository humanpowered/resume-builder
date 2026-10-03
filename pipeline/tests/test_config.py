"""
Config loaders fail loudly or they are worse than useless.

A config file that is silently ignored looks exactly like one that works. Each
loader has to stop the run and name both the file and the problem, so these
tests point the loaders at temp files and check they refuse.
"""
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  -- puts src/ on the path; must come first

import settings
from settings import TUNING_DEFAULTS, load_titles, load_tuning


class TuningYaml(unittest.TestCase):
    def tuning(self, text):
        """Run load_tuning against a file holding `text`, or no file at all."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tuning.yaml"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            saved = settings.TUNING_YAML
            settings.TUNING_YAML = path
            try:
                return load_tuning()
            finally:
                settings.TUNING_YAML = saved

    def test_missing_file_uses_defaults(self):
        """Absent is fine. Present and wrong is not."""
        self.assertEqual(self.tuning(None), TUNING_DEFAULTS)

    def test_a_value_overrides_its_default(self):
        got = self.tuning("score_threshold: 8\n")
        self.assertEqual(got["score_threshold"], 8)
        self.assertEqual(got["model"], TUNING_DEFAULTS["model"])

    def test_a_typo_in_a_key_stops_the_run(self):
        """The whole point. A misspelled key that is ignored means a setting
        you believe you changed and did not."""
        with self.assertRaises(SystemExit) as caught:
            self.tuning("score_treshold: 8\n")
        message = str(caught.exception)
        self.assertIn("score_treshold", message)
        self.assertIn("tuning.yaml", message)

    def test_wrong_type_stops_the_run(self):
        with self.assertRaises(SystemExit):
            self.tuning("score_threshold: not a number\n")

    def test_negative_number_stops_the_run(self):
        with self.assertRaises(SystemExit):
            self.tuning("max_letter_words: -5\n")

    def test_invalid_yaml_stops_the_run(self):
        with self.assertRaises(SystemExit) as caught:
            self.tuning("score_threshold: [unclosed\n")
        self.assertIn("tuning.yaml", str(caught.exception))

    def test_a_list_instead_of_a_mapping_stops_the_run(self):
        with self.assertRaises(SystemExit):
            self.tuning("- score_threshold\n- 8\n")


class PlaceholderPositioning(unittest.TestCase):
    """The cover letter's first paragraph ships as a [FILL IN: ...] marker, and
    doctor refuses to run while it is still there. The check compared against
    the exact default string, which letter.example.yaml does not reproduce word
    for word -- so copying the example and running produced letters with
    "[FILL IN: ...]" in the first paragraph and no complaint."""

    def positioning_is_rejected(self, text):
        return "[FILL IN" in text

    def test_the_default_is_rejected(self):
        from settings import PLACEHOLDER_POSITIONING
        self.assertTrue(self.positioning_is_rejected(PLACEHOLDER_POSITIONING))

    def test_the_shipped_examples_wording_is_also_rejected(self):
        """Shorter than the default, and the reason the old check missed it."""
        self.assertTrue(self.positioning_is_rejected(
            "[FILL IN: one sentence saying who you are and what you build.]"))

    def test_real_positioning_is_accepted(self):
        self.assertFalse(self.positioning_is_rejected(
            "I build measurement systems that survive a real marketing budget."))


class TitlesCsv(unittest.TestCase):
    def titles(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "titles.csv"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            saved = settings.TITLES_CSV
            settings.TITLES_CSV = path
            try:
                return load_titles()
            finally:
                settings.TITLES_CSV = saved

    HEADER = "term,mode,active,notes\n"

    def test_include_and_exclude_are_separated(self):
        include, exclude = self.titles(
            self.HEADER
            + "marketing analytics,include,yes,\n"
            + "intern,exclude,yes,\n")
        self.assertEqual(include, ["marketing analytics"])
        self.assertEqual(exclude, ["intern"])

    def test_inactive_rows_are_skipped_rather_than_deleted(self):
        """A row switched off with active=no keeps its note, which is the
        reason the file is a spreadsheet and not a YAML list."""
        include, _ = self.titles(
            self.HEADER
            + "marketing analytics,include,yes,\n"
            + "growth analytics,include,no,tried it; too noisy\n")
        self.assertEqual(include, ["marketing analytics"])

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(self.titles(None))

    def test_a_missing_column_stops_the_run(self):
        with self.assertRaises(SystemExit) as caught:
            self.titles("term,active,notes\nmarketing analytics,yes,\n")
        self.assertIn("titles.csv", str(caught.exception))

    def test_an_unknown_mode_stops_the_run(self):
        with self.assertRaises(SystemExit):
            self.titles(self.HEADER + "marketing analytics,includ,yes,\n")


if __name__ == "__main__":
    unittest.main()
