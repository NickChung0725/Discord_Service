from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import ai_weather
from alignment_ai_picker import (
    build_dossier,
    build_pick_rows,
    enrich_with_tracker,
    format_pick_report,
    load_alignment_candidates,
    parse_portfolio_json,
    prefilter_candidates,
)
from fundamental_analyzer import FundamentalAnalyzer


def _frame(rows: list[dict]) -> pd.DataFrame:
    base = {
        "market": "上市",
        "industry_category": "半導體業",
        "stock_name": "測試",
        "date": "2026-10-16",
        "close": 100.0,
        "MA3": 101.0,
        "MA17": 99.0,
        "MA45": 98.5,
        "MA117": 90.0,
        "MA189": 80.0,
        "gap_percent": -0.5,
        "signal": "已超過",
        "ma17_trend": "📈 連漲上揚 (5日 +1.00%)",
        "is_new": False,
        "streak": 3,
        "daily_status": "🔄 持續符合",
    }
    return pd.DataFrame([{**base, **row} for row in rows])


class CandidateSelectionTests(unittest.TestCase):
    def test_load_returns_none_when_file_missing_and_renames_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp)
            self.assertIsNone(load_alignment_candidates(daily, date(2026, 10, 16)))
            pd.DataFrame(
                [{"stock_id": "2330", "stock_name": "台積電", "MA17_MA45_gap_percent": -0.3}]
            ).to_csv(daily / "2026-10-16_alignment_signals.csv", index=False)
            loaded = load_alignment_candidates(daily, date(2026, 10, 16))
            self.assertEqual(loaded.loc[0, "stock_id"], "2330")
            self.assertAlmostEqual(loaded.loc[0, "gap_percent"], -0.3)

    def test_prefilter_prefers_new_then_rising_trend_then_small_gap(self) -> None:
        frame = _frame(
            [
                {"stock_id": "1111", "is_new": False, "ma17_trend": "📉 持續下彎", "gap_percent": -0.1},
                {"stock_id": "2222", "is_new": True, "ma17_trend": "📈 連漲上揚", "gap_percent": -0.9},
                {"stock_id": "3333", "is_new": False, "ma17_trend": "📈 連漲上揚", "gap_percent": -0.2},
                {"stock_id": "4444", "is_new": False, "daily_status": "🔥 轉強突破", "ma17_trend": "↗️ 轉折向上", "gap_percent": -0.4},
            ]
        )
        selected, skipped = prefilter_candidates(frame, 3)
        self.assertEqual(skipped, 1)
        self.assertEqual(list(selected["stock_id"]), ["4444", "2222", "3333"])

    def test_prefilter_noop_below_cap(self) -> None:
        frame = _frame([{"stock_id": "1111"}, {"stock_id": "2222"}])
        selected, skipped = prefilter_candidates(frame, 25)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(selected), 2)

    def test_enrich_marks_first_appearance_as_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frame = _frame([{"stock_id": "2330"}]).drop(columns=["is_new", "streak", "daily_status"])
            enriched = enrich_with_tracker(frame, Path(tmp), date(2026, 10, 16))
            self.assertTrue(bool(enriched.loc[0, "is_new"]))
            self.assertEqual(int(enriched.loc[0, "streak"]), 1)


class PortfolioParsingTests(unittest.TestCase):
    def test_parse_strips_fences_and_drops_unknown_or_duplicate_ids(self) -> None:
        raw = "```json\n" + json.dumps(
            {
                "market_comment": "ok",
                "top_picks": [
                    {"stock_id": "2330", "score": 90, "thesis": "t", "risk": "r"},
                    {"stock_id": "9999", "score": 80},
                    {"stock_id": "2330", "score": 70},
                    {"stock_id": "2317", "score": 60},
                ],
                "watchlist": [{"stock_id": "2330", "reason": "dup"}, {"stock_id": "3008", "reason": "w"}],
                "avoid": [{"stock_id": "2454", "reason": "a"}],
            }
        ) + "\n```"
        decision = parse_portfolio_json(raw, {"2330", "2317", "3008", "2454"}, max_top=2)
        self.assertEqual([p["stock_id"] for p in decision["top_picks"]], ["2330", "2317"])
        self.assertEqual([p["rank"] for p in decision["top_picks"]], [1, 2])
        self.assertEqual([w["stock_id"] for w in decision["watchlist"]], ["3008"])
        self.assertEqual([a["stock_id"] for a in decision["avoid"]], ["2454"])

    def test_parse_returns_none_on_garbage(self) -> None:
        self.assertIsNone(parse_portfolio_json("no json here", {"2330"}, 3))
        self.assertIsNone(parse_portfolio_json("{broken", {"2330"}, 3))


