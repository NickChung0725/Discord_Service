from __future__ import annotations

import tempfile
import sqlite3
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pandas as pd
import requests

from finmind_stock_scanner import (
    SIGNAL_TRACKER_HEADERS,
    SIGNAL_TRACKER_SHEET_NAME,
    add_moving_averages,
    build_signal_tracker_rows,
    classify_latest_alignment_signal,
    classify_latest_signal,
    FinMindRequestLimiter,
    _rate_limit_finmind_http_requests,
    format_alignment_report,
    load_scan_config,
    list_industries,
    list_markets,
    post_alignment_report_to_discord,
    scan_configured_universe,
    scan_industry,
    select_stocks,
    sync_alignment_to_gsheet,
    _cleanup_old_daily_reports,
)


def sample_stock_info() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "industry_category": "半導體業",
                "type": "twse",
            },
            {
                "stock_id": "2303",
                "stock_name": "聯電",
                "industry_category": "半導體業",
                "type": "twse",
            },
            {
                "stock_id": "6488",
                "stock_name": "環球晶",
                "industry_category": "半導體業",
                "type": "tpex",
            },
            {
                "stock_id": "7777",
                "stock_name": "興櫃測試",
                "industry_category": "半導體業",
                "type": "emerging",
            },
            {
                "stock_id": "1101",
                "stock_name": "台泥",
                "industry_category": "水泥工業",
                "type": "twse",
            },
        ]
    )


class RecalcFromCacheTests(unittest.TestCase):
    def _prices_csv_frame(self) -> pd.DataFrame:
        from finmind_stock_scanner import add_moving_averages

        series = [90.0] * 100 + [100.0] * 75 + [100.0] * 28 + [103.0] * 14 + [104.0] * 3
        raw = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=len(series), freq="B"),
                "close": series,
                "Trading_Volume": [1_000_000] * len(series),
            }
        )
        analyzed = add_moving_averages(raw)
        analyzed.insert(0, "market", "上市")
        analyzed.insert(0, "industry_category", "半導體業")
        analyzed.insert(0, "stock_name", "測試")
        analyzed.insert(0, "stock_id", "9999")
        return analyzed

    def test_recalc_uses_saved_moving_averages_without_api(self) -> None:
        from finmind_stock_scanner import recalc_signals_from_prices

        _, alignments = recalc_signals_from_prices(self._prices_csv_frame())

        self.assertIn("MA17_MA45_gap_percent", alignments.columns)
        self.assertEqual(list(alignments["stock_id"]), ["9999"])
        self.assertEqual(alignments.iloc[0]["signal"], "剛金叉起漲")
        self.assertEqual(alignments.iloc[0]["vol_lots"], 1000)

    def test_recalc_from_cache_overwrites_csvs_and_dry_run_does_not(self) -> None:
        from finmind_stock_scanner import recalc_from_cache

        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily"
            daily.mkdir()
            self._prices_csv_frame().to_csv(daily / "2026-10-20_prices.csv", index=False)
            config = {"output_directory": tmp}

            recalc_from_cache(config, as_of=date(2026, 10, 20), dry_run=True)
            self.assertFalse((daily / "2026-10-20_alignment_signals.csv").exists())

            recalc_from_cache(config, as_of=date(2026, 10, 20))
            saved = pd.read_csv(
                daily / "2026-10-20_alignment_signals.csv", dtype={"stock_id": str}
            )
            self.assertEqual(list(saved["stock_id"]), ["9999"])

    def test_recalc_from_cache_requires_prices_csv(self) -> None:
        from finmind_stock_scanner import recalc_from_cache

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                recalc_from_cache({"output_directory": tmp}, as_of=date(2026, 10, 20))


