"""SASD v6.2.0 Step 1（玩股網連結＋K 線標線）與 Step 2（資料庫重建遷移、三階 5 日對答案與留痕修復）測試。"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_ROOT = Path(__file__).resolve().parent.parent
for _sub in (_ROOT, _ROOT / "services", _ROOT / "quant", _ROOT / "taishin"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from alignment_ai_picker import (
    _extract_trade_levels_for_chart,
    _links,
    _record_v62_decision_signals,
    wantgoo_chart_url,
)
from chart_generator import _add_trade_level_overlays
from fundamental_analyzer import FundamentalAnalyzer
from signal_audit_service import (
    CATEGORY_AVOID,
    CATEGORY_TOP_PICK,
    CATEGORY_WATCHLIST,
    FILL_PENDING,
    FILL_PULLBACK,
    FILL_UNFILLED,
    OUTCOME_PENDING,
    OUTCOME_STOP,
    OUTCOME_TP1,
    OUTCOME_TP2,
    PULLBACK_FILL_WINDOW,
    STRATEGY_ALIGNMENT_2130,
    STRATEGY_REBOUND_2200,
    SignalAuditStore,
    evaluate_forward_path,
    parse_plan_price,
)
from stock_ai_command import StockWarRoomView


class TestV62Step2AuditUpgrade(unittest.TestCase):
    def test_parse_plan_price_filters_non_price_numbers(self) -> None:
        self.assertEqual(
            parse_plan_price("【第1批 30% 試單】回測 17MA (500.0)", close_ref=510.0),
            500.0,
        )
        self.assertEqual(
            parse_plan_price("【第1批 30% 試單】498.50~502.00（回測5MA~17MA/0.236回檔量縮區）", close_ref=510.0),
            502.0,
        )
        self.assertEqual(
            parse_plan_price("【第1批 30% 試單】498.50~502.00（回測5MA~17MA/0.236回檔量縮區）", close_ref=510.0, pick="low"),
            498.5,
        )
        self.assertEqual(
            parse_plan_price("【第3批 30% 黃金坑】60日量 POC (475.0)", close_ref=510.0),
            475.0,
        )
        self.assertEqual(
            parse_plan_price("545.0（Python 確定性目標）", close_ref=510.0),
            545.0,
        )
        self.assertEqual(
            parse_plan_price("無法解析文字", close_ref=510.0, fallback=488.0),
            488.0,
        )

    def test_legacy_v54_db_migration_creates_backup_and_new_unique_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "signal_audit.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE signal_audit_log (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT
                    , signal_date TEXT NOT NULL
                    , stock_id TEXT NOT NULL
                    , stock_name TEXT NOT NULL
                    , sector_name TEXT
                    , regime_state TEXT NOT NULL
                    , scanner_signal_type TEXT NOT NULL
                    , ai_verdict_grade TEXT NOT NULL
                    , execution_strategy_type TEXT NOT NULL
                    , close_t REAL NOT NULL
                    , ideal_buy_zone_low REAL
                    , ideal_buy_zone_high REAL
                    , chase_limit_price REAL NOT NULL
                    , hard_stop_loss REAL NOT NULL
                    , tp1_price REAL NOT NULL
                    , deterministic_rr_ratio REAL NOT NULL
                    , open_t1 REAL
                    , effective_entry_price REAL
                    , entry_fill_status TEXT DEFAULT 'PENDING'
                    , was_buy_zone_filled INTEGER DEFAULT 0
                    , outcome_status_10d TEXT DEFAULT 'PENDING'
                    , net_return_5d_pct REAL
                    , net_return_10d_pct REAL
                    , mfe_10d_pct REAL
                    , mae_10d_pct REAL
                    , is_whipsawed INTEGER DEFAULT 0
                    , is_false_negative_miss INTEGER DEFAULT 0
                    , created_at TEXT NOT NULL
                    , updated_at TEXT NOT NULL
                    , UNIQUE (signal_date, stock_id)
                );
                INSERT INTO signal_audit_log (
                    signal_date, stock_id, stock_name, sector_name, regime_state
                    , scanner_signal_type, ai_verdict_grade, execution_strategy_type
                    , close_t, chase_limit_price, hard_stop_loss, tp1_price
                    , deterministic_rr_ratio, outcome_status_10d, created_at, updated_at
                ) VALUES
                    ('2026-10-01', '2330', '台積電', '晶圓代工', 'GREEN_BULL',
                     'RIGHT_BREAKOUT', 'S_TIER_SNIPER', 'BREAKOUT',
                     1000.0, 1020.0, 950.0, 1100.0, 2.0, 'TP1_HIT_WIN',
                     '2026-10-01T21:30:00', '2026-10-08T19:00:00'),
                    ('2026-10-01', '2606', '裕民', '航運', 'GREEN_BULL',
                     'RIGHT_BREAKOUT', 'REJECT_TRAP', 'NO_TRADE',
                     60.0, 62.0, 55.0, 66.0, 1.2, 'PENDING',
                     '2026-10-01T21:30:00', '2026-10-01T21:30:00');
                """
            )
            conn.commit()
            conn.close()

            store = SignalAuditStore(db_path)

            backups = list(Path(tmpdir).glob("signal_audit.db.bak_*"))
            self.assertEqual(len(backups), 1)

            rows = store.fetch_all_rows()
            self.assertEqual(len(rows), 2)
            by_sid = {r["stock_id"]: r for r in rows}
            self.assertEqual(by_sid["2330"]["strategy_id"], STRATEGY_ALIGNMENT_2130)
            self.assertEqual(by_sid["2330"]["pick_category"], CATEGORY_TOP_PICK)
            self.assertEqual(by_sid["2330"]["bars_elapsed"], 10)
            self.assertEqual(by_sid["2606"]["pick_category"], CATEGORY_AVOID)
            self.assertEqual(by_sid["2606"]["bars_elapsed"], 0)

            # 同日同股可同時記錄於 22:00 rebound_2200，不會觸發舊 UNIQUE (signal_date, stock_id) 衝突
            store.record_signal(
                {
                    "signal_date": "2026-10-01",
                    "strategy_id": STRATEGY_REBOUND_2200,
                    "pick_category": CATEGORY_TOP_PICK,
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "regime_state": "GREEN_BULL",
                    "scanner_signal_type": "STYLE_B_PULLBACK",
                    "ai_verdict_grade": "S_TIER_CORE",
                    "execution_strategy_type": "STRATEGIC_3TIER_PULLBACK",
                    "close_t": 1000.0,
                    "tier1_entry_price": 985.0,
                    "tier2_entry_price": 970.0,
                    "tier3_entry_price": 955.0,
                    "chase_limit_price": 1000.0,
                    "hard_stop_loss": 940.0,
                    "tp1_price": 1080.0,
                    "tp2_price": 1150.0,
                    "deterministic_rr_ratio": 2.1,
                }
            )
            self.assertEqual(len(store.fetch_all_rows()), 3)

    def test_3tier_5day_fill_weighted_avg_and_tp2_conservative_return(self) -> None:
        self.assertEqual(PULLBACK_FILL_WINDOW, 5)
        signal = {
            "scanner_signal_type": "STYLE_B_PULLBACK",
            "ai_verdict_grade": "S_TIER_CORE",
            "pick_category": CATEGORY_TOP_PICK,
            "close_t": 510.0,
            "tier1_entry_price": 500.0,
            "tier2_entry_price": 488.0,
            "tier3_entry_price": 475.0,
            "chase_limit_price": 520.0,
            "hard_stop_loss": 468.0,
            "tp1_price": 545.0,
            "tp2_price": 580.0,
        }

        # T+1~T+4 未觸價仍在 5 日窗口內 -> PENDING（但已更新期間高低價）
        bars_4d_unfilled = [
            {"open": 512.0, "high": 520.0, "low": 505.0, "close": 515.0},
            {"open": 515.0, "high": 525.0, "low": 508.0, "close": 520.0},
            {"open": 520.0, "high": 530.0, "low": 512.0, "close": 526.0},
            {"open": 526.0, "high": 535.0, "low": 518.0, "close": 530.0},
        ]
        res_4d = evaluate_forward_path(signal, bars_4d_unfilled)
        self.assertEqual(res_4d["entry_fill_status"], FILL_PENDING)
        self.assertEqual(res_4d["bars_elapsed"], 4)
        self.assertEqual(res_4d["period_high_price"], 535.0)
        self.assertEqual(res_4d["period_low_price"], 505.0)

        # T+5 仍未觸價 -> UNFILLED_MISSED
        bars_5d_unfilled = [
            *bars_4d_unfilled,
            {"open": 530.0, "high": 540.0, "low": 522.0, "close": 538.0},
        ]
        res_5d = evaluate_forward_path(signal, bars_5d_unfilled)
        self.assertEqual(res_5d["entry_fill_status"], FILL_UNFILLED)

        # T+2 觸第 1 批 (500)、T+5 觸第 2 批 (488)、T+7 達 TP1 (545)、T+9 達 TP2 (580)
        bars_tp2 = [
            {"open": 510.0, "high": 515.0, "low": 504.0, "close": 506.0},  # T+1
            {"open": 505.0, "high": 508.0, "low": 498.0, "close": 502.0},  # T+2 -> Tier 1 @ 500
            {"open": 502.0, "high": 506.0, "low": 495.0, "close": 497.0},  # T+3
            {"open": 497.0, "high": 500.0, "low": 490.0, "close": 492.0},  # T+4
            {"open": 492.0, "high": 496.0, "low": 485.0, "close": 494.0},  # T+5 -> Tier 2 @ 488
            {"open": 496.0, "high": 525.0, "low": 495.0, "close": 522.0},  # T+6
            {"open": 525.0, "high": 550.0, "low": 520.0, "close": 546.0},  # T+7 -> Hit TP1 (545)
            {"open": 548.0, "high": 570.0, "low": 545.0, "close": 565.0},  # T+8
            {"open": 568.0, "high": 585.0, "low": 562.0, "close": 582.0},  # T+9 -> Hit TP2 (580)
        ]
        res = evaluate_forward_path(signal, bars_tp2)
        expected_raw_avg = (0.30 * 500.0 + 0.40 * 488.0) / 0.70
        expected_entry = round(expected_raw_avg * 1.005, 2)
        self.assertEqual(res["entry_fill_status"], FILL_PULLBACK)
        self.assertEqual(res["filled_tiers_text"], "第1+2批(70%)")
        self.assertAlmostEqual(float(res["effective_entry_price"]), expected_entry, places=2)
        self.assertEqual(res["outcome_status_10d"], OUTCOME_TP2)
        # 保守口徑：勝率與獲利因子鎖定 TP1 (545.0) 報酬
        expected_tp1_ret = round((545.0 - expected_entry) / expected_entry * 100.0, 4)
        self.assertAlmostEqual(float(res["net_return_10d_pct"]), expected_tp1_ret, places=3)
        self.assertEqual(res["bars_elapsed"], 9)
        self.assertEqual(res["period_high_price"], 585.0)
        self.assertEqual(res["period_low_price"], 485.0)
        self.assertEqual(res["latest_close_price"], 582.0)

    def test_record_v62_decision_signals_writes_top_watchlist_and_avoid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "signal_audit.db"
            cfg = {"enable_signal_audit": True, "signal_audit_db_path": str(db_path)}

            recorded = _record_v62_decision_signals(
                config=cfg,
                output_root=root,
                target_date=date(2026, 11, 18),
                strategy_id=STRATEGY_ALIGNMENT_2130,
                market_regime={"light": "🟡黃燈"},
                top_picks=[
                    {
                        "stock_id": "2379",
                        "stock_name": "瑞昱",
                        "sector": "IC設計",
                        "target_price_1": "545.0（Python 確定性目標）",
                        "target_price_2": "580.0",
                        "stop_loss_price": "468.0",
                    }
                ],
                watchlist=[{"stock_id": "3017", "reason": "正乖離過大"}],
                avoid=[{"stock_id": "2606", "reason": "籌碼排雷"}],
                row_map={
                    "2379": {"stock_id": "2379", "stock_name": "瑞昱", "close": 510.0, "sub_industry": "網通IC"},
                    "3017": {"stock_id": "3017", "stock_name": "奇鋐", "close": 665.0, "MA17": 620.0, "sub_industry": "散熱"},
                    "2606": {"stock_id": "2606", "stock_name": "裕民", "close": 62.5, "sub_industry": "航運"},
                },
                tech_packages={
                    "2379": {
                        "close_t": 510.0,
                        "tier1_price": 500.0,
                        "tier2_price": 488.0,
                        "tier3_price": 475.0,
                        "chase_limit_price": 520.0,
                        "hard_stop_loss": 468.0,
                        "tp1_price": 545.0,
                    }
                },
                audit_records={
                    "2379": {
                        "Audit_Grade": "S_TIER_CORE",
                        "Execution_Tactic": "STRATEGIC_3TIER_PULLBACK",
                        "Tier1_Entry_Plan": "【第1批 30% 試單】回測 17MA (500.0)",
                        "Tier2_Entry_Plan": "【第2批 40% 核心】回踩 45MA (488.0)",
                        "Tier3_Entry_Plan": "【第3批 30% 黃金坑】60日量 POC (475.0)",
                    }
                },
            )
            self.assertEqual(recorded, 3)

            store = SignalAuditStore(db_path)
            rows = {r["stock_id"]: r for r in store.fetch_all_rows()}
            self.assertEqual(set(rows.keys()), {"2379", "3017", "2606"})
            self.assertEqual(rows["2379"]["pick_category"], CATEGORY_TOP_PICK)
            self.assertEqual(rows["2379"]["tier1_entry_price"], 500.0)
            self.assertEqual(rows["2379"]["tier2_entry_price"], 488.0)
            self.assertEqual(rows["2379"]["tier3_entry_price"], 475.0)
            self.assertEqual(rows["2379"]["tp2_price"], 580.0)
            self.assertEqual(rows["3017"]["pick_category"], CATEGORY_WATCHLIST)
            self.assertEqual(rows["2606"]["pick_category"], CATEGORY_AVOID)

            # 驗證 weekly summary 不會將 Watchlist / Avoid 混入實戰 settled_trades
            store.backfill_pending(
                lambda sid, _dt: [
                    {"open": 500.0, "high": 560.0, "low": 495.0, "close": 555.0},
                ]
                if sid == "2379"
                else [{"open": 62.5, "high": 70.0, "low": 62.0, "close": 69.0}]
            )
            summary = store.build_weekly_summary()
            self.assertEqual(summary["settled_trades"], 1)
            self.assertAlmostEqual(float(summary["false_negative_rate"]), 1.0)


