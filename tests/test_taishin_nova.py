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
from taishin_config import (
    DCA_0050_SHEET_NAME,
    HOLDINGS_SHEET_NAME,
    SETTLEMENT_SHEET_NAME,
    STATEMENT_SHEET_NAME,
    TAISHIN_SHEET_SCHEMAS,
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
    compute_weighted_avg_cost_from_statements,
    eval_dca_0050,
    evaluate_0050_dip_decision,
    merge_statement_and_fills,
    normalize_settlement_or_pnl_record,
    normalize_statement_record,
    parse_portfolio_ai_response,
    sync_daily,
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
        self.assertEqual(len(TAISHIN_SHEET_SCHEMAS), 4)
        for tab in TAISHIN_SHEET_SCHEMAS:
            self.assertNotIn(tab, fundamental_config.SHEET_SCHEMAS)

        # Guardrail 6: Ensure github_uploader is never referenced in Taishin modules
        for mod_name in ("taishin_config.py", "taishin_client.py", "taishin_portfolio_service.py"):
            with open(mod_name, encoding="utf-8") as fh:
                source = fh.read()
            self.assertNotIn("github_uploader", source)

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

    def test_settlement_shortfall_alert_detection(self) -> None:
        shortfall_row = normalize_settlement_or_pnl_record(
            {"record_date": "2026-11-01", "stock_no": "2330", "qty": 1000, "net_amount": -1050000},
            record_type="TODAY_SETTLEMENT",
            bank_balance=500000.0,
        )
        self.assertEqual(shortfall_row["shortfall_alert"], "⚠️餘額不足")

        ok_row = normalize_settlement_or_pnl_record(
            {"record_date": "2026-11-01", "stock_no": "2330", "qty": 1000, "net_amount": -1050000},
            record_type="TODAY_SETTLEMENT",
            bank_balance=1500000.0,
        )
        self.assertEqual(ok_row["shortfall_alert"], "✅正常")

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
        self.assertIn("週五 13:15 保底定投", fri_decision["trigger_reason"])
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


if __name__ == "__main__":
    unittest.main()
