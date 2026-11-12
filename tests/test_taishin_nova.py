"""Unit tests for Taishin Securities Nova API module (SASD v5.1.0).

100% mocks external credentials, Taishin SDK binary calls, Google Sheets, and Discord HTTP requests.
"""

from __future__ import annotations

from datetime import date
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from gspread.exceptions import APIError
import pandas as pd

import tests  # noqa: F401
import fundamental_config
from taishin_client import (
    BSAction,
    MarketType,
    TaishinClient,
    TaishinCredentials,
    call_sdk_with_date_range,
    get_statement_method,
    iter_date_chunks,
    normalize_sdk_obj,
)
from macro_context import classify_macro_risk_tone, fetch_global_macro_context
from taishin_config import (
    DCA_0050_SHEET_NAME,
    HOLDINGS_SHEET_NAME,
    MEMORY_SHEET_NAME,
    SETTLEMENT_SHEET_NAME,
    STATEMENT_SHEET_NAME,
    TAISHIN_SHEET_SCHEMAS,
    UNTRACKED_HOLDING_TAG,
    build_statement_unique_key,
    clean_leading_zero_code,
    protect_leading_zero,
)
from taishin_portfolio_service import (
    TaishinPortfolioError,
    TaishinSheetManager,
    apply_ai_diagnosis_to_holdings,
    backfill_history,
    build_portfolio_holdings_rows,
    compute_0050_technical_indicators,
    compute_ratchet_trailing_stop,
    compute_style_b_trim_plan,
    compute_weighted_avg_cost_from_statements,
    dump_portfolio_context,
    eval_dca_0050,
    evaluate_0050_dip_decision,
    extract_first_price,
    merge_statement_and_fills,
    normalize_settlement_or_pnl_record,
    normalize_statement_record,
    parse_portfolio_ai_response,
    reconcile_position_memory,
    run_morning_brief,
    sync_daily,
    sync_memory,
)


class FakePyO3Object:
    """Simulate a Rust PyO3 extension object without `__dict__` using `__slots__`."""

    __slots__ = ("stock_no", "stock_name", "qty", "price", "action", "_internal_ptr")

    def __init__(self, stock_no: str, stock_name: str, qty: int, price: float, action: Any) -> None:
        self.stock_no = stock_no
        self.stock_name = stock_name
        self.qty = qty
        self.price = price
        self.action = action
        self._internal_ptr = 0xDEADBEEF

    def some_method(self) -> str:
        return "should_be_ignored"


