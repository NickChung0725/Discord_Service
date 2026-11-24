from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import tests  # noqa: F401
from fundamental_config import get_expected_watermarks
from fundamental_cache import (
    _anchor_pick,
    clear_live_check_timestamps,
    record_live_check_timestamp,
    should_bypass_revenue_cache,
)


class FundamentalConfigTests(unittest.TestCase):
    def test_expected_watermarks_returns_strings(self) -> None:
        rev_ym, fin_qtr = get_expected_watermarks(date(2026, 10, 14))
        self.assertRegex(rev_ym, r"^\d{4}-\d{2}$")
        self.assertRegex(fin_qtr, r"^\d{4}Q[1-4]$")

    def test_expected_rev_raw_date_handles_december_rollover(self) -> None:
        # Expected rev_ym for Jan 15 is previous year's December, whose raw_date should be next year's Jan 1.
        from fundamental_cache import _expected_rev_raw_date

        self.assertEqual(_expected_rev_raw_date(date(2026, 1, 15)).isoformat(), "2026-01-01")

    def test_anchor_pick_returns_float_nan_when_no_alias_matches(self) -> None:
        pivot = pd.DataFrame({"other_account": [1.0, 2.0]}, index=[3, 7])

        result = _anchor_pick(pivot, ["missing_account"])

        self.assertEqual(result.dtype, "float64")
        self.assertTrue(result.isna().all())
        self.assertEqual(result.index.tolist(), [3, 7])

    def test_anchor_pick_supports_dual_track_spec(self) -> None:
        pivot_type = pd.DataFrame({"A": [1.0, None]}, index=[0, 1])
        pivot_origin = pd.DataFrame({"中文": [None, 2.0]}, index=[0, 1])

        result = _anchor_pick(
            pivot_type,
            {"types": ["A"], "origins": ["中文"]},
            pivot_origin,
        )

        self.assertEqual(result.tolist(), [1.0, 2.0])

    def test_normalize_balance_sheet_reconciles_total_equity_when_noncontrolling_differs(self) -> None:
        from fundamental_cache import FundamentalCacheManager, validate_balance_sheet_rows

        mgr = FundamentalCacheManager.__new__(FundamentalCacheManager)
        df_raw = pd.DataFrame(
            [
                {"stock_id": "2905", "date": "2026-06-30", "type": "TotalAssets", "value": 1_000_000.0, "origin_name": "資產總計"},
                {"stock_id": "2905", "date": "2026-06-30", "type": "Liabilities", "value": 700_000.0, "origin_name": "負債總計"},
                {"stock_id": "2905", "date": "2026-06-30", "type": "EquityAttributableToOwnersOfParent", "value": 200_000.0, "origin_name": "歸屬於母公司業主之權益合計"},
                {"stock_id": "2905", "date": "2026-06-30", "type": "Equity", "value": 200_000.0, "origin_name": "歸屬於母公司業主之權益合計"},
            ]
        )
        normalized = mgr._normalize_quarterly_statement(df_raw, "2905", statement="BS")
        validate_balance_sheet_rows(normalized)
        self.assertEqual(float(normalized.loc[0, "total_assets"]), 1_000_000.0)
        self.assertEqual(float(normalized.loc[0, "total_liabilities"]), 700_000.0)
        self.assertEqual(float(normalized.loc[0, "parent_equity"]), 200_000.0)
        self.assertEqual(float(normalized.loc[0, "total_equity"]), 300_000.0)

    def test_calendar_driven_cache_bypass_during_revenue_window(self) -> None:
        clear_live_check_timestamps()
        today = date(2026, 11, 6)
        tz = timezone(timedelta(hours=8))
        now_t0 = datetime(2026, 11, 6, 9, 0, 0, tzinfo=tz)

        # 1) 11 月 6 日（>= 5 號），快取內最新月營收仍停留在 (2026, 9) < 預期上月 (2026, 10) -> 強制 Bypass
        self.assertTrue(should_bypass_revenue_cache((2026, 9), today))
        self.assertTrue(should_bypass_revenue_cache((2026, 9), today, stock_id="2330", now=now_t0))

        # 2) 重拉確認廠商尚未公佈 10 月營收，記錄 live check 時間戳；6 小時冷卻期內再次呼叫回傳 False，不重複打擊 FinMind API
        record_live_check_timestamp("2330", now_t0)
        within_cooldown = now_t0 + timedelta(hours=3)
        self.assertFalse(
            should_bypass_revenue_cache((2026, 9), today, stock_id="2330", now=within_cooldown)
        )

        # 3) 超過 6 小時冷卻期後，若仍停留在 (2026, 9)，允許再次 Bypass 重拉
        after_cooldown = now_t0 + timedelta(hours=6, minutes=1)
        self.assertTrue(
            should_bypass_revenue_cache((2026, 9), today, stock_id="2330", now=after_cooldown)
        )
        clear_live_check_timestamps()

    def test_upsert_delta_uses_insert_rows_and_a1_table_range(self) -> None:
        from fundamental_cache import FundamentalCacheManager

        mgr = FundamentalCacheManager.__new__(FundamentalCacheManager)
        mgr._headers_cache = {"02_Income_Statement": ["pk_key", "stock_id", "raw_date"]}
        mgr._records_cache = {"02_Income_Statement": (9e12, [])}
        mgr._cache_ttl = 90.0
        mgr.batch_mode = False

        captured: list[tuple[tuple, dict]] = []

        class _DummyWS:
            title = "02_Income_Statement"

            def append_rows(self, *args, **kwargs):
                captured.append((args, kwargs))

            def batch_update(self, *args, **kwargs):
                pass

        df_delta = pd.DataFrame([{"pk_key": "3034_2026Q3", "stock_id": "3034", "raw_date": "2026-09-30"}])
        mgr._upsert_delta(_DummyWS(), df_delta, "pk_key")

        self.assertEqual(len(captured), 1)
        _, kwargs = captured[0]
        self.assertEqual(kwargs.get("value_input_option"), "USER_ENTERED")
        self.assertEqual(kwargs.get("insert_data_option"), "INSERT_ROWS")
        self.assertEqual(kwargs.get("table_range"), "A1")

    def test_clear_basic_filters_on_managed_tabs(self) -> None:
        from fundamental_cache import FundamentalCacheManager

        mgr = FundamentalCacheManager.__new__(FundamentalCacheManager)
        sent_bodies: list[dict] = []

        class _DummySpreadsheet:
            def fetch_sheet_metadata(self):
                return {
                    "sheets": [
                        {"properties": {"title": "01_Monthly_Revenue", "sheetId": 101}, "basicFilter": {"range": {}}},
                        {"properties": {"title": "04_Cash_Flow", "sheetId": 104}},
                        {"properties": {"title": "User_Custom_Tab", "sheetId": 999}, "basicFilter": {"range": {}}},
                    ]
                }

            def batch_update(self, body):
                sent_bodies.append(body)

        mgr._spreadsheet = _DummySpreadsheet()
        mgr._clear_basic_filters_on_managed_tabs({})

        self.assertEqual(len(sent_bodies), 1)
        self.assertEqual(sent_bodies[0], {"requests": [{"clearBasicFilter": {"sheetId": 101}}]})


if __name__ == "__main__":
    unittest.main()
