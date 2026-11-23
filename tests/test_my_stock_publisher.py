"""SASD v6.3.0 MY_STOCK 私人戰情指揮中心（模式 B 密碼學、雙向隔離、實戰邏輯與無明文掃描）測試。"""

from __future__ import annotations

import base64
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from cryptography.exceptions import InvalidTag
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
for _sub in (_ROOT, _ROOT / "services", _ROOT / "quant", _ROOT / "taishin"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from github_uploader import (  # noqa: E402
    GitHubUploadError,
    get_my_stock_portal_url,
    sync_portal_batch,
)
from my_stock_publisher import (  # noqa: E402
    DEFAULT_PADDING_BLOCK_SIZE,
    PHASE_MEMORY_SYNC,
    PHASE_MORNING_BRIEF,
    PRIVATE_COMMIT_MESSAGE,
    build_aad_for_path,
    build_my_stock_bundles,
    compress_and_pad,
    compute_settlement_slots,
    decrypt_envelope_to_payload,
    encrypt_payload_to_envelope,
    evaluate_position_discipline,
    generate_my_stock_secret,
    publish_my_stock_portal,
    round_tw_tick,
    save_morning_brief_state,
    unpad_and_decompress,
)
from signal_audit_service import (  # noqa: E402
    CATEGORY_TOP_PICK,
    STRATEGY_ALIGNMENT_2130,
    SignalAuditStore,
)


def _mock_resp(status_code: int, payload: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = text or json.dumps(payload or {})
    return resp


class TestMyStockCrypto(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip_and_32kb_padding(self) -> None:
        sec = generate_my_stock_secret()
        pw = sec["MY_STOCK_ENC_PASSPHRASE"]
        salt_hex = sec["MY_STOCK_KDF_SALT"]
        self.assertGreaterEqual(len(pw), 24)
        self.assertEqual(len(bytes.fromhex(salt_hex)), 16)

        payload = {
            "stock_name": "台積電 測試中文 🚀",
            "avg_cost": 980.25,
            "items": [1, 2, 3.14159],
        }
        aad = build_aad_for_path("private/data/latest.enc")
        # Use lower iterations only in fast unit tests (default 600k tested separately)
        env = encrypt_payload_to_envelope(payload, pw, salt_hex, aad, iterations=10_000)

        # Ciphertext = padded plaintext (multiple of 32 KB) + 16-byte GCM auth tag
        ct_bytes = base64.b64decode(env["ct"])
        padded_len = len(ct_bytes) - 16
        self.assertGreaterEqual(padded_len, DEFAULT_PADDING_BLOCK_SIZE)
        self.assertEqual(padded_len % DEFAULT_PADDING_BLOCK_SIZE, 0)

        recovered = decrypt_envelope_to_payload(env, pw, expected_aad=aad)
        self.assertEqual(recovered, payload)

    def test_tamper_aad_wrong_password_and_random_iv(self) -> None:
        sec = generate_my_stock_secret()
        pw = sec["MY_STOCK_ENC_PASSPHRASE"]
        salt_hex = sec["MY_STOCK_KDF_SALT"]
        aad = build_aad_for_path("private/data/manifest.enc")
        data = {"hello": "world", "val": 123.45}

        env1 = encrypt_payload_to_envelope(data, pw, salt_hex, aad, iterations=10_000)
        env2 = encrypt_payload_to_envelope(data, pw, salt_hex, aad, iterations=10_000)
        self.assertNotEqual(env1["iv"], env2["iv"])
        self.assertNotEqual(env1["ct"], env2["ct"])

        # 1. Wrong password -> InvalidTag
        with self.assertRaises(InvalidTag):
            decrypt_envelope_to_payload(env1, pw + "X", expected_aad=aad)

        # 2. Wrong expected AAD -> InvalidTag
        with self.assertRaises(InvalidTag):
            decrypt_envelope_to_payload(env1, pw, expected_aad="mystock:v1:private/data/latest.enc")

        # 3. 1-bit flip in ciphertext -> InvalidTag
        ct_raw = bytearray(base64.b64decode(env1["ct"]))
        ct_raw[10] ^= 0x01
        tampered = {**env1, "ct": base64.b64encode(bytes(ct_raw)).decode("ascii")}
        with self.assertRaises(InvalidTag):
            decrypt_envelope_to_payload(tampered, pw, expected_aad=aad)

    def test_compress_and_pad_unpad_exact(self) -> None:
        raw = ("測試中文內容與浮點數1234.56" * 50).encode("utf-8")
        padded = compress_and_pad(raw)
        self.assertEqual(len(padded) % DEFAULT_PADDING_BLOCK_SIZE, 0)
        self.assertEqual(unpad_and_decompress(padded), raw)

    def test_round_tw_tick(self) -> None:
        self.assertEqual(round_tw_tick(8.126, stock_id="2303"), 8.13)
        self.assertEqual(round_tw_tick(34.23, stock_id="2303"), 34.25)
        self.assertEqual(round_tw_tick(78.46, stock_id="2303"), 78.5)
        self.assertEqual(round_tw_tick(245.7, stock_id="2317"), 245.5)
        self.assertEqual(round_tw_tick(685.6, stock_id="3017"), 686.0)
        self.assertEqual(round_tw_tick(1053.0, stock_id="2330"), 1055.0)
        # ETF rule: >= 50 uses 0.05 tick
        self.assertEqual(round_tw_tick(192.43, stock_id="0050"), 192.45)


class TestDualTrackPathIsolation(unittest.TestCase):
    def test_private_scope_blocks_non_private_writes_and_protects_public_files(self) -> None:
        # 1. Writing outside private/ MUST raise GitHubUploadError immediately
        with self.assertRaises(GitHubUploadError):
            sync_portal_batch(
                {"index.html": b"hack"},
                scope="private",
                token="tok",
                repository="user/repo",
                branch="gh-pages",
            )
        with self.assertRaises(GitHubUploadError):
            sync_portal_batch(
                {"private/../index.html": b"traversal"},
                scope="private",
                token="tok",
                repository="user/repo",
                branch="gh-pages",
            )

        # 2. Public scope MUST reject writing into private/
        with self.assertRaises(GitHubUploadError):
            sync_portal_batch(
                {"private/index.html": b"hack"},
                scope="public",
                token="tok",
                repository="user/repo",
                branch="gh-pages",
            )

    def test_private_scope_prunes_only_expired_private_daily_enc(self) -> None:
        posted_trees: list[dict] = []

        def _fake_get(url: str, **kwargs):
            if "/git/ref/heads/" in url:
                return _mock_resp(200, {"object": {"sha": "head_1"}})
            if "/git/commits/" in url:
                return _mock_resp(200, {"tree": {"sha": "tree_1"}})
            if "/git/trees/" in url:
                return _mock_resp(
                    200,
                    {
                        "tree": [
                            {"path": "index.html", "type": "blob", "sha": "pub_idx"},
                            {"path": "charts/2330_fund.html", "type": "blob", "sha": "pub_chart"},
                            {"path": "data/daily/2026-09-01.json", "type": "blob", "sha": "pub_daily"},
                            {"path": "private/data/daily/2026-10-01.enc", "type": "blob", "sha": "old_priv_enc"},
                            {"path": "private/data/daily/2026-11-23.enc", "type": "blob", "sha": "keep_priv_enc"},
                        ]
                    },
                )
            raise AssertionError(url)

        def _fake_post(url: str, **kwargs):
            body = kwargs.get("json") or {}
            if url.endswith("/git/blobs"):
                return _mock_resp(201, {"sha": "blob_new"})
            if url.endswith("/git/trees"):
                posted_trees.append(body)
                return _mock_resp(201, {"sha": "tree_new"})
            if url.endswith("/git/commits"):
                return _mock_resp(201, {"sha": "commit_new"})
            raise AssertionError(url)

        with (
            patch("github_uploader.requests.get", side_effect=_fake_get),
            patch("github_uploader.requests.post", side_effect=_fake_post),
            patch("github_uploader.requests.patch", return_value=_mock_resp(200, {})),
        ):
            res = sync_portal_batch(
                {"private/data/latest.enc": b"{}"},
                delete_paths=["index.html", "charts/2330_fund.html", "data/daily/2026-09-01.json"],
                keep_daily_dates={"2026-11-23"},
                commit_message=PRIVATE_COMMIT_MESSAGE,
                scope="private",
                token="tok",
                repository="NickChung0725/Discord_Service",
                branch="gh-pages",
            )

        self.assertEqual(res["uploaded"], ["private/data/latest.enc"])
        self.assertEqual(res["deleted"], ["private/data/daily/2026-10-01.enc"])
        self.assertEqual(res["portal_url"], "https://NickChung0725.github.io/Discord_Service/private/")
        # Verify NO public paths appear in tree entries
        for entry in posted_trees[0]["tree"]:
            self.assertTrue(entry["path"].startswith("private/"))

    def test_public_scope_never_deletes_private_files(self) -> None:
        def _fake_get(url: str, **kwargs):
            if "/git/ref/heads/" in url:
                return _mock_resp(200, {"object": {"sha": "head_1"}})
            if "/git/commits/" in url:
                return _mock_resp(200, {"tree": {"sha": "tree_1"}})
            if "/git/trees/" in url:
                return _mock_resp(
                    200,
                    {
                        "tree": [
                            {"path": "private/index.html", "type": "blob", "sha": "p_idx"},
                            {"path": "private/data/latest.enc", "type": "blob", "sha": "p_lat"},
                            {"path": "data/daily/2026-09-01.json", "type": "blob", "sha": "old_pub"},
                        ]
                    },
                )
            raise AssertionError(url)

        with (
            patch("github_uploader.requests.get", side_effect=_fake_get),
            patch("github_uploader.requests.post", side_effect=[
                _mock_resp(201, {"sha": "b1"}),
                _mock_resp(201, {"sha": "t1"}),
                _mock_resp(201, {"sha": "c1"}),
            ]),
            patch("github_uploader.requests.patch", return_value=_mock_resp(200, {})),
        ):
            res = sync_portal_batch(
                {"data/manifest.json": b"{}"},
                delete_paths=["private/index.html", "private/data/latest.enc"],
                keep_daily_dates={"2026-11-23"},
                scope="public",
                token="tok",
                repository="user/repo",
                branch="gh-pages",
            )
        self.assertEqual(res["deleted"], ["data/daily/2026-09-01.json"])


class TestMyStockBusinessLogicAndEndToEnd(unittest.TestCase):
    def test_settlement_three_slots_and_no_legacy_bank_keys(self) -> None:
        # Trade date: Monday 2026-11-23
        # T+2 today (11-23) <- bought on Thu 11-19 (net -50000 => payable +50000)
        # T+1 tomorrow (11-24) <- sold on Fri 11-20 (net +80000 => receivable -80000)
        # T+0 day after (11-25) <- bought on Mon 11-23 (net -30000 => payable +30000)
        stmts = [
            {"trade_date": "2026-11-19", "settlement_date": "2026-11-23", "stock_no": "2330", "stock_name": "台積電", "action": "BUY", "filled_qty": 100, "net_amount": -50000},
            {"trade_date": "2026-11-20", "settlement_date": "2026-11-24", "stock_no": "2317", "stock_name": "鴻海", "action": "SELL", "filled_qty": 400, "net_amount": 80000},
            {"trade_date": "2026-11-23", "settlement_date": "", "stock_no": "0050", "stock_name": "元大台灣50", "action": "BUY", "filled_qty": 150, "net_amount": -30000},
        ]
        res = compute_settlement_slots(
            trade_date=date(2026, 11, 23),
            statement_rows=stmts,
            settlement_rows=[],
        )
        self.assertEqual(res["t_plus_2_due_today"], 50000)
        self.assertEqual(res["t_plus_1_due_tomorrow"], -80000)
        self.assertEqual(res["t_plus_0_due_day_after"], 30000)
        self.assertNotIn("bank_balance", res)
        self.assertNotIn("shortfall_alert", res)

    def test_zero_token_discipline_checker_flags_unexecuted_stop_and_compliant_tp(self) -> None:
        mem_broken = {
            "stock_no": "3324",
            "status": "OPEN",
            "trailing_stop": 650.0,
            "initial_tp1": 750.0,
            "recent_battle_log": (
                "[2026-11-20 盤前晨報] 🚨 觸及停損（防守價 650.00）｜指令：跌破防守線停損\n"
                "[2026-11-20 盤後事實] 今日無買賣（續抱 1,000 股）｜收盤 635.0 (-3.2%)"
            ),
        }
        disc_broken = evaluate_position_discipline(mem_broken, statement_rows=[], tech_info={"close": 635.0})
        self.assertEqual(disc_broken["status"], "UNEXECUTED_STOP")
        self.assertIn("破線未砍", disc_broken["badge"])

        mem_tp = {
            "stock_no": "2330",
            "status": "TP1_PARTIAL_TAKEN",
            "trailing_stop": 990.0,
            "initial_tp1": 1070.0,
            "recent_battle_log": (
                "[2026-11-20 盤前晨報] 💰 分批停利（防守價 990.00）｜指令：掛盤中零股賣 500 股\n"
                "[2026-11-20 盤後事實] 今日賣出 500 股｜收盤 1075.0 (+2.4%)"
            ),
        }
        disc_tp = evaluate_position_discipline(
            mem_tp,
            statement_rows=[{"trade_date": "2026-11-20", "stock_no": "2330", "action": "SELL", "filled_qty": 500}],
            tech_info={"close": 1075.0},
        )
        self.assertEqual(disc_tp["status"], "COMPLIANT")
        self.assertIn("紀律", disc_tp["badge"])

    def test_full_bundle_empty_portfolio_and_zero_plaintext_leak_plus_key_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daily_dir = root / "daily"
            state_dir = root / ".state"
            daily_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            # Create 40-day synthetic prices CSV for 2330, 3324, 3017 (high corr between 3324 & 3017)
            dates = pd.bdate_range("2026-09-15", periods=40)
            price_rows = []
            for idx, dt in enumerate(dates):
                d_s = dt.date().isoformat()
                base_move = 1.0 + 0.01 * ((idx % 5) - 2) + 0.003 * idx
                price_rows.append({"date": d_s, "stock_id": "2330", "close": round(980.0 + idx * 2.0, 2)})
                price_rows.append({"date": d_s, "stock_id": "3324", "close": round(640.0 * base_move, 2)})
                price_rows.append({"date": d_s, "stock_id": "3017", "close": round(680.0 * base_move, 2)})
            pd.DataFrame(price_rows).to_csv(daily_dir / "2026-11-23_prices.csv", index=False)

            (state_dir / "stock_taxonomy.json").write_text(
                json.dumps(
                    {
                        "2330": {"stock_name": "台積電", "sub_industry": "晶圓代工"},
                        "3324": {"stock_name": "雙鴻", "sub_industry": "散熱模組"},
                        "3017": {"stock_name": "奇鋐", "sub_industry": "散熱模組"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # Setup SQLite signal_audit.db
            db_path = root / "signal_audit.db"
            store = SignalAuditStore(db_path)
            store.record_signal(
                {
                    "signal_date": "2026-11-01",
                    "strategy_id": STRATEGY_ALIGNMENT_2130,
                    "pick_category": CATEGORY_TOP_PICK,
                    "stock_id": "2330",
                    "stock_name": "台積電",
                    "sector_name": "晶圓代工",
                    "regime_state": "GREEN_BULL",
                    "scanner_signal_type": "STYLE_B_PULLBACK",
                    "ai_verdict_grade": "S_TIER_CORE",
                    "execution_strategy_type": "STRATEGIC_3TIER_PULLBACK",
                    "close_t": 980.0,
                    "tier1_entry_price": 975.0,
                    "chase_limit_price": 995.0,
                    "hard_stop_loss": 930.0,
                    "tp1_price": 1050.0,
                    "deterministic_rr_ratio": 2.2,
                }
            )
            store.backfill_pending(lambda _sid, _dt: [{"open": 978.0, "high": 1060.0, "low": 972.0, "close": 1055.0}])

            # 1. Empty portfolio (0-Token mode) must not crash
            empty_bundle = build_my_stock_bundles(
                phase=PHASE_MORNING_BRIEF,
                trade_date=date(2026, 11, 23),
                holdings_rows=[],
                memory_rows=[],
                statement_rows=[],
                settlement_rows=[],
                dca_rows=[],
                tech_map={},
                market_regime={"light": "🟡黃燈", "recommended_max_exposure_pct": 60},
                reports_dir=root,
                audit_db_path=db_path,
            )
            self.assertEqual(empty_bundle["latest"]["holdings"], [])
            self.assertEqual(empty_bundle["latest"]["order_ticket_summary"]["total_buy_amount"], 0)

            # 2. Full rich portfolio with v6.2.3 features + weakest holding swap + unexecuted stop
            holdings = [
                {"stock_no": "2330", "stock_name": "台積電", "order_type": "現股", "qty": 1000, "avg_cost_price": 980.0, "total_cost": 980000, "current_price": 1075.0, "market_value": 1075000, "unrealized_pnl": 95000, "return_pct": 9.69, "weight_pct": 55.0, "ma17": 1030, "ma45": 1000, "ma117": 960, "ma189": 920},
                {"stock_no": "3324", "stock_name": "雙鴻", "order_type": "現股", "qty": 1000, "avg_cost_price": 660.0, "total_cost": 660000, "current_price": 635.0, "market_value": 635000, "unrealized_pnl": -25000, "return_pct": -3.79, "weight_pct": 38.0, "ma17": 655, "ma45": 650, "ma117": 620, "ma189": 590},
                {"stock_no": "0050", "stock_name": "元大台灣50", "order_type": "盤中零股", "qty": 500, "avg_cost_price": 185.0, "total_cost": 92500, "current_price": 192.0, "market_value": 96000, "unrealized_pnl": 3500, "return_pct": 3.78, "weight_pct": 7.0},
            ]
            memories = [
                {
                    "position_id": "2026-11-10_2330",
                    "stock_no": "2330",
                    "stock_name": "台積電",
                    "status": "OPEN",
                    "entry_source": "alignment_2130",
                    "source_scan_date": "2026-11-09",
                    "source_rank": 1,
                    "opened_date": "2026-11-10",
                    "initial_qty": 1000,
                    "peak_qty": 1000,
                    "current_qty": 1000,
                    "avg_cost_price": 980.0,
                    "initial_tp1": 1070.0,
                    "initial_tp2": 1140.0,
                    "initial_sl": 945.0,
                    "trailing_stop": 1010.0,
                    "entry_thesis": "CoWoS 產能翻倍",
                    "invalidation_rule": "月營收 YoY 轉負",
                    "Allocation_Weight_Pct": 30,
                    "Tier1_Entry_Plan": "【第1批 30% 試單】回測 980 元",
                    "Tier2_Entry_Plan": "【第2批 40% 核心】回測 955 元",
                    "Tier3_Entry_Plan": "【第3批 30% 黃金坑】948 元",
                    "wave_buy_log": "已買 1 批：第1批(2026-11-10) 買 1,000 股 @ 980.0 元",
                    "recent_battle_log": "[2026-11-20 盤前晨報] 🟢 順勢續抱（防守價 1010.00）\n[2026-11-20 盤後事實] 今日無買賣（續抱 1,000 股）｜收盤 1055.0",
                },
                {
                    "position_id": "2026-11-12_3324",
                    "stock_no": "3324",
                    "stock_name": "雙鴻",
                    "status": "OPEN",
                    "entry_source": "alignment_2130",
                    "source_scan_date": "2026-11-11",
                    "opened_date": "2026-11-12",
                    "initial_qty": 1000,
                    "peak_qty": 1000,
                    "current_qty": 1000,
                    "avg_cost_price": 660.0,
                    "initial_tp1": 740.0,
                    "initial_tp2": 800.0,
                    "initial_sl": 640.0,
                    "trailing_stop": 645.0,
                    "entry_thesis": "水冷散熱滲透率提升",
                    "invalidation_rule": "跌破 640 停損",
                    "recent_battle_log": "[2026-11-20 盤前晨報] 🚨 觸及停損（防守價 645.00）\n[2026-11-20 盤後事實] 今日無買賣（續抱 1,000 股）｜收盤 635.0",
                },
            ]
            picks_bundle = {
                "ai_daily_picks": [
                    {
                        "scan_date": "2026-11-22",
                        "category": "TOP_PICK",
                        "stock_id": "2330",
                        "stock_name": "台積電",
                        "rank": 1,
                        "close_price": 1075.0,
                        "TP1_Target_Or_POC": 1140.0,
                        "Hard_Stop_Loss": 1010.0,
                    },
                    {
                        "scan_date": "2026-11-22",
                        "category": "TOP_PICK",
                        "stock_id": "3017",
                        "stock_name": "奇鋐",
                        "rank": 2,
                        "sub_industry": "散熱模組",
                        "close_price": 685.0,
                        "Tier1_Entry_Price": 675.0,
                        "TP1_Target_Or_POC": 745.0,
                        "Hard_Stop_Loss": 635.0,
                        "Allocation_Weight_Pct": 15.0,
                    },
                ],
                "rebound_watchlist": [],
            }
            deep_fund_map = {
                "2330": {
                    "strategic_tech_pkg": {
                        "poc_60d": 1005.0,
                        "val_60d": 975.0,
                        "vah_60d": 1045.0,
                        "trust_vwap_20d": 1018.5,
                        "foreign_vwap_20d": 1012.0,
                        "rs_beta": {"beta_60d": 1.2, "summary": "RS+6% Beta=1.2"},
                        "daily_table_markdown": "| 日期 | 收盤 |\n|---|---|\n| 2026-11-23 | 1075 |",
                    }
                }
            }

            save_morning_brief_state(
                root,
                trade_date=date(2026, 11, 23),
                headline_summary="費半大漲，2330 準備 TP1 鎖利，3324 嚴格處理破線",
                full_markdown="完整晨報測試內容",
                decisions_by_sno={
                    "2330": {"ai_action_tag": "💰 分批停利", "trailing_stop": 1010.0, "order_instruction": "掛盤中零股賣 500 股 @ 1070"},
                },
            )

            sec1 = generate_my_stock_secret()
            with patch("my_stock_publisher.DEFAULT_KDF_ITERATIONS", 10_000):
                pub_res = publish_my_stock_portal(
                    phase=PHASE_MORNING_BRIEF,
                    trade_date=date(2026, 11, 23),
                    holdings_rows=holdings,
                    memory_rows=memories,
                    statement_rows=[
                        {"trade_date": "2026-11-10", "stock_no": "2330", "action": "BUY", "filled_qty": 1000, "filled_price": 980.0, "net_amount": -981396}
                    ],
                    settlement_rows=[],
                    dca_rows=[
                        {"iso_year_week": "2026-W47", "eval_date": "2026-11-20", "close_price": 192.0, "dip_score": 45, "order_qty_shares": 50, "trigger_reason": "觀察"}
                    ],
                    tech_map={"2330": {"close": 1075.0, "ma17": 1030.0}, "3324": {"close": 635.0, "ma17": 655.0}},
                    market_regime={"light": "🟢綠燈", "recommended_max_exposure_pct": 80},
                    picks_bundle=picks_bundle,
                    deep_fund_map=deep_fund_map,
                    reports_dir=root,
                    audit_db_path=db_path,
                    passphrase=sec1["MY_STOCK_ENC_PASSPHRASE"],
                    salt_hex=sec1["MY_STOCK_KDF_SALT"],
                    dry_run=True,
                    raise_on_error=True,
                )

            self.assertTrue(pub_res["dry_run"])
            mirror = Path(pub_res["mirror_dir"])

            # 3. Plaintext Leakage Static Scan on ALL published files (including .enc, index.html, manifest.webmanifest)
            forbidden_plaintext_tokens = ("台積電", "雙鴻", "奇鋐", "981396", "CoWoS 產能翻倍", "水冷散熱滲透率提升")
            for rel in pub_res["files"]:
                raw_text = (mirror / rel).read_text(encoding="utf-8", errors="ignore")
                for token in forbidden_plaintext_tokens:
                    self.assertNotIn(token, raw_text, f"Plaintext leak {token!r} found in {rel}!")

            # 4. Decrypt latest.enc & verify all Phase 1~3 combat features
            latest_env = json.loads((mirror / "private/data/latest.enc").read_text(encoding="utf-8"))
            latest_data = decrypt_envelope_to_payload(
                latest_env,
                sec1["MY_STOCK_ENC_PASSPHRASE"],
                expected_aad="mystock:v1:private/data/latest.enc",
            )
            by_sid = {h["stock_id"]: h for h in latest_data["holdings"]}
            # 3324 must be promoted to P0 due to UNEXECUTED_STOP
            self.assertEqual(by_sid["3324"]["priority_tier"], "P0")
            self.assertEqual(by_sid["3324"]["discipline_check"]["status"], "UNEXECUTED_STOP")
            # 2330 must have P1, reselection badge, 3-tier blueprint, VWAP/POC & 5d table
            self.assertEqual(by_sid["2330"]["priority_tier"], "P1")
            self.assertTrue(by_sid["2330"]["reselection"]["is_reselected"])
            self.assertEqual(by_sid["2330"]["memory"]["tier_blueprint"]["bullets_fired_count"], 1)
            self.assertEqual(by_sid["2330"]["microstructure"]["trust_vwap_20d"], 1018.5)
            self.assertEqual(by_sid["2330"]["microstructure"]["vol_poc_60d"], 1005.0)
            self.assertIn("2026-11-23", by_sid["2330"]["microstructure"]["daily_table_5d_markdown"])
            # 0050 must be in LONG_TERM pool
            self.assertEqual(by_sid["0050"]["pool_type"], "LONG_TERM")
            # Opportunity pool 3017 must detect sector weight > 35% (3324 has 38% in 散熱模組) & recommend swapping 3324
            opp_by_sid = {o["stock_id"]: o for o in latest_data["opportunity_pool"]}
            self.assertEqual(opp_by_sid["3017"]["exposure_check"]["status"], "SECTOR_OR_CORR_FULL_SWAP_ONLY")
            self.assertEqual(opp_by_sid["3017"]["exposure_check"]["weakest_holding_to_swap"]["stock_id"], "3324")
            self.assertIn("3324", opp_by_sid["3017"]["exposure_check"]["badge"])
            # Static order ticket summary
            self.assertGreater(latest_data["order_ticket_summary"]["total_sell_amount"], 0)

            # 5. Verify history_pnl.enc decrypts and includes system benchmark from signal_audit.db
            hist_env = json.loads((mirror / "private/data/history_pnl.enc").read_text(encoding="utf-8"))
            hist_data = decrypt_envelope_to_payload(
                hist_env,
                sec1["MY_STOCK_ENC_PASSPHRASE"],
                expected_aad="mystock:v1:private/data/history_pnl.enc",
            )
            self.assertTrue(hist_data["live_vs_system"]["system_top_pick"]["available"])
            self.assertEqual(hist_data["discipline_stats"]["unexecuted_stop_count"], 1)

            # 6. Verify Key Rotation (--rotate-all) invalidates old key across all files
            sec2 = generate_my_stock_secret()
            with patch("my_stock_publisher.DEFAULT_KDF_ITERATIONS", 10_000):
                publish_my_stock_portal(
                    phase=PHASE_MEMORY_SYNC,
                    trade_date=date(2026, 11, 23),
                    holdings_rows=holdings,
                    memory_rows=memories,
                    statement_rows=[],
                    settlement_rows=[],
                    dca_rows=[],
                    tech_map={},
                    market_regime={},
                    picks_bundle=picks_bundle,
                    reports_dir=root,
                    audit_db_path=db_path,
                    passphrase=sec2["MY_STOCK_ENC_PASSPHRASE"],
                    salt_hex=sec2["MY_STOCK_KDF_SALT"],
                    dry_run=True,
                    rotate_all=True,
                    raise_on_error=True,
                )
            rotated_daily_env = json.loads((mirror / "private/data/daily/2026-11-23.enc").read_text(encoding="utf-8"))
            with self.assertRaises(InvalidTag):
                decrypt_envelope_to_payload(
                    rotated_daily_env,
                    sec1["MY_STOCK_ENC_PASSPHRASE"],
                    expected_aad="mystock:v1:private/data/daily/2026-11-23.enc",
                )
            dec_new = decrypt_envelope_to_payload(
                rotated_daily_env,
                sec2["MY_STOCK_ENC_PASSPHRASE"],
                expected_aad="mystock:v1:private/data/daily/2026-11-23.enc",
            )
            self.assertEqual(dec_new["trade_date"], "2026-11-23")

    def test_discord_link_prepender_and_url_cleanliness(self) -> None:
        from taishin_portfolio_service import _prepend_my_stock_link

        with patch.dict("os.environ", {"MY_STOCK_PUBLISH_ENABLED": "true", "GITHUB_REPO": "NickChung0725/Discord_Service"}):
            out = _prepend_my_stock_link(["☀️ 【台新證券 盤前開盤作戰晨報】"])
            self.assertTrue(out[0].startswith("🔐 [開啟 MY_STOCK 私人戰情指揮中心](https://NickChung0725.github.io/Discord_Service/private/)"))
            self.assertNotIn("key=", out[0].lower())
            self.assertNotIn("passphrase", out[0].lower())

        with patch.dict("os.environ", {"MY_STOCK_PUBLISH_ENABLED": "false", "GITHUB_REPO": "NickChung0725/Discord_Service"}):
            out2 = _prepend_my_stock_link(["☀️ 原始訊息"])
            self.assertEqual(out2, ["☀️ 原始訊息"])

        self.assertEqual(
            get_my_stock_portal_url("NickChung0725/Discord_Service"),
            "https://NickChung0725.github.io/Discord_Service/private/",
        )

    def test_private_web_shell_security_csp_and_zero_external_deps(self) -> None:
        html_path = _ROOT / "web" / "private" / "index.html"
        manifest_path = _ROOT / "web" / "private" / "manifest.webmanifest"
        self.assertTrue(html_path.is_file())
        self.assertTrue(manifest_path.is_file())

        html_text = html_path.read_text(encoding="utf-8")
        # 1. Strict CSP & anti-crawler meta tags
        self.assertIn("Content-Security-Policy", html_text)
        self.assertIn("default-src 'self'", html_text)
        self.assertIn("connect-src 'self'", html_text)
        self.assertIn('content="noindex, nofollow, noarchive"', html_text)
        # 2. Zero external script/stylesheet/CDN imports
        self.assertNotIn("https://cdn.", html_text)
        self.assertNotIn("https://unpkg.", html_text)
        self.assertNotIn("https://fonts.googleapis.com", html_text)
        # 3. Mode B Web Crypto + IndexedDB non-extractable key + native gzip decompression
        self.assertIn("crypto.subtle.deriveKey", html_text)
        self.assertIn("extractable: false", html_text)
        self.assertIn("indexedDB.open", html_text)
        self.assertIn('DecompressionStream("gzip")', html_text)
        # 4. Combat features present (gap slider, order ticket modal, privacy curtain, 0050 radar, perf view)
        self.assertIn('id="gap-slider"', html_text)
        self.assertIn('id="ticket-modal"', html_text)
        self.assertIn('id="privacy-curtain"', html_text)
        self.assertIn('id="dca-0050-card"', html_text)
        self.assertIn('id="history-pnl-root"', html_text)
        # 5. Manifest valid JSON with zero personal info
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(man["name"], "MY_STOCK")
        self.assertEqual(man["display"], "standalone")


if __name__ == "__main__":
    unittest.main()
