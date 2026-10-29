from __future__ import annotations

import unittest
from datetime import datetime, timezone

import pandas as pd

from fundamental_analyzer import (
    FundamentalAnalyzer,
    RSS_NO_CONTENT_NOTE,
    build_hard_warnings,
    filter_and_clean_rss,
    sanitize_stage1_json,
)


class RssFilterTests(unittest.TestCase):
    NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)

    def test_drops_noise_titles_and_stale_items_and_flags_shell_summaries(self) -> None:
        items = [
            {"title": "【股市爆料同學會】5426 振發 今日股價與討論", "description": "x", "pubDate": ""},
            {"title": "舊聞：某公司法說", "description": "舊聞內容不同", "pubDate": "Mon, 01 Sep 2025 00:00:00 GMT"},
            {"title": "振發 法說會釋出擴產展望", "description": "振發 法說會釋出擴產展望", "pubDate": "Mon, 20 Oct 2026 00:00:00 GMT"},
            {"title": "振發 Q3 毛利率改善", "description": "管理層表示毛利率可望持續改善，訂單能見度達半年。", "pubDate": "Tue, 21 Oct 2026 00:00:00 GMT"},
        ]
        cleaned = filter_and_clean_rss(items, is_guidance=True, now=self.NOW)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]["description"], RSS_NO_CONTENT_NOTE)
        self.assertIn("訂單能見度", cleaned[1]["description"])
        self.assertEqual(items[2]["description"], "振發 法說會釋出擴產展望")  # input not mutated

    def test_caps_at_five_items(self) -> None:
        items = [{"title": f"新聞{i}", "description": f"內容不相同的說明文字 {i} 號", "pubDate": ""} for i in range(9)]
        self.assertEqual(len(filter_and_clean_rss(items, now=self.NOW)), 5)


class SanitizerTests(unittest.TestCase):
    def test_forces_no_data_when_claims_empty_and_fixes_bad_quarter(self) -> None:
        data = {
            "verdict": "2026Q9 營收強",
            "guidance": {"claims": "無", "verdict": "高度可信", "reality_check": "x"},
            "highlights": [],
            "risks": ["a", "b", "c", "d", "e"],
        }
        fixed = sanitize_stage1_json(data, 100.0)
        self.assertEqual(fixed["guidance"]["verdict"], "無資料")
        self.assertEqual(fixed["verdict"], "2026-09 營收強")
        self.assertEqual(fixed["highlights"], ["無顯著基本面亮點"])
        self.assertEqual(len(fixed["risks"]), 4)
        self.assertEqual(set(fixed["trend_tags"]), {"margin_trend", "rev_momentum", "fcf_nature"})
        self.assertIn("invalidation", fixed["trade_plan"])

    def test_keeps_substantive_guidance_and_flags_target_below_close(self) -> None:
        data = {
            "guidance": {"claims": "管理層預期下半年訂單滿載、毛利率回升", "verdict": "待驗證"},
            "highlights": ["h"],
            "risks": ["r"],
            "trade_plan": {"target_price_1": "95 元 (17MA)", "target_price_2": "120 元"},
        }
        fixed = sanitize_stage1_json(data, 100.0)
        self.assertEqual(fixed["guidance"]["verdict"], "待驗證")
        self.assertTrue(fixed["trade_plan"]["target_price_1"].startswith("⚠️"))
        self.assertEqual(fixed["trade_plan"]["target_price_2"], "120 元")

    def test_hard_warnings(self) -> None:
        self.assertEqual(build_hard_warnings({"vol_lots": 500, "op_margin_pct": 5.0}), [])
        self.assertEqual(len(build_hard_warnings({"vol_lots": 100, "op_margin_pct": -1.0, "contract_liab": None})), 2)
        self.assertEqual(build_hard_warnings({"vol_lots": None, "op_margin_pct": -1.0, "contract_liab": 5.0}), [])


class SnapshotTrendTests(unittest.TestCase):
    def test_pe_pb_qoq_yoy_and_revenue_trend(self) -> None:
        wide = pd.DataFrame(
            [
                {"year_qtr": "2025Q2", "gross_margin_pct": 40.57, "op_margin_pct": 20.0, "ttm_eps": 8.0},
                {"year_qtr": "2026Q1", "gross_margin_pct": 39.32, "op_margin_pct": 21.0, "ttm_eps": 9.0},
                {"year_qtr": "2026Q2", "gross_margin_pct": 42.42, "op_margin_pct": 23.0, "ttm_eps": 10.0},
            ]
        )
        rev = pd.DataFrame({"yoy_pct": [10.0, 20.0, 60.0]})
        lines = FundamentalAnalyzer.build_snapshot_trend_lines(
            wide, rev, {"parent_equity": 1000.0, "capital_stock": 100.0}, 100.0
        )
        text = "\n".join(lines)
        self.assertIn("PE 10.00 倍", text)
        self.assertIn("PB 1.00 倍", text)  # BPS = 1000 / (100 / 10) = 100
        self.assertIn("毛利率 42.42%（QoQ +3.10%, YoY +1.85%）", text)
        self.assertIn("加速成長", text)

    def test_pe_na_when_ttm_eps_non_positive(self) -> None:
        wide = pd.DataFrame([{"year_qtr": "2026Q2", "ttm_eps": -1.0}])
        text = "\n".join(FundamentalAnalyzer.build_snapshot_trend_lines(wide, pd.DataFrame(), {}, 50.0))
        self.assertIn("PE N/A", text)
        self.assertIn("PB N/A", text)


if __name__ == "__main__":
    unittest.main()

