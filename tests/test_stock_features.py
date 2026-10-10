from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

import chart_generator
from chart_generator import (
    InvalidStockIDError,
    _add_indicators,
    _fetch_stock_data,
    _fetch_yahoo_volume,
    generate_stock_html_with_summary,
)
from github_uploader import upload_to_github


def sample_stock_data(row_count: int = 40) -> pd.DataFrame:
    closes = [float(value) for value in range(50, 50 + row_count)]
    return pd.DataFrame(
        {
            "date": pd.date_range(end=pd.Timestamp.today().normalize(), periods=row_count, freq="B"),
            "open": [close - 0.5 for close in closes],
            "max": [close + 1 for close in closes],
            "min": [close - 1 for close in closes],
            "close": closes,
            "Trading_Volume": [12_000] * row_count,
            "Regular_Trading_Volume": [17_216_484] * row_count,
        }
    )


class StockChartTests(unittest.TestCase):
    def test_indicators_calculate_moving_averages_volume_kd_and_macd(self) -> None:
        result = _add_indicators(sample_stock_data(row_count=240))

        self.assertAlmostEqual(result["MA17"].iloc[-1], 281)
        self.assertAlmostEqual(result["MA45"].iloc[-1], 267)
        self.assertAlmostEqual(result["MA117"].iloc[-1], 231)
        self.assertAlmostEqual(result["MA189"].iloc[-1], 195)
        self.assertEqual(result["VolumeLots"].iloc[-1], 17_216.484)
        self.assertTrue(result["K"].dropna().between(0, 100).all())
        self.assertTrue(result["D"].dropna().between(0, 100).all())
        self.assertTrue(pd.notna(result["MACD"].iloc[-1]))
        self.assertTrue(pd.notna(result["MACDSignal"].iloc[-1]))

    def test_generator_writes_responsive_html_and_returns_latest_summary(self) -> None:
        with tempfile.TemporaryDirectory() as output_directory:
            with (
                patch.object(chart_generator, "_OUTPUT_DIR", Path(output_directory)),
                patch.object(chart_generator, "_fetch_stock_data", return_value=sample_stock_data()),
            ):
                file_path, closing_price, trading_date = generate_stock_html_with_summary("2330")

            html_file = Path(file_path)
            self.assertTrue(html_file.is_file())
            self.assertEqual(html_file.name, "2330.html")
            self.assertEqual(closing_price, 89)
            self.assertEqual(
                trading_date,
                sample_stock_data().iloc[-1]["date"].strftime("%Y-%m-%d"),
            )
            html = html_file.read_text(encoding="utf-8")
            self.assertIn("KD", html)
            self.assertIn("MACD", html)
            self.assertIn("Yahoo Finance", html)
            self.assertIn(r"HTML \u7522\u51fa\u6642\u9593\uff08\u53f0\u7063\uff09\uff1a", html)
            self.assertIn("選取區間價格", html)
            self.assertIn("value.match(/^", html)
            self.assertIn("plotly_relayouting", html)
            self.assertIn("(high + low) / 2", html)
            self.assertIn("xaxis.autorange", html)
            self.assertIn('"showspikes":true', html)
            self.assertIn('"spikemode":"across"', html)
            self.assertIn('"spikesnap":"cursor"', html)
            self.assertIn('"rangebreaks":[{"values":', html)
            self.assertIn('"date":', html)
            self.assertIn('"high": 90.0, "low": 88.0', html)
            self.assertNotIn("__PRICE_RANGE_DATA__", html)
            self.assertIn("cdn.plot.ly", html)
            self.assertIn("responsive", html)

    def test_generator_rejects_invalid_stock_id_before_fetching(self) -> None:
        with patch.object(chart_generator, "_fetch_stock_data") as fetch_stock_data:
            with self.assertRaises(InvalidStockIDError):
                generate_stock_html_with_summary("2330<script>")
        fetch_stock_data.assert_not_called()

    def test_yahoo_volume_parses_shares_and_taiwan_trading_date(self) -> None:
        response = Mock()
        response.json.return_value = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1_788_224_400],
                        "indicators": {"quote": [{"volume": [17_216_484]}]},
                    }
                ]
            }
        }
        with patch("chart_generator.requests.get", return_value=response) as get_request:
            volumes = _fetch_yahoo_volume("2330", "2026-09-01", "2026-09-01")

        self.assertEqual(volumes.loc[0, "date"], pd.Timestamp("2026-09-01"))
        self.assertEqual(volumes.loc[0, "Regular_Trading_Volume"], 17_216_484)
        self.assertEqual(
            get_request.call_args.args[0],
            "https://query1.finance.yahoo.com/v8/finance/chart/2330.TW",
        )

    def test_finmind_prices_merge_with_yahoo_volume(self) -> None:
        trading_date = pd.Timestamp("2026-09-01")
        prices = pd.DataFrame(
            {
                "date": [trading_date],
                "open": [2395],
                "max": [2440],
                "min": [2390],
                "close": [2440],
                "Trading_Volume": [31_855_287],
            }
        )
        loader = Mock()
        loader.taiwan_stock_daily.return_value = prices
        yahoo_volumes = pd.DataFrame(
            {
                "date": [trading_date],
                "Regular_Trading_Volume": [17_216_484],
            }
        )
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("chart_generator.DataLoader", return_value=loader),
            patch("chart_generator._fetch_yahoo_volume", return_value=yahoo_volumes),
            patch("chart_generator.date") as mocked_date,
        ):
            mocked_date.today.return_value = trading_date.date()
            result = _fetch_stock_data("2330")

        self.assertEqual(result.loc[0, "Trading_Volume"], 31_855_287)
        self.assertEqual(result.loc[0, "Regular_Trading_Volume"], 17_216_484)


