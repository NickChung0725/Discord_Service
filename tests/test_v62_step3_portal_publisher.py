"""SASD v6.2.0 Step 3（網站打包器、30 日 JSON 清理、併發鎖與 GitHub Git Trees 單次發布）測試。"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
for _sub in (_ROOT, _ROOT / "services", _ROOT / "quant", _ROOT / "taishin"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from github_uploader import (
    GitHubUploadError,
    _compute_git_blob_sha,
    get_github_pages_base_url,
    sync_portal_batch,
)
from portal_publisher import (
    build_portal_bundle,
    publish_portal_data,
    record_manual_stock_ai_query,
)
from signal_audit_service import (
    CATEGORY_AVOID,
    CATEGORY_TOP_PICK,
    CATEGORY_WATCHLIST,
    STRATEGY_ALIGNMENT_2130,
    SignalAuditStore,
)


def _mock_resp(status_code: int, payload: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = text or json.dumps(payload or {})
    return resp


class TestGitHubPortalBatchSync(unittest.TestCase):
    def test_get_github_pages_base_url(self) -> None:
        self.assertEqual(
            get_github_pages_base_url("NickChung0725/Discord_Service"),
            "https://NickChung0725.github.io/Discord_Service",
        )
        self.assertEqual(
            get_github_pages_base_url("nick/nick.github.io"),
            "https://nick.github.io",
        )

    def test_single_commit_skips_unchanged_deletes_expired_daily_never_deletes_charts(self) -> None:
        unchanged_html = b"<html>same</html>"
        unchanged_sha = _compute_git_blob_sha(unchanged_html)
        new_manifest = b'{"updated_at":"2026-11-19"}'

        posted_trees: list[dict] = []
        created_blobs: list[dict] = []

        def _fake_get(url: str, **kwargs):
            if "/git/ref/heads/" in url:
                return _mock_resp(200, {"object": {"sha": "head_sha_1"}})
            if "/git/commits/" in url:
                return _mock_resp(200, {"tree": {"sha": "base_tree_1"}})
            if "/git/trees/" in url:
                return _mock_resp(
                    200,
                    {
                        "tree": [
                            {"path": "index.html", "type": "blob", "sha": unchanged_sha},
                            {"path": "charts/2330_fund.html", "type": "blob", "sha": "chart_sha"},
                            {"path": "data/daily/2026-09-01.json", "type": "blob", "sha": "old_json_sha"},
                            {"path": "data/daily/2026-11-18.json", "type": "blob", "sha": "keep_json_sha"},
                        ]
                    },
                )
            raise AssertionError(f"Unexpected GET {url}")

        def _fake_post(url: str, **kwargs):
            body = kwargs.get("json") or {}
            if url.endswith("/git/blobs"):
                created_blobs.append(body)
                return _mock_resp(201, {"sha": f"blob_sha_{len(created_blobs)}"})
            if url.endswith("/git/trees"):
                posted_trees.append(body)
                return _mock_resp(201, {"sha": "new_tree_sha"})
            if url.endswith("/git/commits"):
                return _mock_resp(201, {"sha": "new_commit_sha"})
            raise AssertionError(f"Unexpected POST {url}")

        def _fake_patch(url: str, **kwargs):
            return _mock_resp(200, {"object": {"sha": "new_commit_sha"}})

        with (
            patch("github_uploader.requests.get", side_effect=_fake_get),
            patch("github_uploader.requests.post", side_effect=_fake_post),
            patch("github_uploader.requests.patch", side_effect=_fake_patch),
        ):
            res = sync_portal_batch(
                {
                    "index.html": unchanged_html,
                    "data/manifest.json": new_manifest,
                },
                delete_paths=["charts/2330_fund.html"],  # 故意測試：必須被安全護欄擋下
                keep_daily_dates={"2026-11-18", "2026-11-19"},
                token="fake-token",
                repository="NickChung0725/Discord_Service",
                branch="gh-pages",
            )

        self.assertEqual(res["commit_sha"], "new_commit_sha")
        self.assertEqual(res["uploaded"], ["data/manifest.json"])
        self.assertEqual(res["skipped_unchanged"], ["index.html"])
        self.assertEqual(res["deleted"], ["data/daily/2026-09-01.json"])
        self.assertEqual(len(created_blobs), 1)
        self.assertEqual(len(posted_trees), 1)
        tree_entries = {e["path"]: e["sha"] for e in posted_trees[0]["tree"]}
        self.assertIn("data/manifest.json", tree_entries)
        self.assertIsNone(tree_entries["data/daily/2026-09-01.json"])
        self.assertNotIn("charts/2330_fund.html", tree_entries)

    def test_conflict_422_retries_and_succeeds(self) -> None:
        patch_calls = 0

        def _fake_get(url: str, **kwargs):
            if "/git/ref/heads/" in url:
                return _mock_resp(200, {"object": {"sha": f"head_{patch_calls}"}})
            if "/git/commits/" in url:
                return _mock_resp(200, {"tree": {"sha": f"tree_{patch_calls}"}})
            if "/git/trees/" in url:
                return _mock_resp(200, {"tree": []})
            raise AssertionError(url)

        def _fake_post(url: str, **kwargs):
            if url.endswith("/git/blobs"):
                return _mock_resp(201, {"sha": "blob_1"})
            if url.endswith("/git/trees"):
                return _mock_resp(201, {"sha": "new_tree"})
            if url.endswith("/git/commits"):
                return _mock_resp(201, {"sha": f"commit_{patch_calls}"})
            raise AssertionError(url)

        def _fake_patch(url: str, **kwargs):
            nonlocal patch_calls
            patch_calls += 1
            if patch_calls == 1:
                return _mock_resp(422, {"message": "Update is not a fast forward"})
            return _mock_resp(200, {"object": {"sha": "commit_1"}})

        with (
            patch("github_uploader.requests.get", side_effect=_fake_get),
            patch("github_uploader.requests.post", side_effect=_fake_post),
            patch("github_uploader.requests.patch", side_effect=_fake_patch),
            patch("github_uploader.time.sleep"),
        ):
            res = sync_portal_batch(
                {"data/manifest.json": "{}"},
                token="fake-token",
                repository="NickChung0725/Discord_Service",
                branch="gh-pages",
            )
        self.assertEqual(patch_calls, 2)
        self.assertEqual(res["commit_sha"], "commit_1")


class TestPortalPublisher(unittest.TestCase):
    def test_build_portal_bundle_pending_verified_manual_queries_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            daily_dir = root / "daily"
            state_dir = root / ".state"
            daily_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            db_path = root / "signal_audit.db"

            # 建立 3 個交易日資料：最舊 2026-10-01、已驗證 2026-11-05、19:00 待審計 2026-11-19
            (daily_dir / "2026-10-01_market_regime.json").write_text(
                json.dumps({"light": "🟢綠燈", "summary": "舊日測試"}), encoding="utf-8"
            )
            (daily_dir / "2026-11-05_market_regime.json").write_text(
                json.dumps(
                    {
                        "light": "🟡黃燈",
                        "summary": "區間震盪",
                        "recommended_max_exposure_pct": "60%",
                        "regime_quota": {"regime_state": "YELLOW_ROTATION"},
                    }
                ),
                encoding="utf-8",
            )
            (daily_dir / "2026-11-05_ai_recommendations.json").write_text(
                json.dumps(
                    {
                        "date": "2026-11-05",
                        "portfolio_model": "gemini-4-argon",
                        "total_signals": 18,
                        "analyzed": 15,
                        "decision": {
                            "market_comment": "【建議總持倉水位：60%】優先布局網通龍頭。",
                            "recommended_total_exposure_pct": 60,
                            "top_picks": [
                                {
                                    "rank": 1,
                                    "stock_id": "2379",
                                    "stock_name": "瑞昱",
                                    "sector": "IC設計",
                                    "score": 88,
                                    "outlook_verdict": "高度可信",
                                    "target_price_1": "545.0",
                                    "target_price_2": "580.0",
                                    "stop_loss_price": "468.0",
                                    "bull_thesis": "Wi-Fi 7 放量",
                                    "bear_case_and_invalidation": "營收 YoY < 10%",
                                }
                            ],
                            "watchlist": [{"stock_id": "3017", "stock_name": "奇鋐", "reason": "等拉回"}],
                            "avoid": [{"stock_id": "2606", "stock_name": "裕民", "reason": "爆量留上影"}],
                        },
                        "audit": {
                            "2379": {
                                "Allocation_Weight_Pct": 35,
                                "Tier1_Entry_Plan": "【第1批 30% 試單】回測 17MA (500.0)",
                                "Tier2_Entry_Plan": "【第2批 40% 核心】回踩 45MA (488.0)",
                                "Tier3_Entry_Plan": "【第3批 30% 黃金坑】60日量 POC (475.0)",
                                "Tier1_Entry_Price": 500.0,
                                "Tier2_Entry_Price": 488.0,
                                "Tier3_Entry_Price": 475.0,
                                "Chase_Limit_Price": 520.0,
                                "TP1_Target_Or_POC": 545.0,
                                "TP2_Target_Price": 580.0,
                                "Hard_Stop_Loss": 468.0,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # 19:00 剛掃完（尚無 ai_recommendations.json）
            (daily_dir / "2026-11-19_market_regime.json").write_text(
                json.dumps({"light": "🟢綠燈", "summary": "多頭排列"}), encoding="utf-8"
            )
            pd.DataFrame(
                [
                    {
                        "stock_id": "2330",
                        "stock_name": "台積電",
                        "industry_category": "半導體業",
                        "close": 1050.0,
                        "signal": "剛金叉起漲",
                        "vol_status": "🔥 放量點火 (2.1x)",
                    }
                ]
            ).to_csv(daily_dir / "2026-11-19_alignment_signals.csv", index=False)

            # 寫入並回填 signal_audit.db
            store = SignalAuditStore(db_path)
            store.record_signal(
                {
                    "signal_date": "2026-11-05",
                    "strategy_id": STRATEGY_ALIGNMENT_2130,
                    "pick_category": CATEGORY_TOP_PICK,
                    "stock_id": "2379",
                    "stock_name": "瑞昱",
                    "sector_name": "IC設計",
                    "regime_state": "YELLOW_ROTATION",
                    "scanner_signal_type": "STYLE_B_PULLBACK",
                    "ai_verdict_grade": "S_TIER_CORE",
                    "execution_strategy_type": "STRATEGIC_3TIER_PULLBACK",
                    "close_t": 510.0,
                    "tier1_entry_price": 500.0,
                    "tier2_entry_price": 488.0,
                    "tier3_entry_price": 475.0,
                    "chase_limit_price": 520.0,
                    "hard_stop_loss": 468.0,
                    "tp1_price": 545.0,
                    "tp2_price": 580.0,
                    "deterministic_rr_ratio": 2.0,
                }
            )
            store.record_signal(
                {
                    "signal_date": "2026-11-05",
                    "strategy_id": STRATEGY_ALIGNMENT_2130,
                    "pick_category": CATEGORY_AVOID,
                    "stock_id": "2606",
                    "stock_name": "裕民",
                    "sector_name": "航運",
                    "regime_state": "YELLOW_ROTATION",
                    "scanner_signal_type": "RIGHT_BREAKOUT",
                    "ai_verdict_grade": "REJECT_TRAP",
                    "execution_strategy_type": "NO_TRADE",
                    "close_t": 62.5,
                    "chase_limit_price": 64.0,
                    "hard_stop_loss": 57.5,
                    "tp1_price": 67.5,
                    "deterministic_rr_ratio": 0.0,
                }
            )
            store.backfill_pending(
                lambda sid, _dt: [
                    {"open": 505.0, "high": 510.0, "low": 498.5, "close": 506.0},
                    {"open": 508.0, "high": 552.0, "low": 505.0, "close": 548.0},
                ]
                if sid == "2379"
                else [
                    {"open": 62.0, "high": 72.0, "low": 61.5, "close": 70.5},
                ]
            )

            # 模擬 /stock_ai 手動查詢紀錄
            record_manual_stock_ai_query(
                "2454",
                stock_name="聯發科",
                sector="手機晶片",
                score=86,
                war_room_url="https://example.github.io/charts/2454_fund.html",
                query_date=date(2026, 11, 18),
                output_root=root,
            )

            fake_index = root / "index.html"
            fake_index.write_text("<!doctype html><html><body>PORTAL</body></html>", encoding="utf-8")

            bundle = build_portal_bundle(
                as_of=date(2026, 11, 19),
                output_root=root,
                audit_db_path=db_path,
                web_index_path=fake_index,
                retention_trading_days=2,  # 驗證只保留最新 2 個交易日（剔除 2026-10-01）
            )

            self.assertEqual(bundle["keep_dates"], ["2026-11-19", "2026-11-05"])
            self.assertIn("index.html", bundle["files"])
            self.assertIn("data/manifest.json", bundle["files"])
            self.assertIn("data/audit_summary.json", bundle["files"])
            self.assertNotIn("data/daily/2026-10-01.json", bundle["files"])

            # 驗證 2026-11-19 19:00 待審計狀態
            day19 = bundle["daily_payloads"]["2026-11-19"]
            self.assertEqual(len(day19["strategies"]), 1)
            self.assertEqual(day19["strategies"][0]["status"], "PENDING_AI")
            self.assertIn("等待 21:30 AI 審計", day19["strategies"][0]["status_badge"])
            self.assertEqual(day19["strategies"][0]["pending_candidates"][0]["stock_id"], "2330")

            # 驗證 2026-11-05 對答案與單一玩股網外部連結
            day05 = bundle["daily_payloads"]["2026-11-05"]
            strat = day05["strategies"][0]
            self.assertEqual(strat["status"], "READY")
            self.assertTrue(strat["has_false_negative_alert"])
            pick = strat["top_picks"][0]
            self.assertEqual(set(pick["links"].keys()), {"wantgoo", "war_room"})
            self.assertEqual(pick["links"]["wantgoo"], "https://www.wantgoo.com/stock/2379/technical-chart")
            self.assertFalse(pick["actual_verification"]["is_pending"])
            self.assertIn("達標 TP1", pick["actual_verification"]["outcome_badge"])
            self.assertIn("命中第1批", pick["actual_verification"]["entry_result_badge"])

            # 驗證 /stock_ai 手動查詢納入 manifest 搜尋索引
            search_idx = bundle["manifest"]["stock_search_index"]
            self.assertIn("2454", search_idx)
            self.assertEqual(search_idx["2454"]["appearances"][0]["category"], "MANUAL_QUERY")
            self.assertEqual(search_idx["2454"]["appearances"][0]["strategy_id"], "manual_stock_ai")

            # 驗證 dry_run 寫出本地預覽與非致命錯誤防護
            dry_res = publish_portal_data(
                {"enable_portal_publish": True},
                as_of=date(2026, 11, 19),
                output_root=root,
                audit_db_path=db_path,
                web_index_path=fake_index,
                dry_run=True,
            )
            self.assertTrue(dry_res["dry_run"])
            self.assertTrue((root / "portal" / "data" / "manifest.json").is_file())

            with patch("portal_publisher.sync_portal_batch", side_effect=GitHubUploadError("boom")):
                err_res = publish_portal_data(
                    {"enable_portal_publish": True},
                    as_of=date(2026, 11, 19),
                    output_root=root,
                    audit_db_path=db_path,
                    dry_run=False,
                    raise_on_error=False,
                )
            self.assertIn("error", err_res)


class TestStep4WebPortalAndDiscordHeader(unittest.TestCase):
    def test_web_index_html_has_rwd_cache_bust_dual_mode_and_only_wantgoo(self) -> None:
        index_path = _ROOT / "web" / "index.html"
        self.assertTrue(index_path.is_file())
        content = index_path.read_text(encoding="utf-8")

        self.assertIn("https://www.wantgoo.com/stock/", content)
        self.assertNotIn("tradingview.com", content.lower())
        self.assertNotIn("histock.tw", content.lower())
        self.assertNotIn("tw.stock.yahoo.com", content.lower())
        self.assertIn('cache: "no-store"', content)
        self.assertIn("min-width: 768px", content)
        self.assertIn("明日掛單作戰模式", content)
        self.assertIn("今日大盤防禦空手觀望", content)
        self.assertIn("ruler-desktop", content)
        self.assertIn("ruler-mobile", content)
        # 純唯讀工具：不使用 localStorage
        self.assertNotIn("localStorage", content)

    def test_discord_reports_include_portal_link_on_first_line(self) -> None:
        from alignment_ai_picker import format_pick_report
        from finmind_stock_scanner import format_alignment_report, format_rebound_report

        with patch.dict("os.environ", {"GITHUB_REPO": "NickChung0725/Discord_Service"}):
            align_msgs = format_alignment_report(pd.DataFrame(), as_of=date(2026, 11, 19))
            rebound_msgs = format_rebound_report(pd.DataFrame(), as_of=date(2026, 11, 19))
            pick_msgs = format_pick_report(
                date(2026, 11, 19),
                {"market_comment": "觀望", "top_picks": [], "watchlist": [], "avoid": []},
                "",
                pd.DataFrame(columns=["stock_id", "stock_name"]),
                {},
                [],
                total_signals=0,
                skipped=0,
                model="gemini-4-argon",
            )

        expected = "https://NickChung0725.github.io/Discord_Service/?date=2026-11-19"
        for msgs in (align_msgs, rebound_msgs, pick_msgs):
            first_line = msgs[0].splitlines()[0]
            self.assertIn("開啟今日量化戰情總部", first_line)
            self.assertIn(expected, first_line)


if __name__ == "__main__":
    unittest.main()
