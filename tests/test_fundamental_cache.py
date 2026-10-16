from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from fundamental_config import get_expected_watermarks
from fundamental_cache import _anchor_pick


class FundamentalConfigTests(unittest.TestCase):
    def test_expected_watermarks_returns_strings(self) -> None:
        rev_ym, fin_qtr = get_expected_watermarks(date(2026, 10, 14))
        self.assertRegex(rev_ym, r"^\d{4}-\d{2}$")
        self.assertRegex(fin_qtr, r"^\d{4}Q[1-4]$")

    def test_expected_rev_raw_date_handles_december_rollover(self) -> None:
        # Expected rev_ym for Jan 15 is previous year's December, whose raw_date should be next year's Jan 1.
        from fundamental_cache import _expected_rev_raw_date

        self.assertEqual(_expected_rev_raw_date(date(2026, 1, 15)).isoformat(), "2026-01-01")

    def test_anchor_pick_returns_float_nan_when_no_alias_matches(self) -> None:
        pivot = pd.DataFrame({"other_account": [1.0, 2.0]}, index=[3, 7])

        result = _anchor_pick(pivot, ["missing_account"])

        self.assertEqual(result.dtype, "float64")
        self.assertTrue(result.isna().all())
        self.assertEqual(result.index.tolist(), [3, 7])

    def test_anchor_pick_supports_dual_track_spec(self) -> None:
        pivot_type = pd.DataFrame({"A": [1.0, None]}, index=[0, 1])
        pivot_origin = pd.DataFrame({"中文": [None, 2.0]}, index=[0, 1])

        result = _anchor_pick(
            pivot_type,
            {"types": ["A"], "origins": ["中文"]},
            pivot_origin,
        )

        self.assertEqual(result.tolist(), [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
