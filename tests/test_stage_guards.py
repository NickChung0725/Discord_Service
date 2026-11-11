from __future__ import annotations

import unittest
from datetime import datetime, timezone

import pandas as pd

import tests  # noqa: F401
from fundamental_analyzer import (
    FundamentalAnalyzer,
    RSS_NO_CONTENT_NOTE,
    build_hard_warnings,
    check_revenue_staleness,
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

    def test_exempts_overheated_or_avoid_tactical_targets_from_deadlock_warning(self) -> None:
        # 當個股超漲 (如收盤 125 元、17MA 乖離 > +12%) 判定為「🚫 觀望避開」或填寫「超漲不設目標價（待拉回 17MA 重估）」時，
        # 不得誤將 "17MA" 解析為 17 元或強迫報出高於 125 元的目標價而觸發 ⚠️ 警告死鎖。
        data = {
            "trade_plan": {
                "tactical_type": "🚫 觀望避開",
                "target_price_1": "超漲不設目標價（待拉回 17MA 95.66 元重估）",
                "target_price_2": "N/A（待拉回支撐確認後重估）",
            }
        }
        fixed = sanitize_stage1_json(data, 125.0)
        self.assertFalse(fixed["trade_plan"]["target_price_1"].startswith("⚠️"))
        self.assertFalse(fixed["trade_plan"]["target_price_2"].startswith("⚠️"))

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
            wide,
            rev,
            {"parent_equity": 1000.0, "capital_stock": 100.0},
            100.0,
            div_latest={"cash_dividend": 4.5},
        )
        text = "\n".join(lines)
        self.assertIn("PE 10.00 倍", text)
        self.assertIn("PB 1.00 倍", text)  # BPS = 1000 / (100 / 10) = 100
        self.assertIn("近一年現金殖利率 4.50%", text)
        self.assertIn("毛利率 42.42%（QoQ +3.10%, YoY +1.85%）", text)
        self.assertIn("加速成長", text)

    def test_stale_revenue_guard_flags_outdated_monthly_revenue(self) -> None:
        from datetime import date

        is_stale, msg = check_revenue_staleness("2024-11", reference_date=date(2026, 10, 25))
        self.assertTrue(is_stale)
        self.assertIn("⚠️ 資料缺漏/過期", msg)

        wide = pd.DataFrame([{"year_qtr": "2026Q2", "gross_margin_pct": 30.0, "op_margin_pct": 10.0, "ttm_eps": 5.0}])
        rev_stale = pd.DataFrame(
            [{"rev_ym": "2024-11", "yoy_pct": -52.0}, {"rev_ym": "2024-11", "yoy_pct": -52.0}]
        )
        text = "\n".join(FundamentalAnalyzer.build_snapshot_trend_lines(wide, rev_stale, {}, 100.0))
        self.assertIn("⚠️ 資料缺漏/過期", text)
        self.assertNotIn("高檔鈍化／動能趨緩", text)

    def test_pe_na_when_ttm_eps_non_positive(self) -> None:
        wide = pd.DataFrame([{"year_qtr": "2026Q2", "ttm_eps": -1.0}])
        text = "\n".join(FundamentalAnalyzer.build_snapshot_trend_lines(wide, pd.DataFrame(), {}, 50.0))
        self.assertIn("PE N/A", text)
        self.assertIn("PB N/A", text)

    def test_build_markdown_context_injects_stage0_header_scales_units_and_includes_margin_cols(self) -> None:
        from unittest.mock import Mock

        cache = Mock()
        cache._get_worksheet.side_effect = lambda name: name  # noqa: SLF001
        cache._get_records_cached.return_value = [  # noqa: SLF001
            {"stock_id": "2618", "stock_name": "長榮航", "industry_category": "航運業"}
        ]
        sheets = {
            "05_AI_Metrics_Wide": pd.DataFrame(
                [
                    {
                        "year_qtr": "2026Q2",
                        "rev_亿元": 550.0,
                        "gross_margin_pct": 22.5,
                        "op_expense_pct": 8.0,
                        "op_margin_pct": 14.5,
                        "non_op_share_pct": 3.2,
                        "parent_net_margin_pct": 12.1,
                        "eps": 1.25,
                        "ttm_eps": 4.74,
                        "qtr_rev_yoy_pct": 12.0,
                        "dio_days": 15.0,
                        "dso_days": 20.0,
                        "dpo_days": 25.0,
                        "ccc_days": 10.0,
                        "total_debt_亿元": 800.0,
                        "net_debt_ratio_pct": -35.81,
                        "current_ratio_pct": 120.0,
                        "quick_ratio_pct": 110.0,
                        "interest_coverage": 15.0,
                        "parent_roe_pct": 18.0,
                        "ocf_to_ni_pct": 140.0,
                        "ebitda_亿元": 120.0,
                    }
                ]
            ),
            "02_Income_Statement": pd.DataFrame(),
            "03_Balance_Sheet": pd.DataFrame(
                [
                    {
                        "year_qtr": "2026Q2",
                        "inventory": 75_302_959_000.0,  # 753.03 億
                        "accounts_receivable": 12_000_000_000.0,  # 120.00 億
                        "accounts_payable": 10_000_000_000.0,
                        "contract_liab_current": 50_000_000_000.0,
                        "cash_and_equiv": 115_800_000_000.0,  # 1158.00 億
                        "short_term_debt": 5_000_000_000.0,
                        "bonds_payable": 0.0,
                        "long_term_debt": 75_000_000_000.0,
                        "total_liabilities": 180_000_000_000.0,
                        "total_equity": 138_000_000_000.0,
                        "parent_equity": 138_000_000_000.0,
                        "capital_stock": 54_000_000_000.0,
                    }
                ]
            ),
            "04_Cash_Flow": pd.DataFrame(
                [
                    {
                        "year_qtr": "2026Q2",
                        "operating_cf": 15_000_000_000.0,  # 150.00 億
                        "capex": 5_223_000_000.0,  # 52.23 億
                        "free_cf": 9_777_000_000.0,  # 97.77 億
                    }
                ]
            ),
            "01_Monthly_Revenue": pd.DataFrame(
                [{"rev_ym": "2024-11", "revenue": 18_500_000_000.0, "mom_pct": 1.2, "yoy_pct": 8.5, "raw_date": "2024-11-10"}]
            ),
            "06_Dividend_Policy": pd.DataFrame(
                [{"div_year": "2025", "cash_dividend": 2.0, "stock_dividend": 0.0, "total_dividend": 2.0, "payout_ratio_pct": 42.0}]
            ),
            "07_Chip_Summary": pd.DataFrame(
                [
                    {
                        "trade_week": "2026W43",
                        "raw_date": "2026-10-25",
                        "foreign_holding_pct": 28.5,
                        "three_inst_net_5d_lots": 39000,
                        "chip_quadrant_tag": "🟢 資減法人買（籌碼沉澱最優）",
                        "margin_balance_lots": 42000,
                        "margin_utilization_pct": 12.5,
                        "margin_chg_5d_lots": -1500,
                        "margin_chg_20d_lots": -3200,
                        "short_margin_ratio_pct": 8.4,
                        "est_margin_maint_pct": 168.5,
                    }
                ]
            ),
        }
        cache._read_stock_rows.side_effect = lambda ws, sid: sheets.get(ws, pd.DataFrame())  # noqa: SLF001

        analyzer = FundamentalAnalyzer(cache)
        analyzer.fetch_free_stock_news_and_search = Mock(return_value="")  # type: ignore[method-assign]
        ctx = analyzer.build_markdown_context("2618", include_outlook=False)

        self.assertIn("【Stage 0 產業定錨】", ctx)
        self.assertIn("【Python 預先計算估值與技術錨點", ctx)
        self.assertIn("753.03", ctx)  # 75,302,959,000 元 -> 753.03 億
        self.assertNotIn("75302959000", ctx)
        self.assertIn("97.77", ctx)  # free_cf 9,777,000,000 元 -> 97.77 億
        self.assertIn("185.00", ctx)  # 月營收 18,500,000,000 元 -> 185.00 億
        self.assertIn("⚠️ 資料缺漏/過期", ctx)  # 2024-11 月營收過期警示
        self.assertIn("chip_quadrant_tag", ctx)
        self.assertIn("est_margin_maint_pct", ctx)


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