class TestV62Step1WantGooAndChartLevels(unittest.TestCase):
    def test_links_only_wantgoo_external_chart(self) -> None:
        url = wantgoo_chart_url("2379")
        self.assertEqual(url, "https://www.wantgoo.com/stock/2379/technical-chart")
        link_md = _links("2379", "https://example.github.io/charts/2379_fund.html")
        self.assertIn("https://www.wantgoo.com/stock/2379/technical-chart", link_md)
        self.assertIn("玩股網技術線圖", link_md)
        self.assertIn("全方位戰情室（含財報+K線）", link_md)
        self.assertNotIn("tradingview", link_md.lower())
        self.assertNotIn("histock", link_md.lower())
        self.assertNotIn("yahoo", link_md.lower())

    def test_stock_ai_war_room_view_has_wantgoo_button(self) -> None:
        async def _build_view() -> StockWarRoomView:
            return StockWarRoomView(
                war_room_url="https://example.github.io/charts/2379_fund.html",
                stock_id="2379",
            )

        view = asyncio.run(_build_view())
        urls = [getattr(item, "url", "") for item in view.children]
        labels = [getattr(item, "label", "") for item in view.children]
        self.assertIn("https://www.wantgoo.com/stock/2379/technical-chart", urls)
        self.assertIn("📈 玩股網技術線圖", labels)
        self.assertIn("https://example.github.io/charts/2379_fund.html", urls)

    def test_chart_overlay_adds_tp1_tp2_buy_zone_and_stop_loss(self) -> None:
        fig = make_subplots(rows=4, cols=1)
        fig.add_trace(go.Scatter(x=["2026-11-18"], y=[510.0], name="Close"), row=1, col=1)
        _add_trade_level_overlays(
            fig,
            {
                "tp1": 545.0,
                "tp2": 580.0,
                "stop_loss": 468.0,
                "buy_zone_low": 475.0,
                "buy_zone_high": 500.0,
            },
        )
        shapes = list(fig.layout.shapes or [])
        # 1 hrect (buy zone) + 3 hlines (TP1, TP2, SL)
        self.assertEqual(len(shapes), 4)
        annotations = [str(a.text) for a in (fig.layout.annotations or [])]
        self.assertTrue(any("TP1 545.0" in t for t in annotations))
        self.assertTrue(any("TP2 580.0" in t for t in annotations))
        self.assertTrue(any("硬停損 468.0" in t for t in annotations))
        self.assertTrue(any("低接支撐區 475.0~500.0" in t for t in annotations))

    def test_extract_trade_levels_and_war_room_html_includes_wantgoo_link(self) -> None:
        decision = {
            "top_picks": [
                {
                    "stock_id": "2379",
                    "target_price_1": "545.0",
                    "target_price_2": "580.0",
                    "stop_loss_price": "468.0",
                }
            ]
        }
        audits = {
            "2379": {
                "Tier1_Entry_Price": 500.0,
                "Tier2_Entry_Price": 488.0,
                "Tier3_Entry_Price": 475.0,
                "TP1_Target_Or_POC": 545.0,
                "TP2_Target_Price": 580.0,
                "Hard_Stop_Loss": 468.0,
            }
        }
        levels = _extract_trade_levels_for_chart(decision, audits, "2379")
        self.assertEqual(
            levels,
            {
                "tp1": 545.0,
                "tp2": 580.0,
                "stop_loss": 468.0,
                "buy_zone_low": 475.0,
                "buy_zone_high": 500.0,
            },
        )

        mock_cache = MagicMock()
        mock_cache._get_worksheet.return_value = MagicMock()
        mock_cache._read_stock_rows.return_value = pd.DataFrame()
        analyzer = FundamentalAnalyzer(mock_cache)
        captured_levels: list[dict] = []

        def _fake_tech(stock_id: str, *, include_plotlyjs: bool = False, trade_levels=None) -> str:
            if trade_levels:
                captured_levels.append(dict(trade_levels))
            return "<div>FAKE_KLINE</div>"

        with (
            patch("chart_generator.build_stock_technical_chart_html", side_effect=_fake_tech),
            patch("fundamental_analyzer.upload_to_github", return_value="https://example.github.io/charts/2379_fund.html") as mock_upload,
            patch.object(analyzer, "_taxonomy_badge_sync", return_value=""),
        ):
            url = asyncio.run(
                analyzer.render_and_publish_war_room_html(
                    "2379",
                    "分析模型：test\n體質優異",
                    trade_levels=levels,
                )
            )
            self.assertEqual(url, "https://example.github.io/charts/2379_fund.html")
            self.assertEqual(len(captured_levels), 1)
            self.assertEqual(captured_levels[0]["tp1"], 545.0)
            written_path = Path(mock_upload.call_args.args[0])
            html_content = written_path.read_text(encoding="utf-8")
            self.assertIn("https://www.wantgoo.com/stock/2379/technical-chart", html_content)
            self.assertIn("玩股網技術線圖", html_content)


if __name__ == "__main__":
    unittest.main()
