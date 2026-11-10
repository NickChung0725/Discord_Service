from __future__ import annotations

import unittest
from datetime import datetime, timezone

import pandas as pd

import tests  # noqa: F401
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


class Phase3RegimeAndAuditTests(unittest.TestCase):
    def test_regime_router_shuts_down_breakout_in_red_market(self) -> None:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for sub in (root, root / "quant", root / "taishin"):
            if str(sub) not in sys.path:
                sys.path.insert(0, str(sub))

        from alignment_ai_picker import (
            StockAuditItem,
            _enforce_post_audit_guards,
            enforce_regime_quotas,
        )
        from macro_context import evaluate_market_regime_quota

        quota = evaluate_market_regime_quota(
            taiex_close=21800.0,
            taiex_ma20=22400.0,
            taiex_ma20_slope=-0.35,
            otc_close=252.0,
            otc_ma20=260.0,
            otc_ma20_slope=-0.42,
            foreign_futures_net_short_change=1200.0,
            foreign_futures_total_net_short=35000.0,
        )
        self.assertEqual(quota.regime_state, "RED_DEFENSIVE")
        self.assertEqual(quota.max_right_breakout_picks, 0)
        self.assertEqual(quota.max_style_b_pullback_picks, 1)
        self.assertAlmostEqual(quota.max_allowed_sector_pe_ratio, 0.85)

        # 驗證 Pass 1 後置硬攔截：紅燈下右側突破股強制轉為 REJECT_TRAP
        item = StockAuditItem(
            stock_id="2382",
            stock_name="廣達",
            bear_case_flaws=["大盤偏弱"],
            bull_case_catalysts=["營收成長"],
            conflict_resolution_reasoning="嘗試突破",
            execution_strategy_type="BREAKOUT_0915_VWAP_OR_1315_CLOSE",
            thesis_invalidation_triggers=["跌破均線"],
            verdict_grade="S_TIER_SNIPER",
        )
        guarded = _enforce_post_audit_guards(
            [item],
            {"2382": {"signal_type": "RIGHT_BREAKOUT", "deterministic_rr_ratio": 2.5}},
            {},
            context_by_stock={"2382": {"sector_breadth_pct": 60.0, "sector_member_count": 5}},
            regime_quota=quota.to_dict(),
        )
        self.assertEqual(guarded[0].verdict_grade, "REJECT_TRAP")
        self.assertEqual(guarded[0].execution_strategy_type, "NO_TRADE")

        # 驗證決策層配額調控：黃燈下右側突破最多 1 檔，超額自動移列 watchlist
        yellow_q = {
            "regime_state": "YELLOW_ROTATION",
            "max_right_breakout_picks": 1,
            "max_style_b_pullback_picks": 2,
        }
        decision = {
            "top_picks": [
                {"stock_id": "2330", "rank": 1, "audit": {"Execution_Tactic": "BREAKOUT_0915_VWAP_OR_1315_CLOSE"}},
                {"stock_id": "2317", "rank": 2, "audit": {"Execution_Tactic": "BREAKOUT_0915_VWAP_OR_1315_CLOSE"}},
                {"stock_id": "2454", "rank": 3, "audit": {"Execution_Tactic": "STYLE_B_PULLBACK_LIMIT"}},
            ],
            "watchlist": [],
            "avoid": [],
        }
        enforced = enforce_regime_quotas(decision, {}, yellow_q)
        self.assertEqual([p["stock_id"] for p in enforced["top_picks"]], ["2330", "2454"])
        self.assertEqual([w["stock_id"] for w in enforced["watchlist"]], ["2317"])

    def test_signal_audit_stop_first_pessimistic_and_false_negative_tracking(self) -> None:
        import tempfile
        from pathlib import Path
        from quant.signal_audit_service import (
            SignalAuditStore,
            evaluate_forward_path,
            format_weekly_summary_markdown,
        )

        # 1) 同日 High 觸及 TP1 (112) 且 Low 跌破 Stop (96) -> Stop-First 悲觀原則判定 STOP_LOSS_HIT + is_whipsawed=1
        bars_collision = [
            {"date": "2026-10-02", "open": 100.0, "high": 113.0, "low": 95.0, "close": 108.0},
            {"date": "2026-10-05", "open": 108.0, "high": 115.0, "low": 107.0, "close": 114.0},
            {"date": "2026-10-06", "open": 114.0, "high": 116.0, "low": 112.0, "close": 115.0},
            {"date": "2026-10-07", "open": 115.0, "high": 117.0, "low": 113.0, "close": 116.0},
            {"date": "2026-10-08", "open": 116.0, "high": 118.0, "low": 114.0, "close": 117.0},
        ]
        res_whip = evaluate_forward_path(
            {
                "scanner_signal_type": "RIGHT_BREAKOUT",
                "ai_verdict_grade": "S_TIER_SNIPER",
                "execution_strategy_type": "BREAKOUT_0915_VWAP_OR_1315_CLOSE",
                "close_t": 100.0,
                "chase_limit_price": 103.0,
                "hard_stop_loss": 96.0,
                "tp1_price": 112.0,
            },
            bars_collision,
        )
        self.assertEqual(res_whip["entry_fill_status"], "FILLED_T1")
        self.assertAlmostEqual(float(res_whip["effective_entry_price"]), 100.5, places=2)
        self.assertEqual(res_whip["outcome_status_10d"], "STOP_LOSS_HIT")
        self.assertEqual(res_whip["is_whipsawed"], 1)

        # 2) REJECT_TRAP 後續未破停損且觸及 TP1 / 大漲 -> is_false_negative_miss = 1
        bars_miss = [
            {"date": "2026-10-02", "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0},
            {"date": "2026-10-05", "open": 104.0, "high": 110.0, "low": 103.0, "close": 109.0},
            {"date": "2026-10-06", "open": 109.0, "high": 115.0, "low": 108.0, "close": 114.0},
            {"date": "2026-10-07", "open": 114.0, "high": 119.0, "low": 113.0, "close": 118.0},
            {"date": "2026-10-08", "open": 118.0, "high": 120.0, "low": 117.0, "close": 119.5},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "signal_audit.db"
            store = SignalAuditStore(db_path)
            store.record_signal(
                {
                    "signal_date": "2026-10-01",
                    "stock_id": "3017",
                    "stock_name": "奇鋐",
                    "sector_name": "AI伺服器散熱",
                    "regime_state": "YELLOW_ROTATION",
                    "scanner_signal_type": "RIGHT_BREAKOUT",
                    "ai_verdict_grade": "REJECT_TRAP",
                    "execution_strategy_type": "NO_TRADE",
                    "close_t": 100.0,
                    "chase_limit_price": 103.0,
                    "hard_stop_loss": 95.0,
                    "tp1_price": 114.0,
                    "deterministic_rr_ratio": 2.4,
                }
            )
            updated = store.backfill_pending(lambda sid, s_dt: bars_miss)
            self.assertEqual(updated, 1)
            summary = store.build_weekly_summary()
            self.assertAlmostEqual(float(summary["false_negative_rate"]), 1.0)
            md = format_weekly_summary_markdown(summary, as_of_str="2026-10-09")
            self.assertIn("Weekly Quantitative Audit Summary", md)


if __name__ == "__main__":
    unittest.main()

