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
    add_moving_averages,
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
        self.assertEqual(len(config["excluded_stocks"]), 19)

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
                "date": pd.date_range("2026-01-01", periods=50, freq="B"),
                "close": [100.0] * 49 + [100.5],
            }
        )

        analyzed = add_moving_averages(prices)
        signal = classify_latest_signal(analyzed)

        self.assertAlmostEqual(analyzed["MA17"].iloc[-1], 100.0294117647)
        self.assertAlmostEqual(analyzed["MA45"].iloc[-1], 100.0111111111)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["signal"], "接近（差距 <= 1%）")
        self.assertIsNone(signal["MA117"])
        self.assertIsNone(signal["MA189"])
        self.assertTrue(pd.isna(analyzed["MA117"].iloc[49]))
        self.assertTrue(pd.isna(analyzed["MA189"].iloc[49]))

    def test_classifies_ma45_above_ma17(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=45, freq="B"),
                "close": [150.0] * 44 + [100.0],
            }
        )

        signal = classify_latest_signal(add_moving_averages(prices))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["signal"], "已超過")

    def test_does_not_select_stocks_well_below_ma17(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.date_range("2026-01-01", periods=45, freq="B"),
                "close": [100.0] * 44 + [200.0],
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
        self.assertGreater(signal["MA17"], signal["MA117"])
        self.assertGreater(signal["MA17"], signal["MA189"])
        self.assertGreater(signal["MA45"], signal["MA117"])
        self.assertGreater(signal["MA45"], signal["MA189"])
        self.assertLessEqual(signal["MA17_MA45_gap_percent"], 1)

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
            (
                prices_path,
                signals_path,
                alignment_path,
                failures_path,
                signal_count,
                alignment_count,
            ) = scan_industry(
                loader=loader,
                stocks=stocks,
                market="上市",
                industry="半導體業",
                output_dir=Path(directory),
                as_of=date(2026, 3, 31),
            )

            self.assertTrue(prices_path.is_file())
            self.assertTrue(signals_path.is_file())
            self.assertTrue(alignment_path.is_file())
            self.assertIsNotNone(failures_path)
            assert failures_path is not None
            self.assertTrue(failures_path.is_file())
            self.assertEqual(signal_count, 1)
            self.assertEqual(alignment_count, 1)
            saved_prices = pd.read_csv(
                prices_path, dtype={"stock_id": str}, parse_dates=["date"]
            )
            saved_signals = pd.read_csv(signals_path)
            saved_alignment = pd.read_csv(alignment_path)
            saved_failures = pd.read_csv(failures_path, dtype={"stock_id": str})

        self.assertTrue((saved_prices["date"] >= pd.Timestamp("2025-09-30")).all())
        self.assertTrue((saved_prices["date"] <= pd.Timestamp("2026-03-31")).all())
        self.assertLess(len(saved_prices), 220)
        self.assertEqual(len(saved_signals), 1)
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
            self.assertTrue(reports["signals"].is_file())
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


if __name__ == "__main__":
    unittest.main()
