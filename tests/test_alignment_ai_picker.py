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

import tests  # noqa: F401
import ai_service
from alignment_ai_picker import (
    build_dossier,
    build_pick_rows,
    enforce_sector_cap,
    enrich_with_tracker,
    format_pick_report,
    hard_prefilter,
    limit_stage2_candidates,
    load_alignment_candidates,
    merge_pre_filtered_avoid,
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
                    {"stock_id": "2330", "score": 90, "bull_thesis": "t", "bear_case_and_invalidation": "r"},
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
            '"top_picks": [{"rank": 1, "stock_id": "2618", "score": 83, "outlook_verdict": "待驗證", "bull_thesis": "營收成長", "bear_case_and_invalidation": "油價"}], '
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
            '"top_picks": [{"rank": 1, "stock_id": "2330", "score": 95, "outlook_verdict": "高度可信", "bull_thesis": "強", "bear_case_and_invalidation": "無",},], '
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
                {"rank": 1, "stock_id": "2330", "score": 90, "outlook_verdict": "高度可信", "bull_thesis": "理由", "bear_case_and_invalidation": "風險", "entry_zone_and_trigger": "拉回17MA低接"}
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
                    "entry_zone_and_trigger": "拉回 17MA 990~1000 元，或帶量突破 3MA",
                    "bull_thesis": "基本面佳",
                    "bear_case_and_invalidation": "匯率；跌破 45MA 退場",
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
        self.assertIn("進場觸發：拉回 17MA 990~1000 元，或帶量突破 3MA", joined)
        self.assertIn("理由（藍軍）：基本面佳", joined)
        self.assertIn("風險與失效（紅軍）：匯率；跌破 45MA 退場", joined)
        self.assertNotIn("理由：", joined)

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
        decision["top_picks"][0]["tactical_type"] = "🎯 短中期波段 (5~20日)"
        decision["top_picks"][0]["target_price_1"] = "1,120 元"
        decision["top_picks"][0]["target_price_2"] = "1,200 元"
        decision["top_picks"][0]["stop_loss_price"] = "960 元"
        decision["avoid"] = [{"stock_id": "2454", "reason": "x"}]
        s1_summary = json.dumps(
            {
                "score": 77,
                "trade_plan": {
                    "tactical_type": "波段",
                    "target_price_1": "225 元 (前高)",
                    "target_price_2": "245 元",
                    "stop_loss_price": "192 元",
                    "invalidation": "跌破 45MA",
                },
            }
        )
        results = {
            sid: {"report": SimpleNamespace(war_room_url=f"https://example.com/{sid}_fund.html"), "summary": s1_summary}
            for sid in ("2330", "2317", "2454")
        }
        rows = build_pick_rows(date(2026, 10, 16), decision, frame, results)
        self.assertEqual([r["category"] for r in rows], ["TOP_PICK", "WATCHLIST", "AVOID"])
        self.assertEqual(rows[0]["pk_key"], "2026-10-16_2330")
        self.assertEqual(rows[0]["fundamental_score"], 77)
        self.assertEqual(rows[0]["entry_thesis"], "【進場】拉回17MA低接｜理由")
        self.assertEqual(rows[0]["risk_note"], "風險")
        self.assertEqual(rows[0]["target_price_1"], "1,120 元")
        self.assertEqual(rows[0]["target_price_2"], "1,200 元")
        self.assertEqual(rows[0]["stop_loss_price"], "960 元")
        # WATCHLIST inherits trade_plan from Stage 1 summary; AVOID leaves target prices blank
        self.assertEqual(rows[1]["target_price_1"], "225 元 (前高)")
        self.assertEqual(rows[1]["risk_note"], "跌破 45MA")
        self.assertEqual(rows[2]["target_price_1"], "")
        for r in rows:
            self.assertTrue(r["war_room_url"].startswith("https://example.com/"))

    def test_war_room_url_populated_for_all_analyzed_candidates(self) -> None:
        # 驗證即使非 TOP_PICK（包含 WATCHLIST 與 AVOID），只要經過 AI 深度分析皆具備戰情室網址
        frame = _frame([{"stock_id": "2330"}, {"stock_id": "2317"}, {"stock_id": "2454"}])
        decision = self._decision()
        decision["watchlist"] = [{"stock_id": "2317", "reason": "次佳"}]
        decision["avoid"] = [{"stock_id": "2454", "reason": "避開"}]
        results = {
            "2330": {"report": SimpleNamespace(war_room_url="https://example.com/2330_fund.html"), "summary": "{}"},
            "2317": {"report": SimpleNamespace(war_room_url="https://example.com/2317_fund.html"), "summary": "{}"},
            "2454": {"report": SimpleNamespace(war_room_url="https://example.com/2454_fund.html"), "summary": "{}"},
        }
        rows = build_pick_rows(date(2026, 10, 16), decision, frame, results)
        url_map = {r["stock_id"]: r["war_room_url"] for r in rows}
        self.assertEqual(url_map["2330"], "https://example.com/2330_fund.html")
        self.assertEqual(url_map["2317"], "https://example.com/2317_fund.html")
        self.assertEqual(url_map["2454"], "https://example.com/2454_fund.html")

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


class Stage0AndStage2GuardTests(unittest.TestCase):
    def test_hard_prefilter_blocks_low_volume_and_loss_without_contract_liab(self) -> None:
        frame = _frame(
            [
                {"stock_id": "1111", "vol_lots": 250},
                {"stock_id": "2222", "vol_lots": 5000},
                {"stock_id": "3333", "vol_lots": 5000},
                {"stock_id": "4444", "vol_lots": 5000},
                {"stock_id": "5555"},
            ]
        )
        metrics = {
            "2222": {"op_margin_pct": -3.0, "contract_liab": 0.2},
            "3333": {"op_margin_pct": -3.0, "contract_liab": 12.0},
            "4444": {"op_margin_pct": 8.0, "contract_liab": 0.0},
        }
        kept, avoid = hard_prefilter(frame, metrics)
        self.assertEqual(list(kept["stock_id"]), ["3333", "4444", "5555"])
        self.assertEqual([a["stock_id"] for a in avoid], ["1111", "2222"])
        self.assertIn("< 300 張", avoid[0]["reason"])
        self.assertIn("營益率", avoid[1]["reason"])

    def test_merge_pre_filtered_avoid_appends_without_duplicates(self) -> None:
        decision = {
            "top_picks": [{"stock_id": "2330"}],
            "watchlist": [{"stock_id": "2317"}],
            "avoid": [{"stock_id": "9999", "reason": "ai"}],
        }
        merged = merge_pre_filtered_avoid(
            decision,
            [{"stock_id": "1111", "reason": "x"}, {"stock_id": "9999", "reason": "dup"}, {"stock_id": "2330", "reason": "dup"}],
        )
        self.assertEqual([a["stock_id"] for a in merged["avoid"]], ["1111", "9999"])

    def test_enforce_sector_cap_demotes_third_pick_in_same_sector(self) -> None:
        frame = _frame(
            [
                {"stock_id": "2603", "industry_category": "航運業"},
                {"stock_id": "2609", "industry_category": "航運業"},
                {"stock_id": "2615", "industry_category": "航運業"},
                {"stock_id": "2330", "industry_category": "半導體業"},
            ]
        )
        decision = {
            "top_picks": [{"rank": i, "stock_id": s} for i, s in enumerate(["2603", "2609", "2615", "2330"], start=1)],
            "watchlist": [],
            "avoid": [],
        }
        result = enforce_sector_cap(decision, frame)
        self.assertEqual([p["stock_id"] for p in result["top_picks"]], ["2603", "2609", "2330"])
        self.assertEqual([p["rank"] for p in result["top_picks"]], [1, 2, 3])
        self.assertEqual([w["stock_id"] for w in result["watchlist"]], ["2615"])
        self.assertIn("同產業名額限制", result["watchlist"][0]["reason"])

    def test_limit_stage2_candidates_only_trims_above_threshold(self) -> None:
        ids = [f"{1000 + i}" for i in range(16)]
        frame = _frame([{"stock_id": sid, "industry_category": "X"} for sid in ids])
        results = {sid: {"summary": json.dumps({"score": i})} for i, sid in enumerate(ids)}
        trimmed = limit_stage2_candidates(frame, results)
        self.assertEqual(len(trimmed), 3)  # 同產業最多取 3 檔進入 Stage 2
        self.assertEqual(len(limit_stage2_candidates(frame.head(15), results)), 15)

    def test_limit_stage2_candidates_penalizes_overheated(self) -> None:
        frame = _frame(
            [
                {"stock_id": "2330", "industry_category": "半導體", "close": 120.0, "MA17": 100.0},
                {"stock_id": "2603", "industry_category": "航運", "close": 100.0, "MA17": 100.0},
            ]
        )
        results = {
            "2330": {"summary": json.dumps({"score": 90})},
            "2603": {"summary": json.dumps({"score": 80})},
        }
        large = pd.concat([frame] * 8, ignore_index=True)  # 2 rows * 8 = 16 rows (> threshold)
        trimmed = limit_stage2_candidates(large, results)
        # 2330 超漲(>17MA+12%) 會被扣分，排序應落後 2603
        self.assertEqual(trimmed.iloc[0]["stock_id"], "2603")


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
            patch.dict("os.environ", {"VERTEX_STAGE2_MODEL": "gemini-2.5-pro"}, clear=False),
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
            model = ai_service.get_vertex_stage2_model()
            res = asyncio.run(
                run_portfolio_selection(
                    ["dossier-1"],
                    max_top=5,
                    model=model,
                    market_regime={"light": "🟢綠燈", "summary": "🟢綠燈"},
                    selection_mode="trend",
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


import numpy as np
from unittest.mock import AsyncMock

from alignment_ai_picker import (
    NO_PICK_MESSAGE,
    BatchAuditResponse,
    StockAuditItem,
    _enforce_post_audit_guards,
    apply_audit_to_decision,
    build_audit_record,
    run_red_blue_audit,
    select_audit_survivors,
)
from finmind_stock_scanner import (
    align_news_with_price_action,
    compute_deterministic_trade_setup,
)
from taishin_portfolio_service import evaluate_candidate_exposure


def _audit_item(stock_id: str = "2330", *, grade: str = "S_TIER_SNIPER", tactic: str = "BREAKOUT_0915_VWAP_OR_1315_CLOSE") -> StockAuditItem:
    return StockAuditItem(
        stock_id=stock_id,
        stock_name="測試",
        bear_case_flaws=["上方 POC 套牢", "長上影線"],
        bull_case_catalysts=["投信買超"],
        conflict_resolution_reasoning="多頭勝出",
        execution_strategy_type=tactic,
        thesis_invalidation_triggers=["投信轉賣超", "跌破紅K一半"],
        verdict_grade=grade,
    )


def _ohlcv(n: int = 70, *, start_close: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    dates = pd.bdate_range("2026-07-01", periods=n)
    close = np.array([start_close + step * i for i in range(n)], dtype=float)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.3,
            "max": close + 1.0,
            "min": close - 1.0,
            "close": close,
            "Trading_Volume": np.full(n, 1_000_000.0),
        }
    )


class RedBlueAuditPhase1Test(unittest.TestCase):
    def test_schema_field_order_enforces_reasoning_before_verdict(self) -> None:
        keys = list(StockAuditItem.model_json_schema()["properties"].keys())
        self.assertEqual(keys[2], "bear_case_flaws")
        self.assertEqual(keys[-1], "verdict_grade")

    def test_zero_pick_veto_when_all_candidates_rejected(self) -> None:
        items = [_audit_item("2330", grade="REJECT_TRAP", tactic="NO_TRADE"), _audit_item("2317", grade="REJECT_TRAP", tactic="NO_TRADE")]
        payload = BatchAuditResponse(audited_stocks=items).model_dump_json()
        blocks = [("2330", "block"), ("2317", "block")]
        with patch(
            "alignment_ai_picker.ask_vertex_ai_with_meta",
            new=AsyncMock(return_value=SimpleNamespace(text=payload)),
        ):
            audited, missing = asyncio.run(run_red_blue_audit(blocks))
        self.assertEqual(missing, [])
        self.assertEqual(select_audit_survivors(_enforce_post_audit_guards(audited, {}, {})), [])

        decision = {"market_comment": NO_PICK_MESSAGE, "top_picks": [], "watchlist": [], "avoid": []}
        decision = apply_audit_to_decision(decision, {})
        messages = format_pick_report(
            date(2026, 11, 5),
            decision,
            "",
            pd.DataFrame({"stock_id": ["2330", "2317"]}),
            {},
            [],
            total_signals=2,
            skipped=0,
            model="m",
        )
        self.assertIn("今日無符合高勝率標準之標的，建議空手觀望", "\n".join(messages))

    def test_portfolio_correlation_and_sector_guard(self) -> None:
        rng = np.random.RandomState(7)
        base = rng.normal(0, 1, 60)
        base = (base - base.mean()) / base.std()
        noise = rng.normal(0, 1, 60)
        noise = noise - np.dot(noise, base) / np.dot(base, base) * base
        noise = (noise - noise.mean()) / noise.std()
        hold_ret = 0.85 * base + np.sqrt(1 - 0.85**2) * noise
        dates = pd.bdate_range("2026-07-01", periods=61)

        def to_prices(ret: np.ndarray) -> pd.DataFrame:
            close = 100 * np.cumprod(np.concatenate([[1.0], 1 + 0.01 * ret]))
            return pd.DataFrame({"date": dates, "close": close})

        result = evaluate_candidate_exposure(
            "3017",
            "電腦及週邊設備",
            [{"stock_id": "2383", "sector": "電子零組件", "return_pct": -3.0}],
            candidate_prices=to_prices(base),
            holding_prices={"2383": to_prices(hold_ret)},
        )
        self.assertEqual(result.exposure_status, "SECTOR_OR_CORR_FULL_SWAP_ONLY")
        self.assertEqual(result.max_corr_holding_id, "2383")
        self.assertAlmostEqual(result.max_corr_value, 0.85, places=2)
        self.assertEqual(result.weakest_holding_to_swap, "2383")

    def test_news_price_reaction_flags_distribution_trap(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.bdate_range("2026-10-26", periods=7),
                "open": [100, 100, 100, 100, 100, 100, 104],
                "max": [101, 101, 101, 101, 101, 101, 110],
                "min": [99, 99, 99, 99, 99, 99, 100],
                "close": [100, 100, 100, 100, 100, 100, 100],
                "Trading_Volume": [1000, 1000, 1000, 1000, 1000, 1000, 2200],
            }
        )
        news = [{"title": "利多不斷", "pub_date": "2026-11-03", "url": "", "summary": "訂單大爆發"}]
        aligned = align_news_with_price_action(news, prices)
        self.assertEqual(aligned[0]["reaction_tag"], "【⚠️ 爆量收黑/長上影線：疑似利多出貨】")
        self.assertEqual(aligned[0]["vol_ratio_5d"], 2.2)
        self.assertEqual(aligned[0]["upper_shadow_ratio"], 0.6)

    def test_post_audit_guard_downgrades_poor_rr_ratio(self) -> None:
        breakout = {"signal_type": "RIGHT_BREAKOUT", "deterministic_rr_ratio": 1.35}
        pullback = {"signal_type": "STYLE_B_PULLBACK", "deterministic_rr_ratio": 1.35}
        out = _enforce_post_audit_guards(
            [_audit_item("2330"), _audit_item("2317", tactic="STYLE_B_PULLBACK_LIMIT")],
            {"2330": breakout, "2317": pullback},
            {},
        )
        by_id = {i.stock_id: i for i in out}
        # SASD v5.9.0: low R/R is annotated for CIO instead of hard REJECT_TRAP
        self.assertEqual(by_id["2330"].verdict_grade, "S_TIER_SNIPER")
        self.assertIn("1.35", by_id["2330"].conflict_resolution_reasoning)
        self.assertTrue(any("1.35" in f for f in by_id["2330"].bear_case_flaws))
        self.assertEqual(by_id["2317"].verdict_grade, "S_TIER_SNIPER")
        self.assertEqual(select_audit_survivors(out), [by_id["2330"], by_id["2317"]])

    def test_swap_only_exposure_overrides_tactic_and_record_banner(self) -> None:
        exposure = SimpleNamespace(
            exposure_status="SECTOR_OR_CORR_FULL_SWAP_ONLY",
            max_corr_holding_id="2383",
            weakest_holding_to_swap="2383",
        )
        out = _enforce_post_audit_guards([_audit_item("3017", grade="A_TIER_PULLBACK")], {}, {"3017": exposure})
        self.assertEqual(out[0].execution_strategy_type, "PORTFOLIO_SWAP_ONLY")
        record = build_audit_record(out[0], None, exposure)
        self.assertIn("僅限換股", record["Swap_Banner"])
        self.assertIn("2383", record["Swap_Banner"])

    def test_deterministic_trade_setup_breakout_and_pullback(self) -> None:
        prices = _ohlcv()
        breakout = compute_deterministic_trade_setup(prices, "RIGHT_BREAKOUT")
        assert breakout is not None
        self.assertLessEqual(breakout["chase_limit_price"], round(breakout["close_t"] * 1.035, 2))
        self.assertLess(breakout["hard_stop_loss"], breakout["entry_reference_price"])
        self.assertEqual(breakout["tp1_source"], "ATR_3X_TARGET")
        pullback = compute_deterministic_trade_setup(prices, "STYLE_B_PULLBACK")
        assert pullback is not None
        self.assertEqual(pullback["chase_limit_price"], pullback["ideal_buy_zone_high"])
        self.assertLess(pullback["hard_stop_loss"], pullback["ideal_buy_zone_low"])
        self.assertIsNone(compute_deterministic_trade_setup(prices.head(10)))


if __name__ == "__main__":
    unittest.main()