class TestTaishinNova(unittest.TestCase):
    # ----------------------------------------------------------------------- #
    # 1. Zero-Intrusion & Security Guardrails
    # ----------------------------------------------------------------------- #
    def test_zero_intrusion_on_public_fundamental_schemas_and_no_github_uploader(self) -> None:
        self.assertEqual(len(fundamental_config.SHEET_SCHEMAS), 12)
        self.assertEqual(len(TAISHIN_SHEET_SCHEMAS), 5)
        self.assertIn(MEMORY_SHEET_NAME, TAISHIN_SHEET_SCHEMAS)
        for tab in TAISHIN_SHEET_SCHEMAS:
            self.assertNotIn(tab, fundamental_config.SHEET_SCHEMAS)

        # Guardrail 6: Ensure github_uploader is never referenced in Taishin modules,
        # and bot_server.py never imports personal Taishin accounting modules.
        for mod_name in (
            "taishin/taishin_config.py",
            "taishin/taishin_client.py",
            "taishin/macro_context.py",
            "taishin_portfolio_service.py",
        ):
            with open(mod_name, encoding="utf-8") as fh:
                source = fh.read()
            self.assertNotIn("github_uploader", source)

        with open("bot_server.py", encoding="utf-8") as bot_fh:
            bot_source = bot_fh.read()
        self.assertNotIn("taishin_portfolio_service", bot_source)
        self.assertNotIn("taishin_client", bot_source)

    def test_sheet_manager_rejects_same_spreadsheet_id_as_public_cache(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GSHEET_SPREADSHEET_ID": "PUBLIC_SHEET_123",
                "TAISHIN_GSHEET_SPREADSHEET_ID": "PUBLIC_SHEET_123",
            },
        ):
            with self.assertRaises(TaishinPortfolioError):
                TaishinSheetManager()

    # ----------------------------------------------------------------------- #
    # 2. Rust PyO3 Binary Object Normalization
    # ----------------------------------------------------------------------- #
    def test_normalize_sdk_obj_converts_pyo3_slots_object_to_json_serializable_dict(self) -> None:
        pyo3_obj = FakePyO3Object("0050", "元大台灣50", 200, 188.5, BSAction.Buy)
        self.assertFalse(hasattr(pyo3_obj, "__dict__"))

        normalized = normalize_sdk_obj(pyo3_obj)
        self.assertIsInstance(normalized, dict)
        self.assertEqual(normalized["stock_no"], "0050")
        self.assertEqual(normalized["stock_name"], "元大台灣50")
        self.assertEqual(normalized["qty"], 200)
        self.assertEqual(normalized["price"], 188.5)
        self.assertNotIn("_internal_ptr", normalized)
        self.assertNotIn("some_method", normalized)

        # Must be directly JSON-serializable
        dumped = json.dumps(normalized, ensure_ascii=False)
        self.assertIn("0050", dumped)

    # ----------------------------------------------------------------------- #
    # 3. Statement Method Spelling Compatibility & Dynamic Date Probing
    # ----------------------------------------------------------------------- #
    def test_get_statement_method_supports_account_statment_and_account_statement(self) -> None:
        class SdkWithTypo:
            def account_statment(self, acc: Any) -> list[Any]:
                return []

        class SdkWithFixedName:
            def account_statement(self, acc: Any) -> list[Any]:
                return []

        self.assertIsNotNone(get_statement_method(SdkWithTypo()))
        self.assertIsNotNone(get_statement_method(SdkWithFixedName()))

    def test_call_sdk_with_date_range_probes_formats_and_filters_by_trade_date(self) -> None:
        # Case A: SDK rejects YYYYMMDD with ValueError, accepts YYYY-MM-DD
        calls_a: list[tuple[Any, ...]] = []

        def fn_iso_only(acc: Any, start_str: str, end_str: str) -> list[Any]:
            calls_a.append((acc, start_str, end_str))
            if "-" not in start_str:
                raise ValueError("Expected YYYY-MM-DD date format")
            return [
                {"trade_date": "2026-09-30", "stock_no": "2330", "qty": 1000},
                {"trade_date": "2026-10-15", "stock_no": "0050", "qty": 200},
                {"trade_date": "2026-11-05", "stock_no": "2317", "qty": 1000},
            ]

        res_a = call_sdk_with_date_range(fn_iso_only, "ACC1", "2026-10-01", "2026-10-31")
        self.assertEqual(len(calls_a), 2)
        self.assertEqual(calls_a[0][1:], ("20261001", "20261031"))
        self.assertEqual(calls_a[1][1:], ("2026-10-01", "2026-10-31"))
        self.assertEqual(len(res_a), 1)
        self.assertEqual(res_a[0]["stock_no"], "0050")

        # Case B: SDK only accepts 1 argument (acc)
        def fn_acc_only(acc: Any) -> list[Any]:
            return [
                {"trade_date": "20261010", "stock_no": "2357", "qty": 1000},
                {"trade_date": "20260801", "stock_no": "2357", "qty": 1000},
            ]

        res_b = call_sdk_with_date_range(fn_acc_only, "ACC1", "2026-10-01", "2026-10-31")
        self.assertEqual(len(res_b), 1)
        self.assertEqual(res_b[0]["trade_date"], "2026-10-10")

    # ----------------------------------------------------------------------- #
    # 4. 30-Day Chunked Historical Backfill & Single Login Guardrail
    # ----------------------------------------------------------------------- #
    def test_iter_date_chunks_splits_into_30_day_windows(self) -> None:
        chunks = iter_date_chunks("2026-01-01", "2026-03-15", chunk_days=30)
        self.assertEqual(
            chunks,
            [
                (date(2026, 1, 1), date(2026, 1, 30)),
                (date(2026, 1, 31), date(2026, 3, 1)),
                (date(2026, 3, 2), date(2026, 3, 15)),
            ],
        )

    def test_backfill_history_chunks_single_login_and_supplements_filled_history(self) -> None:
        fake_sdk = MagicMock()
        fake_sdk.login.return_value = [{"account": "9876543"}]

        def fake_statment(acc: Any, start_s: str, end_s: str) -> list[dict[str, Any]]:
            if start_s == "20260101":
                return [
                    {
                        "trade_date": "2026-01-10",
                        "order_no": "00123",
                        "stock_no": "0050",
                        "stock_name": "元大台灣50",
                        "action": "BUY",
                        "filled_qty": 100,
                        "filled_price": 180.0,
                        "fee": 25,
                        "tax": 0,
                        "net_amount": -18025,
                    }
                ]
            return []

        def fake_fills(acc: Any, start_s: str, end_s: str) -> list[dict[str, Any]]:
            if start_s == "20260101":
                # 1 duplicate already in statement + 1 missing trade that must be supplemented
                return [
                    {
                        "trade_date": "2026-01-10",
                        "order_no": "00123",
                        "stock_no": "0050",
                        "stock_name": "元大台灣50",
                        "action": "BUY",
                        "filled_qty": 100,
                        "filled_price": 180.0,
                    },
                    {
                        "trade_date": "2026-01-15",
                        "order_no": "00456",
                        "stock_no": "00878",
                        "stock_name": "國泰永續高股息",
                        "action": "SELL",
                        "filled_qty": 1000,
                        "filled_price": 22.5,
                    },
                ]
            return []

        fake_sdk.account_statment.side_effect = fake_statment
        fake_sdk.filled_history.side_effect = fake_fills
        fake_sdk.filled_history_detail.return_value = []
        fake_sdk.realized_profit_and_loses.return_value = [
            {
                "record_date": "2026-01-15",
                "stock_no": "00878",
                "stock_name": "國泰永續高股息",
                "qty": 1000,
                "buy_price": 20.0,
                "sell_price": 22.5,
                "net_amount": 22446,
                "realized_pnl": 2446,
                "realized_return_pct": 12.23,
            }
        ]
        fake_sdk.history_settlement.return_value = []

        creds = TaishinCredentials("A123456789", "pw", "certs/taishin_cert.pfx", "cert_pw", "9876543")
        client = TaishinClient(sdk=fake_sdk, credentials=creds)

        mock_sheet_mgr = MagicMock()
        mock_sheet_mgr.append_deduplicated_rows.side_effect = lambda tab, rows, **kw: len(rows)
        sleeps: list[float] = []

        result = backfill_history(
            client=client,
            sheet_mgr=mock_sheet_mgr,
            from_date="2026-01-01",
            to_date="2026-02-15",
            chunk_days=30,
            sleep_seconds=1.5,
            dry_run=False,
            sleep_fn=sleeps.append,
        )

        self.assertEqual(result["chunks_count"], 2)
        self.assertEqual(client.login_count, 1)
        fake_sdk.login.assert_called_once()
        self.assertEqual(sleeps, [1.5])
        self.assertEqual(result["fetched_statements"], 2)

        # Verify supplemented FILLED_HISTORY row has estimated ETF tax (0.1%) and source_api="FILLED_HISTORY"
        written_stmt_rows = mock_sheet_mgr.append_deduplicated_rows.call_args_list[0].args[1]
        supp_row = [r for r in written_stmt_rows if r["stock_no"] == "00878"][0]
        self.assertEqual(supp_row["source_api"], "FILLED_HISTORY")
        self.assertEqual(supp_row["tax"], 22)  # 22500 * 0.001 = 22.5 -> 22

    # ----------------------------------------------------------------------- #
    # 5. ETF Leading-Zero Protection, Deduplication & Batch Append with 429 Retry
    # ----------------------------------------------------------------------- #
    def test_etf_leading_zero_protection_and_batch_dedup_with_429_retry(self) -> None:
        self.assertEqual(protect_leading_zero("0050"), "'0050")
        self.assertEqual(protect_leading_zero("00878"), "'00878")
        self.assertEqual(protect_leading_zero("2330"), "2330")
        self.assertEqual(clean_leading_zero_code("'0050"), "0050")

        mock_ws = MagicMock()
        mock_ws.title = STATEMENT_SHEET_NAME
        headers = TAISHIN_SHEET_SCHEMAS[STATEMENT_SHEET_NAME]
        # Existing sheet already has 1 record for 0050 on 2026-10-01
        existing_row = [
            "2026-10-01",
            "2026-10-03",
            "STATEMENT",
            "9876543",
            "00100",
            "0050",
            "元大台灣50",
            "盤中零股",
            "BUY",
            "100",
            "185.5",
            "18550",
            "26",
            "0",
            "-18576",
            "{}",
            "2026-10-01T11:00:00+08:00",
        ]
        mock_ws.get_all_values.return_value = [headers, existing_row]

        # Simulate HTTP 429 on the first append_rows call, then succeed
        fake_resp = MagicMock()
        fake_resp.status_code = 429
        fake_resp.text = "Quota exceeded"
        api_err_429 = APIError(fake_resp)

        call_counter = {"append": 0}

        def flaky_append_rows(batch: list[list[Any]], **kwargs: Any) -> dict[str, Any]:
            call_counter["append"] += 1
            if call_counter["append"] == 1:
                raise api_err_429
            return {"updates": {"updatedRows": len(batch)}}

        mock_ws.append_rows.side_effect = flaky_append_rows

        mock_sp = MagicMock()
        mock_sp.worksheets.return_value = [mock_ws]
        mock_ws.row_values.return_value = headers

        retry_sleeps: list[float] = []
        mgr = TaishinSheetManager(
            spreadsheet_id="PRIVATE_TAISHIN_SHEET",
            spreadsheet=mock_sp,
            sleep_fn=retry_sleeps.append,
        )

        # Prepare 1 duplicate of existing row + 505 new rows (to test 500-row batch splitting)
        incoming: list[dict[str, Any]] = [
            {
                "trade_date": "2026-10-01",
                "settlement_date": "2026-10-03",
                "source_api": "STATEMENT",
                "account_no": "9876543",
                "order_no": "00100",
                "stock_no": "0050",
                "stock_name": "元大台灣50",
                "order_type": "盤中零股",
                "action": "BUY",
                "filled_qty": 100,
                "filled_price": 185.5,
                "gross_amount": 18550,
                "fee": 26,
                "tax": 0,
                "net_amount": -18576,
                "raw_json": "{}",
                "synced_at": "2026-10-01T11:00:00+08:00",
            }
        ]
        for i in range(1, 506):
            incoming.append(
                {
                    "trade_date": "2026-10-02",
                    "settlement_date": "2026-10-06",
                    "source_api": "STATEMENT",
                    "account_no": "9876543",
                    "order_no": f"0{i:04d}",
                    "stock_no": "0050",
                    "stock_name": "元大台灣50",
                    "order_type": "盤中零股",
                    "action": "BUY",
                    "filled_qty": i,
                    "filled_price": 186.0,
                    "gross_amount": i * 186,
                    "fee": 1,
                    "tax": 0,
                    "net_amount": -(i * 186 + 1),
                    "raw_json": "{}",
                    "synced_at": "2026-10-02T11:00:00+08:00",
                }
            )

        added = mgr.append_deduplicated_rows(STATEMENT_SHEET_NAME, incoming, sort_key="trade_date")
        self.assertEqual(added, 505)
        # 505 rows split into 500 + 5 -> 2 batches; first batch retried once on 429 -> total 3 calls
        self.assertEqual(call_counter["append"], 3)
        self.assertEqual(retry_sleeps, [2.0])
        mock_ws.append_row.assert_not_called()

        # Verify leading-zero single quote was applied to order_no and stock_no in the batch payload
        first_batch_matrix = mock_ws.append_rows.call_args_list[0].args[0]
        order_no_idx = headers.index("order_no")
        stock_no_idx = headers.index("stock_no")
        self.assertEqual(first_batch_matrix[0][stock_no_idx], "'0050")
        self.assertTrue(str(first_batch_matrix[0][order_no_idx]).startswith("'0"))

    # ----------------------------------------------------------------------- #
    # 6. Weighted Average Cost Fallback & AI Holdings Diagnosis Parsing
    # ----------------------------------------------------------------------- #
    def test_weighted_avg_cost_fallback_and_ai_diagnosis_merge(self) -> None:
        statements = [
            {"trade_date": "2026-09-01", "order_no": "1", "stock_no": "0050", "action": "BUY", "filled_qty": 100, "filled_price": 180.0},
            {"trade_date": "2026-09-15", "order_no": "2", "stock_no": "0050", "action": "BUY", "filled_qty": 300, "filled_price": 190.0},
        ]
        avg_costs = compute_weighted_avg_cost_from_statements(statements)
        # (100*180 + 300*190) / 400 = 75000 / 400 = 187.5
        self.assertEqual(avg_costs["0050"], 187.5)

        raw_inv = [{"stock_no": "0050", "stock_name": "元大台灣50", "qty": 400, "avg_cost_price": 0, "current_price": 200.0}]
        holdings = build_portfolio_holdings_rows(
            raw_inv,
            statement_records=statements,
            tech_map={"0050": {"close": 200.0, "ma17": 195.0, "ma45": 190.0, "ma117": 182.0}},
            public_cache={"0050": {"war_room_url": "https://example.com/0050_fund.html"}},
            snapshot_date=date(2026, 11, 1),
        )
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0]["avg_cost_price"], 187.5)
        self.assertEqual(holdings[0]["total_cost"], 75000)
        self.assertEqual(holdings[0]["market_value"], 80000)
        self.assertEqual(holdings[0]["unrealized_pnl"], 5000)
        self.assertEqual(holdings[0]["war_room_url"], "https://example.com/0050_fund.html")

        ai_raw = """```json
        {
          "portfolio_overview": "投組穩健，0050 具安全墊優勢。",
          "holdings": [
            {
              "stock_no": "0050",
              "order_type": "盤中零股",
              "ai_action_tag": "順勢續抱",
              "ai_target_price": 215.0,
              "ai_stop_loss": 193.5,
              "ai_advice_summary": "現價高於買入均價 187.5 元達 +6.67%，守穩 17MA 續抱。"
            }
          ]
        }
        ```"""
        parsed = parse_portfolio_ai_response(ai_raw)
        merged = apply_ai_diagnosis_to_holdings(holdings, parsed)
        self.assertEqual(merged[0]["ai_action_tag"], "🟢 順勢續抱")
        self.assertEqual(merged[0]["ai_target_price"], 215.0)
        self.assertEqual(merged[0]["ai_stop_loss"], 193.5)

    def test_normalize_settlement_or_pnl_record_without_bank_balance(self) -> None:
        pnl_row = normalize_settlement_or_pnl_record(
            {
                "record_date": "2026-11-01",
                "stock_no": "2330",
                "stock_name": "台積電",
                "qty": 1000,
                "buy_price": 1000.0,
                "sell_price": 1050.0,
                "net_amount": 1045512,
                "realized_pnl": 44087,
                "realized_return_pct": 4.41,
            },
            record_type="REALIZED_PNL",
        )
        self.assertEqual(pnl_row["stock_no"], "2330")
        self.assertEqual(pnl_row["realized_pnl"], 44087)
        self.assertNotIn("bank_balance", pnl_row)
        self.assertNotIn("shortfall_alert", pnl_row)

    # ----------------------------------------------------------------------- #
    # 7. 0050 Smart Weekly Dip-Buying DCA Engine & Order Safety Gate
    # ----------------------------------------------------------------------- #
    def test_0050_dca_mon_thu_dip_trigger_and_friday_forced_buy_and_safety_gate(self) -> None:
        # Build synthetic 45-day price series for 0050 with a dip on the last day
        rows = []
        for i in range(44):
            rows.append({"date": f"2026-09-{i+1:02d}", "close": 190.0, "max": 191.0, "min": 189.0})
        rows.append({"date": "2026-10-28", "close": 184.0, "max": 186.0, "min": 183.5})
        df = pd.DataFrame(rows)

        metrics = compute_0050_technical_indicators(df)
        self.assertLessEqual(metrics["bias_ma17_pct"], -1.5)
        self.assertGreaterEqual(metrics["drop_10d_pct"], 2.5)

        # Case 1: Wednesday (2026-10-28 is Wednesday) with dip -> triggers buy immediately
        wed_decision = evaluate_0050_dip_decision(
            metrics,
            eval_date=date(2026, 10, 28),
            already_bought_this_week=False,
            weekly_budget_twd=10000,
        )
        self.assertTrue(wed_decision["should_buy"])
        self.assertGreater(wed_decision["order_qty_shares"], 0)
        self.assertIn("觸發低點訊號", wed_decision["trigger_reason"])

        # Case 2: Thursday without dip -> waits for Friday
        calm_metrics = {
            "close_price": 195.0,
            "intraday_low": 194.5,
            "ma17": 193.0,
            "ma45": 188.0,
            "rsi_14": 58.0,
            "kd_k": 65.0,
            "bias_ma17_pct": 1.04,
            "drop_10d_pct": 0.5,
        }
        thu_decision = evaluate_0050_dip_decision(
            calm_metrics,
            eval_date=date(2026, 10, 29),  # Thursday
            already_bought_this_week=False,
            weekly_budget_twd=10000,
        )
        self.assertFalse(thu_decision["should_buy"])
        self.assertEqual(thu_decision["order_qty_shares"], 0)

        # Case 3: Friday (2026-10-30) without Mon-Thu buy -> forces baseline DCA (0.6x ~ 1.5x)
        fri_decision = evaluate_0050_dip_decision(
            calm_metrics,
            eval_date=date(2026, 10, 30),  # Friday
            already_bought_this_week=False,
            weekly_budget_twd=10000,
        )
        self.assertTrue(fri_decision["should_buy"])
        self.assertIn("週五保底定投", fri_decision["trigger_reason"])
        self.assertGreaterEqual(fri_decision["multiplier"], 0.6)
        self.assertLessEqual(fri_decision["multiplier"], 1.5)
        self.assertGreater(fri_decision["order_qty_shares"], 0)

        # Case 4: Already bought this week -> skips even on Friday or dip
        skip_decision = evaluate_0050_dip_decision(
            metrics,
            eval_date=date(2026, 10, 30),
            already_bought_this_week=True,
            weekly_budget_twd=10000,
        )
        self.assertFalse(skip_decision["should_buy"])
        self.assertEqual(skip_decision["order_qty_shares"], 0)

        # Case 5: Order safety gate (DRY_RUN vs LIVE_ORDER with MarketType.IntradayOdd)
        fake_sdk = MagicMock()
        fake_sdk.login.return_value = [{"account": "9876543"}]
        fake_sdk.stock.place_order.return_value = {"order_no": "ODD99988"}
        creds = TaishinCredentials("A123456789", "pw", "certs/taishin_cert.pfx", "cert_pw", "9876543")
        client = TaishinClient(sdk=fake_sdk, credentials=creds)

        # When enable_trading=False -> DRY_RUN, place_order NOT called
        res_dry = eval_dca_0050(
            client=client,
            sheet_mgr=None,
            price_df=df,
            eval_date=date(2026, 10, 28),
            already_bought_override=False,
            enable_trading=False,
            confirm_live_order=True,
            post_discord=False,
            dry_run=False,
        )
        self.assertEqual(res_dry["order_mode"], "DRY_RUN")
        fake_sdk.stock.place_order.assert_not_called()

        # When enable_trading=True AND confirm_live_order=True -> LIVE_ORDER, place_order called
        res_live = eval_dca_0050(
            client=client,
            sheet_mgr=None,
            price_df=df,
            eval_date=date(2026, 10, 28),
            already_bought_override=False,
            enable_trading=True,
            confirm_live_order=True,
            post_discord=False,
            dry_run=False,
        )
        self.assertEqual(res_live["order_mode"], "LIVE_ORDER")
        self.assertEqual(res_live["order_no"], "ODD99988")
        fake_sdk.stock.place_order.assert_called_once()
        placed_order = fake_sdk.stock.place_order.call_args.args[1]
        self.assertEqual(placed_order.market_type, MarketType.IntradayOdd)
        self.assertEqual(placed_order.symbol, "0050")

    # ----------------------------------------------------------------------- #
    # 8. SASD v5.2.0: Price Extractor, Attribution, Style B Trim & Ratchet Lock
    # ----------------------------------------------------------------------- #
    def test_extract_first_price_ignores_ma_percent_and_dates(self) -> None:
        self.assertEqual(extract_first_price("1,020 元 (17MA+12%)"), 1020.0)
        self.assertEqual(extract_first_price("於 17MA 支撐 985.5 元防守"), 985.5)
        self.assertEqual(extract_first_price("收盤 -4% 或 95.2 元"), 95.2)
        self.assertEqual(extract_first_price("2026Q3 合理 18倍PE 約 245 元"), 245.0)
        self.assertEqual(extract_first_price(1150.0), 1150.0)

    def test_style_b_trim_plan_handles_multi_lot_and_single_lot_intraday_odd(self) -> None:
        # >= 2 lots -> whole lots via 整張現股
        plan_3lots = compute_style_b_trim_plan(3000)
        self.assertEqual(plan_3lots["trim_qty"], 1000)
        self.assertEqual(plan_3lots["remain_qty"], 2000)
        self.assertEqual(plan_3lots["order_method"], "整張現股")

        plan_2lots = compute_style_b_trim_plan(2000)
        self.assertEqual(plan_2lots["trim_qty"], 1000)
        self.assertEqual(plan_2lots["remain_qty"], 1000)
        self.assertEqual(plan_2lots["order_method"], "整張現股")

        # 1 lot (1,000 shares) -> 500 shares via 盤中零股
        plan_1lot = compute_style_b_trim_plan(1000)
        self.assertEqual(plan_1lot["trim_qty"], 500)
        self.assertEqual(plan_1lot["remain_qty"], 500)
        self.assertEqual(plan_1lot["order_method"], "盤中零股")

    def test_position_memory_attribution_scale_in_partial_tp1_and_close_lifecycle(self) -> None:
        picks_bundle = {
            "ai_daily_picks": [
                {
                    "scan_date": "2026-10-30",
                    "rank": 1,
                    "category": "TOP_PICK",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "close_at_pick": 1000,
                    "tactical_type": "🎯 短中期波段 (5~20日)",
                    "target_price_1": "1,120 元 (17MA+12%)",
                    "target_price_2": "1,220 元 (20倍PE)",
                    "stop_loss_price": "960 元 (-4%)",
                    "entry_thesis": "Q3 毛利率創高 + 法人連買",
                    "risk_note": "跌破 45MA 退場",
                    "war_room_url": "https://example.com/2330_fund.html",
                },
                {
                    "scan_date": "2026-10-30",
                    "rank": "W1",
                    "category": "WATCHLIST",
                    "stock_id": "2317",
                    "stock_name": "鴻海",
                    "close_at_pick": 200,
                    "entry_thesis": "拉回 17MA 低接",
                },
            ],
            "rebound_watchlist": [
                {
                    "scan_date": "2026-10-30",
                    "stock_id": "3008",
                    "stock_name": "大立光",
                    "close_price": 2300,
                    "high_60d": 2800,
                    "support_ma": "117MA",
                    "support_price": 2280,
                    "target_ma17": 2480,
                }
            ],
            "watchlist_master": {
                "2317": {
                    "stock_name": "鴻海",
                    "ai_summary_json": json.dumps(
                        {
                            "verdict": "AI伺服器動能強",
                            "trade_plan": {
                                "tactical_type": "波段",
                                "target_price_1": "225 元",
                                "target_price_2": "245 元",
                                "stop_loss_price": "192 元",
                                "invalidation": "月營收YoY轉負",
                            },
                        }
                    ),
                }
            },
        }

        statements_t1 = [
            # Matched TOP_PICK #1
            {"trade_date": "2026-10-31", "order_no": "1", "stock_no": "2330", "action": "BUY", "filled_qty": 1000, "filled_price": 1005.0},
            # Matched WATCHLIST W1
            {"trade_date": "2026-10-31", "order_no": "2", "stock_no": "2317", "action": "BUY", "filled_qty": 1000, "filled_price": 202.0},
            # Matched Rebound_Watchlist
            {"trade_date": "2026-10-31", "order_no": "3", "stock_no": "3008", "action": "BUY", "filled_qty": 100, "filled_price": 2310.0},
            # Untracked: 0050 DCA + legacy stock 2881 bought long ago
            {"trade_date": "2026-10-31", "order_no": "4", "stock_no": "0050", "action": "BUY", "filled_qty": 100, "filled_price": 190.0},
            {"trade_date": "2026-05-10", "order_no": "5", "stock_no": "2881", "action": "BUY", "filled_qty": 2000, "filled_price": 70.0},
        ]

        raw_inv_t1 = [
            {"stock_no": "2330", "stock_name": "台積電", "qty": 1000, "avg_cost_price": 1005.0, "current_price": 1015.0},
            {"stock_no": "2317", "stock_name": "鴻海", "qty": 1000, "avg_cost_price": 202.0, "current_price": 204.0},
            {"stock_no": "3008", "stock_name": "大立光", "qty": 100, "avg_cost_price": 2310.0, "current_price": 2330.0},
            {"stock_no": "0050", "stock_name": "元大台灣50", "qty": 100, "avg_cost_price": 190.0, "current_price": 192.0},
            {"stock_no": "2881", "stock_name": "富邦金", "qty": 2000, "avg_cost_price": 70.0, "current_price": 85.0},
        ]
        holdings_t1 = build_portfolio_holdings_rows(
            raw_inv_t1,
            statement_records=statements_t1,
            tech_map={"2330": {"close": 1015.0, "ma17": 995.0, "ma45": 970.0, "ma117": 930.0}},
            public_cache={},
            snapshot_date=date(2026, 10, 31),
        )

        # Day 1: Reconcile creates 3 memory cards; 0050 & 2881 stay untracked!
        mem_t1, proj_t1, events_t1 = reconcile_position_memory(
            existing_memory_rows=[],
            holdings_rows=holdings_t1,
            statement_records=statements_t1,
            settlement_records=[],
            picks_bundle=picks_bundle,
            as_of=date(2026, 10, 31),
        )
        self.assertEqual(len(mem_t1), 3)
        self.assertEqual({m["stock_no"] for m in mem_t1}, {"2330", "2317", "3008"})
        self.assertEqual(len([e for e in events_t1 if e["type"] == "NEW_BOUND"]), 3)

        card_2330 = [m for m in mem_t1 if m["stock_no"] == "2330"][0]
        self.assertEqual(card_2330["entry_source"], "AI_DAILY_TOP_PICK")
        self.assertEqual(card_2330["initial_tp1"], 1120.0)
        self.assertEqual(card_2330["initial_tp2"], 1220.0)
        self.assertEqual(card_2330["initial_sl"], 960.0)
        card_2330["user_note"] = "法說展望超預期"

        card_2317 = [m for m in mem_t1 if m["stock_no"] == "2317"][0]
        self.assertEqual(card_2317["entry_source"], "AI_DAILY_WATCHLIST")
        self.assertEqual(card_2317["initial_tp1"], 225.0)

        card_3008 = [m for m in mem_t1 if m["stock_no"] == "3008"][0]
        self.assertEqual(card_3008["entry_source"], "REBOUND_WATCHLIST")
        self.assertEqual(card_3008["initial_tp1"], 2480.0)

        # Verify untracked holdings have blank target/stop and UNTRACKED_HOLDING_TAG
        hold_0050 = [h for h in proj_t1 if h["stock_no"] == "0050"][0]
        self.assertEqual(hold_0050["ai_action_tag"], UNTRACKED_HOLDING_TAG)
        self.assertEqual(hold_0050["ai_target_price"], "")
        self.assertEqual(hold_0050["ai_stop_loss"], "")

        # Day 2: Scale-in (add another 1,000 shares of 2330 at 995 -> avg 1000) AND 2330 re-appears on picks with TP1=1190
        # Anchor TP1=1120 & user_note MUST stay locked without drifting!
        picks_bundle_t2 = {
            **picks_bundle,
            "ai_daily_picks": [
                *picks_bundle["ai_daily_picks"],
                {
                    "scan_date": "2026-11-02",
                    "rank": 1,
                    "category": "TOP_PICK",
                    "stock_id": "2330",
                    "target_price_1": "1,190 元",
                    "target_price_2": "1,300 元",
                    "stop_loss_price": "980 元",
                },
            ],
        }
        statements_t2 = [
            *statements_t1,
            {"trade_date": "2026-11-03", "order_no": "6", "stock_no": "2330", "action": "BUY", "filled_qty": 1000, "filled_price": 995.0},
        ]
        raw_inv_t2 = [
            {"stock_no": "2330", "stock_name": "台積電", "qty": 2000, "avg_cost_price": 1000.0, "current_price": 1125.0},
            {"stock_no": "2317", "stock_name": "鴻海", "qty": 1000, "avg_cost_price": 202.0, "current_price": 205.0},
            {"stock_no": "3008", "stock_name": "大立光", "qty": 100, "avg_cost_price": 2310.0, "current_price": 2340.0},
        ]
        holdings_t2 = build_portfolio_holdings_rows(
            raw_inv_t2,
            statement_records=statements_t2,
            tech_map={"2330": {"close": 1125.0, "ma17": 1060.0, "ma45": 1010.0, "ma117": 950.0}},
            public_cache={},
            snapshot_date=date(2026, 11, 3),
        )
        mem_t2, _, _ = reconcile_position_memory(
            existing_memory_rows=mem_t1,
            holdings_rows=holdings_t2,
            statement_records=statements_t2,
            settlement_records=[],
            picks_bundle=picks_bundle_t2,
            as_of=date(2026, 11, 3),
        )
        card_2330_t2 = [m for m in mem_t2 if m["stock_no"] == "2330"][0]
        self.assertEqual(card_2330_t2["peak_qty"], 2000)
        self.assertEqual(card_2330_t2["avg_cost_price"], 1000.0)
        self.assertEqual(card_2330_t2["initial_tp1"], 1120.0)  # Locked! Did NOT drift to 1190!
        self.assertEqual(card_2330_t2["user_note"], "法說展望超預期")
        self.assertGreaterEqual(card_2330_t2["trailing_stop"], 1006.0)  # Ratcheted to breakeven+!

        # Day 3: User sells half (1,000 shares) of 2330 at TP1 (1125) and sells all of 2317
        statements_t3 = [
            *statements_t2,
            {"trade_date": "2026-11-04", "order_no": "7", "stock_no": "2330", "action": "SELL", "filled_qty": 1000, "filled_price": 1125.0},
            {"trade_date": "2026-11-04", "order_no": "8", "stock_no": "2317", "action": "SELL", "filled_qty": 1000, "filled_price": 210.0},
        ]
        settlements_t3 = [
            {"record_date": "2026-11-04", "record_type": "REALIZED_PNL", "stock_no": "2317", "realized_pnl": 7100}
        ]
        raw_inv_t3 = [
            {"stock_no": "2330", "stock_name": "台積電", "qty": 1000, "avg_cost_price": 1000.0, "current_price": 1110.0},
            {"stock_no": "3008", "stock_name": "大立光", "qty": 100, "avg_cost_price": 2310.0, "current_price": 2340.0},
        ]
        holdings_t3 = build_portfolio_holdings_rows(
            raw_inv_t3,
            statement_records=statements_t3,
            tech_map={"2330": {"close": 1110.0, "ma17": 1065.0, "ma45": 1015.0, "ma117": 955.0}},
            public_cache={},
            snapshot_date=date(2026, 11, 4),
        )
        mem_t3, proj_t3, events_t3 = reconcile_position_memory(
            existing_memory_rows=mem_t2,
            holdings_rows=holdings_t3,
            statement_records=statements_t3,
            settlement_records=settlements_t3,
            picks_bundle=picks_bundle_t2,
            as_of=date(2026, 11, 4),
        )
        card_2330_t3 = [m for m in mem_t3 if m["stock_no"] == "2330"][0]
        self.assertEqual(card_2330_t3["status"], "TP1_PARTIAL_TAKEN")
        # Active target on 02_Portfolio_Holdings now advances to TP2 (1220.0)!
        hold_2330_t3 = [h for h in proj_t3 if h["stock_no"] == "2330"][0]
        self.assertEqual(hold_2330_t3["ai_target_price"], 1220.0)

        card_2317_t3 = [m for m in mem_t3 if m["stock_no"] == "2317"][0]
        self.assertEqual(card_2317_t3["status"], "CLOSED")
        self.assertEqual(card_2317_t3["realized_pnl"], 7100)
        self.assertTrue(any(e["type"] == "PARTIAL_TAKEN" and e["stock_no"] == "2330" for e in events_t3))
        self.assertTrue(any(e["type"] == "CLOSED" and e["stock_no"] == "2317" for e in events_t3))

        # Verify Ratchet Stop never decreases even if price later pulls back
        prev_stop = card_2330_t3["trailing_stop"]
        dipped_stop = compute_ratchet_trailing_stop(
            prev_trailing_stop=prev_stop,
            initial_sl=960.0,
            avg_cost_price=1000.0,
            current_price=1010.0,
            initial_tp1=1120.0,
            ma17=1020.0,
            status="TP1_PARTIAL_TAKEN",
        )
        self.assertGreaterEqual(dipped_stop, prev_stop)

    # ----------------------------------------------------------------------- #
    # 9. SASD v5.2.0: 08:15 Morning Brief (0-Token Mode vs VERTEX_STAGE2_MODEL) & Macro/Dump
    # ----------------------------------------------------------------------- #
    def test_morning_brief_skips_ai_when_only_untracked_holdings_and_calls_vertex_when_tracked(self) -> None:
        fake_sdk = MagicMock()
        fake_sdk.login.return_value = [{"account": "9876543"}]
        fake_sdk.account_statment.return_value = []
        fake_sdk.filled_history.return_value = []
        fake_sdk.filled_history_detail.return_value = []
        fake_sdk.realized_profit_and_loses.return_value = []
        fake_sdk.history_settlement.return_value = []
        fake_sdk.today_settlement.return_value = [
            {"record_date": "2026-11-02", "stock_no": "0050", "qty": 100, "net_amount": -19000}
        ]
        # Path A: Only 0050 in inventory -> 0 tracked memory cards -> 0 AI calls!
        fake_sdk.inventories.return_value = [
            {"stock_no": "0050", "stock_name": "元大台灣50", "qty": 500, "avg_cost_price": 185.0, "current_price": 192.0}
        ]

        creds = TaishinCredentials("A123456789", "pw", "certs/taishin_cert.pfx", "cert_pw", "9876543")
        client = TaishinClient(sdk=fake_sdk, credentials=creds)

        fake_macro = {
            "risk_tone": "🟢 國際順風偏多",
            "risk_note": "費半大漲",
            "compact_line": "費半指數 +2.45% ｜ 台積電ADR +3.10% ｜ 美元/台幣 32.05 (-0.12%)",
            "headlines": [{"title": "費半強彈創波段高", "source": "Reuters", "pub_time": "11-02 05:30"}],
            "prompt_block": "- 費半 +2.45%",
        }

        with patch("taishin_portfolio_service.ask_vertex_ai_with_meta") as mock_vertex:
            res_zero_token = run_morning_brief(
                client=client,
                sheet_mgr=None,
                as_of=date(2026, 11, 2),
                post_discord=False,
                dry_run=True,
                macro_context_override=fake_macro,
                picks_bundle_override={"ai_daily_picks": [], "rebound_watchlist": [], "watchlist_master": {}},
            )
            self.assertFalse(res_zero_token["ai_called"])
            self.assertEqual(res_zero_token["active_tracked_count"], 0)
            mock_vertex.assert_not_called()
            joined_zero = "\n".join(res_zero_token["messages"])
            self.assertIn("0 Token 快報模式", joined_zero)
            self.assertIn("費半指數 +2.45%", joined_zero)

        # Path B: Tracked 2330 holding (1,000 shares single lot reaching TP1 1120) -> calls VERTEX_STAGE2_MODEL!
        fake_sdk.account_statment.return_value = [
            {"trade_date": "2026-10-31", "order_no": "10", "stock_no": "2330", "action": "BUY", "filled_qty": 1000, "filled_price": 1000.0}
        ]
        fake_sdk.inventories.return_value = [
            {"stock_no": "2330", "stock_name": "台積電", "qty": 1000, "avg_cost_price": 1000.0, "current_price": 1122.0},
            {"stock_no": "0050", "stock_name": "元大台灣50", "qty": 500, "avg_cost_price": 185.0, "current_price": 192.0},
        ]

        async def _fake_vertex_call(prompt: str, **kwargs: Any) -> Any:
            self.assertIn("盤中零股", prompt)
            self.assertIn("賣出 500 股", prompt)
            # AI tries to suggest a lower stop (950.0) than breakeven ratchet (~1006.0) -> Python must clamp it up!
            resp_json = json.dumps(
                {
                    "morning_battle_headline": "費半強彈助攻，2330 達標 TP1 執行賣半鎖利。",
                    "positions": [
                        {
                            "position_id": "2026-10-31_2330",
                            "stock_no": "2330",
                            "ai_action_tag": "💰 分批停利",
                            "recommended_trailing_stop": 950.0,
                            "thesis_intact": True,
                            "order_instruction": "09:00 掛盤中零股賣出 500 股鎖利，餘 500 股續抱看 TP2 1,220 元",
                            "pm_rationale": "已觸及原定 TP1 1,120 元且 ADR 大漲，順勢落袋一半。",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            mock_meta = MagicMock()
            mock_meta.text = f"```json\n{resp_json}\n```"
            mock_meta.model_version = "gemini-2.5-pro-002"
            return mock_meta

        picks_with_2330 = {
            "ai_daily_picks": [
                {
                    "scan_date": "2026-10-30",
                    "rank": 1,
                    "category": "TOP_PICK",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "target_price_1": "1,120 元",
                    "target_price_2": "1,220 元",
                    "stop_loss_price": "960 元",
                    "entry_thesis": "法說優於預期",
                    "risk_note": "跌破 45MA",
                }
            ],
            "rebound_watchlist": [],
            "watchlist_master": {},
        }

        with patch("taishin_portfolio_service.ask_vertex_ai_with_meta", side_effect=_fake_vertex_call) as mock_v2:
            res_tracked = run_morning_brief(
                client=client,
                sheet_mgr=None,
                as_of=date(2026, 11, 2),
                post_discord=False,
                dry_run=True,
                macro_context_override=fake_macro,
                picks_bundle_override=picks_with_2330,
            )
            self.assertTrue(res_tracked["ai_called"])
            mock_v2.assert_called_once()
            mem_card = res_tracked["memory_rows"][0]
            # Clamped to breakeven floor (>= 1006.0) despite AI suggesting 950.0!
            self.assertGreaterEqual(mem_card["trailing_stop"], 1006.0)
            joined_battle = "\n".join(res_tracked["messages"])
            self.assertIn("掛盤中零股賣出 500 股鎖利", joined_battle)
            self.assertIn("0050 元大台灣50", joined_battle)

    def test_macro_risk_tone_and_dump_portfolio_context_for_skill(self) -> None:
        tone_bear, _ = classify_macro_risk_tone({"SOX": {"change_pct": -3.2}, "VIX": {"price": 27.5, "change_pct": 18.0}})
        self.assertIn("🔴", tone_bear)
        tone_bull, _ = classify_macro_risk_tone({"SOX": {"change_pct": 2.1}, "TSM": {"change_pct": 2.5}, "IXIC": {"change_pct": 1.1}, "VIX": {"price": 15.2, "change_pct": -4.0}})
        self.assertIn("🟢", tone_bull)

        mock_mgr = MagicMock()
        mock_mgr.read_records.side_effect = lambda tab: (
            [{"stock_no": "2330", "stock_name": "台積電", "order_type": "現股", "qty": 1000, "avg_cost_price": 1000.0, "total_cost": 1000000, "current_price": 1080.0, "market_value": 1080000, "unrealized_pnl": 80000, "return_pct": 8.0, "weight_pct": 100.0}]
            if tab == HOLDINGS_SHEET_NAME
            else (
                [{"position_id": "2026-10-31_2330", "stock_no": "2330", "stock_name": "台積電", "status": "OPEN", "entry_source": "AI_DAILY_TOP_PICK", "source_scan_date": "2026-10-30", "source_rank": "1", "opened_date": "2026-10-31", "initial_qty": 1000, "peak_qty": 1000, "current_qty": 1000, "avg_cost_price": 1000.0, "initial_tp1": 1120.0, "initial_tp2": 1220.0, "initial_sl": 960.0, "trailing_stop": 1006.0, "entry_thesis": "Q3創高", "invalidation_rule": "破45MA"}]
                if tab == MEMORY_SHEET_NAME
                else []
            )
        )
        with patch("taishin_portfolio_service.load_public_watchlist_cache", return_value={}):
            ctx = dump_portfolio_context(sheet_mgr=mock_mgr, refresh_live=False, include_macro=False, as_of=date(2026, 11, 2))
        self.assertEqual(ctx["account_summary"]["tracked_positions_count"], 1)
        self.assertNotIn("bank_balance", ctx["account_summary"])
        self.assertEqual(ctx["tracked_positions"][0]["initial_tp1"], 1120.0)
        self.assertEqual(ctx["tracked_positions"][0]["live_metrics"]["trim_plan"]["trim_qty"], 500)

    def test_yao_style_morning_battle_left_side_stop_reanchoring(self) -> None:
        """Verify Yao-style dual-track stop anchoring: breakdown with intact thesis allows left-side structural floor."""
        import asyncio
        from types import SimpleNamespace
        import taishin_portfolio_service as svc
        from taishin_config import normalize_ai_action_tag

        self.assertEqual(normalize_ai_action_tag("🛡️ 破線轉價值防守"), "🛡️ 破線轉價值防守")
        self.assertEqual(normalize_ai_action_tag("價值防守"), "🛡️ 破線轉價值防守")
        self.assertEqual(normalize_ai_action_tag("🔄 趁彈減碼換股"), "🔄 趁彈減碼換股")
        self.assertEqual(normalize_ai_action_tag("汰弱留強"), "🔄 趁彈減碼換股")

        active_memories = [
            {
                "position_id": "2026-10-22_3034",
                "stock_no": "3034",
                "stock_name": "聯詠",
                "status": "OPEN",
                "entry_source": "AI_DAILY_TOP_PICK",
                "source_scan_date": "2026-10-22",
                "source_rank": "1",
                "tactical_type": "🎯 短中期波段持有 (5~20日)",
                "opened_date": "2026-10-22",
                "initial_qty": 100,
                "peak_qty": 100,
                "current_qty": 100,
                "avg_cost_price": 582.82,
                "initial_tp1": 641.1,
                "initial_tp2": 692.39,
                "initial_sl": 542.63,
                "trailing_stop": 542.63,
                "entry_thesis": "2026Q2 EPS 10.4元、毛利率42.42%、單季FCF 48.11億元",
                "invalidation_rule": "存貨升至 133.10 億元，DIO 增至 66.8 天",
            }
        ]
        agg_holdings = {
            "3034": {
                "stock_no": "3034",
                "stock_name": "聯詠",
                "qty": 100,
                "avg_cost_price": 582.82,
                "current_price": 471.0,
                "return_pct": -19.19,
                "unrealized_pnl": -11182,
                "weight_pct": 1.9,
                "ma17": 561.59,
                "ma45": 549.51,
                "ma117": 526.64,
                "ma189": 524.09,
            }
        }
        tech_map = {
            "3034": {
                "close": 471.0,
                "ma17": 561.59,
                "ma45": 549.51,
                "ma117": 526.64,
                "ma189": 524.09,
                "bias_ma17_pct": -16.1,
                "bias_ma189_pct": -10.1,
                "prev_high": 480.0,
                "prev_low": 468.0,
                "atr14": 12.0,
                "vol_poc_60d": 465.0,
                "rsi_14": 24.5,
                "daily_return_pr": 68.0,
            }
        }
        deep_fund_map = {
            "3034": {
                "income": {"period": "2026Q2", "eps": 10.4, "gross_margin_pct": 42.42, "operating_margin_pct": 26.1, "gm_qoq_pp": 1.2},
                "balance": {"period": "2026Q2", "inventory_yi": 125.0, "contract_liabilities_yi": 15.2, "dio_days": "62.1", "dso_days": "45.0"},
                "cashflow": {"ocf_yi": 55.0, "fcf_yi": 48.11},
                "valuation": {"pe": "11.2", "pb": "3.4", "dividend_yield": "6.8"},
                "chip": {"foreign_5d_lots": "+850", "trust_5d_lots": "+320", "margin_chg_5d_lots": "-1200", "est_margin_maint_pct": "131.5", "chip_quadrant_tag": "🟢 法人買＋融資減"},
            }
        }

        fake_ai_json = """```json
        {
          "morning_battle_headline": "大盤殺融資564億後迎費半暴漲6.85%報復性強彈！嚴禁開盤砍殺基本面頂尖之錯殺股。",
          "positions": [
            {
              "position_id": "2026-10-22_3034",
              "stock_no": "3034",
              "ai_action_tag": "🎯 逢低加碼",
              "recommended_trailing_stop": 460.0,
              "thesis_intact": true,
              "order_instruction": "早盤開高勿追，待拉回測試475元守穩昨低時，掛盤中零股加碼50股攤平。",
              "pm_rationale": "雖跌破右側防守價，但17MA負乖離達-16.1%屬系統性錯殺，Q2 FCF 48億且存貨未惡化，權重僅1.9%具低接空間。"
            }
          ]
        }
        ```"""

        captured_prompts = []

        async def _fake_ask_vertex(prompt, **kwargs):
            captured_prompts.append(prompt)
            return SimpleNamespace(text=fake_ai_json, model_version="gemini-4-argon")

        with patch.object(svc, "ask_vertex_ai_with_meta", side_effect=_fake_ask_vertex):
            enriched, decisions, headline, model_ver = asyncio.run(
                svc.run_morning_battle_ai_review(
                    active_memories,
                    agg_holdings=agg_holdings,
                    public_cache={},
                    market_regime={"light": "🔴紅燈", "summary": "大盤融資斷頭宣洩"},
                    macro_context={"prompt_block": "費半 +6.85%"},
                    eval_date=date(2026, 11, 10),
                    tech_map=tech_map,
                    deep_fund_map=deep_fund_map,
                    fresh_top_picks_block="- 候選 #1 `2330 台積電`",
                )
            )

        self.assertEqual(len(captured_prompts), 1)
        sent_prompt = captured_prompts[0]
        self.assertIn("17MA乖離 -16.1%", sent_prompt)
        self.assertIn("估算融資維持率=`131.5%`", sent_prompt)
        self.assertIn("最新存貨=`125.0` 億元", sent_prompt)
        self.assertIn("2330 台積電", sent_prompt)

        self.assertEqual(enriched[0]["last_ai_action"], "🎯 逢低加碼")
        # Verify stop was allowed to re-anchor to 460.0 instead of being clamped back to 542.63
        self.assertEqual(enriched[0]["trailing_stop"], 460.0)
        self.assertEqual(decisions["3034"]["trailing_stop"], 460.0)

    def test_load_public_deep_fundamental_context_matches_sheet_schemas_and_na_handling(self) -> None:
        """Verify load_public_deep_fundamental_context reads real SHEET_SCHEMAS tabs/columns and formats N/A cleanly."""
        import taishin_portfolio_service as svc

        fake_tabs = {
            "05_AI_Metrics_Wide": [
                ["pk_key", "stock_id", "raw_date", "year_qtr", "gross_margin_pct", "op_margin_pct", "eps", "ttm_eps", "dio_days", "dso_days", "contract_liab_亿元", "ocf_亿元", "fcf_亿元", "inventories"],
                ["3034_2026Q1", "3034", "2026-03-31", "2026Q1", "39.06", "20.10", "8.5", "35.0", "64.0", "59.0", "12.0", "40.0", "35.0", "12000"],
                ["3034_2026Q2", "3034", "2026-06-30", "2026Q2", "42.42", "22.56", "10.4", "40.0", "66.8", "61.0", "15.2", "55.0", "48.11", "13310"],
                ["9999_2026Q2", "9999", "2026-06-30", "2026Q2", "30.0", "10.0", "1.0", "", "", "", "", "", "", ""],
            ],
            "03_Balance_Sheet": [
                ["pk_key", "stock_id", "raw_date", "year_qtr", "inventory", "contract_liab_current", "parent_equity", "capital_stock"],
                ["3034_2026Q2", "3034", "2026-06-30", "2026Q2", "12500000000", "1520000000", "60850000000", "6085000000"],
                ["9999_2026Q2", "9999", "2026-06-30", "2026Q2", "", "", "", ""],
            ],
            "01_Monthly_Revenue": [
                ["pk_key", "stock_id", "raw_date", "rev_ym", "mom_pct", "yoy_pct"],
                ["3034_2026-10", "3034", "2026-10-31", "2026-10", "3.45", "12.80"],
            ],
            "06_Dividend_Policy": [
                ["pk_key", "stock_id", "raw_date", "div_year", "cash_dividend"],
                ["3034_2025", "3034", "2026-06-01", "2025", "32.0"],
            ],
            "07_Chip_Summary": [
                ["pk_key", "stock_id", "raw_date", "trade_week", "foreign_net_buy_5d_lots", "trust_net_buy_5d_lots", "margin_chg_5d_lots", "est_margin_maint_pct", "chip_quadrant_tag"],
                ["3034_2026-11-11", "3034", "2026-11-11", "2026-W46", "850", "320", "-120", "131.5", "🟢 法人買＋融資減"],
                ["9999_2026-11-11", "9999", "2026-11-11", "2026-W46", "", "", "", "", ""],
            ],
            "08_Stock_Taxonomy": [
                ["stock_id", "sector_group", "sub_industry", "valuation_regime", "peers", "updated_at"],
                ["3034", "半導體", "驅動IC設計", "穩健價值與高息股", "[\"3545\", \"8016\"]", "2026-10-01"],
            ],
        }

        class _FakeWS:
            def __init__(self, rows: list[list[str]]) -> None:
                self._rows = rows

            def get_all_values(self) -> list[list[str]]:
                return self._rows

        class _FakeSP:
            def worksheet(self, name: str) -> _FakeWS:
                if name not in fake_tabs:
                    raise KeyError(name)
                return _FakeWS(fake_tabs[name])

        class _FakeGC:
            def open_by_key(self, key: str) -> _FakeSP:
                return _FakeSP()

        with (
            patch.dict(os.environ, {"GSHEET_SPREADSHEET_ID": "FAKE_PUBLIC_ID", "GOOGLE_APPLICATION_CREDENTIALS": "tests/test_taishin_nova.py"}),
            patch("taishin_portfolio_service.gspread.service_account", return_value=_FakeGC()),
        ):
            deep = svc.load_public_deep_fundamental_context({"3034", "9999"}, prices_by_sid={"3034": 471.0})

        d3034 = deep["3034"]
        self.assertEqual(d3034["income"]["period"], "2026Q2")
        self.assertEqual(d3034["income"]["eps"], 10.4)
        self.assertEqual(d3034["income"]["gm_qoq_pp"], 3.36)
        self.assertEqual(d3034["balance"]["inventory_yi"], 133.1)
        self.assertEqual(d3034["balance"]["contract_liabilities_yi"], 15.2)
        self.assertEqual(d3034["cashflow"]["ocf_yi"], 55.0)
        self.assertEqual(d3034["cashflow"]["fcf_yi"], 48.11)
        self.assertEqual(d3034["revenue"]["rev_ym"], "2026-10")
        self.assertEqual(d3034["revenue"]["yoy_pct"], 12.8)
        self.assertIsNone(d3034["revenue"]["cum_yoy_pct"])
        self.assertEqual(d3034["valuation"]["pe"], round(471.0 / 40.0, 2))
        self.assertEqual(d3034["valuation"]["pb"], 4.71)
        self.assertEqual(d3034["valuation"]["dividend_yield"], round(32.0 / 471.0 * 100.0, 2))
        self.assertEqual(d3034["chip"]["foreign_5d_lots"], "+850")
        self.assertEqual(d3034["chip"]["trust_5d_lots"], "+320")
        self.assertEqual(d3034["chip"]["est_margin_maint_pct"], "131.5")

        # Missing values must degrade to N/A rather than misleading 0.0
        d9999 = deep["9999"]
        self.assertEqual(d9999["balance"]["inventory_yi"], "N/A")
        self.assertEqual(d9999["balance"]["contract_liabilities_yi"], "N/A")
        self.assertEqual(d9999["cashflow"]["ocf_yi"], "N/A")
        self.assertEqual(d9999["cashflow"]["fcf_yi"], "N/A")
        self.assertEqual(d9999["chip"]["foreign_5d_lots"], "N/A")
        self.assertEqual(d9999["chip"]["est_margin_maint_pct"], "N/A")


if __name__ == "__main__":
    unittest.main()
