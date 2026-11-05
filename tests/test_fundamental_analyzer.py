from __future__ import annotations

from datetime import date, datetime
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import tests  # noqa: F401
from fundamental_analyzer import (
    FundamentalAnalyzer,
    _analyze_revenue_momentum,
    _compute_forward_valuation,
    _evaluate_margin_trends,
    _infer_safe_weighted_shares,
    build_ai_fundamental_prompt_block,
)
from taxonomy_service import compute_sector_median_pe


class FundamentalAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_diagnosis_uses_cached_summary_when_period_matches(self) -> None:
        cache = Mock()
        cache._get_worksheet.return_value = Mock()  # noqa: SLF001
        cache._get_records_cached.return_value = [  # noqa: SLF001
            {
                "stock_id": "2330",
                "last_fin_qtr": "2026Q2",
                "last_rev_ym": "2026-09",
                "max_div_raw_date": "2026-08-01",
                "max_chip_raw_date": "2026-10-14",
                "ai_cache_day": "2026-10-16",
                "ai_cached_period": "2026Q2_2026-09_2026-08-01_2026-10-14",
                "ai_summary_json": json.dumps({"verdict": "v", "score": 80, "trend_tags": {}, "trade_plan": {}}),
                "ai_model_version": "gemini-cached",
                "war_room_url": "https://example.invalid/2330_fund.html",
            }
        ]

        analyzer = FundamentalAnalyzer(cache)
        with patch("fundamental_analyzer.ask_gemini_diagnosis_with_meta", new_callable=AsyncMock) as mock_ask:
            report = await analyzer.run_full_diagnosis("2330", force_ai=False)

        mock_ask.assert_not_awaited()
        self.assertTrue(report.cached)
        self.assertTrue(report.ai_text.startswith("分析模型：gemini-cached"))
        self.assertIn("體質評分 80", report.ai_text)
        self.assertEqual(report.war_room_url, "https://example.invalid/2330_fund.html")

    async def test_full_diagnosis_reruns_when_cached_summary_schema_is_old(self) -> None:
        cache = Mock()
        cache._get_worksheet.return_value = Mock()  # noqa: SLF001
        cache._get_records_cached.return_value = [  # noqa: SLF001
            {
                "stock_id": "2330",
                "last_fin_qtr": "2026Q2",
                "last_rev_ym": "2026-09",
                "max_div_raw_date": "2026-08-01",
                "max_chip_raw_date": "2026-10-14",
                "ai_cached_period": "2026Q2_2026-09_2026-08-01_2026-10-14",
                "ai_summary_json": json.dumps({"verdict": "v", "score": 70}),
            }
        ]
        analyzer = FundamentalAnalyzer(cache)
        raw = 'text\n===SUMMARY_JSON===\n{"verdict":"v","score":70,"trend_tags":{},"trade_plan":{}}'
        meta = Mock(text=raw, model_version="gemini-test")
        with patch.object(analyzer, "build_markdown_context", return_value="ctx"), patch(
            "fundamental_analyzer.ask_gemini_diagnosis_with_meta", new_callable=AsyncMock, return_value=meta
        ) as mock_ask:
            report = await analyzer.run_full_diagnosis("2330", publish_war_room=False)

        mock_ask.assert_awaited_once()
        self.assertFalse(report.cached)
        self.assertTrue(report.ai_text.startswith("分析模型：gemini-test"))
        self.assertIn("text", report.ai_text)
        self.assertEqual(json.loads(report.summary_json)["score"], 70)

    async def test_full_diagnosis_reruns_when_cached_summary_is_empty(self) -> None:
        cache = Mock()
        cache._get_worksheet.return_value = Mock()  # noqa: SLF001
        cache._get_records_cached.return_value = [  # noqa: SLF001
            {
                "stock_id": "2330",
                "last_fin_qtr": "2026Q2",
                "last_rev_ym": "2026-09",
                "max_div_raw_date": "2026-08-01",
                "max_chip_raw_date": "2026-10-14",
                "ai_cached_period": "2026Q2_2026-09_2026-08-01_2026-10-14",
                "ai_summary_json": "",
            }
        ]
        analyzer = FundamentalAnalyzer(cache)
        raw = 'text\n===SUMMARY_JSON===\n{"verdict":"v","score":70}'
        meta = Mock(text=raw, model_version="gemini-test")
        with patch.object(analyzer, "build_markdown_context", return_value="ctx"), patch(
            "fundamental_analyzer.ask_gemini_diagnosis_with_meta", new_callable=AsyncMock, return_value=meta
        ) as mock_ask:
            report = await analyzer.run_full_diagnosis("2330", publish_war_room=False)

        mock_ask.assert_awaited_once()
        self.assertFalse(report.cached)
        self.assertTrue(report.ai_text.startswith("分析模型：gemini-test"))
        self.assertIn("text", report.ai_text)
        self.assertEqual(json.loads(report.summary_json)["score"], 70)
        self.assertEqual(json.loads(report.summary_json)["model_version"], "gemini-test")
        updates = cache._update_watchlist_master.call_args.args[2]  # noqa: SLF001
        self.assertEqual(updates["ai_cached_period"], "2026Q2_2026-09_2026-08-01_2026-10-14")
        self.assertEqual(updates["ai_model_version"], "gemini-test")

    async def test_full_diagnosis_persists_ai_summary_even_if_war_room_upload_fails(self) -> None:
        cache = Mock()
        cache._get_worksheet.return_value = Mock()  # noqa: SLF001
        cache._get_records_cached.return_value = [  # noqa: SLF001
            {
                "stock_id": "2834",
                "last_fin_qtr": "2026Q2",
                "last_rev_ym": "2026-09",
                "max_div_raw_date": "2026-08-01",
                "max_chip_raw_date": "2026-10-30",
                "ai_cached_period": "",
                "ai_summary_json": "",
                "war_room_url": "https://example.invalid/old_2834_fund.html",
            }
        ]
        analyzer = FundamentalAnalyzer(cache)
        raw = 'text\n===SUMMARY_JSON===\n{"verdict":"v","score":75,"trend_tags":{},"trade_plan":{}}'
        meta = Mock(text=raw, model_version="gemini-3.8-flash")
        with (
            patch.object(analyzer, "build_markdown_context", return_value="ctx"),
            patch.object(
                analyzer,
                "render_and_publish_war_room_html",
                new_callable=AsyncMock,
                side_effect=RuntimeError("GitHub 上傳失敗（HTTP 500）"),
            ),
            patch(
                "fundamental_analyzer.ask_gemini_diagnosis_with_meta",
                new_callable=AsyncMock,
                return_value=meta,
            ),
        ):
            report = await analyzer.run_full_diagnosis("2834", publish_war_room=True)

        self.assertFalse(report.cached)
        self.assertEqual(json.loads(report.summary_json)["score"], 75)
        self.assertEqual(report.war_room_url, "https://example.invalid/old_2834_fund.html")
        cache._update_watchlist_master.assert_called_once()  # noqa: SLF001

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

    def test_flexible_par_value_stock_share_inference(self) -> None:
        # 面額 1 元（而非 10 元）的高價股：實收資本額 3 億元，但最新季稅後淨利 6 億元、EPS 2.00 元
        quarterly_statements = [
            {
                "date": "2026Q3",
                "CapitalStock": 300_000_000.0,
                "IncomeAfterTaxes": 600_000_000.0,
                "EPS": 2.00,
            }
        ]
        shares, tier = _infer_safe_weighted_shares(quarterly_statements)
        # 真實流通股數應為 3 億股 (300,000,000)，而非用股本除 10 算出的 3,000 萬股
        self.assertAlmostEqual(shares, 300_000_000.0)
        self.assertEqual(tier, "TIER1_LATEST_QUARTER_EXACT")

    def test_micro_profit_turnaround_stock_avoids_zero_div_and_rounding_explosion(self) -> None:
        # 1) 最新季 Q3 微利 EPS = 0.01 元（若直接除會算出 149,000,000 股，膨脹 49%），前季 Q2 EPS = -0.50 元
        statements_rounding = [
            {"date": "2026Q3", "IncomeAfterTaxes": 1_490_000.0, "EPS": 0.01},
            {"date": "2026Q2", "IncomeAfterTaxes": -50_000_000.0, "EPS": -0.50},
        ]
        shares_1, tier_1 = _infer_safe_weighted_shares(statements_rounding)
        self.assertAlmostEqual(shares_1, 100_000_000.0)
        self.assertEqual(tier_1, "TIER2_LOOKBACK_2026Q2")

        # 2) 最新季 Q3 損益兩平 EPS = 0.00 元，確保不發生 ZeroDivisionError 並回溯至 Q2
        statements_zero_eps = [
            {"date": "2026Q3", "IncomeAfterTaxes": 1_490_000.0, "EPS": 0.00},
            {"date": "2026Q2", "IncomeAfterTaxes": -50_000_000.0, "EPS": -0.50},
        ]
        shares_2, tier_2 = _infer_safe_weighted_shares(statements_zero_eps)
        self.assertAlmostEqual(shares_2, 100_000_000.0)
        self.assertEqual(tier_2, "TIER2_LOOKBACK_2026Q2")

    def test_lunar_new_year_revenue_exemption_prevents_false_rejection(self) -> None:
        # 模擬時間 3 月 10 日（已公佈 2 月營收），2 月因農曆春節長假工作天數減少，單月較 1 月下滑 22%（且低於近 3 月均線），
        # 但 1~2 月累計營收 YoY 高達 +42%（高於去年 Q4 的 +25%）。
        monthly_revenues = [
            {"rev_ym": "2026-02", "revenue": 780_000_000.0, "mom_pct": -22.0, "yoy_pct": 30.0},
            {"rev_ym": "2026-01", "revenue": 1_000_000_000.0, "mom_pct": 5.0, "yoy_pct": 53.1},
            {"rev_ym": "2025-12", "revenue": 950_000_000.0, "mom_pct": 2.0, "yoy_pct": 26.0},
        ]
        result = _analyze_revenue_momentum(
            monthly_revenues,
            last_q4_yoy_pct=25.0,
            jan_feb_combined_yoy_pct=42.0,
            as_of=date(2026, 3, 10),
        )
        self.assertEqual(result["mode"], "LUNAR_NEW_YEAR_EXEMPT_MODE")
        self.assertEqual(result["status"], "RIGHT_SIDE_EXPANDING")
        self.assertAlmostEqual(float(result["jan_feb_combined_yoy_pct"]), 42.0)

    def test_fake_revenue_dilution_flags_margin_contraction(self) -> None:
        # 營收 YoY 成長 +28%，但最新季毛利率 QoQ 下滑 -2.1%、營益率 QoQ 下滑 -2.4%
        quarterly_statements = [
            {"year_qtr": "2026Q3", "gross_margin_pct": 18.4, "op_margin_pct": 7.2},
            {"year_qtr": "2026Q2", "gross_margin_pct": 20.5, "op_margin_pct": 9.6},
        ]
        res = _evaluate_margin_trends(quarterly_statements, revenue_yoy_pct=28.0)
        self.assertEqual(res["tag"], "FAKE_REVENUE_DILUTION")
        self.assertIn("殺價搶單警訊", res["label"])
        self.assertAlmostEqual(float(res["gpm_qoq_diff"]), -2.1)
        self.assertAlmostEqual(float(res["opm_qoq_diff"]), -2.4)

    def test_forward_valuation_and_sector_median_pe(self) -> None:
        # 測試同業中位數 PE 過濾離群值 (<=0, >120) 與樣本數 >= 4 中位數計算
        sector_pe = compute_sector_median_pe(
            "AI伺服器",
            {"2382": 20.0, "3231": 16.0, "6669": 22.0, "2356": 19.0, "9991": -5.0, "9992": 180.0},
        )
        self.assertAlmostEqual(sector_pe, 19.5)

        val = _compute_forward_valuation(
            implied_annual_revenue=18_000_000_000.0,
            quarterly_statements=[
                {"year_qtr": "2026Q3", "op_margin_pct": 15.0},
                {"year_qtr": "2026Q2", "op_margin_pct": 14.6},
            ],
            safe_weighted_shares=300_000_000.0,
            latest_close_price=100.8,
            sector_benchmark_pe=sector_pe,
            trailing_pe=32.5,
        )
        # Implied Core Forward EPS = 18e9 * 0.148 * 0.80 / 3e8 = 7.104 -> Forward PE = 100.8 / 7.104 = 14.19
        self.assertAlmostEqual(float(val["implied_forward_pe"]), 14.19, places=1)
        self.assertIn("轉機成長收斂股", val["valuation_tag"])

        block = build_ai_fundamental_prompt_block(
            "2382",
            "廣達",
            "伺服器與電腦週邊",
            quarterly_statements=[
                {"year_qtr": "2026Q3", "IncomeAfterTaxes": 600_000_000.0, "EPS": 2.0, "gross_margin_pct": 24.5, "op_margin_pct": 14.8},
                {"year_qtr": "2026Q2", "IncomeAfterTaxes": 540_000_000.0, "EPS": 1.8, "gross_margin_pct": 23.3, "op_margin_pct": 13.0},
            ],
            monthly_revenues=[
                {"rev_ym": "2026-10", "revenue": 1_500_000_000.0, "yoy_pct": 38.0},
                {"rev_ym": "2026-09", "revenue": 1_400_000_000.0, "yoy_pct": 32.0},
                {"rev_ym": "2026-08", "revenue": 1_300_000_000.0, "yoy_pct": 28.0},
            ],
            latest_close_price=98.0,
            sector_benchmark_pe=19.5,
            trailing_pe=31.0,
            prev_quarter_revenue=3_900_000_000.0,
        )
        self.assertIn("TIER1_LATEST_QUARTER_EXACT", block)
        self.assertIn("RIGHT_SIDE_EXPANDING", block)
        self.assertIn("MARGIN_TRIPLE_EXPANSION", block)


if __name__ == "__main__":
    unittest.main()

