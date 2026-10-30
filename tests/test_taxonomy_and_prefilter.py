"""SASD v5.0：全市場 ALL 展開、雙軌流動性、下市偵測、同業防幻覺、細分產業限額、限流器網域過濾。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

from alignment_ai_picker import (
    _effective_sector_key,
    enforce_sector_cap,
    limit_stage2_candidates,
)
from finmind_stock_scanner import (
    _configured_universe,
    _rate_limit_finmind_http_requests,
)
from taxonomy_service import (
    VALID_COMPONENT_NATURES,
    format_taxonomy_badge,
    passes_dual_track_liquidity,
    sanitize_taxonomy_record,
    update_registry_with_daily_snapshot,
)


def _stock_info() -> pd.DataFrame:
    rows = [
        ("2330", "台積電", "半導體業", "twse"),
        ("2327", "國巨", "電子零組件業", "twse"),
        ("3702A", "大聯大特", "電子通路業", "twse"),
        ("3036A", "文曄特", "電子通路業", "twse"),
        ("0050", "ETF 測試", "ETF", "twse"),
        ("6488", "環球晶", "半導體業", "tpex"),
    ]
    return pd.DataFrame(
        rows, columns=["stock_id", "stock_name", "industry_category", "type"]
    )


def _config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "markets": ["上市", "上櫃"],
        "industries": ["ALL"],
        "excluded_stocks": [],
        "excluded_industry_categories": ["ETF"],
        "only_common_stocks_4digits": True,
    }
    config.update(overrides)
    return config


class TaxonomyAndPrefilterTests(unittest.TestCase):
    def test_configured_universe_supports_all_and_4digit_filter(self) -> None:
        universe = _configured_universe(_stock_info(), _config())

        all_ids = {
            sid for frame in universe.values() for sid in frame["stock_id"].tolist()
        }
        self.assertIn("2330", all_ids)
        self.assertIn("2327", all_ids)
        self.assertIn("6488", all_ids)
        self.assertNotIn("3702A", all_ids)
        self.assertNotIn("3036A", all_ids)
        self.assertNotIn("0050", all_ids)
        self.assertFalse(any(industry == "ETF" for _m, industry in universe))

    def test_configured_universe_applies_prefilter_and_registry_delisting(self) -> None:
        snapshot = {
            f"{1000 + i}": {"vol_lots": 1.0, "turnover_twd": 1.0, "close": 10.0}
            for i in range(900)
        }
        snapshot["2330"] = {"vol_lots": 30000.0, "turnover_twd": 3e10, "close": 1000.0}
        snapshot["2327"] = {"vol_lots": 10.0, "turnover_twd": 1e6, "close": 100.0}
        snapshot["6488"] = {"vol_lots": 5000.0, "turnover_twd": 1e9, "close": 500.0}
        registry = {"6488": {"status": "DELISTED"}}

        universe = _configured_universe(
            _stock_info(),
            _config(enable_twse_tpex_prefilter=True),
            snapshot,
            registry,
        )
        all_ids = {
            sid for frame in universe.values() for sid in frame["stock_id"].tolist()
        }
        self.assertEqual(all_ids, {"2330"})

    def test_dual_track_liquidity_protects_high_price_stocks(self) -> None:
        snapshot = {
            f"{1000 + i}": {"vol_lots": 1.0, "turnover_twd": 1.0, "close": 10.0}
            for i in range(900)
        }
        snapshot["5555"] = {"vol_lots": 400.0, "turnover_twd": 2e8, "close": 500.0}
        snapshot["6666"] = {"vol_lots": 200.0, "turnover_twd": 3e6, "close": 15.0}
        registry = {
            "5555": {"vol_ma20_lots": 300.0},
            "6666": {"vol_ma20_lots": 180.0},
        }
        self.assertTrue(passes_dual_track_liquidity("5555", snapshot, registry))
        self.assertFalse(passes_dual_track_liquidity("6666", snapshot, registry))
        # 當日無收盤報價（不在快照）→ 排除
        self.assertFalse(passes_dual_track_liquidity("7777", snapshot, registry))

    def test_delisted_auto_detection_after_missing_threshold(self) -> None:
        info = pd.DataFrame(
            [
                {"stock_id": "2330", "stock_name": "台積電", "industry_category": "半導體業"},
                {"stock_id": "2473", "stock_name": "思源", "industry_category": "電子零組件業"},
            ]
        )
        snapshot = {
            f"{1000 + i}": {"vol_lots": 1.0, "turnover_twd": 1.0, "close": 10.0}
            for i in range(900)
        }
        snapshot["2330"] = {"vol_lots": 100.0, "turnover_twd": 1e8, "close": 1000.0}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            start = date(2026, 10, 1)
            reg: dict = {}
            for offset in range(5):
                reg = update_registry_with_daily_snapshot(
                    path, info, snapshot, start + timedelta(days=offset), 5
                )
                if offset < 4:
                    self.assertEqual(reg["stocks"]["2473"]["status"], "ACTIVE")
            self.assertEqual(reg["stocks"]["2473"]["status"], "DELISTED")
            self.assertEqual(reg["stocks"]["2330"]["status"], "ACTIVE")
            self.assertFalse(
                passes_dual_track_liquidity("2473", snapshot, reg["stocks"])
            )

    def test_sanitize_taxonomy_hallucinated_peers(self) -> None:
        record = sanitize_taxonomy_record(
            {
                "sector_group": "不存在板塊",
                "sub_industry": "被動元件-MLCC與晶片電阻",
                "component_nature": "神秘元件",
                "peers": ["2492 華新科", "9999 虛構科技", "2327 國巨"],
                "theme_tags": ["車用電子"],
            },
            stock_id="2327",
            stock_name="國巨",
            official_industry="電子零組件業",
            valid_stock_map={"2327": "國巨", "2492": "華新科"},
            as_of=date(2026, 10, 29),
        )
        self.assertEqual(record["peers"], '["2492 華新科"]')
        self.assertIn(record["component_nature"], VALID_COMPONENT_NATURES)
        self.assertEqual(record["sector_group"], "電子硬體與零組件")
        self.assertEqual(record["updated_at"], "2026-10-29")
        self.assertIn("2492 華新科", format_taxonomy_badge(record))

    def test_stage2_sub_industry_cap_allows_different_sub_industries(self) -> None:
        def row(sid: str, sub: str) -> dict:
            return {
                "stock_id": sid,
                "industry_category": "電子零組件業",
                "sub_industry": sub,
            }

        frame = pd.DataFrame(
            [
                row("2327", "被動元件-MLCC"),
                row("2492", "被動元件-MLCC"),
                row("6173", "被動元件-MLCC"),
                row("3037", "PCB-ABF載板"),
                row("8046", "PCB-ABF載板"),
            ]
        )
        decision = {
            "top_picks": [
                {"rank": i, "stock_id": sid}
                for i, sid in enumerate(["2327", "2492", "6173", "3037", "8046"], 1)
            ],
            "watchlist": [],
            "avoid": [],
        }
        result = enforce_sector_cap(decision, frame, cap=2)
        self.assertEqual(
            [p["stock_id"] for p in result["top_picks"]],
            ["2327", "2492", "3037", "8046"],
        )
        self.assertEqual([w["stock_id"] for w in result["watchlist"]], ["6173"])

    def test_effective_sector_key_falls_back_to_industry(self) -> None:
        self.assertEqual(
            _effective_sector_key({"stock_id": "1", "sub_industry": "A", "industry_category": "B"}),
            "A",
        )
        self.assertEqual(
            _effective_sector_key({"stock_id": "1", "sub_industry": "", "industry_category": "B"}),
            "B",
        )
        self.assertEqual(_effective_sector_key({"stock_id": "9"}), "__NO_SECTOR__:9")

    def test_limit_stage2_uses_sub_industry(self) -> None:
        rows = [
            {
                "stock_id": f"{2000 + i}",
                "industry_category": "電子零組件業",
                "sub_industry": "MLCC" if i % 2 == 0 else "PCB",
            }
            for i in range(20)
        ]
        trimmed = limit_stage2_candidates(pd.DataFrame(rows), {})
        self.assertEqual(len(trimmed), 6)  # 兩個細分產業 × 3 檔

    def test_rate_limiter_only_throttles_finmind_host(self) -> None:
        limiter = Mock()
        with patch(
            "finmind_stock_scanner.requests.adapters.HTTPAdapter.send",
            autospec=True,
        ) as adapter_send:
            with _rate_limit_finmind_http_requests(
                limiter, only_hosts=("finmindtrade.com",)
            ):
                adapter = requests.adapters.HTTPAdapter()
                for url in (
                    "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX_ALL",
                    "https://tw.stock.yahoo.com/quote/2330.TW/profile",
                ):
                    adapter.send(requests.Request("GET", url).prepare())
                limiter.wait_for_slot.assert_not_called()
                adapter.send(
                    requests.Request(
                        "GET", "https://api.finmindtrade.com/api/v4/data"
                    ).prepare()
                )
                limiter.wait_for_slot.assert_called_once()
        self.assertEqual(adapter_send.call_count, 3)


if __name__ == "__main__":
    unittest.main()