class ReportTests(unittest.TestCase):
    def _decision(self) -> dict:
        return {
            "market_comment": "整體偏多",
            "top_picks": [
                {"rank": 1, "stock_id": "2330", "score": 90, "outlook_verdict": "高度可信", "thesis": "理由", "risk": "風險"}
            ],
            "watchlist": [{"stock_id": "2317", "reason": "等突破"}],
            "avoid": [],
        }

    def test_report_chunks_stay_under_limit_and_include_sections(self) -> None:
        frame = _frame([{"stock_id": "2330", "stock_name": "台積電"}, {"stock_id": "2317", "stock_name": "鴻海"}])
        results = {"2330": {"report": SimpleNamespace(war_room_url="https://x.invalid/a.html"), "summary": "{}"}}
        decision = self._decision()
        decision["watchlist"] = [{"stock_id": "2317", "reason": "很長" * 400}]
        messages = format_pick_report(
            date(2026, 10, 16),
            decision,
            "",
            frame,
            results,
            [("1234", "財報資料不足")],
            total_signals=30,
            skipped=5,
            model="gemini-3.8-flash",
        )
        self.assertTrue(all(len(m) <= 2000 for m in messages))
        joined = "\n".join(messages)
        self.assertIn("🥇 #1 2330 台積電", joined)
        self.assertIn("🟢高度可信", joined)
        self.assertIn("全方位戰情室（含財報+K線）", joined)
        self.assertIn("1234", joined)
        self.assertIn("5 檔因超過候選上限", joined)

    def test_report_falls_back_to_raw_text_when_unparsed(self) -> None:
        frame = _frame([{"stock_id": "2330"}])
        messages = format_pick_report(
            date(2026, 10, 16), None, "RAW MODEL TEXT", frame, {}, [], total_signals=1, skipped=0, model="m"
        )
        self.assertIn("RAW MODEL TEXT", "\n".join(messages))

    def test_pick_rows_cover_all_categories(self) -> None:
        frame = _frame([{"stock_id": "2330"}, {"stock_id": "2317"}, {"stock_id": "2454"}])
        decision = self._decision()
        decision["avoid"] = [{"stock_id": "2454", "reason": "x"}]
        results = {
            sid: {"report": SimpleNamespace(war_room_url=""), "summary": json.dumps({"score": 77})}
            for sid in ("2330", "2317", "2454")
        }
        rows = build_pick_rows(date(2026, 10, 16), decision, frame, results)
        self.assertEqual([r["category"] for r in rows], ["TOP_PICK", "WATCHLIST", "AVOID"])
        self.assertEqual(rows[0]["pk_key"], "2026-10-16_2330")
        self.assertEqual(rows[0]["fundamental_score"], 77)

    def test_dossier_contains_all_sections(self) -> None:
        row = _frame([{"stock_id": "2330", "stock_name": "台積電"}]).to_dict("records")[0]
        text = build_dossier(1, row, "  - snapshot", '{"verdict":"v"}')
        for needle in ("【候選標的 #1】2330 台積電", "[A.", "[B.", "[C.", "snapshot", '"verdict"'):
            self.assertIn(needle, text)


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = FundamentalAnalyzer(Mock())

    def test_summary_extracts_json_after_marker(self) -> None:
        raw = '長文分析...\n===SUMMARY_JSON===\n{"verdict":"強","score":88,"highlights":["a"],"risks":["b"]}'
        human, parsed = self.analyzer._split_ai_output(raw)  # noqa: SLF001
        self.assertEqual(human, "長文分析...")
        self.assertEqual(parsed["score"], 88)
        summary = self.analyzer._summarize_ai(raw)  # noqa: SLF001
        self.assertEqual(json.loads(summary)["verdict"], "強")
        self.assertIn("體質評分 88", self.analyzer._render_summary_text(summary))  # noqa: SLF001

    def test_summary_falls_back_to_text_without_valid_json(self) -> None:
        raw = "x" * 3000 + "\n===SUMMARY_JSON===\n{bad json"
        summary = self.analyzer._summarize_ai(raw)  # noqa: SLF001
        self.assertEqual(len(summary), 2500)
        self.assertEqual(self.analyzer._render_summary_text("plain"), "plain")  # noqa: SLF001


class GeminiFallbackTests(unittest.TestCase):
    def test_default_call_signature_unchanged(self) -> None:
        with patch.object(ai_weather, "_generate_ai_response", return_value="hi") as gen:
            self.assertEqual(asyncio.run(ai_weather.ask_gemini("p")), "hi")
        gen.assert_called_once_with("p")

    def test_quota_error_degrades_to_fallback_model(self) -> None:
        calls: list[str | None] = []

        def fake(prompt: str, model: str | None = None) -> str:
            calls.append(model)
            if model == "gemini-3.8-flash":
                raise RuntimeError("429 RESOURCE_EXHAUSTED daily quota")
            return "fallback answer"

        with patch.object(ai_weather, "_generate_ai_response", side_effect=fake):
            answer = asyncio.run(
                ai_weather.ask_gemini("p", model="gemini-3.8-flash", fallback_model="gemini-flash-lite-latest")
            )
        self.assertEqual(answer, "fallback answer")
        self.assertEqual(calls, ["gemini-3.8-flash", "gemini-flash-lite-latest"])

    def test_non_quota_error_is_not_swallowed(self) -> None:
        with patch.object(ai_weather, "_generate_ai_response", side_effect=ValueError("bad prompt")):
            with self.assertRaises(ValueError):
                asyncio.run(ai_weather.ask_gemini("p", model="a", fallback_model="b"))


if __name__ == "__main__":
    unittest.main()