class FinMindStockScannerTests(unittest.TestCase):
    def test_lists_market_counts_and_industries(self) -> None:
        info = sample_stock_info()

        markets = list_markets(info).set_index("market")
        industries = list_industries(info, "上市")

        self.assertEqual(markets.loc["上市", "stock_count"], 3)
        self.assertEqual(markets.loc["上櫃", "stock_count"], 1)
        self.assertEqual(markets.loc["興櫃", "stock_count"], 1)
        self.assertEqual(
            industries.to_dict("records"),
            [
                {"industry_category": "半導體業", "stock_count": 2},
                {"industry_category": "水泥工業", "stock_count": 1},
            ],
        )

    def test_selects_by_market_and_industry(self) -> None:
        selected = select_stocks(sample_stock_info(), "上櫃", "半導體業")

        self.assertEqual(selected["stock_id"].tolist(), ["6488"])

    def test_configuration_excludes_deregistered_codes_and_etf_categories(self) -> None:
        from finmind_stock_scanner import _configured_universe

        info = pd.concat(
            [
                sample_stock_info(),
                pd.DataFrame(
                    [
                        {
                            "stock_id": "0050",
                            "stock_name": "ETF 測試",
                            "industry_category": "ETF",
                            "type": "twse",
                        },
                        {
                            "stock_id": "2473",
                            "stock_name": "已下市測試",
                            "industry_category": "半導體業",
                            "type": "twse",
                        },
                    ]
                ),
            ],
            ignore_index=True,
        )
        config = {
            "markets": ["上市", "上櫃"],
            "industries": ["半導體業"],
            "excluded_stocks": [
                {
                    "stock_id": "2473",
                    "stock_name": "已下市測試",
                    "reason": "已下市",
                }
            ],
            "excluded_industry_categories": ["ETF"],
        }

        universe = _configured_universe(info, config)

        self.assertEqual(universe[("上市", "半導體業")]["stock_id"].tolist(), ["2330", "2303"])
        self.assertEqual(universe[("上櫃", "半導體業")]["stock_id"].tolist(), ["6488"])

    def test_loads_project_scan_configuration(self) -> None:
        config = load_scan_config(Path("finmind_scanner.json"))

        self.assertEqual(config["markets"], ["上市", "上櫃"])
        self.assertEqual(config["max_requests_per_hour"], 540)
        self.assertEqual(config["min_request_interval_seconds"], 6.7)
        excluded_ids = {item["stock_id"] for item in config["excluded_stocks"]}
        self.assertIn("3702A", excluded_ids)
        self.assertIn("3036A", excluded_ids)

    def test_request_limiter_enforces_rolling_hour_across_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            current_time = [1000.0]
            limiter = FinMindRequestLimiter(
                Path(directory) / "request_history.sqlite3",
                max_requests_per_hour=2,
                min_interval_seconds=1800,
            )
            with (
                patch(
                    "finmind_stock_scanner.time.time",
                    side_effect=lambda: current_time[0],
                ),
                patch(
                    "finmind_stock_scanner.time.sleep",
                    side_effect=lambda duration: current_time.__setitem__(
                        0, current_time[0] + duration
                    ),
                ) as sleep,
            ):
                limiter.wait_for_slot()
                limiter.wait_for_slot()
                limiter.wait_for_slot()

            self.assertEqual(sleep.call_count, 2)
            with closing(sqlite3.connect(limiter.database_path)) as connection:
                timestamps = [
                    row[0]
                    for row in connection.execute(
                        "SELECT requested_at FROM request_times ORDER BY requested_at"
                    )
                ]
            self.assertEqual(len(timestamps), 2)
            self.assertGreaterEqual(timestamps[-1] - 1000.0, 3600)

    def test_http_limiter_counts_each_outbound_attempt_including_retries_and_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            limiter = FinMindRequestLimiter(
                Path(directory) / "request_history.sqlite3",
                max_requests_per_hour=10,
                min_interval_seconds=360,
            )
            with (
                patch.object(limiter, "wait_for_slot") as wait_for_slot,
                patch(
                    "finmind_stock_scanner.requests.adapters.HTTPAdapter.send",
                    autospec=True,
                ) as adapter_send,
            ):
                # Each login and retry sends a separate outbound HTTP attempt.
                with _rate_limit_finmind_http_requests(limiter):
                    adapter = requests.adapters.HTTPAdapter()
                    request = requests.Request(
                        "GET", "https://example.invalid"
                    ).prepare()
                    for _ in range(4):
                        adapter.send(request)

            self.assertEqual(adapter_send.call_count, 4)
            self.assertEqual(wait_for_slot.call_count, 4)

    def test_calculates_ma17_and_ma45_and_classifies_near_cross(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [100.0] * 190 + [100.5] * 30,
            }
        )

        analyzed = add_moving_averages(prices)
        signal = classify_latest_signal(analyzed)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["signal"], "剛金叉起漲")
        self.assertGreaterEqual(signal["MA3"], signal["MA17"])
        self.assertGreater(signal["MA45"], signal["MA117"])
        self.assertGreater(signal["MA117"], signal["MA189"])
        self.assertLessEqual(signal["gap_percent"], 2.5)
        self.assertGreater(signal["gap_percent"], 0.0)
        self.assertIn("ma17_trend", signal)

    def test_classifies_ma45_above_ma17(self) -> None:
        # Construct series where MA3 >= MA45 >= MA17 (-1.5% <= gap < 0%) and MA45 > MA117 > MA189
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [90.0] * 100 + [100.0] * 75 + [105.0] * 28 + [103.0] * 14 + [106.0] * 3,
            }
        )

        signal = classify_latest_signal(add_moving_averages(prices))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["signal"], "即將金叉")
        self.assertGreaterEqual(signal["MA3"], signal["MA45"])
        self.assertGreaterEqual(signal["MA45"], signal["MA17"])
        self.assertGreater(signal["MA45"], signal["MA117"])
        self.assertGreater(signal["MA117"], signal["MA189"])
        self.assertGreaterEqual(signal["gap_percent"], -1.5)
        self.assertLess(signal["gap_percent"], 0.0)

    def test_rejects_signal_when_gap_is_below_negative_1_point_5(self) -> None:
        # MA45 >= MA17, but gap is below -1.5% (e.g. -5.8%), not yet close enough to golden cross
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [90.0] * 100 + [100.0] * 75 + [120.0] * 28 + [108.0] * 14 + [118.0] * 3,
            }
        )
        self.assertIsNone(classify_latest_signal(add_moving_averages(prices)))

    def test_classifies_golden_cross_in_extended_range(self) -> None:
        # Gap between 1.0% and 2.5% (e.g. ~1.9%), previously rejected, now accepted as 剛金叉起漲
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [90.0] * 100 + [100.0] * 75 + [100.0] * 28 + [103.0] * 14 + [104.0] * 3,
            }
        )
        signal = classify_latest_signal(add_moving_averages(prices))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["signal"], "剛金叉起漲")
        self.assertGreater(signal["gap_percent"], 1.0)
        self.assertLessEqual(signal["gap_percent"], 2.5)

    def test_rejects_signal_when_gap_exceeds_positive_2_point_5(self) -> None:
        # Gap > 2.5%, over-extended beyond initial takeoff
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [90.0] * 100 + [100.0] * 75 + [100.0] * 28 + [105.0] * 14 + [108.0] * 3,
            }
        )
        self.assertIsNone(classify_latest_signal(add_moving_averages(prices)))

    def test_rejects_signal_when_ma3_is_not_above_all_lines(self) -> None:
        # MA45 >= MA17 and MA45 > MA117 > MA189, but MA3 < MA45
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [90.0] * 100 + [100.0] * 75 + [120.0] * 28 + [110.0] * 17,
            }
        )
        analyzed = add_moving_averages(prices)
        latest = analyzed.iloc[-1]
        self.assertGreaterEqual(latest["MA45"], latest["MA17"])
        self.assertLess(latest["MA3"], latest["MA45"])
        self.assertIsNone(classify_latest_signal(analyzed))

    def test_rejects_signal_when_ma189_is_not_below_ma117(self) -> None:
        # Make MA189 > MA117 while MA45 is near MA17
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [150.0] * 103 + [90.0] * 72 + [105.0] * 44 + [105.2],
            }
        )
        analyzed = add_moving_averages(prices)
        latest = analyzed.iloc[-1]
        self.assertGreater(latest["MA189"], latest["MA117"])
        self.assertIsNone(classify_latest_signal(analyzed))

    def test_does_not_select_stocks_well_below_ma17(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [90.0] * 100 + [100.0] * 119 + [200.0],
            }
        )

        self.assertIsNone(classify_latest_signal(add_moving_averages(prices)))

    def test_classifies_alignment_when_fast_averages_are_above_long_averages(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [100.0] * 190 + [100.5] * 30,
            }
        )

        signal = classify_latest_alignment_signal(add_moving_averages(prices))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreaterEqual(signal["MA3"], signal["MA17"])
        self.assertGreater(signal["MA45"], signal["MA117"])
        self.assertGreater(signal["MA117"], signal["MA189"])
        self.assertLessEqual(signal["MA17_MA45_gap_percent"], 2.5)

    def test_does_not_classify_alignment_if_fast_averages_are_not_both_above(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=220, freq="B"),
                "close": [100.0] * 190 + [99.5] * 30,
            }
        )

        self.assertIsNone(
            classify_latest_alignment_signal(add_moving_averages(prices))
        )

    def test_formats_empty_alignment_report(self) -> None:
        report = format_alignment_report(
            pd.DataFrame(), as_of=date(2026, 10, 12), failed_count=3
        )

        self.assertEqual(len(report), 1)
        self.assertIn("2026-10-12", report[0])
        self.assertIn("今日沒有符合條件", report[0])
        self.assertIn("3 檔股票資料抓取失敗", report[0])

    def test_posts_alignment_report_with_bot_authentication(self) -> None:
        signals = pd.DataFrame(
            [
                {
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "close": 1000,
                    "MA17": 100,
                    "MA45": 100.5,
                    "MA117": 99,
                    "MA189": 98,
                    "MA17_MA45_gap_percent": 0.5,
                }
            ]
        )
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        with patch(
            "finmind_stock_scanner.urlopen", return_value=response
        ) as urlopen:
            sent_count = post_alignment_report_to_discord(
                signals,
                channel_id="123456789",
                bot_token="test-token",
                as_of=date(2026, 10, 12),
            )

        self.assertEqual(sent_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://discord.com/api/v10/channels/123456789/messages")
        self.assertEqual(request.get_header("Authorization"), "Bot test-token")

    def test_scan_writes_prices_signals_and_failures(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.date_range(
                    end="2026-03-31", periods=220, freq="B"
                ),
                "close": [100.0] * 190 + [100.5] * 30,
                "open": [100.0] * 220,
                "Trading_Volume": [1000] * 220,
                "stock_id": ["from-finmind"] * 220,
            }
        )
        loader = Mock()
        loader.taiwan_stock_daily.side_effect = [prices, pd.DataFrame()]
        stocks = select_stocks(sample_stock_info(), "上市", "半導體業")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            FinMindRequestLimiter, "wait_for_slot"
        ):
            (prices_path, alignment_path, failures_path, alignment_count) = scan_industry(
                loader=loader,
                stocks=stocks,
                market="上市",
                industry="半導體業",
                output_dir=Path(directory),
                as_of=date(2026, 3, 31),
            )

            self.assertTrue(prices_path.is_file())
            self.assertTrue(alignment_path.is_file())
            self.assertIsNotNone(failures_path)
            assert failures_path is not None
            self.assertTrue(failures_path.is_file())
            self.assertEqual(alignment_count, 1)
            saved_prices = pd.read_csv(
                prices_path, dtype={"stock_id": str}, parse_dates=["date"]
            )
            saved_alignment = pd.read_csv(alignment_path)
            saved_failures = pd.read_csv(failures_path, dtype={"stock_id": str})

        self.assertTrue((saved_prices["date"] >= pd.Timestamp("2025-09-30")).all())
        self.assertTrue((saved_prices["date"] <= pd.Timestamp("2026-03-31")).all())
        self.assertLess(len(saved_prices), 220)
        self.assertEqual(saved_prices["stock_id"].unique().tolist(), ["2330"])
        self.assertEqual(len(saved_alignment), 1)
        self.assertIn("MA189", saved_prices.columns)
        self.assertEqual(saved_failures["stock_id"].tolist(), ["2303"])
        self.assertEqual(loader.taiwan_stock_daily.call_count, 2)

    def test_configured_scan_writes_consolidated_daily_csvs(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.date_range(
                    end="2026-03-31", periods=220, freq="B"
                ),
                "close": [100.0] * 190 + [100.5] * 30,
                "open": [100.0] * 220,
                "Trading_Volume": [1000] * 220,
            }
        )
        loader = Mock()
        loader.taiwan_stock_daily.return_value = prices
        config = {
            "markets": ["上市", "上櫃"],
            "industries": ["半導體業"],
            "excluded_stocks": [],
            "excluded_industry_categories": ["ETF"],
            "max_requests_per_hour": 540,
            "min_request_interval_seconds": 6.7,
            "local_retention_days": 30,
            "post_alignment_to_discord": False,
            "stock_channel_id": "123456",
            "output_directory": "ignored",
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "finmind_stock_scanner.time.sleep"
        ):
            reports, failure_count, alignment_count, group_failure_count = (
                scan_configured_universe(
                    loader,
                    sample_stock_info(),
                    config,
                    output_dir=Path(directory),
                    as_of=date(2026, 3, 31),
                )
            )

            self.assertTrue(reports["prices"].is_file())
            self.assertTrue(reports["alignment_signals"].is_file())
            self.assertTrue(reports["failures"].is_file())
            self.assertEqual(failure_count, 0)
            self.assertEqual(group_failure_count, 0)
            self.assertEqual(alignment_count, 3)
            saved_prices = pd.read_csv(
                reports["prices"], dtype={"stock_id": str}
            )

        self.assertEqual(set(saved_prices["market"]), {"上市", "上櫃"})
        self.assertEqual(set(saved_prices["stock_id"]), {"2330", "2303", "6488"})
        self.assertGreater(len(saved_prices), 0)
        self.assertLess(len(saved_prices), 660)

    def test_build_signal_tracker_rows_tracks_new_and_continuing_stocks(self) -> None:
        day1_df = pd.DataFrame(
            [
                {
                    "market": "上市",
                    "industry_category": "半導體業",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "date": "2026-10-15",
                    "close": 1000.0,
                    "MA17": 990.0,
                    "MA45": 985.0,
                    "MA117": 950.0,
                    "MA189": 910.0,
                    "gap_percent": -0.50,
                    "signal": "即將金叉",
                }
            ]
        )
        day1_rows = build_signal_tracker_rows(
            day1_df,
            existing_records=[],
            as_of=date(2026, 10, 15),
            updated_at="2026-10-15 18:00:00",
        )
        self.assertEqual(len(day1_rows), 1)
        self.assertEqual(day1_rows[0]["當日新進"], "🆕 是（新進榜）")
        self.assertEqual(day1_rows[0]["連續天數"], 1)

        day2_df = pd.DataFrame(
            [
                {
                    "market": "上市",
                    "industry_category": "半導體業",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "date": "2026-10-16",
                    "close": 1010.0,
                    "MA17": 992.0,
                    "MA45": 985.0,
                    "MA117": 955.0,
                    "MA189": 915.0,
                    "gap_percent": 0.71,
                    "signal": "剛金叉起漲",
                },
                {
                    "market": "上櫃",
                    "industry_category": "半導體業",
                    "stock_id": "6488",
                    "stock_name": "環球晶",
                    "date": "2026-10-16",
                    "close": 500.0,
                    "MA17": 490.0,
                    "MA45": 487.0,
                    "MA117": 460.0,
                    "MA189": 430.0,
                    "gap_percent": 0.61,
                    "signal": "剛金叉起漲",
                },
            ]
        )
        day2_rows = build_signal_tracker_rows(
            day2_df,
            existing_records=day1_rows,
            as_of=date(2026, 10, 16),
            updated_at="2026-10-16 18:00:00",
        )

        self.assertEqual(len(day2_rows), 3)
        # 最新日期排最前面，且當日新進排在同日最上方
        self.assertEqual(day2_rows[0]["執行日期"], "2026-10-16")
        self.assertEqual(day2_rows[0]["股票代號"], "6488")
        self.assertEqual(day2_rows[0]["當日新進"], "🆕 是（新進榜）")
        self.assertEqual(day2_rows[0]["連續天數"], 1)

        self.assertEqual(day2_rows[1]["執行日期"], "2026-10-16")
        self.assertEqual(day2_rows[1]["股票代號"], "2330")
        self.assertEqual(day2_rows[1]["當日新進"], "🔄 否（持續符合）")
        self.assertEqual(day2_rows[1]["連續天數"], 2)
        self.assertIn("轉強突破", str(day2_rows[1]["每日執行狀況"]))

    def test_cleanup_does_not_delete_legacy_signals_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp) / "daily"
            daily.mkdir()
            (daily / "2026-01-01_signals.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            (daily / "2026-01-01_alignment_signals.csv").write_text(
                "stock_id,MA17_MA45_gap_percent\n2330,0.1\n", encoding="utf-8"
            )

            _cleanup_old_daily_reports(daily, as_of=date(2026, 2, 15), retention_days=30)

            self.assertTrue((daily / "2026-01-01_signals.csv").exists())
            self.assertFalse((daily / "2026-01-01_alignment_signals.csv").exists())

    def test_sync_alignment_to_gsheet_auto_creates_worksheet_and_writes_rows(self) -> None:
        signals_df = pd.DataFrame(
            [
                {
                    "market": "上市",
                    "industry_category": "半導體業",
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "date": "2026-10-16",
                    "close": 1000.0,
                    "MA17": 990.0,
                    "MA45": 985.0,
                    "MA117": 950.0,
                    "MA189": 910.0,
                    "MA17_MA45_gap_percent": 0.51,
                    "signal": "剛金叉起漲",
                }
            ]
        )
        fake_ws = MagicMock()
        fake_ws.id = 42
        fake_ws.row_count = 1000
        fake_ws.col_count = len(SIGNAL_TRACKER_HEADERS)

        fake_spreadsheet = MagicMock()
        fake_spreadsheet.worksheets.return_value = []
        fake_spreadsheet.add_worksheet.return_value = fake_ws

        fake_gc = MagicMock()
        fake_gc.open_by_key.return_value = fake_spreadsheet

        with patch("finmind_stock_scanner.gspread.service_account", return_value=fake_gc):
            row_count = sync_alignment_to_gsheet(
                signals_df,
                as_of=date(2026, 10, 16),
                spreadsheet_id="test-sheet-id",
                credentials_path="sheetbot.json",
            )

        self.assertEqual(row_count, 1)
        fake_spreadsheet.add_worksheet.assert_called_once_with(
            title=SIGNAL_TRACKER_SHEET_NAME,
            rows=1000,
            cols=len(SIGNAL_TRACKER_HEADERS),
        )
        fake_ws.update.assert_called_once()
        updated_values = fake_ws.update.call_args.kwargs["values"]
        self.assertEqual(updated_values[0], SIGNAL_TRACKER_HEADERS)
        self.assertEqual(updated_values[1][4], "2330")
        self.assertIn("🆕", updated_values[1][6])


