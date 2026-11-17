from __future__ import annotations

import unittest

import pandas as pd

import tests  # noqa: F401
from finmind_stock_scanner import (
    _snap_to_psychological_support,
    add_moving_averages,
    classify_rebound_signal,
    compute_rebound_trade_setup,
)
from fundamental_analyzer import (
    _normalize_monthly_revenue_pcts,
    build_hard_warnings,
    compute_fundamental_features,
)
from alignment_ai_picker import (
    _apply_portfolio_prompt_overrides,
    apply_audit_to_decision,
    prefilter_rebound_candidates,
)
from fundamental_config import PROMPT_TEMPLATES


class ReboundV6Tests(unittest.TestCase):
    def test_bug_a_normalizes_100x_monthly_revenue_mom_pct(self) -> None:
        df_rev = pd.DataFrame(
            [
                {"rev_ym": "2026-08", "revenue": 82_247_374_000, "mom_pct": "1147.00%", "yoy_pct": 45.66},
                {"rev_ym": "2026-09", "revenue": 88_130_084_000, "mom_pct": "715.00%", "yoy_pct": 45.52},
                {"rev_ym": "2026-10", "revenue": 88_615_136_000, "mom_pct": "55.00%", "yoy_pct": 47.13},
            ]
        )
        normalized = _normalize_monthly_revenue_pcts(df_rev)
        self.assertAlmostEqual(float(normalized.loc[1, "mom_pct"]), 7.15, places=2)
        self.assertAlmostEqual(float(normalized.loc[2, "mom_pct"]), 0.55, places=2)

    def test_bug_b_strategic_capex_expansion_exemption(self) -> None:
        statements = [
            {
                "year_qtr": "2026Q3",
                "rev_亿元": 2441.61,
                "gross_margin_pct": 22.10,
                "op_margin_pct": 13.57,
                "eps": 6.13,
                "ttm_eps": 17.55,
                "parent_roe_pct": 18.94,
                "ocf_亿元": 504.62,
                "capex_亿元": -1259.58,
                "fcf_亿元": -754.96,
                "ocf_to_ni_pct": 186.94,
                "qtr_rev_yoy_pct": 44.84,
            },
            {
                "year_qtr": "2026Q2",
                "rev_亿元": 1910.64,
                "gross_margin_pct": 21.00,
                "op_margin_pct": 11.04,
                "eps": 4.81,
                "ocf_亿元": 470.14,
                "capex_亿元": -815.11,
                "fcf_亿元": -344.98,
                "ocf_to_ni_pct": 223.32,
                "qtr_rev_yoy_pct": 26.74,
            },
        ]
        summary = compute_fundamental_features(
            "3711",
            "日月光投控",
            "IC封測",
            quarterly_statements=statements,
            monthly_revenues=[{"rev_ym": "2026-10", "revenue": 886.15e8, "yoy_pct": 47.13}],
            latest_close_price=598.0,
            paid_in_capital_twd=43.8e9,
        )
        self.assertIsNotNone(summary.fcf_semantics)
        assert summary.fcf_semantics is not None
        self.assertEqual(summary.fcf_semantics["tag"], "STRATEGIC_CAPEX_EXPANSION")
        self.assertTrue(summary.fcf_semantics["is_strategic_capex"])
        self.assertIn("STRATEGIC_CAPEX_EXPANSION", summary.prompt_block)

    def test_module1_r2_w_bottom_pullback_and_189ma_support(self) -> None:
        # 構造長線多頭 (189MA 上揚)，近 60 日高點 765，近 12 日左腳回測 189MA (~544, 最低 515)
        # 隨後反彈至 ~630 逼近下彎 17MA (~626)，今日量縮收 598，K9 > D9 且 K9 < 75，MACD OSC 改善
        closes = (
            [470.0] * 90
            + [545.0] * 60
            + [680.0] * 30
            + [750.0] * 8
            + [660.0, 620.0, 580.0, 545.0, 527.0, 561.0, 575.0, 590.0, 600.0, 608.0, 598.0]
        )
        highs = [c + 5.0 for c in closes]
        lows = [c - 5.0 for c in closes]
        # 左腳最低 515 落在倒數第 7 根 (527 那天)
        lows[-7] = 515.0
        # 反彈高點 630 落在倒數第 2 根 (608 那天)
        highs[-2] = 630.0
        vols = [30_000_000] * (len(closes) - 1) + [15_000_000]  # 最後一日量縮

        df = pd.DataFrame(
            {
                "stock_id": ["3711"] * len(closes),
                "stock_name": ["日月光投控"] * len(closes),
                "date": pd.date_range("2026-01-01", periods=len(closes), freq="B"),
                "close": closes,
                "max": highs,
                "min": lows,
                "Trading_Volume": vols,
            }
        )
        analyzed = add_moving_averages(df)
        self.assertIn("K9", analyzed.columns)
        self.assertIn("D9", analyzed.columns)
        self.assertIn("MACD_OSC", analyzed.columns)

        sig = classify_rebound_signal(analyzed)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig["rebound_subtype"], "W_BOTTOM_PULLBACK_SETUP")
        self.assertEqual(sig["support_ma"], "189MA")

        setup = compute_rebound_trade_setup(
            analyzed,
            support_ma=str(sig["support_ma"]),
            support_price=float(sig["support_price"]),
            rebound_subtype=str(sig["rebound_subtype"]),
        )
        self.assertIsNotNone(setup)
        assert setup is not None
        self.assertLess(setup["hard_stop_loss"], 515.0)
        self.assertGreaterEqual(setup["ideal_buy_zone_high"], setup["ideal_buy_zone_low"])
        self.assertIn("anchor_tiers", setup)

    def test_module1_r1_rejects_falling_knife_macd_kd(self) -> None:
        # 連跌多日導致 OSC[t] < OSC[t-1] < 0 且 K9 < D9 時，即使收盤強度 >= 0.55 也不接刀
        closes = [85.0] * 80 + [100.0] * 85 + [105.0] * 30 + [126.0] * 15 + [123.0, 120.0, 117.0, 114.0, 111.0]
        highs = [c + 1.0 for c in closes]
        lows = [c - 2.0 for c in closes]
        df = pd.DataFrame(
            {
                "date": pd.date_range("2025-06-01", periods=len(closes), freq="B"),
                "close": closes,
                "max": highs,
                "min": lows,
                "Trading_Volume": [2_000_000] * len(closes),
            }
        )
        analyzed = add_moving_averages(df)
        self.assertIsNone(classify_rebound_signal(analyzed))

    def test_module2_psychological_magnet_snap(self) -> None:
        self.assertEqual(_snap_to_psychological_support(547.58), 550.0)
        self.assertEqual(_snap_to_psychological_support(99.2), 100.0)
        self.assertEqual(_snap_to_psychological_support(530.0), 530.0)

    def test_module3_hard_warning_exemption_and_prefilter(self) -> None:
        exempt_metrics = {
            "vol_lots": 20000,
            "op_margin_pct": 13.57,
            "contract_liab": 0.0,
            "three_inst_net_5d_lots": -5000.0,
            "trust_net_buy_5d_lots": 8274.0,
            "margin_chg_5d_lots": 1000.0,
            "margin_utilization_pct": 35.0,
            "tdcc_1000_lots_pct": 80.94,
        }
        self.assertEqual(build_hard_warnings(exempt_metrics), [])

        candidates = pd.DataFrame(
            [
                {
                    "stock_id": f"{1000 + i}",
                    "rebound_subtype": "W_BOTTOM_PULLBACK_SETUP" if i == 0 else "REBOUND_TOUCH_HOLD",
                    "K9": 45.0 if i == 0 else 20.0,
                    "D9": 35.0 if i == 0 else 30.0,
                    "MACD_OSC": 0.2 if i == 0 else -1.5,
                    "potential_return_pct": 12.0 if i == 0 else 6.0,
                }
                for i in range(15)
            ]
        )
        top_df, skipped = prefilter_rebound_candidates(candidates, max_candidates=12)
        self.assertEqual(len(top_df), 12)
        self.assertEqual(skipped, 3)
        self.assertEqual(top_df.iloc[0]["stock_id"], "1000")

    def test_module4_rebound_prompt_and_audit_preserve_ai_tiers(self) -> None:
        prompt = _apply_portfolio_prompt_overrides(
            PROMPT_TEMPLATES["portfolio_selection"],
            selection_mode="rebound",
        )
        self.assertIn("189MA", prompt)
        self.assertIn("三層結構錨點選單", prompt)
        self.assertIn("賣壓歸因與錯殺辨識", prompt)

        decision = {
            "top_picks": [
                {
                    "stock_id": "3711",
                    "target_price_1": "625.00（17MA反壓）",
                    "target_price_2": "680.00（前波起跌點）",
                    "stop_loss_price": "512.00（破515左腳低點）",
                }
            ],
            "watchlist": [],
            "avoid": [],
        }
        audit_records = {
            "3711": {
                "TP1_Target_Or_POC": 620.0,
                "Hard_Stop_Loss": 510.0,
                "Ideal_Buy_Or_Ref": "548.0~552.0",
            }
        }
        updated = apply_audit_to_decision(decision, audit_records, selection_mode="rebound")
        self.assertEqual(updated["top_picks"][0]["target_price_1"], "625.00（17MA反壓）")
        self.assertEqual(updated["top_picks"][0]["stop_loss_price"], "512.00（破515左腳低點）")
        self.assertIn("audit", updated["top_picks"][0])

    def test_module4_rebound_watchlist_rows_include_audit_and_deterministic_setup(self) -> None:
        from finmind_stock_scanner import (
            REBOUND_WATCHLIST_HEADERS,
            build_rebound_watchlist_rows,
            scan_rebound_from_prices,
        )

        closes = (
            [470.0] * 90
            + [545.0] * 60
            + [680.0] * 30
            + [750.0] * 8
            + [660.0, 620.0, 580.0, 545.0, 527.0, 561.0, 575.0, 590.0, 600.0, 608.0, 598.0]
        )
        highs = [c + 5.0 for c in closes]
        lows = [c - 5.0 for c in closes]
        lows[-7] = 515.0
        highs[-2] = 630.0
        vols = [30_000_000] * (len(closes) - 1) + [15_000_000]
        df = pd.DataFrame(
            {
                "stock_id": ["3711"] * len(closes),
                "stock_name": ["日月光投控"] * len(closes),
                "industry_category": ["半導體業"] * len(closes),
                "market": ["上市"] * len(closes),
                "date": pd.date_range("2026-01-01", periods=len(closes), freq="B"),
                "close": closes,
                "max": highs,
                "min": lows,
                "Trading_Volume": vols,
            }
        )
        reb_df, _ = scan_rebound_from_prices(df)
        self.assertEqual(len(reb_df), 1)
        rows = build_rebound_watchlist_rows(
            reb_df,
            audit_records={
                "3711": {
                    "Audit_Grade": "A_TIER_PULLBACK",
                    "Execution_Tactic": "STYLE_B_PULLBACK_LIMIT",
                    "Ideal_Buy_Or_Ref": "548.0~552.0",
                    "Chase_Limit_Price": 552.0,
                    "Hard_Stop_Loss": 512.0,
                    "TP1_Target_Or_POC": 626.0,
                    "Deterministic_RR": 1.95,
                    "Bear_Case_Warning": "法說短線波動",
                    "Invalidation_Triggers": ["收盤跌破 512 元"],
                }
            },
        )
        self.assertEqual(len(rows), 1)
        r = rows[0]
        for col in REBOUND_WATCHLIST_HEADERS:
            self.assertIn(col, r)
        self.assertEqual(r["Ideal_Buy_Or_Ref"], "548.0~552.0")
        self.assertEqual(r["Chase_Limit_Price"], 552.0)
        self.assertEqual(r["Hard_Stop_Loss"], 512.0)
        self.assertEqual(r["TP1_Target_Or_POC"], 626.0)
        self.assertEqual(r["Audit_Grade"], "A_TIER_PULLBACK")

    def test_v61_strategic_tech_and_chip_package_and_support_locator(self) -> None:
        from finmind_stock_scanner import build_market_daily_trend_block, build_strategic_tech_and_chip_package

        dates = pd.date_range("2026-08-01", periods=65, freq="B")
        closes = [2500.0 + i * 4.0 for i in range(65)]
        opens = [c - 10.0 for c in closes]
        highs = [c + 20.0 for c in closes]
        lows = [c - 30.0 for c in closes]
        vols = [2_500_000] * 65
        vols[-10] = 6_500_000  # High-volume bullish support bar
        df_p = pd.DataFrame(
            {
                "stock_id": ["3017"] * 65,
                "date": dates,
                "open": opens,
                "max": highs,
                "min": lows,
                "close": closes,
                "Trading_Volume": vols,
            }
        )
        df_chip = pd.DataFrame(
            {
                "date": [d.strftime("%Y-%m-%d") for d in dates[-25:]],
                "foreign_net_lots": [120.0] * 25,
                "trust_net_lots": [350.0] * 25,
                "dealer_net_lots": [-10.0] * 25,
                "margin_chg_lots": [-25.0] * 25,
                "margin_balance_lots": [2900.0] * 25,
            }
        )
        pkg = build_strategic_tech_and_chip_package(
            "3017",
            "奇鋐",
            df_p,
            daily_chip_df=df_chip,
            forward_eps=115.0,
            peer_median_pe=20.0,
            annual_cash_dividend=20.88,
            selection_mode="trend",
        )
        block = str(pkg["prompt_block"])
        self.assertIn("Python 大量支撐定位器", block)
        self.assertIn("下檔大量支撐區 #1", block)
        self.assertIn("法人買超持倉成本錨點 (VWAP)", block)
        self.assertIn("近 20 個交易日「價、量、五線歷史均線、KD/RSI/MACD、三大法人、融資」逐日對齊明細表", block)
        self.assertIsNotNone(pkg["trust_vwap_20d"])
        self.assertIsNotNone(pkg["foreign_vwap_20d"])

        df_mkt = df_p.copy()
        df_mkt["stock_id"] = "0050"
        mkt_block = build_market_daily_trend_block(df_mkt, benchmark_id="0050", days=10)
        self.assertIn("台股大盤基準 (0050) 近 10 交易日", mkt_block)

    def test_v61_quality_selection_and_strategic_entry_planner_batch(self) -> None:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from quant.alignment_ai_picker import (
            parse_quality_selection_json,
            parse_strategic_layout_json,
            run_quality_selection,
            run_strategic_entry_planner,
        )

        q_json = (
            '{"selection_summary": "散熱龍頭基本面強勁", '
            '"quality_picks": [{"rank": 1, "stock_id": "3017", "stock_name": "奇鋐", '
            '"sector": "散熱", "quality_score": 92, "quality_grade": "S_TIER_CORE", '
            '"outlook_verdict": "高度可信", "fundamental_moat_and_catalysts": "三率連增", '
            '"chip_and_institutional_backing": "投信鎖碼", "fundamental_invalidation": "營收衰退"}], '
            '"watchlist": [], "avoid": []}'
        )
        s_json = (
            '{"market_light": "🟡黃燈", "recommended_total_exposure_pct": 60, '
            '"market_comment": "美股費半穩健，建議總倉位60%分批低接", '
            '"strategic_picks": [{"rank": 1, "stock_id": "3017", "stock_name": "奇鋐", '
            '"sector": "散熱", "score": 90, "allocation_weight_pct": 50, '
            '"tactical_type": "🎯 強勢股拉回三階低接", '
            '"current_tech_and_chip_diagnosis": "等待回測17MA與投信成本區", '
            '"tier1_entry": "【第1批 30% 試單】2,680~2,700元（回踩17MA）", '
            '"tier2_entry": "【第2批 40% 核心】2,600~2,630元（投信成本與大量區）", '
            '"tier3_entry": "【第3批 30% 黃金坑】2,500~2,540元（季線防守）", '
            '"weighted_avg_entry_price": "2,612.00", "chase_limit_price": "2,720.00", '
            '"target_price_1": "2,950.00", "target_price_2": "3,200.00", '
            '"stop_loss_price": "2,420.00", "invalidation_rule": "跌破季線且投信連賣"}]}'
        )
        with patch(
            "quant.alignment_ai_picker.run_vertex_batch_requests",
            new=AsyncMock(return_value={"QUALITY_SELECT_STAGE2": SimpleNamespace(text=q_json)}),
        ) as mock_q_batch:
            asyncio.run(
                run_quality_selection(
                    ["dossier 3017"],
                    max_top=5,
                    selection_mode="trend",
                    use_vertex_batch=True,
                    task_type="AI_PICK_QUALITY_SELECT",
                )
            )
            _, q_kwargs = mock_q_batch.call_args
            self.assertEqual(q_kwargs["task_type"], "AI_PICK_QUALITY_SELECT")

        parsed_q = parse_quality_selection_json(q_json, {"3017"}, 5)
        self.assertIsNotNone(parsed_q)
        self.assertEqual(parsed_q["quality_picks"][0]["stock_id"], "3017")

        with patch(
            "quant.alignment_ai_picker.run_vertex_batch_requests",
            new=AsyncMock(return_value={"STRATEGIC_LAYOUT_STAGE3": SimpleNamespace(text=s_json)}),
        ) as mock_s_batch:
            asyncio.run(
                run_strategic_entry_planner(
                    ["strategic block 3017"],
                    macro_block="SOX +1.5%",
                    market_daily_block="TAIEX 10d table",
                    selection_mode="rebound",
                    use_vertex_batch=True,
                    task_type="REBOUND_STRATEGIC_LAYOUT",
                )
            )
            s_args, s_kwargs = mock_s_batch.call_args
            self.assertEqual(s_kwargs["task_type"], "REBOUND_STRATEGIC_LAYOUT")
            prompt_sent = s_args[0]["STRATEGIC_LAYOUT_STAGE3"]
            self.assertIn("17MA 是上方反壓（TP1），絕非支撐", prompt_sent)
            self.assertIn("SOX +1.5%", prompt_sent)

        parsed_s = parse_strategic_layout_json(s_json, {"3017"}, 5)
        self.assertIsNotNone(parsed_s)
        self.assertEqual(parsed_s["recommended_total_exposure_pct"], 60)
        self.assertEqual(parsed_s["strategic_picks"][0]["allocation_weight_pct"], 50)
        self.assertIn("2,680~2,700", parsed_s["strategic_picks"][0]["tier1_entry"])