class GitHubUploaderTests(unittest.TestCase):
    def test_upload_creates_pages_file_and_returns_pages_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            chart_file = Path(directory) / "2330.html"
            chart_file.write_text("<html>chart</html>", encoding="utf-8")
            created_response = Mock(status_code=201)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "GITHUB_TOKEN": "test-token",
                        "GITHUB_REPO": "example/stock-charts",
                    },
                    clear=True,
                ),
                patch("github_uploader.requests.get", return_value=Mock(status_code=404)),
                patch("github_uploader.requests.put", return_value=created_response) as put_request,
            ):
                pages_url = upload_to_github(str(chart_file), "2330")

        self.assertEqual(
            pages_url,
            "https://example.github.io/stock-charts/charts/2330.html",
        )
        payload = put_request.call_args.kwargs["json"]
        self.assertEqual(payload["branch"], "gh-pages")
        self.assertEqual(
            put_request.call_args.args[0],
            "https://api.github.com/repos/example/stock-charts/contents/charts/2330.html",
        )
        self.assertEqual(
            base64.b64decode(payload["content"]).decode("utf-8"),
            "<html>chart</html>",
        )

    def test_upload_includes_sha_when_updating_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            chart_file = Path(directory) / "2330.html"
            chart_file.write_text("<html>updated</html>", encoding="utf-8")
            existing_file = Mock(status_code=200)
            existing_file.json.return_value = {"sha": "existing-file-sha"}
            with (
                patch.dict(
                    "os.environ",
                    {
                        "GITHUB_TOKEN": "test-token",
                        "GITHUB_REPO": "example/stock-charts",
                        "GITHUB_BRANCH": "gh-pages",
                    },
                    clear=True,
                ),
                patch("github_uploader.requests.get", return_value=existing_file),
                patch(
                    "github_uploader.requests.put",
                    return_value=Mock(status_code=200),
                ) as put_request,
            ):
                upload_to_github(str(chart_file), "2330")

        self.assertEqual(put_request.call_args.kwargs["json"]["sha"], "existing-file-sha")
        self.assertEqual(put_request.call_args.kwargs["json"]["branch"], "gh-pages")
        self.assertEqual(
            put_request.call_args.args[0],
            "https://api.github.com/repos/example/stock-charts/contents/charts/2330.html",
        )


if __name__ == "__main__":
    unittest.main()
