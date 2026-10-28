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

import ai_service
from alignment_ai_picker import (
    build_dossier,
    build_pick_rows,
    enrich_with_tracker,
    format_pick_report,
    load_alignment_candidates,
    parse_portfolio_json,
    post_messages_to_discord,
    prefilter_candidates,
    run_portfolio_selection,
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

    def test_parse_auto_repairs_trailing_hallucinated_words_and_unclosed_array(self) -> None:
        # Realistic error: AI omitted closing bracket of avoid array and appended a stray word before closing brace
        raw = (
            '{"market_comment": "盤勢震盪", '
            '"top_picks": [{"rank": 1, "stock_id": "2618", "score": 83, "outlook_verdict": "待驗證", "thesis": "營收成長", "risk": "油價"}], '
            '"watchlist": [{"stock_id": "6277", "reason": "毛利高"}], '
            '"avoid": [{"stock_id": "9912", "reason": "虧損"}, {"stock_id": "5450", "reason": "低量"} questionnaires}'
        )
        decision = parse_portfolio_json(raw, {"2618", "6277", "9912", "5450"}, max_top=3)
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision["market_comment"], "盤勢震盪")
        self.assertEqual([p["stock_id"] for p in decision["top_picks"]], ["2618"])
        self.assertEqual([w["stock_id"] for w in decision["watchlist"]], ["6277"])
        self.assertEqual([a["stock_id"] for a in decision["avoid"]], ["9912", "5450"])

    def test_parse_auto_repairs_trailing_commas(self) -> None:
        raw = (
            '{"market_comment": "好", '
            '"top_picks": [{"rank": 1, "stock_id": "2330", "score": 95, "outlook_verdict": "高度可信", "thesis": "強", "risk": "無",},], '
            '"watchlist": [], '
            '"avoid": [],}'
        )
        decision = parse_portfolio_json(raw, {"2330"}, max_top=1)
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual([p["stock_id"] for p in decision["top_picks"]], ["2330"])


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
            model_version="gemini-3.8-flash-001",
        )
        self.assertTrue(all(len(m) <= 2000 for m in messages))
        joined = "\n".join(messages)
        self.assertIn("🥇 #1 2330 台積電", joined)
        self.assertIn("🟢高度可信", joined)
        self.assertIn("全方位戰情室（含財報+K線）", joined)
        self.assertIn("1234", joined)
        self.assertIn("5 檔因超過候選上限", joined)
        self.assertIn("分析模型：gemini-3.8-flash-001", joined)

    def test_report_renders_tactical_type_and_target_prices_and_market_light(self) -> None:
        frame = _frame([{"stock_id": "2330", "stock_name": "台積電"}])
        results = {"2330": {"report": SimpleNamespace(war_room_url=""), "summary": "{}"}}
        decision = {
            "market_light": "🟡黃燈",
            "market_comment": "震盪環境以低接為主",
            "top_picks": [
                {
                    "rank": 1,
                    "stock_id": "2330",
                    "score": 88,
                    "tactical_type": "🎯 短中期波段持有 (5~20日)（依附 45MA）",
                    "target_price_1": "1,020 元 (17MA)",
                    "target_price_2": "1,100 元 (前高)",
                    "stop_loss_price": "950 元 (-4%)",
                    "outlook_verdict": "高度可信",
                    "thesis": "基本面佳",
                    "risk": "匯率",
                }
            ],
            "watchlist": [],
            "avoid": [],
        }
        messages = format_pick_report(
            date(2026, 10, 21),
            decision,
            "",
            frame,
            results,
            [],
            total_signals=5,
            skipped=0,
            model="gemini-3.8-flash",
            model_version="gemini-3.8-flash-001",
            market_regime={"light": "🟡黃燈", "summary": "🟡黃燈｜0050 收盤 190.00"},
        )
        joined = "\n".join(messages)
        self.assertIn("🚦 大盤紅綠燈：🟡黃燈｜0050 收盤 190.00", joined)
        self.assertIn("定位：🎯 短中期波段持有 (5~20日)（依附 45MA）", joined)
        self.assertIn("第一目標價 1,020 元 (17MA)", joined)
        self.assertIn("第二目標價 1,100 元 (前高)", joined)
        self.assertIn("嚴格停損 950 元 (-4%)", joined)

    def test_report_falls_back_to_raw_text_when_unparsed(self) -> None:
        frame = _frame([{"stock_id": "2330"}])
        messages = format_pick_report(
            date(2026, 10, 16),
            None,
            "RAW MODEL TEXT",
            frame,
            {},
            [],
            total_signals=1,
            skipped=0,
            model="m",
            model_version="",
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
        row = _frame(
            [{"stock_id": "2330", "stock_name": "台積電", "vol_lots": 5420, "vol_ma5_lots": 3800, "vol_ma20_lots": 2400, "vol_status": "🔥 放量點火 (2.26x｜5MA>20MA)"}]
        ).to_dict("records")[0]
        text = build_dossier(1, row, "  - snapshot", '{"verdict":"v"}')
        for needle in ("【候選標的 #1】2330 台積電", "[A.", "量能結構：今日 5,420 張", "[B.", "[C.", "snapshot", '"verdict"'):
            self.assertIn(needle, text)

    def test_volume_status_classification(self) -> None:
        from finmind_stock_scanner import describe_volume_status

        self.assertIn("低量冷門", describe_volume_status(180, 200, 210))
        self.assertIn("🔥 放量點火", describe_volume_status(5000, 3500, 2000))
        self.assertIn("5MA>20MA", describe_volume_status(5000, 3500, 2000))
        self.assertIn("📈 溫和放量", describe_volume_status(1500, 1300, 1000))
        self.assertIn("💧 量縮整理", describe_volume_status(600, 900, 1000))
        self.assertIn("⚠️ 天量過熱", describe_volume_status(5000, 2000, 1000))


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = FundamentalAnalyzer(Mock())

    def test_summary_extracts_json_after_marker(self) -> None:
        raw = '長文分析...\n===SUMMARY_JSON===\n{"verdict":"強","score":88,"highlights":["a"],"risks":["b"]}'
        human, parsed = self.analyzer._split_ai_output(raw)  # noqa: SLF001
        self.assertEqual(human, "長文分析...")
        self.assertEqual(parsed["score"], 88)
        summary = self.analyzer._summarize_ai(raw, model_version="gemini-test")  # noqa: SLF001
        self.assertEqual(json.loads(summary)["verdict"], "強")
        self.assertEqual(json.loads(summary)["model_version"], "gemini-test")
        self.assertIn("體質評分 88", self.analyzer._render_summary_text(summary))  # noqa: SLF001

    def test_summary_falls_back_to_text_without_valid_json(self) -> None:
        raw = "x" * 3000 + "\n===SUMMARY_JSON===\n{bad json"
        summary = self.analyzer._summarize_ai(raw, model_version="gemini-test")  # noqa: SLF001
        self.assertEqual(len(summary), 2500)
        self.assertIn("分析模型：gemini-test", summary)
        self.assertEqual(self.analyzer._render_summary_text("plain"), "plain")  # noqa: SLF001


class GeminiFallbackTests(unittest.TestCase):
    def test_default_call_signature_unchanged(self) -> None:
        with patch.object(ai_service, "_generate_ai_response", return_value="hi") as gen:
            self.assertEqual(asyncio.run(ai_service.ask_gemini("p")), "hi")
        gen.assert_called_once_with("p", None)

    def test_quota_error_degrades_to_fallback_model(self) -> None:
        calls: list[str | None] = []

        def fake(prompt: str, model: str | None = None) -> str:
            calls.append(model)
            if model == "gemini-3.8-flash":
                raise RuntimeError("429 RESOURCE_EXHAUSTED daily quota")
            return "fallback answer"

        with patch.object(ai_service, "_generate_ai_response", side_effect=fake):
            answer = asyncio.run(
                ai_service.ask_gemini("p", model="gemini-3.8-flash", fallback_model="gemini-flash-lite-latest")
            )
        self.assertEqual(answer, "fallback answer")
        self.assertEqual(calls, ["gemini-3.8-flash", "gemini-flash-lite-latest"])

    def test_non_quota_error_is_not_swallowed(self) -> None:
        with patch.object(ai_service, "_generate_ai_response", side_effect=ValueError("bad prompt")):
            with self.assertRaises(ValueError):
                asyncio.run(ai_service.ask_gemini("p", model="a", fallback_model="b"))

    def test_portfolio_selection_uses_vertex_ai_and_env_vertex_model(self) -> None:
        with (
            patch.dict("os.environ", {"VERTEX_MODEL": "gemini-2.5-pro"}, clear=False),
            patch("alignment_ai_picker.ask_vertex_ai_with_meta") as vertex_call,
        ):
            vertex_call.return_value = ai_service.AIResponse(
                provider="vertex_ai",
                requested_model="gemini-2.5-pro",
                model_version="gemini-2.5-pro-001",
                text='{"top_picks":[]}',
                prompt_tokens=1,
                output_tokens=2,
            )
            model = ai_service.get_vertex_model()
            res = asyncio.run(
                run_portfolio_selection(
                    ["dossier-1"],
                    max_top=5,
                    model=model,
                    market_regime={"light": "🟢綠燈", "summary": "🟢綠燈"},
                )
            )
        self.assertEqual(model, "gemini-2.5-pro")
        self.assertEqual(getattr(res, "text", None), '{"top_picks":[]}')
        vertex_call.assert_called_once()
        self.assertEqual(vertex_call.call_args.kwargs["model"], "gemini-2.5-pro")


class DiscordPostReplyTests(unittest.TestCase):
    def test_multi_chunk_messages_reply_to_first_message(self) -> None:
        calls = []

        def fake_urlopen(request, timeout=10):
            payload = json.loads(request.data.decode("utf-8"))
            calls.append(payload)
            mock_resp = Mock()
            mock_resp.status = 200
            mock_resp.__enter__ = Mock(return_value=mock_resp)
            mock_resp.__exit__ = Mock(return_value=None)
            mock_resp.read.return_value = json.dumps({"id": "msg-first-12345"}).encode("utf-8")
            return mock_resp

        messages = [
            "Message chunk 1",
            "Title\n（續）\n\n#3 3010 華立",
            "Another title\n（續）\n\n#4 2330 台積電",
        ]
        with patch("alignment_ai_picker.urlopen", side_effect=fake_urlopen):
            with patch("time.sleep", return_value=None):
                sent = post_messages_to_discord(messages, channel_id="123456789", bot_token="fake-token")

        self.assertEqual(sent, 3)
        self.assertEqual(len(calls), 3)
        # Message 1 should NOT have message_reference
        self.assertNotIn("message_reference", calls[0])
        self.assertEqual(calls[0]["content"], "Message chunk 1")
        # Message 2 and 3 MUST reply to message 1
        self.assertIn("message_reference", calls[1])
        self.assertEqual(calls[1]["message_reference"]["message_id"], "msg-first-12345")
        self.assertFalse(calls[1]["message_reference"]["fail_if_not_exists"])
        self.assertEqual(calls[1]["allowed_mentions"], {"replied_user": False})
        # Continuation chunks should be trimmed (no repeated heading / no （續）)
        self.assertEqual(calls[1]["content"], "#3 3010 華立")

        self.assertIn("message_reference", calls[2])
        self.assertEqual(calls[2]["message_reference"]["message_id"], "msg-first-12345")
        self.assertEqual(calls[2]["content"], "#4 2330 台積電")


if __name__ == "__main__":
    unittest.main()
