"""
Reading a pay range out of a posting.

A wrong number here is worse than no number: it reaches the tracker looking
exactly like a right one, and it feeds the salary floor that the scorer uses to
judge seniority. Every case below is a shape that appeared in a real posting.
"""
import unittest

import helpers  # noqa: F401  -- puts src/ on the path; must come first

from score_and_tailor import extract_salary_range
from scraper import _indeed_pay_range


class AnnualRanges(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(
            extract_salary_range("The base salary range is $150,000 - $185,000"),
            (150000, 185000))

    def test_k_notation(self):
        self.assertEqual(extract_salary_range("Base pay $150K-$185K"),
                         (150000, 185000))

    def test_to_instead_of_a_dash(self):
        self.assertEqual(
            extract_salary_range("salary of $180,000 to $220,000"),
            (180000, 220000))

    def test_em_dash(self):
        self.assertEqual(
            extract_salary_range("The typical starting salary range is:$160,300—$253,600 USD"),
            (160300, 253600))

    def test_bare_hundreds_in_a_pay_context_mean_thousands(self):
        self.assertEqual(extract_salary_range("salary of $150 to $250"),
                         (150000, 250000))

    def test_cents_on_an_annual_figure_are_formatting(self):
        """Cents used to disqualify a range as hourly, which threw away the one
        authoritative figure in the posting."""
        self.assertEqual(
            extract_salary_range("Salary: $160,300.00/yr - $253,600.00/yr"),
            (160300, 253600))


class NotSalaries(unittest.TestCase):
    def test_hourly_with_a_trailing_unit(self):
        self.assertIsNone(extract_salary_range("Salary: $50 - $65 per hour"))

    def test_hourly_with_cents(self):
        self.assertIsNone(extract_salary_range("Compensation: $45.00 - $65.00 per hour"))

    def test_cents_on_a_small_number_mean_a_rate_even_with_no_unit(self):
        self.assertIsNone(extract_salary_range("$45.00 - $65.00"))

    def test_hourly_unit_before_the_dash(self):
        self.assertIsNone(extract_salary_range("Salary: $120.00/hr - $140.00/hr"))

    def test_monthly(self):
        self.assertIsNone(extract_salary_range("Pay: $12,000/mo - $14,000/mo"))

    def test_equity_percentages(self):
        self.assertEqual(
            extract_salary_range("Equity of 0.1% - 0.5% and a salary range of "
                                 "$180,000 to $220,000"),
            (180000, 220000))

    def test_nothing_to_find(self):
        self.assertIsNone(extract_salary_range("no numbers here at all"))
        self.assertIsNone(extract_salary_range(""))
        self.assertIsNone(extract_salary_range(None))


class ChoosingBetweenSeveralRanges(unittest.TestCase):
    """The reason this function was rewritten. Postings state more than one
    range deliberately, and the largest is usually the one that does not apply
    to the reader."""

    def test_general_range_beats_a_location_premium(self):
        text = ("The typical starting salary range for this role is:"
                "$160,300—$253,600 USD The typical starting salary range for "
                "this role in the select locations listed above is:"
                "$192,300—$304,200 USD")
        self.assertEqual(extract_salary_range(text), (160300, 253600))

    def test_us_range_beats_a_foreign_currency_range(self):
        """The largest pair here is Canadian dollars, which was being stored as
        dollars."""
        text = ("For US based applicants, the salary range is $163,560 - $240,092 "
                "USD + equity. For Toronto and Vancouver based applicants, the "
                "salary range is $210,013 - $246,625 CAD + equity.")
        self.assertEqual(extract_salary_range(text), (163560, 240092))

    def test_benefits_figures_are_not_the_pay_range(self):
        text = ("We match up to $2000 for donations. The salary range is "
                "$160,000 - $200,000.")
        self.assertEqual(extract_salary_range(text), (160000, 200000))

    def test_a_range_in_a_pay_context_beats_an_earlier_one_without(self):
        text = ("Revenue grew from $100,000 to $200,000 last year. "
                "Pay: $170,000 - $190,000")
        self.assertEqual(extract_salary_range(text), (170000, 190000))


class IndeedPayShapes(unittest.TestCase):
    """The Indeed actor returns baseSalary in more than one shape, and the flat
    reading crashed on the nested one."""

    def test_flat_annual(self):
        self.assertEqual(
            _indeed_pay_range({"min": 160000, "max": 200000,
                               "unitOfWork": "YEAR", "currencyCode": "USD"}),
            (160000, 200000, "year"))

    def test_flat_hourly_keeps_its_unit(self):
        """The unit has to survive: the salary parser reads it back out of the
        line this builds, and discards the range when it says hour."""
        self.assertEqual(_indeed_pay_range({"min": 50, "max": 65,
                                            "unitOfWork": "HOUR"}),
                         (50, 65, "hour"))

    def test_nested_schema_org(self):
        self.assertEqual(
            _indeed_pay_range({"@type": "MonetaryAmount", "currency": "USD",
                               "value": {"minValue": 145000, "maxValue": 175000,
                                         "unitText": "YEAR"}}),
            (145000, 175000, "year"))

    def test_malformed_shapes_give_up_quietly(self):
        for bad in ({"min": 150000}, {}, None, "150000", [150000, 200000],
                    {"min": None, "max": 200000}):
            with self.subTest(bad=bad):
                self.assertIsNone(_indeed_pay_range(bad))

    def test_the_hourly_line_it_builds_is_rejected_downstream(self):
        """The two halves have to agree, so check them together."""
        low, high, unit = _indeed_pay_range({"min": 50, "max": 65,
                                             "unitOfWork": "HOUR"})
        line = f"Salary: ${low:,} - ${high:,} per {unit}"
        self.assertIsNone(extract_salary_range(line))


if __name__ == "__main__":
    unittest.main()
