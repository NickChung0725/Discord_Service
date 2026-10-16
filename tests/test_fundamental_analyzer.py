from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from fundamental_analyzer import FundamentalAnalyzer


class FundamentalAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_diagnosis_uses_cached_summary_when_period_matches(self) -> None:
        cache = Mock()
        cache._spreadsheet = Mock()  # noqa: SLF001
        watchlist = Mock()
        watchlist.get_all_records.return_value = [
            {
                "stock_id": "2330",
                "last_fin_qtr": "2026Q2",
                "last_rev_ym": "2026-09",
                "max_div_raw_date": "2026-08-01",
                "max_chip_raw_date": "2026-10-14",
                "ai_cache_day": "2026-10-16",
                "ai_cached_period": "2026Q2_2026-09_2026-08-01_2026-10-14_2026-10-16",
                "ai_summary_json": "cached summary",
                "war_room_url": "https://example.invalid/2330_fund.html",
            }
        ]
        cache._spreadsheet.worksheet.return_value = watchlist

        analyzer = FundamentalAnalyzer(cache)
        with patch("fundamental_analyzer.ask_gemini", new_callable=AsyncMock) as mock_ask:
            report = await analyzer.run_full_diagnosis("2330", force_ai=False)

        mock_ask.assert_not_awaited()
        self.assertTrue(report.cached)
        self.assertEqual(report.ai_text, "cached summary")
        self.assertEqual(report.war_room_url, "https://example.invalid/2330_fund.html")


if __name__ == "__main__":
    unittest.main()
