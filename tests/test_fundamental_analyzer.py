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

    def test_build_latest_ma_context_reads_local_prices_csv_or_skips_when_missing(self) -> None:
        import tempfile
        from pathlib import Path
        import pandas as pd

        cache = Mock()
        analyzer = FundamentalAnalyzer(cache)

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            # 1) 沒有 prices.csv 時，回傳空字串（沒有就算了）
            self.assertEqual(analyzer.build_latest_ma_context("2330", base_dir=base_dir), "")

            # 2) 建立最新 prices.csv，包含 2330 股價與預先算好的 MA189
            day_dir = base_dir / "2026-10-16"
            day_dir.mkdir(parents=True, exist_ok=True)
            prices_df = pd.DataFrame(
                {
                    "stock_id": ["2330"] * 50,
                    "date": pd.date_range("2026-08-01", periods=50, freq="B").strftime("%Y-%m-%d"),
                    "close": [980.0 + i for i in range(50)],
                    "MA17": [1010.0] * 49 + [1020.0],
                    "MA45": [1005.0] * 49 + [1015.0],
                    "MA117": [970.0] * 50,
                    "MA189": [940.0] * 50,
                }
            )
            prices_df.to_csv(day_dir / "prices.csv", index=False)

            ma_ctx = analyzer.build_latest_ma_context("2330", base_dir=base_dir)
            self.assertIn("表 7：近期日線與均線排列動態", ma_ctx)
            self.assertIn("MA3", ma_ctx)
            self.assertIn("17MA近期走勢", ma_ctx)
            self.assertIn("940.00", ma_ctx)


if __name__ == "__main__":
    unittest.main()

