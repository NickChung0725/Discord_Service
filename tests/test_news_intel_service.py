"""Unit tests for services.news_intel_service (MVP) and free-tier structured AI helper."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from services.ai_service import (
    AIResponse,
    call_google_ai_structured,
    is_news_intel_prompt_logging_enabled,
)
from services.news_intel_service import (
    MacroIntelCard,
    StockIntelCard,
    classify_mops_or_notice,
    extract_event_date_from_text,
    get_macro_intel,
    get_stock_intel,
    is_spam_or_noise_title,
    render_cross_stock_block,
    render_macro_prompt_block,
    render_morning_brief_stock_block,
    render_stage1_stock_block,
    strip_mops_header,
)


class NewsCleaningAndClassificationTests(unittest.TestCase):
    def test_strip_mops_header_removes_yahoo_banner_and_boilerplate(self) -> None:
        raw = (
            "加入為 Google 偏好來源 將 Yahoo 設為首選來源，在 Google 上查看更多我們的精彩報導 "
            "聯詠 3034.TW 435.5 ( 1.04% ) "
            "日 期：2026年11月11日 公司名稱：聯詠 (3034) "
            "主 旨：聯詠取消購置建物案 發言人：陳健興 "
            "說 明：1. 原公告日期：115/11/03 3. 變動緣由：因雙方未達共識故終止"
        )
        cleaned = strip_mops_header(raw)
        self.assertTrue(cleaned.startswith("1. 原公告日期：115/11/03"))
        self.assertIn("因雙方未達共識故終止", cleaned)
        self.assertNotIn("發言人", cleaned)
        self.assertNotIn("Google 偏好來源", cleaned)

    def test_extract_roc_event_date(self) -> None:
        text = "符合條款第四條第XX款：12 事實發生日：115/11/19 1.召開法人說明會之日期：115/11/24 2.時間：14時"
        self.assertEqual(extract_event_date_from_text(text), "2026-11-24")

    def test_spam_and_mops_routing(self) -> None:
        self.assertTrue(is_spam_or_noise_title("盤中速報 - 達發(6526)股價拉至漲停", "台股盤中"))
        self.assertTrue(is_spam_or_noise_title("殺盤嚇不倒真AI！【2454聯發科】⊕雙亮燈", "台股新聞"))
        self.assertTrue(is_spam_or_noise_title("美股科技股反彈 這兩檔 ETF 飆逾20%", "", is_macro=True))
        self.assertFalse(is_spam_or_noise_title("800G光通訊需求升溫達發100G晶片拚明年放量", "台股新聞"))

        self.assertEqual(
            classify_mops_or_notice("達發:公告本公司參與機構投資人說明會", "台股公告"),
            "calendar",
        )
        self.assertEqual(
            classify_mops_or_notice("【公告】聯詠受邀參加由花旗證券所舉辦\"Citi's 2026 Taiwan Corporate Day\"", "台股公告"),
            "calendar",
        )
        self.assertEqual(
            classify_mops_or_notice("【公告】鈊象董事會通過115年第3季財務報告", "台股公告"),
            "routine_skip",
        )
        self.assertEqual(
            classify_mops_or_notice("鉅亨速報 - Factset 最新調查：達發(6526-TW)目標價調升至900元", "台股預估"),
            "factset",
        )
        self.assertEqual(
            classify_mops_or_notice("【公告】聯詠取消前於民國115年11月3日董事會通過購置建物案", "台股公告"),
            "substantive_mops",
        )


class NewsIntelCacheAndFallbackTests(unittest.TestCase):
    def test_macro_intel_ai_caching_and_expiry(self) -> None:
        fake_articles = [
            {
                "news_id": "101",
                "title": "輝達恢復黃仁勳執行長職務",
                "source": "鉅亨網美股",
                "category": "美股",
                "pub_date": "2026-11-24 07:30",
                "mm_dd": "11-24",
                "pub_ts": 1795400000.0,
                "url": "https://news.cnyes.com/news/id/101",
                "body": "輝達董事會宣布恢復黃仁勳執行長職務，同時重申內部調查持續進行。",
            }
        ]
        ai_payload = json.dumps(
            {
                "core_drivers": ["輝達董事會恢復黃仁勳執行長職務，帶動科技股大漲 [11-24｜鉅亨網美股]"],
                "unresolved_risks": ["輝達重申相關調查工作仍持續進行 [11-24｜鉅亨網美股]"],
                "sector_impacts": ["台灣 AI 供應鏈與半導體權值股迎反彈契機 [11-24｜鉅亨網美股]"],
            },
            ensure_ascii=False,
        )
        ai_resp = AIResponse(
            provider="google_ai_studio",
            requested_model="gemini-flash-lite-latest",
            model_version="gemini-flash-lite-test",
            text=ai_payload,
            prompt_tokens=200,
            output_tokens=100,
        )

        t0 = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            cdir = Path(tmpdir)
            with (
                patch("services.news_intel_service.fetch_macro_articles", return_value=fake_articles) as mock_fetch,
                patch("services.news_intel_service._pace_ai_call"),
                patch("services.ai_service.call_google_ai_structured", return_value=ai_resp) as mock_ai,
            ):
                card1 = get_macro_intel(cache_dir=cdir, now_utc=t0, enable_ai=True)
                self.assertEqual(card1.mode, "ai")
                self.assertFalse(card1.from_cache)
                self.assertEqual(mock_fetch.call_count, 1)
                self.assertEqual(mock_ai.call_count, 1)

                # Within 2h TTL -> cache hit, 0 crawl & 0 AI calls
                card2 = get_macro_intel(cache_dir=cdir, now_utc=t0 + timedelta(minutes=30), enable_ai=True)
                self.assertTrue(card2.from_cache)
                self.assertEqual(mock_fetch.call_count, 1)
                self.assertEqual(mock_ai.call_count, 1)

                # Beyond 2h TTL -> refreshes
                card3 = get_macro_intel(cache_dir=cdir, now_utc=t0 + timedelta(hours=3), enable_ai=True)
                self.assertFalse(card3.from_cache)
                self.assertEqual(mock_fetch.call_count, 2)
                self.assertEqual(mock_ai.call_count, 2)

    def test_zero_cost_fallback_on_ai_error_never_calls_vertex(self) -> None:
        fake_articles = [
            {
                "news_id": "202",
                "title": "美國BIS新增黑名單",
                "source": "鉅亨網國際",
                "category": "國際政經",
                "pub_date": "2026-11-24 08:00",
                "mm_dd": "11-24",
                "pub_ts": 1795400000.0,
                "url": "https://news.cnyes.com/news/id/202",
                "body": "美國商務部工業暨安全局將 7 家東南亞實體增列貿易黑名單。",
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch("services.news_intel_service.fetch_macro_articles", return_value=fake_articles),
                patch("services.news_intel_service._pace_ai_call"),
                patch("services.ai_service.call_google_ai_structured", side_effect=RuntimeError("429 quota exceeded")),
                patch("services.ai_service._call_vertex_ai_once") as mock_vertex,
            ):
                card = get_macro_intel(cache_dir=Path(tmpdir), enable_ai=True)
                self.assertEqual(card.mode, "fallback_excerpt")
                self.assertEqual(len(card.core_drivers), 1)
                self.assertIn("貿易黑名單", card.core_drivers[0])
                mock_vertex.assert_not_called()

    def test_stock_differentiated_ttl_and_early_outlook_invalidation(self) -> None:
        t0 = datetime(2026, 11, 23, 13, 30, tzinfo=timezone.utc)  # 21:30 TW
        bundle_initial = {
            "stock_id": "6526",
            "stock_name": "達發",
            "calendar_events": [{"date": "2026-11-24", "event": "參與機構投資人說明會", "source": "Yahoo"}],
            "factset_articles": [
                {
                    "news_id": "6660275",
                    "title": "Factset 最新調查：達發目標價調升至900元",
                    "source": "鉅亨速報/FactSet",
                    "category": "台股預估",
                    "bucket": "factset",
                    "pub_date": "2026-11-15 10:00",
                    "ymd": "2026-11-15",
                    "mm_dd": "11-15",
                    "pub_ts": (t0 - timedelta(days=8)).timestamp(),
                    "url": "https://news.cnyes.com/news/id/6660275",
                    "body": "FactSet 最新調查將達發目標價上調 8.43% 至 900 元。",
                }
            ],
            "recent_articles": [],
            "outlook_articles": [
                {
                    "news_id": "6646047",
                    "title": "800G光通訊需求升溫 達發100G晶片拚明年放量",
                    "source": "鉅亨網",
                    "category": "台股新聞",
                    "bucket": "outlook",
                    "pub_date": "2026-11-01 12:00",
                    "ymd": "2026-11-01",
                    "mm_dd": "11-01",
                    "pub_ts": (t0 - timedelta(days=22)).timestamp(),
                    "url": "https://news.cnyes.com/news/id/6646047",
                    "body": "達發 100G 單通道光通訊晶片預計明年放量出貨。",
                }
            ],
        }

        ai_out_1 = json.dumps(
            {
                "factset_consensus": ["FactSet 調查目標價調升 8.43% 至 900 元 [11-15｜鉅亨速報/FactSet]"],
                "recent_catalysts": [],
                "risk_flags": [],
                "outlook_claims": ["100G 光通訊晶片預計明年放量出貨 [2026-11-01｜鉅亨網]"],
                "outlook_has_substance": True,
            },
            ensure_ascii=False,
        )
        ai_out_2 = json.dumps(
            {
                "factset_consensus": ["FactSet 調查目標價調升 8.43% 至 900 元 [11-15｜鉅亨速報/FactSet]"],
                "recent_catalysts": ["法說會釋出 2027 年光通訊倍增展望 [11-24｜鉅亨網]"],
                "risk_flags": [],
                "outlook_claims": ["新法說會預期 2027 年光通訊營收翻倍 [2026-11-24｜鉅亨網]"],
                "outlook_has_substance": True,
            },
            ensure_ascii=False,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cdir = Path(tmpdir)
            with (
                patch("services.news_intel_service.fetch_stock_news_bundle", return_value=bundle_initial) as mock_b,
                patch("services.news_intel_service._pace_ai_call"),
                patch(
                    "services.ai_service.call_google_ai_structured",
                    side_effect=[
                        AIResponse("google_ai_studio", "m", "m1", ai_out_1, 100, 50),
                        AIResponse("google_ai_studio", "m", "m1", ai_out_2, 100, 50),
                    ],
                ) as mock_ai,
            ):
                # 21:30 ai-pick
                c1 = get_stock_intel("6526", "達發", cache_dir=cdir, now_utc=t0, enable_ai=True)
                self.assertFalse(c1.from_cache)
                self.assertTrue(c1.outlook_has_substance)

                # 22:00 rebound (30 mins later) -> full cache hit!
                c2 = get_stock_intel(
                    "6526", "達發", cache_dir=cdir, now_utc=t0 + timedelta(minutes=30), enable_ai=True
                )
                self.assertTrue(c2.from_cache)
                self.assertEqual(mock_b.call_count, 1)
                self.assertEqual(mock_ai.call_count, 1)

                # Next morning 08:15 (10.75h later > 6h recent TTL) with brand-new 法說 article -> early outlook refresh!
                t_morning = t0 + timedelta(hours=10, minutes=45)
                bundle_new_call = {
                    **bundle_initial,
                    "recent_articles": [
                        {
                            "news_id": "6679999",
                            "title": "達發法說會報喜：2027光通訊營收拚倍增",
                            "source": "鉅亨網",
                            "category": "台股新聞",
                            "bucket": "recent",
                            "pub_date": "2026-11-24 07:30",
                            "ymd": "2026-11-24",
                            "mm_dd": "11-24",
                            "pub_ts": (t_morning - timedelta(minutes=45)).timestamp(),
                            "url": "https://news.cnyes.com/news/id/6679999",
                            "body": "達發法說會指出 2027 年光通訊晶片營收有望翻倍。",
                        }
                    ],
                }
                mock_b.return_value = bundle_new_call
                c3 = get_stock_intel("6526", "達發", cache_dir=cdir, now_utc=t_morning, enable_ai=True)
                self.assertFalse(c3.from_cache)
                self.assertEqual(mock_ai.call_count, 2)
                self.assertIn("2027 年光通訊營收翻倍", c3.outlook_claims[0])
                # Verify outlook_fetched_at was updated because of early invalidation
                self.assertGreater(c3.outlook_fetched_at_epoch, c1.outlook_fetched_at_epoch)


class ViewAdapterAndPromptLoggingTests(unittest.TestCase):
    def test_view_adapters_render_expected_sections(self) -> None:
        m_card = MacroIntelCard(
            as_of="2026-11-24T08:15:00+08:00",
            mode="ai",
            core_drivers=["輝達復職帶動費半大漲 [11-24｜鉅亨網美股]"],
            unresolved_risks=["調查尚未結束 [11-24｜鉅亨網美股]"],
            sector_impacts=["台灣 AI 供應鏈受惠 [11-24｜鉅亨網美股]"],
        )
        macro_txt = render_macro_prompt_block(m_card)
        self.assertIn("核心事件主因", macro_txt)
        self.assertIn("尚未解除風險", macro_txt)
        self.assertIn("產業與資金傳導", macro_txt)

        s_card = StockIntelCard(
            stock_id="6526",
            stock_name="達發",
            as_of="2026-11-24T08:15:00+08:00",
            mode="ai",
            calendar_events=[{"date": "2026-11-24", "event": "參與機構投資人說明會", "source": "Yahoo"}],
            factset_consensus=["目標價調升至 900 元 [11-15｜鉅亨速報/FactSet]"],
            recent_catalysts=["800G 光通訊需求升溫 [11-10｜鉅亨網]"],
            risk_flags=[],
            outlook_claims=["100G 晶片拚明年放量 [2026-11-01｜鉅亨網]"],
            outlook_has_substance=True,
        )
        stage1_txt = render_stage1_stock_block(s_card)
        self.assertIn("2026-11-24 參與機構投資人說明會", stage1_txt)
        self.assertIn("100G 晶片拚明年放量", stage1_txt)

        cross_txt = render_cross_stock_block(s_card)
        self.assertIn("目標價調升至 900 元", cross_txt)
        self.assertIn("事件日曆", cross_txt)

        morning_lines = render_morning_brief_stock_block(s_card)
        self.assertTrue(any("近期事件日曆" in ln for ln in morning_lines))
        self.assertTrue(any("機構預估" in ln for ln in morning_lines))

        empty_card = StockIntelCard(stock_id="9999", stock_name="測試", as_of="", mode="empty")
        self.assertIn("已自動減噪", render_morning_brief_stock_block(empty_card)[0])

    def test_news_intel_log_prompts_env_switch(self) -> None:
        with patch.dict(os.environ, {"NEWS_INTEL_LOG_PROMPTS": "false"}):
            self.assertFalse(is_news_intel_prompt_logging_enabled())
        with patch.dict(os.environ, {"NEWS_INTEL_LOG_PROMPTS": "true"}):
            self.assertTrue(is_news_intel_prompt_logging_enabled())

        fake_resp = AIResponse("google_ai_studio", "gemini-flash-lite-latest", "v1", '{"ok":true}', 10, 5)
        with (
            patch("services.ai_service._call_google_ai_once", return_value=fake_resp),
            patch("services.ai_service._append_ai_log") as mock_log,
            patch("services.ai_service._archive_online_call") as mock_bq,
        ):
            call_google_ai_structured("hello", task_type="NEWS_INTEL_STOCK", stock_id="2006", log_prompts=False)
            mock_log.assert_not_called()
            mock_bq.assert_not_called()

            call_google_ai_structured("hello", task_type="NEWS_INTEL_STOCK", stock_id="2006", log_prompts=True)
            mock_log.assert_called_once()
            mock_bq.assert_called_once()

    def test_mops_cross_source_dedup_and_macro_10_indicators(self) -> None:
        from services.news_intel_service import (
            _build_stock_distill_prompt,
            _norm_title_key,
            clean_yahoo_and_cnyes_chrome,
        )
        from taishin.macro_context import MACRO_SYMBOLS, fetch_global_macro_context

        # Yahoo vs cnyes Chinese-numeral & stock-name MOPS dedup
        k_yahoo = _norm_title_key("【公告】聯詠科技115年第3季合併財務報告及第4季業績展望", stock_name="聯詠")
        k_cnyes = _norm_title_key("聯詠:聯詠科技一一五年第三季合併財務報告及第四季業績展望", stock_name="聯詠")
        self.assertEqual(k_yahoo, k_cnyes)

        k_y2 = _norm_title_key("【公告】聯詠取消前於民國115年11月3日董事會通過購置建物案", stock_name="聯詠")
        k_c2 = _norm_title_key("聯詠:公告本公司取消前於民國115年11月3日董事會通過購置建物案", stock_name="聯詠")
        self.assertEqual(k_y2, k_c2)

        # Strip cnyes trailing sentiment tag without prev/next links
        self.assertEqual(clean_yahoo_and_cnyes_chrome("重要事實內容。 # 高檔出貨 79.17 %"), "重要事實內容。")
        self.assertEqual(clean_yahoo_and_cnyes_chrome("重要事實內容。 # 投信認養股"), "重要事實內容。")

        # Prompt dedup between Section B and Section C
        shared_art = {"url": "https://news.cnyes.com/news/id/99", "ymd": "2026-11-04", "source": "鉅亨網", "title": "800G", "body": "放量"}
        prompt_txt = _build_stock_distill_prompt(
            "6526", "達發", {"factset_articles": [], "recent_articles": [shared_art], "outlook_articles": [shared_art]}
        )
        self.assertEqual(prompt_txt.count("標題：800G"), 1)

        # Verify macro_context includes EWT, TNX, DXY (total 10 indicators)
        keys = [k for _, k, _ in MACRO_SYMBOLS]
        self.assertIn("EWT", keys)
        self.assertIn("TNX", keys)
        self.assertIn("DXY", keys)
        self.assertEqual(len(keys), 10)

        fake_q = {
            "^SOX": {"symbol": "^SOX", "price": 9500.0, "prev_close": 9400.0, "change": 100.0, "change_pct": 1.06, "as_of_date": "2026-11-23", "trend_3d_str": "11-23 +1.06%"},
            "^TNX": {"symbol": "^TNX", "price": 4.35, "prev_close": 4.30, "change": 0.05, "change_pct": 1.16, "as_of_date": "2026-11-23", "trend_3d_str": "11-23 4.35 (+1.16%)"},
            "DX-Y.NYB": {"symbol": "DX-Y.NYB", "price": 104.2, "prev_close": 103.8, "change": 0.4, "change_pct": 0.39, "as_of_date": "2026-11-23", "trend_3d_str": "11-23 104.20 (+0.39%)"},
            "EWT": {"symbol": "EWT", "price": 56.5, "prev_close": 55.0, "change": 1.5, "change_pct": 2.73, "as_of_date": "2026-11-23", "trend_3d_str": "11-23 +2.73%"},
        }
        with (
            patch("taishin.macro_context.fetch_yahoo_quote_snapshot", side_effect=lambda s, session=None: fake_q.get(s)),
            patch("services.news_intel_service.get_macro_intel", return_value=MacroIntelCard(as_of="2026-11-24", mode="ai", core_drivers=["輝達復職 [11-24｜鉅亨網美股]"])),
        ):
            ctx = fetch_global_macro_context(include_news=True)
        self.assertIn("台灣ETF +2.73%", ctx["compact_line"])
        self.assertIn("美債10年殖利率 4.35 (+1.16%)", ctx["compact_line"])
        self.assertIn("美元指數 104.2 (+0.39%)", ctx["compact_line"])
        self.assertIn("輝達復職 [11-24｜鉅亨網美股]", ctx["prompt_block"])


if __name__ == "__main__":
    unittest.main()
