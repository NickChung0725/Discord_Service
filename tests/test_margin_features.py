"""Unit tests for SASD v5.5.0:
- Market margin snapshot & evaluate_market_regime風控
- 07_Chip_Summary 融資指標、估算維持率與四象限標籤
- Stage 0 籌碼背離與融資斷頭追繳警戒 (build_hard_warnings)
- 0050 DCA 恐慌殺融資加碼
"""

import json
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from finmind_stock_scanner import (
    evaluate_market_regime,
    fetch_market_margin_snapshot,
)
from fundamental_analyzer import build_hard_warnings
from fundamental_cache import FundamentalCacheManager
from taishin_portfolio_service import evaluate_0050_dip_decision


class TestMarginFeatures(unittest.TestCase):
    def test_evaluate_market_regime_with_margin(self):
        # 建立大盤均線跌破 45MA 且下彎之日線（紅燈）
        dates = pd.date_range("2026-08-01", periods=60, freq="B")
        closes = [24000.0 - i * 50.0 for i in range(60)]
        df_bench = pd.DataFrame({"date": dates, "close": closes})

        # 情境 1: 指數下跌但融資逆勢大增 / 不退 -> 🚨 散戶凹單未洗清
        margin_info_1 = {
            "total_margin_yi": 3200.0,
            "margin_chg_1d_yi": 15.0,
            "margin_chg_5d_yi": 45.0,
        }
        regime_1 = evaluate_market_regime(df_bench, benchmark_label="TAIEX", margin_info=margin_info_1)
        self.assertIn("🔴", regime_1["light"])
        self.assertEqual(regime_1["margin_regime"], "🚨 散戶凹單未洗清（提防多殺多斷頭）")
        self.assertEqual(regime_1["recommended_max_exposure_pct"], "<= 25%")
        self.assertLessEqual(regime_1["recommended_top_picks_cap"], 2)
        self.assertIn("🚨 散戶凹單未洗清", regime_1["summary"])

        # 情境 2: 指數下跌且融資單日大減 45 億（斷頭宣洩）-> ⚡ 融資斷頭宣洩
        margin_info_2 = {
            "total_margin_yi": 3150.0,
            "margin_chg_1d_yi": -45.0,
            "margin_chg_5d_yi": -80.0,
        }
        regime_2 = evaluate_market_regime(df_bench, benchmark_label="TAIEX", margin_info=margin_info_2)
        self.assertEqual(regime_2["margin_regime"], "⚡ 融資斷頭宣洩（留意止穩黃金坑）")
        self.assertEqual(regime_2["recommended_max_exposure_pct"], "35% ~ 50%")
        self.assertEqual(regime_2["recommended_top_picks_cap"], 3)
        self.assertIn("⚡ 融資斷頭宣洩", regime_2["summary"])

        # 情境 3: 偏多綠燈環境 + 5 日融資暴增 >= 80 億 -> ⚠️ 高檔散戶槓桿過熱
        closes_up = [20000.0 + i * 50.0 for i in range(60)]
        df_bench_up = pd.DataFrame({"date": dates, "close": closes_up})
        margin_info_3 = {
            "total_margin_yi": 3300.0,
            "margin_chg_1d_yi": 20.0,
            "margin_chg_5d_yi": 90.0,
        }
        regime_3 = evaluate_market_regime(df_bench_up, benchmark_label="TAIEX", margin_info=margin_info_3)
        self.assertIn("🟢", regime_3["light"])
        self.assertEqual(regime_3["margin_regime"], "⚠️ 高檔散戶槓桿過熱")
        self.assertEqual(regime_3["recommended_max_exposure_pct"], "50%")
        self.assertEqual(regime_3["recommended_top_picks_cap"], 3)

        # 情境 4: 偏多綠燈環境 + 籌碼健康
        margin_info_4 = {
            "total_margin_yi": 3200.0,
            "margin_chg_1d_yi": -5.0,
            "margin_chg_5d_yi": 10.0,
        }
        regime_4 = evaluate_market_regime(df_bench_up, benchmark_label="TAIEX", margin_info=margin_info_4)
        self.assertEqual(regime_4["margin_regime"], "🟢 籌碼健康")
        self.assertEqual(regime_4["recommended_max_exposure_pct"], "70% ~ 80%")
        self.assertEqual(regime_4["recommended_top_picks_cap"], 5)

    def test_fetch_market_margin_snapshot(self):
        # 測試 TWSE / TPEx 解析與歷史計算
        twse_data = {
            "tables": [
                {
                    "data": [
                        ["融資(交易單位)", "100", "100", "0", "100", "100"],
                        ["融券(交易單位)", "50", "50", "0", "50", "50"],
                        ["融資金額(仟元)", "1000", "1000", "0", "590000000", "595000000"],
                    ]
                }
            ]
        }
        tpex_data = {
            "tables": [
                {
                    "summary": [
                        ["", "合計(張)", "100", "100", "100", "0", "100"],
                        ["", "融資金(仟元)", "190000000", "0", "0", "0", "192000000"],
                    ]
                }
            ]
        }

        with patch("finmind_stock_scanner.urlopen") as mock_url:
            mock_resp_twse = MagicMock()
            mock_resp_twse.read.return_value = json.dumps(twse_data).encode("utf-8")
            mock_resp_tpex = MagicMock()
            mock_resp_tpex.read.return_value = json.dumps(tpex_data).encode("utf-8")

            mock_url.side_effect = [
                MagicMock(__enter__=MagicMock(return_value=mock_resp_twse)),
                MagicMock(__enter__=MagicMock(return_value=mock_resp_tpex)),
            ]

            tmp_hist = Path("finmind_reports/.state/test_market_margin_history.json")
            if tmp_hist.is_file():
                tmp_hist.unlink()

            res = fetch_market_margin_snapshot(history_path=tmp_hist, as_of=date(2026, 11, 4))
            self.assertAlmostEqual(res["twse_margin_yi"], 5950.0, places=1)
            self.assertAlmostEqual(res["tpex_margin_yi"], 1920.0, places=1)
            self.assertAlmostEqual(res["total_margin_yi"], 7870.0, places=1)
            # 單日增減 = (5950 - 5900) + (1920 - 1900) = 50 + 20 = 70 億
            self.assertAlmostEqual(res["margin_chg_1d_yi"], 70.0, places=1)

            if tmp_hist.is_file():
                tmp_hist.unlink()

    def test_sync_chip_summary_margin_and_quadrant(self):
        mgr = FundamentalCacheManager.__new__(FundamentalCacheManager)
        mgr._finmind = MagicMock()

        # Mock Shareholding
        df_hold = pd.DataFrame({
            "date": ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04"],
            "ForeignInvestmentSharesRatio": [45.0, 45.1, 45.2, 45.5],
        })
        # Mock Institutional
        df_inst = pd.DataFrame([
            {"date": "2026-11-04", "name": "Foreign_Investor", "buy": 10_000_000, "sell": 5_000_000},
            {"date": "2026-11-04", "name": "Investment_Trust", "buy": 2_000_000, "sell": 1_000_000},
            {"date": "2026-11-04", "name": "Dealer_self", "buy": 1_000_000, "sell": 500_000},
        ])
        # Mock Margin
        dates_m = [f"2026-10-{i:02d}" for i in range(1, 25)]
        margin_rows = []
        for i, d in enumerate(dates_m):
            margin_rows.append({
                "date": d,
                "stock_id": "2330",
                "MarginPurchaseBuy": 100,
                "MarginPurchaseSell": 120,
                "MarginPurchaseCashRepayment": 0,
                "MarginPurchaseLimit": 10000,
                "MarginPurchaseTodayBalance": 5000 - i * 10,
                "ShortSaleTodayBalance": 500,
            })
        df_margin = pd.DataFrame(margin_rows)

        def mock_get_data(dataset, **kwargs):
            if dataset == "TaiwanStockShareholding":
                return df_hold
            if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
                return df_inst
            if dataset == "TaiwanStockMarginPurchaseShortSale":
                return df_margin
            return pd.DataFrame()

        mgr._finmind.get_data.side_effect = mock_get_data

        # Mock prices for est_margin_maint_pct
        prices_df = pd.DataFrame({
            "date": dates_m,
            "close": [1000.0 + i * 2.0 for i in range(len(dates_m))],
        })
        mgr._find_prices_for_margin_calc = MagicMock(return_value=prices_df)

        full, delta, warnings = mgr._sync_chip_summary("2330", pd.DataFrame())
        self.assertFalse(full.empty)
        row = full.iloc[-1]

        # 驗證融資指標
        self.assertAlmostEqual(row["margin_balance_lots"], df_margin.iloc[-1]["MarginPurchaseTodayBalance"])
        self.assertAlmostEqual(row["margin_utilization_pct"], 47.7, delta=0.5)
        self.assertAlmostEqual(row["short_margin_ratio_pct"], 10.48, delta=0.5)
        # 法人買超 > 0, 融資 5 日減少 -> 🟢法人鎖碼(法人買+融資減)
        self.assertEqual(row["chip_quadrant_tag"], "🟢法人鎖碼(法人買+融資減)")
        # 維持率估算存在
        self.assertGreater(row["est_margin_maint_pct"], 150.0)

    def test_build_hard_warnings_chip_and_margin(self):
        # 測試 1: 主力出貨 / 散戶接刀硬傷 (法人賣 > 500, 融資增 > 500, 使用率 >= 30%)
        metrics_bad_chip = {
            "vol_lots": 1500,
            "op_margin_pct": 12.0,
            "contract_liab": 5.0,
            "three_inst_net_5d_lots": -1200.0,
            "margin_chg_5d_lots": 800.0,
            "margin_utilization_pct": 38.5,
            "est_margin_maint_pct": 155.0,
        }
        warns_1 = build_hard_warnings(metrics_bad_chip)
        self.assertTrue(any("籌碼背離硬傷" in w and "主力倒貨散戶接刀" in w for w in warns_1))

        # 測試 2: 斷頭踩踏警戒 (估算維持率 130%~142% 且融資仍在增加)
        metrics_margin_call = {
            "vol_lots": 2000,
            "op_margin_pct": 15.0,
            "contract_liab": 10.0,
            "three_inst_net_5d_lots": 100.0,
            "margin_chg_5d_lots": 150.0,
            "margin_utilization_pct": 25.0,
            "est_margin_maint_pct": 136.5,
        }
        warns_2 = build_hard_warnings(metrics_margin_call)
        self.assertTrue(any("斷頭踩踏警戒" in w and "逼近 130% 追繳線" in w for w in warns_2))

        # 測試 3: 正常無硬傷
        metrics_normal = {
            "vol_lots": 2000,
            "op_margin_pct": 15.0,
            "contract_liab": 10.0,
            "three_inst_net_5d_lots": 500.0,
            "margin_chg_5d_lots": -200.0,
            "margin_utilization_pct": 20.0,
            "est_margin_maint_pct": 168.0,
        }
        warns_3 = build_hard_warnings(metrics_normal)
        self.assertEqual(len(warns_3), 0)

    def test_evaluate_0050_dip_decision_margin_washout(self):
        metrics = {
            "close_price": 185.0,
            "intraday_low": 184.0,
            "ma17": 186.0,
            "ma45": 188.0,
            "rsi_14": 42.0,
            "kd_k": 45.0,
            "bias_ma17_pct": -0.5,
            "drop_10d_pct": 1.0,
        }
        # 平日週二無技術指標觸發，但大盤融資單日暴減 52 億 (斷頭宣洩)
        decision = evaluate_0050_dip_decision(
            metrics,
            eval_date=date(2026, 11, 3),  # Tuesday
            already_bought_this_week=False,
            market_margin_chg_1d_yi=-52.0,
        )
        self.assertTrue(decision["should_buy"])
        self.assertIn("大盤融資斷頭清洗(單日-52億)", decision["trigger_reason"])
        self.assertGreater(decision["dip_score"], 20)

    def test_daily_margin_candle_efficiency_distinguishes_lock_vs_trap(self):
        from finmind_stock_scanner import (
            classify_daily_margin_efficiency,
            evaluate_weekly_tdcc_anchor,
            is_mandatory_margin_covering_window,
        )

        # 情境 A: 融資增加 +220 張，股價強勢收最高 (close=109.8, high=110, low=102)
        res_lock = classify_daily_margin_efficiency(
            open_p=103.0,
            high_p=110.0,
            low_p=102.0,
            close_p=109.8,
            prev_close_p=103.0,
            volume_lots=1100.0,
            margin_net_change_lots=220.0,
            inst_net_buy_lots=50.0,
            trust_holding_pct=2.0,
            trust_3d_net_buy_lots=30.0,
            return_10d_pct=8.0,
        )
        self.assertEqual(res_lock.margin_pattern, "MAIN_FORCE_MARGIN_LOCK")
        self.assertIn("主力融資鎖碼", res_lock.semantic_tag)

        # 情境 B: 融資同樣增加 +250 張，卻留長上影線收最低附近 (high=110, close=102.5, open=106, low=102)
        res_trap = classify_daily_margin_efficiency(
            open_p=106.0,
            high_p=110.0,
            low_p=102.0,
            close_p=102.5,
            prev_close_p=105.0,
            volume_lots=1200.0,
            margin_net_change_lots=250.0,
            inst_net_buy_lots=-100.0,
            trust_holding_pct=0.0,
            trust_3d_net_buy_lots=-20.0,
            return_10d_pct=20.0,
        )
        self.assertEqual(res_trap.margin_pattern, "RETAIL_MARGIN_CHASING_TRAP")
        self.assertIn("散戶融資套牢警訊", res_trap.semantic_tag)

        # 日曆回補雷達（除權息前 6 個交易日）
        in_win = is_mandatory_margin_covering_window(date(2026, 7, 2), [date(2026, 7, 10)])
        self.assertTrue(in_win)

        # 週頻集保總人數列優先（避免分級人數 + 合計列重複加總）
        tdcc_df = pd.DataFrame(
            [
                {"date": "2026-10-23", "HoldingSharesLevel": "1-999", "percent": 5.0, "people": 8000},
                {"date": "2026-10-23", "HoldingSharesLevel": "1,000,001以上", "percent": 42.0, "people": 120},
                {"date": "2026-10-23", "HoldingSharesLevel": "total", "percent": 100.0, "people": 10000},
                {"date": "2026-10-30", "HoldingSharesLevel": "1-999", "percent": 4.2, "people": 7500},
                {"date": "2026-10-30", "HoldingSharesLevel": "1,000,001以上", "percent": 44.5, "people": 135},
                {"date": "2026-10-30", "HoldingSharesLevel": "total", "percent": 100.0, "people": 9300},
            ]
        )
        tdcc_info = evaluate_weekly_tdcc_anchor(tdcc_df, paid_in_capital_twd=3_000_000_000.0)
        self.assertIsNotNone(tdcc_info)
        assert tdcc_info is not None
        self.assertAlmostEqual(tdcc_info["major_holder_2w_diff"], 2.5)
        self.assertAlmostEqual(tdcc_info["total_shareholders_chg_pct"], -7.0)


if __name__ == "__main__":
    unittest.main()
