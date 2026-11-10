"""SASD v5.4.0 Phase 1 ~ Phase 3 端到端整合與微結構風控閉環驗收測試。"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

import sys

_ROOT = Path(__file__).resolve().parent.parent
for _sub in (_ROOT, _ROOT / "services", _ROOT / "quant", _ROOT / "taishin"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import ai_service
from alignment_ai_picker import (
    BatchAuditResponse,
    StockAuditItem,
    _filter_recent_rss_news,
    apply_audit_to_decision,
    build_pick_rows,
    enforce_regime_quotas,
    run_audit_stage,
)
from finmind_stock_scanner import (
    classify_daily_margin_efficiency,
    compensate_ex_dividend_gaps,
    compute_deterministic_trade_setup,
    evaluate_disposition_risk,
    evaluate_weekly_tdcc_anchor,
    extract_ex_dividend_dates,
    is_mandatory_margin_covering_window,
    passes_liquidity_median_guard,
)
import fundamental_config
from fundamental_analyzer import FundamentalAnalyzer, compute_fundamental_features
from macro_context import RegimeRouter, evaluate_market_regime_quota
from signal_audit_service import SignalAuditStore, evaluate_forward_path, format_weekly_summary_markdown
from taishin_config import MEMORY_SHEET_NAME, TAISHIN_SHEET_SCHEMAS
from taishin_portfolio_service import (
    build_portfolio_holdings_rows,
    match_entry_to_picks,
    reconcile_position_memory,
)
from taxonomy_service import build_sector_metrics_from_prices, compute_sector_breadth


class TestPhase1To3EndToEndIntegration(unittest.TestCase):
    def test_liquidity_median_and_disposition_risk(self) -> None:
        dates = pd.bdate_range("2026-09-01", periods=20)
        money = [8_000_000.0] * 19 + [600_000_000.0]
        closes = [100.0] * 14 + [105.0, 112.0, 119.0, 126.0, 132.0, 138.0]
        df = pd.DataFrame(
            {
                "date": [d.strftime("%Y-%m-%d") for d in dates],
                "open": [c - 1.0 for c in closes],
                "max": [c + 4.5 for c in closes],
                "min": [c - 4.5 for c in closes],
                "close": closes,
                "Trading_Volume": [16_000_000.0] * 20,
                "Trading_money": money,
            }
        )
        self.assertFalse(passes_liquidity_median_guard(df, min_median_turnover=50_000_000.0))

        # 5 日漲幅 >= 25% (+38%) 觸發處置過熱預警
        disp_tag = evaluate_disposition_risk(df, shares_outstanding=100_000_000.0)
        self.assertIn("處置", disp_tag)

    def test_ex_dividend_compensation_and_covering_calendar(self) -> None:
        dates = pd.bdate_range("2026-10-01", periods=65)
        closes = [105.0] * 45 + [99.0 + i * 0.25 for i in range(20)]
        df = pd.DataFrame(
            {
                "date": [d.strftime("%Y-%m-%d") for d in dates],
                "open": closes,
                "max": [c + 0.5 for c in closes],
                "min": [c - 0.5 for c in closes],
                "close": closes,
                "Trading_Volume": [3_000_000.0] * 45 + [1_500_000.0] * 20,
            }
        )
        ex_str = dates[45].strftime("%Y-%m-%d")
        div_df = pd.DataFrame(
            [{"ex_dividend_date": ex_str, "cash_dividend": 6.0, "stock_dividend": 0.0}]
        )
        ex_dates = extract_ex_dividend_dates(div_df)
        self.assertEqual(ex_dates, [dates[45].date()])
        self.assertTrue(is_mandatory_margin_covering_window(dates[41].date(), ex_dates))

        adj = compensate_ex_dividend_gaps(df, div_df)
        self.assertLess(float(adj["close"].iloc[0]), 100.0)
        self.assertEqual(float(adj["raw_close"].iloc[0]), 105.0)
        setup = compute_deterministic_trade_setup(adj)
        self.assertLess(float(setup["volume_poc_60d"]), float(setup["close_t"]))

    def test_chip_microstructure_and_sector_breadth(self) -> None:
        lock_res = classify_daily_margin_efficiency(
            open_p=100.0,
            high_p=107.0,
            low_p=99.5,
            close_p=106.5,
            prev_close_p=100.0,
            volume_lots=1100.0,
            margin_net_change_lots=220.0,
            inst_net_buy_lots=80.0,
            trust_holding_pct=2.5,
            trust_3d_net_buy_lots=40.0,
            return_10d_pct=9.0,
        )
        self.assertEqual(lock_res.margin_pattern, "MAIN_FORCE_MARGIN_LOCK")
        self.assertIn("主力融資鎖碼", lock_res.semantic_tag)

        trap_res = classify_daily_margin_efficiency(
            open_p=104.0,
            high_p=110.0,
            low_p=100.0,
            close_p=100.5,
            prev_close_p=103.5,
            volume_lots=1100.0,
            margin_net_change_lots=220.0,
            inst_net_buy_lots=-150.0,
            trust_holding_pct=0.0,
            trust_3d_net_buy_lots=-30.0,
            return_10d_pct=20.0,
        )
        self.assertEqual(trap_res.margin_pattern, "RETAIL_MARGIN_CHASING_TRAP")
        self.assertIn("散戶融資套牢警訊", trap_res.semantic_tag)

        rows = []
        for d, big, ppl in (("2026-10-23", 50.0, 10000), ("2026-10-30", 51.0, 9600), ("2026-11-06", 52.5, 9200)):
            rows.append({"date": d, "HoldingSharesLevel": "1-999", "percent": 10.0, "people": ppl - 20})
            rows.append({"date": d, "HoldingSharesLevel": "1,000,001以上", "percent": big, "people": 20})
            rows.append({"date": d, "HoldingSharesLevel": "total", "percent": 100.0, "people": ppl})
        tdcc_info = evaluate_weekly_tdcc_anchor(pd.DataFrame(rows), 5_000_000_000.0)
        self.assertIsNotNone(tdcc_info)
        assert tdcc_info is not None
        self.assertGreater(float(tdcc_info["major_holder_2w_diff"]), 0.0)
        self.assertIn("1000 張以上大戶", str(tdcc_info["summary"]))

        # Sector breadth from prices
        p_dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-09-01", periods=20)]
        p_rows = []
        for sid, base, step in (("3017", 100.0, 1.0), ("3324", 100.0, 0.8), ("2421", 100.0, -0.6), ("6230", 100.0, -0.8)):
            for i, d_str in enumerate(p_dates):
                p_rows.append({"stock_id": sid, "date": d_str, "close": base + step * i, "industry_category": "電子零組件"})
        u_metrics = build_sector_metrics_from_prices(pd.DataFrame(p_rows))
        tax_map = {sid: {"sub_industry": "AI伺服器散熱"} for sid in ("3017", "3324", "2421", "6230")}
        breadth = compute_sector_breadth("AI伺服器散熱", u_metrics, taxonomy_records=tax_map, target_stock_id="3017")
        self.assertAlmostEqual(breadth["breadth_pct"], 50.0)
        self.assertGreaterEqual(breadth["return_percentile"], 75.0)

    def test_dual_index_regime_router_series_and_futures_override(self) -> None:
        twse_up = [22000.0 + i * 50.0 for i in range(25)]
        tpex_up = [250.0 + i * 1.0 for i in range(25)]
        router = RegimeRouter()
        q_green = router.evaluate_from_series(
            twse_up,
            tpex_up,
            foreign_futures_net_short_change=0.0,
            foreign_futures_total_net_short=10000.0,
        )
        assert q_green is not None
        self.assertEqual(q_green["quota"]["regime_state"], "GREEN_BULL")
        self.assertEqual(q_green["quota"]["max_right_breakout_picks"], 3)

        q_yellow = router.evaluate_from_series(
            twse_up,
            tpex_up,
            foreign_futures_net_short_change=4000.0,
            foreign_futures_total_net_short=15000.0,
        )
        assert q_yellow is not None
        self.assertEqual(q_yellow["quota"]["regime_state"], "YELLOW_ROTATION")
        self.assertEqual(q_yellow["quota"]["max_right_breakout_picks"], 1)

        twse_down = [23000.0 - i * 60.0 for i in range(25)]
        tpex_down = [270.0 - i * 1.2 for i in range(25)]
        q_red = router.evaluate_from_series(
            twse_down,
            tpex_down,
            foreign_futures_net_short_change=500.0,
            foreign_futures_total_net_short=20000.0,
        )
        assert q_red is not None
        self.assertEqual(q_red["quota"]["regime_state"], "RED_DEFENSIVE")
        self.assertEqual(q_red["quota"]["max_right_breakout_picks"], 0)

    def test_phase1_schema_expansion_in_picks_and_position_memory(self) -> None:
        audit_cols = [
            "Audit_Grade",
            "Execution_Tactic",
            "Ideal_Buy_Or_Ref",
            "Chase_Limit_Price",
            "Hard_Stop_Loss",
            "TP1_Target_Or_POC",
            "Deterministic_RR",
            "Bear_Case_Warning",
            "Invalidation_Triggers",
        ]
        for col in audit_cols:
            self.assertIn(col, fundamental_config.SHEET_SCHEMAS["AI_Daily_Picks"])
            self.assertIn(col, TAISHIN_SHEET_SCHEMAS[MEMORY_SHEET_NAME])

        frame = pd.DataFrame(
            [
                {
                    "stock_id": "2382",
                    "stock_name": "廣達",
                    "industry_category": "電腦及週邊設備業",
                    "close": 300.0,
                    "MA17": 292.0,
                    "MA45": 280.0,
                    "signal": "突破均線",
                    "ma17_trend": "上彎",
                    "streak": 2,
                }
            ]
        )
        audit_rec = {
            "Audit_Grade": "S_TIER_SNIPER",
            "Execution_Tactic": "BREAKOUT_0915_VWAP_OR_1315_CLOSE",
            "Ideal_Buy_Or_Ref": "300.0",
            "Chase_Limit_Price": 306.5,
            "Hard_Stop_Loss": 288.0,
            "TP1_Target_Or_POC": 332.0,
            "Deterministic_RR": 2.45,
            "Bear_Case_Warning": "上方整數關卡心理壓力",
            "Invalidation_Triggers": ["投信單日轉賣超 > 800 張", "跌破突破長紅一半"],
        }
        decision = {
            "top_picks": [
                {
                    "rank": 1,
                    "stock_id": "2382",
                    "score": 91,
                    "outlook_verdict": "正向成長",
                    "bull_thesis": "AI 伺服器放量",
                    "bear_case_and_invalidation": "破 17MA 停損",
                    "tactical_type": "右側突破",
                    "target_price_2": "355",
                }
            ],
            "watchlist": [],
            "avoid": [],
        }
        apply_audit_to_decision(decision, {"2382": audit_rec})
        rows = build_pick_rows(
            date(2026, 11, 6),
            decision,
            frame,
            {"2382": {"summary": '{"score": 88}', "report": SimpleNamespace(war_room_url="https://x/2382.html")}},
            audit_records={"2382": audit_rec},
        )
        self.assertEqual(len(rows), 1)
        pick_row = rows[0]
        self.assertEqual(pick_row["Audit_Grade"], "S_TIER_SNIPER")
        self.assertEqual(pick_row["Hard_Stop_Loss"], 288.0)
        self.assertEqual(pick_row["TP1_Target_Or_POC"], 332.0)
        self.assertIn("投信單日轉賣超 > 800 張", str(pick_row["Invalidation_Triggers"]))

        # Verify match_entry_to_picks & reconcile_position_memory inherit Phase 1 deterministic columns
        matched = match_entry_to_picks(
            "2382",
            "2026-11-09",
            ai_daily_picks=[pick_row],
            rebound_watchlist=[],
            avg_cost_price=301.5,
        )
        self.assertIsNotNone(matched)
        assert matched is not None
        self.assertEqual(matched["initial_sl"], 288.0)
        self.assertEqual(matched["initial_tp1"], 332.0)
        self.assertEqual(matched["Audit_Grade"], "S_TIER_SNIPER")

        stmt_rows = [
            {
                "trade_date": "2026-11-09",
                "order_no": "A001",
                "stock_no": "2382",
                "action": "BUY",
                "filled_qty": 1000,
                "filled_price": 301.5,
            }
        ]
        holdings_rows = build_portfolio_holdings_rows(
            [{"stock_no": "2382", "stock_name": "廣達", "qty": 1000, "avg_cost_price": 301.5, "current_price": 305.0}],
            statement_records=stmt_rows,
            tech_map={"2382": {"close": 305.0, "ma17": 293.0, "ma45": 280.0, "ma117": 265.0}},
            public_cache={},
            snapshot_date=date(2026, 11, 9),
        )
        mem_rows, _, _ = reconcile_position_memory(
            existing_memory_rows=[],
            holdings_rows=holdings_rows,
            statement_records=stmt_rows,
            settlement_records=[],
            picks_bundle={"ai_daily_picks": [pick_row], "rebound_watchlist": []},
            as_of=date(2026, 11, 9),
        )
        self.assertEqual(len(mem_rows), 1)
        self.assertEqual(mem_rows[0]["Audit_Grade"], "S_TIER_SNIPER")
        self.assertEqual(mem_rows[0]["Execution_Tactic"], "BREAKOUT_0915_VWAP_OR_1315_CLOSE")
        self.assertEqual(mem_rows[0]["Hard_Stop_Loss"], 288.0)

    def test_vertex_structured_output_and_run_audit_stage_records_sqlite_audit(self) -> None:
        # 1) Verify _call_vertex_ai_once passes response_schema in GenerateContentConfig when provided
        fake_client = MagicMock()
        fake_resp = SimpleNamespace(
            text='{"audited_stocks":[]}',
            model_version="gemini-2.5-pro-001",
            usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5),
        )
        fake_client.models.generate_content.return_value = fake_resp
        with (
            patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT_ID": "demo-proj"}, clear=False),
            patch("ai_service.genai.Client", return_value=fake_client),
        ):
            res = ai_service._call_vertex_ai_once(
                "audit prompt",
                model="gemini-2.5-pro",
                response_schema=BatchAuditResponse,
            )
        self.assertEqual(res.text, '{"audited_stocks":[]}')
        gen_call_kwargs = fake_client.models.generate_content.call_args.kwargs
        self.assertIn("config", gen_call_kwargs)
        self.assertEqual(gen_call_kwargs["config"].response_mime_type, "application/json")
        self.assertIs(gen_call_kwargs["config"].response_schema, BatchAuditResponse)

        # 2) Verify RSS 5-trading-day filter drops stale news
        p_df = pd.DataFrame(
            {"date": [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-10-01", periods=10)]}
        )
        filtered_rss = _filter_recent_rss_news(
            [
                {"title": "舊新聞", "pubDate": "2026-09-15"},
                {"title": "近期新聞", "pubDate": "2026-10-13"},
            ],
            p_df,
            trading_days=5,
        )
        self.assertEqual([n["title"] for n in filtered_rss], ["近期新聞"])

        # 3) Verify run_audit_stage records BOTH S_TIER_SNIPER and REJECT_TRAP into SignalAuditStore
        with tempfile.TemporaryDirectory() as tmpdir:
            root_dir = Path(tmpdir)
            daily_dir = root_dir / "daily"
            daily_dir.mkdir(parents=True, exist_ok=True)
            dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-07-01", periods=65)]
            p_rows = []
            for sid in ("2330", "2317"):
                for idx, dt_s in enumerate(dates):
                    if idx < 64:
                        c = 100.0 + (idx % 3 - 1) * 0.2
                        low_v, high_v = c - 0.8, c + 0.8
                    else:
                        c = 101.6
                        low_v, high_v = 100.6, 102.0
                    p_rows.append(
                        {
                            "stock_id": sid,
                            "date": dt_s,
                            "open": c - 0.2,
                            "max": high_v,
                            "min": low_v,
                            "close": c,
                            "Trading_Volume": 3_000_000.0,
                            "industry_category": "半導體業",
                        }
                    )
            target_dt = date.fromisoformat(dates[-1])
            pd.DataFrame(p_rows).to_csv(daily_dir / f"{target_dt.isoformat()}_prices.csv", index=False)

            analyzed_df = pd.DataFrame(
                [
                    {"stock_id": "2330", "stock_name": "台積電", "close": 101.6, "sub_industry": "晶圓代工"},
                    {"stock_id": "2317", "stock_name": "鴻海", "close": 101.6, "sub_industry": "AI伺服器組裝"},
                ]
            )
            audit_payload = BatchAuditResponse(
                audited_stocks=[
                    StockAuditItem(
                        stock_id="2330",
                        stock_name="台積電",
                        bear_case_flaws=["短線乖離略大"],
                        bull_case_catalysts=["先進製程滿載"],
                        conflict_resolution_reasoning="多頭完勝",
                        execution_strategy_type="BREAKOUT_0915_VWAP_OR_1315_CLOSE",
                        thesis_invalidation_triggers=["跌破17MA"],
                        verdict_grade="S_TIER_SNIPER",
                    ),
                    StockAuditItem(
                        stock_id="2317",
                        stock_name="鴻海",
                        bear_case_flaws=["主力借利多出貨"],
                        bull_case_catalysts=["組裝出貨穩定"],
                        conflict_resolution_reasoning="上檔套牢沉重",
                        execution_strategy_type="NO_TRADE",
                        thesis_invalidation_triggers=["破底"],
                        verdict_grade="REJECT_TRAP",
                    ),
                ]
            ).model_dump_json()

            db_file = root_dir / "signal_audit.db"
            cfg = {
                "enable_signal_audit": True,
                "signal_audit_db_path": str(db_file),
                "taxonomy_cache_path": str(root_dir / "tax.json"),
            }
            with (
                patch(
                    "alignment_ai_picker.ask_vertex_ai_with_meta",
                    new=AsyncMock(return_value=SimpleNamespace(text=audit_payload)),
                ),
                patch(
                    "taishin_portfolio_service.load_portfolio_exposure_inputs",
                    return_value=([], {}),
                ),
            ):
                kept, records, rejected = asyncio.run(
                    run_audit_stage(
                        analyzed_df,
                        cfg,
                        target_date=target_dt,
                        daily_dir=daily_dir,
                        analyzer=None,
                        model="gemini-2.5-pro",
                        market_regime={"light": "🟢綠燈"},
                        dry_run=False,
                    )
                )

            self.assertEqual(list(kept["stock_id"]), ["2330"])
            self.assertIn("2330", records)
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["stock_id"], "2317")

            store = SignalAuditStore(db_file)
            logged = store.fetch_all_rows()
            self.assertEqual(len(logged), 2)
            grades = {r["stock_id"]: r["ai_verdict_grade"] for r in logged}
            self.assertEqual(grades["2330"], "S_TIER_SNIPER")
            self.assertEqual(grades["2317"], "REJECT_TRAP")


if __name__ == "__main__":
    unittest.main()