class ReboundAndMarketRegimeTests(unittest.TestCase):
    def _make_rebound_series(
        self,
        *,
        latest_close: float = 113.0,
        latest_max: float = 114.0,
        latest_min: float = 110.0,
    ) -> pd.DataFrame:
        # 80 days @ 85, 85 days @ 100, 35 days @ 105, 19 days @ 126, 1 day @ latest_close (113)
        # Last 20 days = 19*126 + 113 -> Last 17 days = 16*126 + 113 -> MA17 = 125.24
        # Last 45 days = 25*105 + 19*126 + 113 = 5132 -> MA45 = 114.04 (close 113 is -0.91% from MA45!)
        # MA117 ~ 105.5, MA189 ~ 99.0 -> MA45 >= MA117 > MA189
        # 60d high = 127.0, drop = 14.0 >= 10, drop_pct = 11.02% >= 10%
        # potential_return_pct = (125.24 - 113) / 113 = +10.83% >= 5%
        closes = [85.0] * 80 + [100.0] * 85 + [105.0] * 35 + [126.0] * 19 + [latest_close]
        maxs = [c + 1.0 for c in closes[:-1]] + [latest_max]
        mins = [c - 1.0 for c in closes[:-1]] + [latest_min]
        df = pd.DataFrame(
            {
                "stock_id": ["2357"] * len(closes),
                "stock_name": ["華碩"] * len(closes),
                "industry_category": ["電腦及週邊設備業"] * len(closes),
                "market": ["上市"] * len(closes),
                "date": pd.date_range("2025-06-01", periods=len(closes), freq="B"),
                "close": closes,
                "max": maxs,
                "min": mins,
                "Trading_Volume": [2_000_000] * len(closes),
            }
        )
        return add_moving_averages(df)

    def test_classify_rebound_signal_matches_valid_type_a_setup(self) -> None:
        from finmind_stock_scanner import classify_rebound_signal

        analyzed = self._make_rebound_series(
            latest_close=113.0, latest_max=114.0, latest_min=110.0
        )
        sig = classify_rebound_signal(analyzed)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig["support_ma"], "45MA")
        self.assertGreaterEqual(sig["drop_amount"], 10.0)
        self.assertGreaterEqual(sig["drop_pct"], 10.0)
        self.assertGreaterEqual(sig["potential_return_pct"], 5.0)
        self.assertGreaterEqual(sig["close_strength"], 0.55)

    def test_classify_rebound_rejects_weak_close_strength_upper_shadow(self) -> None:
        from finmind_stock_scanner import classify_rebound_signal

        # Close near daily low -> close_strength = (113 - 112.5) / (118 - 112.5) = 0.09 < 0.55
        analyzed = self._make_rebound_series(
            latest_close=113.0, latest_max=118.0, latest_min=112.5
        )
        self.assertIsNone(classify_rebound_signal(analyzed))

    def test_rebound_min_drop_amount_tiers(self) -> None:
        from finmind_stock_scanner import _rebound_min_drop_amount

        self.assertIsNone(_rebound_min_drop_amount(25.0))
        self.assertEqual(_rebound_min_drop_amount(80.0), 10.0)
        self.assertEqual(_rebound_min_drop_amount(250.0), 30.0)
        self.assertEqual(_rebound_min_drop_amount(600.0), 100.0)
        self.assertEqual(_rebound_min_drop_amount(1200.0), 300.0)

    def test_evaluate_market_regime_lights(self) -> None:
        from finmind_stock_scanner import evaluate_market_regime

        # Green light: close >= MA17 and MA17 rising
        green_df = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=130, freq="B"),
                "close": [150.0] * 100 + [160.0] * 25 + [165.0] * 5,
            }
        )
        res_g = evaluate_market_regime(green_df, "0050")
        self.assertEqual(res_g["light"], "🟢綠燈")

        # Yellow light: close < MA17 but >= MA45
        yellow_df = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=130, freq="B"),
                "close": [140.0] * 85 + [160.0] * 40 + [153.0] * 5,
            }
        )
        res_y = evaluate_market_regime(yellow_df, "0050")
        self.assertEqual(res_y["light"], "🟡黃燈")

        # Red light: close < MA45 and MA45 falling
        red_df = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=130, freq="B"),
                "close": [180.0] * 80 + [170.0] * 30 + [140.0] * 20,
            }
        )
        res_r = evaluate_market_regime(red_df, "0050")
        self.assertEqual(res_r["light"], "🔴紅燈")

    def test_build_rebound_watchlist_rows_matches_09_schema(self) -> None:
        from finmind_stock_scanner import (
            REBOUND_WATCHLIST_HEADERS,
            build_rebound_watchlist_rows,
            scan_rebound_from_prices,
        )

        analyzed = self._make_rebound_series(
            latest_close=113.0, latest_max=114.0, latest_min=110.0
        )
        rebound_df, failures_df = scan_rebound_from_prices(analyzed)
        self.assertEqual(len(failures_df), 0)
        self.assertEqual(len(rebound_df), 1)

        rows = build_rebound_watchlist_rows(
            rebound_df,
            as_of=date(2026, 10, 21),
            market_light="🟡黃燈",
            existing_master_map={"2357": {"ai_summary_json": '{"score":85}', "war_room_url": "https://x/2357.html"}},
        )
        self.assertEqual(len(rows), 1)
        for col in REBOUND_WATCHLIST_HEADERS:
            self.assertIn(col, rows[0])
        self.assertEqual(rows[0]["stock_id"], "2357")
        self.assertEqual(rows[0]["market_light"], "🟡黃燈")
        self.assertEqual(rows[0]["war_room_url"], "https://x/2357.html")


if __name__ == "__main__":
    unittest.main()
