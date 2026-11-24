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
        self.assertIn("近 20 個交易日「價、量、五線歷史均線、KD/RSI/MACD、外資與投信、融資」逐日對齊明細表", block)
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

    def test_v61_execute_pipeline_end_to_end_and_batch_timeout_2h(self) -> None:
        import asyncio
        from datetime import date
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        import services.ai_service as ai_service
        from quant.alignment_ai_picker import (
            execute_v61_quality_and_strategic_pipeline,
            resolve_batch_timeout,
        )

        self.assertEqual(ai_service.get_vertex_batch_timeout_seconds(), 7200.0)
        self.assertEqual(resolve_batch_timeout({"vertex_batch_timeout_seconds": 7200}), 7200.0)

        with TemporaryDirectory() as tmpdir:
            daily_dir = Path(tmpdir) / "daily"
            daily_dir.mkdir(parents=True, exist_ok=True)
            dates = pd.date_range("2026-08-01", periods=65, freq="B")
            closes = [700.0 + i for i in range(65)]
            prices_df = pd.DataFrame(
                {
                    "stock_id": ["2379"] * 65 + ["TAIEX"] * 65,
                    "stock_name": ["瑞昱"] * 65 + ["加權指數"] * 65,
                    "date": list(dates) + list(dates),
                    "open": [c - 2.0 for c in closes] * 2,
                    "max": [c + 5.0 for c in closes] * 2,
                    "min": [c - 5.0 for c in closes] * 2,
                    "close": closes * 2,
                    "Trading_Volume": [3_000_000] * 130,
                }
            )
            prices_df.to_csv(daily_dir / "2026-11-17_prices.csv", index=False)

            analyzed = pd.DataFrame(
                [
                    {
                        "stock_id": "2379",
                        "stock_name": "瑞昱",
                        "market": "上市",
                        "industry_category": "半導體業",
                        "sub_industry": "網通與多媒體IC設計",
                        "close": 764.0,
                        "MA3": 762.0,
                        "MA17": 755.0,
                        "MA45": 749.0,
                        "MA117": 721.0,
                        "MA189": 635.0,
                        "vol_lots": 4107,
                        "vol_ma5_lots": 2591,
                        "vol_ma20_lots": 3287,
                        "vol_status": "📈 溫和放量",
                    }
                ]
            )
            stage1_results = {"2379": {"snapshot": "snap 2379", "summary": '{"score": 88}'}}

            q_json = (
                '{"selection_summary": "網通IC龍頭", '
                '"quality_picks": [{"rank": 1, "stock_id": "2379", "stock_name": "瑞昱", '
                '"sector": "網通與多媒體IC設計", "quality_score": 88, "quality_grade": "S_TIER_CORE", '
                '"outlook_verdict": "高度可信", "fundamental_moat_and_catalysts": "三率穩健", '
                '"chip_and_institutional_backing": "投信買超", "fundamental_invalidation": "營收轉弱"}], '
                '"watchlist": [], "avoid": []}'
            )
            s_json = (
                '{"market_light": "🔴紅燈", "recommended_total_exposure_pct": 40, '
                '"market_comment": "美股偏弱，建議總持倉40%等回檔", '
                '"strategic_picks": [{"rank": 1, "stock_id": "2379", "stock_name": "瑞昱", '
                '"sector": "網通與多媒體IC設計", "score": 88, "allocation_weight_pct": 100, '
                '"tactical_type": "🎯 型態A長均線三階低接", '
                '"current_tech_and_chip_diagnosis": "等待回測117MA", '
                '"tier1_entry": "【第1批 30% 試單】718~722元", '
                '"tier2_entry": "【第2批 40% 核心】695~705元", '
                '"tier3_entry": "【第3批 30% 黃金坑】650~665元", '
                '"weighted_avg_entry_price": "695.00", "chase_limit_price": "725.00", '
                '"target_price_1": "820.00", "target_price_2": "880.00", '
                '"stop_loss_price": "630.00", "invalidation_rule": "跌破630元"}]}'
            )

            async def fake_batch(requests_map, **kwargs):
                self.assertEqual(kwargs.get("timeout_seconds"), 7200.0)
                if "QUALITY_SELECT_STAGE2" in requests_map:
                    return {"QUALITY_SELECT_STAGE2": SimpleNamespace(text=q_json, model_version="gemini-4-argon")}
                return {"STRATEGIC_LAYOUT_STAGE3": SimpleNamespace(text=s_json, model_version="gemini-4-argon")}

            cache = MagicMock()
            cache.get_daily_chip_history.return_value = pd.DataFrame()
            analyzer = MagicMock()
            analyzer.cache._read_stock_rows.return_value = pd.DataFrame()
            analyzer.compute_cross_sectional_pe_map.return_value = {}
            analyzer._fetch_yahoo_stock_rss.return_value = []

            with (
                patch("quant.alignment_ai_picker.run_vertex_batch_requests", side_effect=fake_batch),
                patch(
                    "taishin.macro_context.fetch_global_macro_context",
                    return_value={"prompt_block": "- 國際盤氣候：🔴 國際逆風警戒"},
                ) as mock_macro,
                patch("taishin_portfolio_service.load_portfolio_exposure_inputs", return_value=([], {})),
                patch("finmind_stock_scanner.fetch_or_cache_tdcc_holding", return_value=None),
                patch("services.news_intel_service.get_stock_intel", side_effect=RuntimeError("skip_net_in_test")),
            ):
                decision, audit_records, _raw, model_ver = asyncio.run(
                    execute_v61_quality_and_strategic_pipeline(
                        analyzed,
                        {"market_benchmark_id": "TAIEX", "enable_ex_dividend_compensation": False},
                        target_date=date(2026, 11, 17),
                        daily_dir=daily_dir,
                        cache=cache,
                        analyzer=analyzer,
                        stage1_results=stage1_results,
                        market_regime={"light": "🔴紅燈", "summary": "🔴紅燈"},
                        max_top=5,
                        model="gemini-4-argon",
                        use_vertex_batch=True,
                        poll_interval=60.0,
                        timeout_seconds=7200.0,
                        selection_mode="rebound",
                        dry_run=True,
                    )
                )

            mock_macro.assert_called_once()
            _, macro_kwargs = mock_macro.call_args
            self.assertEqual(macro_kwargs.get("context_mode"), "post_market")
            self.assertTrue(bool(macro_kwargs.get("tw_latest_date")))
            self.assertIsNotNone(decision)
            self.assertEqual(model_ver, "gemini-4-argon")
            self.assertEqual(len(decision["top_picks"]), 1)
            self.assertEqual(decision["top_picks"][0]["stock_id"], "2379")
            self.assertEqual(audit_records["2379"]["Allocation_Weight_Pct"], 100)
            self.assertIn("718~722", audit_records["2379"]["Tier1_Entry_Plan"])

    def test_broken_ma_trend_stock_tiers_never_invert_and_include_forward_eps(self) -> None:
        """驗證急殺跌破 17MA/45MA 之標的：三階低接價嚴格遞減不倒掛、輸出前瞻 EPS 明數且不寫三大法人。"""
        from finmind_stock_scanner import build_market_daily_trend_block, build_strategic_tech_and_chip_package

        dates = pd.date_range("2026-06-01", periods=100, freq="B")
        # 前 99 天緩步上漲至 800，最後一天暴跌至 750（跌破 MA3/MA17/MA45，模擬 3293 鈊象 11/20）
        closes = [700.0 + i * 1.0 for i in range(99)] + [750.0]
        df_stock = pd.DataFrame({
            "stock_id": ["3293"] * 100,
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "open": [c + 2.0 for c in closes],
            "max": [c + 10.0 for c in closes],
            "min": [c - 8.0 for c in closes],
            "close": closes,
            "Trading_Volume": [2_000_000] * 100,
        })
        pkg = build_strategic_tech_and_chip_package(
            "3293",
            "鈊象",
            df_stock,
            forward_eps=46.24,
            peer_median_pe=20.0,
            annual_cash_dividend=36.0,
            selection_mode="alignment",
        )
        # 嚴格單調遞減：hard_stop_loss < tier3 < tier2 < tier1 <= close_t
        self.assertLessEqual(pkg["tier1_price"], pkg["close_t"])
        self.assertLess(pkg["tier2_price"], pkg["tier1_price"])
        self.assertLess(pkg["tier3_price"], pkg["tier2_price"])
        self.assertLess(pkg["hard_stop_loss"], pkg["tier3_price"])

        prompt_block = str(pkg["prompt_block"])
        self.assertIn("破線防禦", prompt_block)
        self.assertIn("本業前瞻EPS=46.24元", prompt_block)
        self.assertIn("外資與投信", prompt_block)
        self.assertNotIn("三大法人", prompt_block)
        self.assertNotRegex(prompt_block, r"(?<!\d)5MA")

        # 驗證 build_market_daily_trend_block 在無 OTC 時明確聲明、有 OTC 時附摘要
        df_taiex = pd.DataFrame({
            "stock_id": ["TAIEX"] * 30,
            "date": [d.strftime("%Y-%m-%d") for d in dates[-30:]],
            "open": [38000.0] * 30,
            "max": [38500.0] * 30,
            "min": [37500.0] * 30,
            "close": [38000.0 - i * 50.0 for i in range(30)],
            "Trading_Volume": [1_000_000] * 30,
        })
        blk_no_otc = build_market_daily_trend_block(df_taiex, benchmark_id="TAIEX", days=5)
        self.assertIn("無 OTC 櫃買指數日線數據", blk_no_otc)

        df_otc = df_taiex.copy()
        df_otc["stock_id"] = "OTC"
        df_otc["close"] = [250.0 - i * 0.5 for i in range(30)]
        blk_with_otc = build_market_daily_trend_block(
            pd.concat([df_taiex, df_otc], ignore_index=True),
            benchmark_id="TAIEX",
            days=5,
        )
        self.assertIn("櫃買指數 (OTC)", blk_with_otc)

    def test_post_market_macro_context_adds_us_date_and_no_double_counting_note(self) -> None:
        """驗證盤後選股呼叫 fetch_global_macro_context 時標註美東收盤日與防重複計價提醒，且不含早盤用語。"""
        from unittest.mock import patch
        from taishin.macro_context import fetch_global_macro_context

        fake_quotes = {
            "^SOX": {"symbol": "^SOX", "price": 5000.0, "prev_close": 5123.5, "change": -123.5, "change_pct": -2.41, "as_of_date": "2026-11-19"},
            "^IXIC": {"symbol": "^IXIC", "price": 18000.0, "prev_close": 18414.0, "change": -414.0, "change_pct": -2.25, "as_of_date": "2026-11-19"},
            "^VIX": {"symbol": "^VIX", "price": 30.15, "prev_close": 28.44, "change": 1.71, "change_pct": 6.01, "as_of_date": "2026-11-19"},
        }
        from services.news_intel_service import MacroIntelCard

        with (
            patch(
                "taishin.macro_context.fetch_yahoo_quote_snapshot",
                side_effect=lambda sym, session=None: fake_quotes.get(sym),
            ),
            patch(
                "services.news_intel_service.get_macro_intel",
                return_value=MacroIntelCard(as_of="2026-11-20T00:00:00+08:00", mode="empty"),
            ),
        ):
            ctx = fetch_global_macro_context(
                context_mode="post_market",
                tw_latest_date="2026-11-20",
            )
        blk = ctx["prompt_block"]
        self.assertEqual(ctx["us_as_of_date"], "2026-11-19")
        self.assertIn("美東交易日 2026-11-19 收盤", blk)
        self.assertIn("時間軸防重複計價提醒", blk)
        self.assertNotIn("早盤嚴防", blk)

    def test_rs_beta_and_tdcc_multiweek_summary(self) -> None:
        """驗證 RS/60日Beta 純本地計算與近 8~12 週集保大戶動態門檻濃縮摘要。"""
        from finmind_stock_scanner import (
            build_strategic_tech_and_chip_package,
            compute_relative_strength_and_beta,
            summarize_tdcc_multiweek_trend,
        )

        dates = pd.date_range("2026-07-01", periods=80, freq="B")
        # 大盤下跌，個股逆勢緩漲 -> 正超額報酬 + 低/負 Beta
        bench_closes = [40000.0 - i * 40.0 + (200.0 if i % 2 == 0 else -150.0) for i in range(80)]
        stock_closes = [700.0 + i * 0.8 + (1.0 if i % 2 == 0 else -0.5) for i in range(80)]
        df_bench = pd.DataFrame({
            "stock_id": ["TAIEX"] * 80,
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "open": bench_closes,
            "max": [c + 100.0 for c in bench_closes],
            "min": [c - 100.0 for c in bench_closes],
            "close": bench_closes,
            "Trading_Volume": [1_000_000] * 80,
        })
        df_stock = pd.DataFrame({
            "stock_id": ["3293"] * 80,
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "open": stock_closes,
            "max": [c + 5.0 for c in stock_closes],
            "min": [c - 5.0 for c in stock_closes],
            "close": stock_closes,
            "Trading_Volume": [2_000_000] * 80,
        })

        rs_info = compute_relative_strength_and_beta(df_stock, df_bench)
        self.assertIsNotNone(rs_info)
        self.assertGreater(rs_info["rs_5d_pct"], 0.0)
        self.assertGreater(rs_info["rs_20d_pct"], 0.0)
        self.assertIsNotNone(rs_info["beta_60d"])

        # 建立 9 週集保資料（每週五），模擬 400 張以上大戶連 3 週增、10 張以下散戶降、股東人數降
        tdcc_dates = pd.date_range("2026-09-25", periods=9, freq="W-FRI")
        major_series = [65.0, 65.2, 65.4, 65.8, 66.2, 66.0, 66.8, 67.5, 68.4]
        retail_series = [14.0, 13.8, 13.6, 13.4, 13.2, 13.0, 12.7, 12.4, 12.1]
        people_series = [26000, 25800, 25600, 25500, 25360, 25100, 24900, 24700, 24510]
        tdcc_rows = []
        for dt, m_pct, r_pct, ppl in zip(tdcc_dates, major_series, retail_series, people_series):
            ds = dt.strftime("%Y-%m-%d")
            # 10張以下散戶 (lvl 1+2+3)
            tdcc_rows.append({"date": ds, "HoldingSharesLevel": "1", "percent": round(r_pct * 0.4, 2), "people": 10000})
            tdcc_rows.append({"date": ds, "HoldingSharesLevel": "2", "percent": round(r_pct * 0.4, 2), "people": 8000})
            tdcc_rows.append({"date": ds, "HoldingSharesLevel": "3", "percent": round(r_pct * 0.2, 2), "people": 3000})
            # 400~1000張 (lvl 12) + 1000張以上 (lvl 15)
            tdcc_rows.append({"date": ds, "HoldingSharesLevel": "12", "percent": 18.4, "people": 30})
            tdcc_rows.append({"date": ds, "HoldingSharesLevel": "15", "percent": round(m_pct - 18.4, 2), "people": 15})
            tdcc_rows.append({"date": ds, "HoldingSharesLevel": "17", "percent": 100.0, "people": ppl})
        tdcc_df = pd.DataFrame(tdcc_rows)

        # 高價股 (754元) -> 強制採 400 張門檻
        tdcc_high_price = summarize_tdcc_multiweek_trend(
            tdcc_df, paid_in_capital_twd=10_000_000_000, close_price=754.0
        )
        self.assertEqual(tdcc_high_price["major_threshold_lots"], 400)
        self.assertEqual(tdcc_high_price["tdcc_base_date"], "2026-11-20")
        self.assertIn("基準日 2026-11-20", tdcc_high_price["summary"])
        self.assertIn("400張以上大戶 68.4%", tdcc_high_price["summary"])
        self.assertIn("連 3 週增", tdcc_high_price["summary"])
        self.assertIn("近4週 -1.1%", tdcc_high_price["summary"])
        self.assertIn("近4週 -850 人", tdcc_high_price["summary"])

        # 大股本 (>=50億) 且低價 (<=200元) -> 採 1000 張門檻
        tdcc_large_cap = summarize_tdcc_multiweek_trend(
            tdcc_df, paid_in_capital_twd=10_000_000_000, close_price=80.0
        )
        self.assertEqual(tdcc_large_cap["major_threshold_lots"], 1000)

        pkg = build_strategic_tech_and_chip_package(
            "3293",
            "鈊象",
            df_stock,
            benchmark_prices=df_bench,
            tdcc_holding_df=tdcc_df,
            paid_in_capital_twd=2_800_000_000,
            selection_mode="alignment",
        )
        self.assertIn("相對大盤強弱度 (RS / 60日Beta)", str(pkg["prompt_block"]))
        self.assertIn("集保大戶籌碼", str(pkg["prompt_block"]))

    def test_wait_observe_and_low_rr_forces_zero_weight_cash_reserve(self) -> None:
        """驗證 AI #3 輸出 WAIT_OBSERVE 或 R/R < 1.5 時自動歸零權重保留現金觀望。"""
        import asyncio
        import json
        import tempfile
        from datetime import date
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from quant.alignment_ai_picker import execute_v61_quality_and_strategic_pipeline

        with tempfile.TemporaryDirectory() as tmpdir:
            daily_dir = Path(tmpdir) / "daily"
            daily_dir.mkdir(parents=True, exist_ok=True)
            dates = pd.date_range("2026-08-01", periods=65, freq="B")
            pd.DataFrame({
                "stock_id": ["3293"] * 65,
                "date": [d.strftime("%Y-%m-%d") for d in dates],
                "open": [760.0] * 65,
                "max": [770.0] * 65,
                "min": [745.0] * 65,
                "close": [750.0] * 65,
                "Trading_Volume": [2_000_000] * 65,
            }).to_csv(daily_dir / "2026-11-20_prices.csv", index=False)

            analyzed = pd.DataFrame([{
                "stock_id": "3293", "stock_name": "鈊象", "industry_category": "遊戲",
                "sub_industry": "遊戲", "close": 750.0, "MA17": 778.0, "MA45": 772.0, "MA117": 750.0,
            }])
            q_json = json.dumps({
                "selection_summary": "ok",
                "quality_picks": [{
                    "rank": 1, "stock_id": "3293", "stock_name": "鈊象", "sector": "遊戲",
                    "quality_score": 92, "quality_grade": "S_TIER_CORE", "outlook_verdict": "無資料",
                    "fundamental_moat_and_catalysts": "高毛利", "chip_and_institutional_backing": "外資買",
                    "fundamental_invalidation": "毛利下滑",
                }],
                "watchlist": [], "avoid": [],
            }, ensure_ascii=False)
            # 測試 R/R < 1.5（三批均價約 730，TP1=750 獲利 +20，SL=700 風險 -30 -> R/R = 0.67 < 1.5）
            s_json = json.dumps({
                "market_light": "🔴紅燈",
                "recommended_total_exposure_pct": 30,
                "market_comment": "空頭防禦",
                "strategic_picks": [{
                    "rank": 1, "stock_id": "3293", "stock_name": "鈊象", "sector": "遊戲",
                    "score": 85, "action": "BUY_3TIER", "allocation_weight_pct": 50,
                    "tactical_type": "🛡️ 破線防禦",
                    "current_tech_and_chip_diagnosis": "破線",
                    "tier1_entry": "740", "tier2_entry": "730", "tier3_entry": "720",
                    "weighted_avg_entry_price": "730", "chase_limit_price": "750",
                    "target_price_1": "750", "target_price_2": "800", "stop_loss_price": "700",
                    "invalidation_rule": "破700停損",
                }],
            }, ensure_ascii=False)

            async def fake_batch(requests_map, **kwargs):
                if "QUALITY_SELECT_STAGE2" in requests_map:
                    return {"QUALITY_SELECT_STAGE2": SimpleNamespace(text=q_json, model_version="gemini-4-argon")}
                return {"STRATEGIC_LAYOUT_STAGE3": SimpleNamespace(text=s_json, model_version="gemini-4-argon")}

            cache = MagicMock()
            cache.get_daily_chip_history.return_value = pd.DataFrame()
            analyzer = MagicMock()
            analyzer.cache._read_stock_rows.return_value = pd.DataFrame()
            analyzer.compute_cross_sectional_pe_map.return_value = {}
            analyzer._fetch_yahoo_stock_rss.return_value = []

            with (
                patch("quant.alignment_ai_picker.run_vertex_batch_requests", side_effect=fake_batch),
                patch("taishin.macro_context.fetch_global_macro_context", return_value={"prompt_block": ""}),
                patch("taishin_portfolio_service.load_portfolio_exposure_inputs", return_value=([], {})),
                patch("finmind_stock_scanner.fetch_or_cache_tdcc_holding", return_value=None),
                patch("services.news_intel_service.get_stock_intel", side_effect=RuntimeError("skip_net_in_test")),
            ):
                _dec, audit_records, _raw, _mv = asyncio.run(
                    execute_v61_quality_and_strategic_pipeline(
                        analyzed,
                        {"market_benchmark_id": "TAIEX", "enable_ex_dividend_compensation": False},
                        target_date=date(2026, 11, 20),
                        daily_dir=daily_dir,
                        cache=cache,
                        analyzer=analyzer,
                        stage1_results={"3293": {"score": 90}},
                        market_regime={"light": "🔴紅燈", "summary": "🔴紅燈"},
                        max_top=5,
                        model="gemini-4-argon",
                        use_vertex_batch=True,
                        poll_interval=60.0,
                        timeout_seconds=7200.0,
                        selection_mode="alignment",
                        dry_run=True,
                    )
                )

            self.assertEqual(audit_records["3293"]["Execution_Tactic"], "WAIT_OBSERVE")
            self.assertEqual(audit_records["3293"]["Allocation_Weight_Pct"], 0)
            self.assertIn("保留現金觀望", str(audit_records["3293"]["Ideal_Buy_Or_Ref"]))

    def test_lower_support_poc_strictly_below_close_and_dump_prompt_only_zero_ai(self) -> None:
        """驗證下檔大量支撐區嚴格低於現價、不印 X~X 重複區間，且 --dump-prompt-only 0-Token 產出完整 Prompt。"""
        import json
        import re
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from finmind_stock_scanner import build_strategic_tech_and_chip_package
        from quant.alignment_ai_picker import dump_strategic_layout_prompt_only

        dates = pd.date_range("2026-08-01", periods=65, freq="B")
        # 模擬 2207 和泰車：前面 64 天在 514~550 盤整，最後一天收 516
        closes = [530.0 + (i % 10) * 2.0 for i in range(64)] + [516.0]
        df_stock = pd.DataFrame({
            "stock_id": ["2207"] * 65,
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "open": [c + 1.0 for c in closes],
            "max": [c + 4.0 for c in closes],
            "min": [c - 3.0 for c in closes],
            "close": closes,
            "Trading_Volume": [500_000] * 65,
        })
        pkg = build_strategic_tech_and_chip_package("2207", "和泰車", df_stock, selection_mode="trend")
        for node_txt in pkg["lower_support_nodes"]:
            m = re.search(r"(\d+(?:\.\d+)?)~(\d+(?:\.\d+)?)\s*元", node_txt)
            if m:
                lo_v, hi_v = float(m.group(1)), float(m.group(2))
                self.assertNotEqual(lo_v, hi_v)
                self.assertLessEqual((lo_v + hi_v) / 2.0, 516.0)

        # 驗證 dump_strategic_layout_prompt_only 0-Token 產出完整 Prompt（含 AI #2 標頭與台新曝險）
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            daily_dir = root / "daily"
            daily_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = Path("finmind_scanner.json")
            df_taiex = df_stock.copy()
            df_taiex["stock_id"] = "TAIEX"
            df_taiex["close"] = [38000.0 - i * 30.0 for i in range(65)]
            pd.concat([df_stock, df_taiex], ignore_index=True).to_csv(
                daily_dir / "2026-11-20_prices.csv", index=False
            )

            with (
                patch("taishin.macro_context.fetch_global_macro_context", return_value={"prompt_block": "- 美股測試"}),
                patch("finmind_stock_scanner.fetch_or_load_market_regime", return_value={"summary": "🔴紅燈"}),
                patch("finmind_stock_scanner.fetch_or_cache_tdcc_holding", return_value=None),
                patch("taishin_portfolio_service.load_portfolio_exposure_inputs", return_value=([], {})),
                patch("quant.alignment_ai_picker.FundamentalCacheManager", side_effect=RuntimeError("offline")),
            ):
                out_md = dump_strategic_layout_prompt_only(
                    config_path=cfg_path,
                    target_date="2026-11-20",
                    selection_mode="trend",
                    stock_ids=["2207"],
                    output_dir=root,
                )

            self.assertTrue(out_md.is_file())
            txt = out_md.read_text(encoding="utf-8")
            self.assertIn("### 🏆 【入選績優股 #1】2207", txt)
            self.assertIn("台新帳戶庫存共伴曝險", txt)
            self.assertIn("極端股災熔斷", txt)
            self.assertIn("僅有單週快照", txt)

    def test_ai2_ai3_antibias_prompt_order_and_parser_bugfixes(self) -> None:
        """驗證解法二：AI #2/#3 陣列外統一排名配資、反方辯證接線、WAIT_OBSERVE+0% 保留與最終確定性重排。"""
        import asyncio
        import json
        import tempfile
        from datetime import date
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from quant.alignment_ai_picker import (
            execute_v61_quality_and_strategic_pipeline,
            parse_quality_selection_json,
            parse_strategic_layout_json,
        )
        from quant.fundamental_config import PROMPT_TEMPLATES

        # 1. 驗證 AI #2 Prompt JSON 範本：先跨股 PK 與反方辯證、最後打獨立分數，且各股不填 rank
        q_json_spec = PROMPT_TEMPLATES["quality_selection"].split("【輸出規定", 1)[1]
        self.assertLess(q_json_spec.index('"cross_stock_pk_reasoning"'), q_json_spec.index('"quality_picks"'))
        self.assertLess(q_json_spec.index('"bear_case_and_rebuttal"'), q_json_spec.index('"quality_score"'))
        self.assertNotIn('"rank"', q_json_spec)

        # 2. 驗證 AI #3 (順勢 & 反彈) Prompt JSON 範本：strategic_picks 內不放 rank/allocation_weight_pct，統一移至尾端 final_ranking_and_allocation
        for key in ("strategic_layout_trend", "strategic_layout_rebound"):
            s_json_spec = PROMPT_TEMPLATES[key].split("【輸出規定", 1)[1]
            self.assertLess(s_json_spec.index('"market_comment"'), s_json_spec.index('"recommended_total_exposure_pct"'))
            self.assertLess(s_json_spec.index('"current_tech_and_chip_diagnosis"'), s_json_spec.index('"tier1_entry"'))
            self.assertLess(s_json_spec.index('"stop_loss_price"'), s_json_spec.index('"rr_math_and_sizing_rationale"'))
            self.assertLess(s_json_spec.index('"rr_math_and_sizing_rationale"'), s_json_spec.index('"action"'))
            self.assertLess(s_json_spec.index('"strategic_picks"'), s_json_spec.index('"final_ranking_and_allocation"'))
            picks_segment = s_json_spec.split('"strategic_picks"', 1)[1].split('"final_ranking_and_allocation"', 1)[0]
            self.assertNotIn('"rank"', picks_segment)
            self.assertNotIn('"allocation_weight_pct"', picks_segment)
            alloc_segment = s_json_spec.split('"final_ranking_and_allocation"', 1)[1]
            self.assertIn('"rank"', alloc_segment)
            self.assertIn('"allocation_weight_pct"', alloc_segment)

        # 3. 驗證 parse_quality_selection_json 無 rank 時自動依 quality_score 降冪排序並重編 rank 1..N
        q_raw = json.dumps(
            {
                "cross_stock_pk_reasoning": "3293 獲利純度高於 2207，但兩者皆可入選。",
                "selection_summary": "兩檔皆為防禦型績優股",
                "quality_picks": [
                    {
                        "stock_id": "2207",
                        "stock_name": "和泰車",
                        "sector": "汽車",
                        "bear_case_and_rebuttal": "11/20 新聞標註疑似出貨實為大盤崩跌拖累且外資反買 196 張，豁免。",
                        "fundamental_moat_and_catalysts": "穩健高息龍頭",
                        "chip_and_institutional_backing": "外資買超",
                        "fundamental_invalidation": "車市銷量連季年減",
                        "outlook_verdict": "待驗證",
                        "quality_score": 88,
                        "quality_grade": "A_TIER_GROWTH",
                    },
                    {
                        "stock_id": "3293",
                        "stock_name": "鈊象",
                        "sector": "遊戲",
                        "bear_case_and_rebuttal": "單季 OCF/NI 降至 23% 屬稅款季節性波動，全年含金量無虞。",
                        "fundamental_moat_and_catalysts": "高毛利海外授權",
                        "chip_and_institutional_backing": "外資穩定持有",
                        "fundamental_invalidation": "毛利率跌破 95%",
                        "outlook_verdict": "高度可信",
                        "quality_score": 94,
                        "quality_grade": "S_TIER_CORE",
                    },
                ],
                "watchlist": [],
                "avoid": [],
            },
            ensure_ascii=False,
        )
        parsed_q = parse_quality_selection_json(q_raw, {"2207", "3293"}, 5)
        self.assertIsNotNone(parsed_q)
        self.assertIn("3293 獲利純度高於 2207", parsed_q["cross_stock_pk_reasoning"])
        self.assertEqual([p["stock_id"] for p in parsed_q["quality_picks"]], ["3293", "2207"])
        self.assertEqual([p["rank"] for p in parsed_q["quality_picks"]], [1, 2])
        self.assertIn("外資反買 196 張", parsed_q["quality_picks"][1]["bear_case_and_rebuttal"])

        # 4. 驗證 parse_strategic_layout_json 合併陣列尾端 final_ranking_and_allocation，並保留 WAIT_OBSERVE + 0%
        s_raw = json.dumps(
            {
                "market_comment": "大盤震盪偏空，總持倉控管 60%，僅布局風報比達標且籌碼強勢者。",
                "market_light": "🔴紅燈",
                "recommended_total_exposure_pct": 60,
                "strategic_picks": [
                    {
                        "stock_id": "3293",
                        "stock_name": "鈊象",
                        "sector": "遊戲",
                        "current_tech_and_chip_diagnosis": "系統性崩跌先觀望",
                        "tactical_type": "🛡️ 破線防禦／暫緩追價觀望",
                        "tier1_entry": "740",
                        "tier2_entry": "730",
                        "tier3_entry": "720",
                        "weighted_avg_entry_price": "730",
                        "chase_limit_price": "745",
                        "target_price_1": "820",
                        "target_price_2": "880",
                        "stop_loss_price": "700",
                        "invalidation_rule": "帶量跌破 700",
                        "rr_math_and_sizing_rationale": "(820-730)/(730-700)=3.0 雖達標，但 VIX>30 熔斷先觀望。",
                        "action": "WAIT_OBSERVE",
                        "score": 85,
                    },
                    {
                        "stock_id": "2207",
                        "stock_name": "和泰車",
                        "sector": "汽車",
                        "current_tech_and_chip_diagnosis": "低 Beta 抗跌且外資反買",
                        "tactical_type": "🎯 強勢股拉回三階低接（依附17MA/45MA與大量區）",
                        "tier1_entry": "512",
                        "tier2_entry": "502",
                        "tier3_entry": "492",
                        "weighted_avg_entry_price": "502",
                        "chase_limit_price": "518",
                        "target_price_1": "560",
                        "target_price_2": "590",
                        "stop_loss_price": "478",
                        "invalidation_rule": "帶量跌破 478",
                        "rr_math_and_sizing_rationale": "(560-502)/(502-478)=2.42 >= 1.5，可分批承接。",
                        "action": "BUY_3TIER",
                        "score": 90,
                    },
                ],
                "final_ranking_and_allocation": [
                    {
                        "stock_id": "2207",
                        "rank": 1,
                        "allocation_weight_pct": 60,
                        "sizing_note": "R/R 2.42 且低 Beta 抗跌，列為首選配置 60%。",
                    },
                    {
                        "stock_id": "3293",
                        "rank": 2,
                        "allocation_weight_pct": 0,
                        "sizing_note": "WAIT_OBSERVE 保留現金 0%。",
                    },
                ],
            },
            ensure_ascii=False,
        )
        parsed_s = parse_strategic_layout_json(s_raw, {"3293", "2207"}, 5)
        self.assertIsNotNone(parsed_s)
        self.assertEqual([p["stock_id"] for p in parsed_s["strategic_picks"]], ["2207", "3293"])
        self.assertEqual(parsed_s["strategic_picks"][0]["allocation_weight_pct"], 60.0)
        self.assertEqual(parsed_s["strategic_picks"][1]["action"], "WAIT_OBSERVE")
        self.assertEqual(parsed_s["strategic_picks"][1]["allocation_weight_pct"], 0.0)
        self.assertIn("低 Beta 抗跌", parsed_s["strategic_picks"][0]["sizing_note"])

        # 5. 端到端驗證：AI #2 原本 3293 排 #1、2207 排 #2；經 AI #3 陣列外配資後，可買且 60% 的 2207 升為 #1，0% 觀望的 3293 降為 #2
        with tempfile.TemporaryDirectory() as tmpdir:
            daily_dir = Path(tmpdir) / "daily"
            daily_dir.mkdir(parents=True, exist_ok=True)
            dates = pd.date_range("2026-08-01", periods=65, freq="B")
            df_3293 = pd.DataFrame(
                {
                    "stock_id": ["3293"] * 65,
                    "date": [d.strftime("%Y-%m-%d") for d in dates],
                    "open": [755.0] * 65,
                    "max": [765.0] * 65,
                    "min": [745.0] * 65,
                    "close": [750.0] * 65,
                    "Trading_Volume": [2_000_000] * 65,
                }
            )
            df_2207 = pd.DataFrame(
                {
                    "stock_id": ["2207"] * 65,
                    "date": [d.strftime("%Y-%m-%d") for d in dates],
                    "open": [515.0] * 65,
                    "max": [525.0] * 65,
                    "min": [510.0] * 65,
                    "close": [516.0] * 65,
                    "Trading_Volume": [1_500_000] * 65,
                }
            )
            pd.concat([df_3293, df_2207], ignore_index=True).to_csv(
                daily_dir / "2026-11-24_prices.csv", index=False
            )

            analyzed = pd.DataFrame(
                [
                    {
                        "stock_id": "2207",
                        "stock_name": "和泰車",
                        "industry_category": "汽車",
                        "sub_industry": "汽車",
                        "close": 516.0,
                        "MA17": 510.0,
                        "MA45": 500.0,
                        "MA117": 490.0,
                    },
                    {
                        "stock_id": "3293",
                        "stock_name": "鈊象",
                        "industry_category": "遊戲",
                        "sub_industry": "遊戲",
                        "close": 750.0,
                        "MA17": 778.0,
                        "MA45": 772.0,
                        "MA117": 750.0,
                    },
                ]
            )
            captured_s3_prompts: list[str] = []

            async def fake_batch(requests_map, **kwargs):
                if "QUALITY_SELECT_STAGE2" in requests_map:
                    return {"QUALITY_SELECT_STAGE2": SimpleNamespace(text=q_raw, model_version="gemini-4-argon")}
                captured_s3_prompts.append(requests_map["STRATEGIC_LAYOUT_STAGE3"])
                return {"STRATEGIC_LAYOUT_STAGE3": SimpleNamespace(text=s_raw, model_version="gemini-4-argon")}

            cache = MagicMock()
            cache.get_daily_chip_history.return_value = pd.DataFrame()
            analyzer = MagicMock()
            analyzer.cache._read_stock_rows.return_value = pd.DataFrame()
            analyzer.compute_cross_sectional_pe_map.return_value = {}
            analyzer._fetch_yahoo_stock_rss.return_value = []

            with (
                patch("quant.alignment_ai_picker.run_vertex_batch_requests", side_effect=fake_batch),
                patch("taishin.macro_context.fetch_global_macro_context", return_value={"prompt_block": ""}),
                patch("taishin_portfolio_service.load_portfolio_exposure_inputs", return_value=([], {})),
                patch("finmind_stock_scanner.fetch_or_cache_tdcc_holding", return_value=None),
                patch("services.news_intel_service.get_stock_intel", side_effect=RuntimeError("skip_net_in_test")),
            ):
                dec, audit_records, _raw, _mv = asyncio.run(
                    execute_v61_quality_and_strategic_pipeline(
                        analyzed,
                        {"market_benchmark_id": "TAIEX", "enable_ex_dividend_compensation": False},
                        target_date=date(2026, 11, 24),
                        daily_dir=daily_dir,
                        cache=cache,
                        analyzer=analyzer,
                        stage1_results={"3293": {"score": 90}, "2207": {"score": 88}},
                        market_regime={"light": "🔴紅燈", "summary": "🔴紅燈"},
                        max_top=5,
                        model="gemini-4-argon",
                        use_vertex_batch=True,
                        poll_interval=60.0,
                        timeout_seconds=7200.0,
                        selection_mode="trend",
                        dry_run=True,
                    )
                )

            self.assertEqual(len(captured_s3_prompts), 1)
            self.assertIn("AI #2 反方風險與豁免辯證", captured_s3_prompts[0])
            self.assertIn("OCF/NI 降至 23%", captured_s3_prompts[0])
            # 最終榜單重排：可買 60% 的 2207 排 #1，0% 觀望的 3293 排 #2
            self.assertEqual([p["stock_id"] for p in dec["top_picks"]], ["2207", "3293"])
            self.assertEqual([p["rank"] for p in dec["top_picks"]], [1, 2])
            self.assertEqual(audit_records["2207"]["Allocation_Weight_Pct"], 60.0)
            self.assertIn("低 Beta 抗跌", str(audit_records["2207"]["Sizing_Note"]))
            self.assertEqual(audit_records["3293"]["Execution_Tactic"], "WAIT_OBSERVE")
            self.assertEqual(audit_records["3293"]["Allocation_Weight_Pct"], 0)
            self.assertIn("OCF/NI 降至 23%", str(audit_records["3293"]["Bear_Case_Warning"]))
            self.assertIn("VIX>30", str(audit_records["3293"]["RR_Math_Rationale"]))
            self.assertIn("OCF/NI 降至 23%", str(dec["top_picks"][1]["bear_case_and_invalidation"]))




