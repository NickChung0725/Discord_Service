"""MY_STOCK 私人持股總覽與早晨戰情指揮中心 — 加密打包與發布模組 (`services/my_stock_publisher.py`).

SASD v6.3.0 (Final Combat-Enhanced Architecture):
- 端到端認證加密（模式 B）：JSON -> Gzip (mtime=0) -> 4-byte length + 隨機填充至 32 KB 倍數 ->
  AES-256-GCM (PBKDF2-HMAC-SHA256 600,000 iter, 固定部署級 KDF_SALT, 單檔獨立 12-byte IV, AAD 路徑綁定)。
- 嚴格雙向隔離：僅允許寫入或刪除 GitHub Pages 之 `private/*`，共用 `finmind_reports/.state/portal_publish.lock`。
- 完整整合 v6.2.3 實戰核心：三階子彈進度、投信/外資 VWAP 與 60 日 POC 支撐、近 5 日晨報 vs 盤後事實戰略日記、
  既有持股「昨晚再度入榜」偵測、機會池「汰弱換股配對」、以及 0-Token「知行合一」執行紀律自動檢核。
- 嚴格遵循紀律：零明文上雲、零 `bank_balance`/`shortfall_alert` 廢棄欄位、不硬編碼排程時間字串。
"""

from __future__ import annotations

import base64
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
import gzip
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import struct
import sys
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
for _sub in (_ROOT, _ROOT / "services", _ROOT / "quant", _ROOT / "taishin"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from github_uploader import (  # noqa: E402
    get_github_pages_base_url,
    get_my_stock_portal_url,
    sync_portal_batch,
)
from portal_publisher import (  # noqa: E402
    LOCK_FILENAME,
    _portal_publish_lock,
    wantgoo_url,
)

logger = logging.getLogger(__name__)

TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")

SCHEMA_VERSION = "6.3.0"
ENVELOPE_VERSION = 1
DEFAULT_KDF_ITERATIONS = 600_000
DEFAULT_PADDING_BLOCK_SIZE = 32_768
DEFAULT_RETENTION_TRADING_DAYS = 14
PRIVATE_COMMIT_MESSAGE = "chore(private): sync encrypted payload"

PRIVATE_WEB_DIR = _ROOT / "web" / "private"
MORNING_STATE_FILENAME = "my_stock_morning_state.json"
DAILY_PLAIN_ARCHIVE_DIRNAME = "my_stock_daily_plain"
LOCAL_MIRROR_DIRNAME = "my_stock_private"

PHASE_MORNING_BRIEF = "MORNING_BRIEF"
PHASE_DCA_0050_EVAL = "DCA_0050_EVAL"
PHASE_MEMORY_SYNC = "MEMORY_SYNC"
PHASE_MANUAL_PUBLISH = "MANUAL_PUBLISH"

_FORBIDDEN_LEGACY_KEYS = frozenset({"bank_balance", "shortfall_alert"})
_BATTLE_LOG_PREFIX_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\s+(盤前晨報|盤後事實)\]\s*(.*)$")


class MyStockCryptoError(RuntimeError):
    """Raised when MY_STOCK encryption/decryption or secret configuration is invalid."""


# --------------------------------------------------------------------------- #
# 1. Numeric, Tick & Utility Helpers
# --------------------------------------------------------------------------- #
def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        if math.isnan(value) or math.isinf(value):
            return default
        return float(value)
    text = str(value).strip().replace(",", "").rstrip("%").strip()
    if not text or text.lower() in ("none", "nan", "n/a"):
        return default
    try:
        num = float(text)
        return default if (math.isnan(num) or math.isinf(num)) else num
    except ValueError:
        return default


def _safe_float_opt(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return None if (math.isnan(value) or math.isinf(value)) else float(value)
    text = str(value).strip().replace(",", "").rstrip("%").strip()
    if not text or text.lower() in ("none", "nan", "n/a"):
        return None
    try:
        num = float(text)
        return None if (math.isnan(num) or math.isinf(num)) else num
    except ValueError:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    return int(round(_safe_float(value, float(default))))


def _clean_code(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    while text.startswith("'"):
        text = text[1:].strip()
    return text


def round_tw_tick(price: float, *, stock_id: str = "", is_etf: bool | None = None) -> float:
    """依台灣證券交易所升降單位規則校準合法委託價位。"""
    p = _safe_float(price, 0.0)
    if p <= 0:
        return 0.0
    etf_flag = is_etf if is_etf is not None else _clean_code(stock_id).startswith("00")
    if etf_flag:
        tick = 0.01 if p < 50.0 else 0.05
    else:
        if p < 10.0:
            tick = 0.01
        elif p < 50.0:
            tick = 0.05
        elif p < 100.0:
            tick = 0.1
        elif p < 500.0:
            tick = 0.5
        elif p < 1000.0:
            tick = 1.0
        else:
            tick = 5.0
    rounded = round(round(p / tick) * tick, 2)
    return rounded


def _add_tw_business_days(base_date: date, days: int) -> date:
    """簡易台股營業日加減（跳過週六日）。"""
    cur = base_date
    step = 1 if days >= 0 else -1
    remaining = abs(days)
    while remaining > 0:
        cur += timedelta(days=step)
        if cur.weekday() < 5:
            remaining -= 1
    return cur


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        pass


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read JSON %s: %s", path, exc)
        return None


def is_my_stock_publish_enabled() -> bool:
    return os.getenv("MY_STOCK_PUBLISH_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# 2. Mode B End-to-End Cryptography (Gzip + 32 KB Padding + AES-256-GCM + AAD)
# --------------------------------------------------------------------------- #
def generate_my_stock_secret() -> dict[str, str]:
    """安全產生高強度 Passphrase（>= 24 字元）與 16-byte KDF Salt（32 Hex 字元）。"""
    return {
        "MY_STOCK_ENC_PASSPHRASE": secrets.token_urlsafe(24),
        "MY_STOCK_KDF_SALT": secrets.token_hex(16),
    }


def derive_master_key(
    passphrase: str,
    salt_bytes: bytes,
    *,
    iterations: int = DEFAULT_KDF_ITERATIONS,
) -> bytes:
    """使用 PBKDF2-HMAC-SHA256 衍生 256-bit AES 主金鑰。"""
    if not passphrase:
        raise MyStockCryptoError("MY_STOCK_ENC_PASSPHRASE 不可為空。")
    if len(salt_bytes) < 16:
        raise MyStockCryptoError("MY_STOCK_KDF_SALT 長度必須至少為 16 bytes（32 個 Hex 字元）。")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt_bytes,
        iterations=int(iterations),
    )
    return kdf.derive(passphrase.encode("utf-8"))


def compress_and_pad(
    plaintext: bytes,
    *,
    block_size: int = DEFAULT_PADDING_BLOCK_SIZE,
) -> bytes:
    """Gzip (mtime=0) -> [4-byte BE length] + Gzip bytes + os.urandom padding 對齊 block_size 倍數。"""
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    gz_bytes = gzip.compress(plaintext, compresslevel=6, mtime=0)
    prefix = struct.pack(">I", len(gz_bytes))
    body_len = 4 + len(gz_bytes)
    total_len = max(block_size, ((body_len + block_size - 1) // block_size) * block_size)
    pad_len = total_len - body_len
    return prefix + gz_bytes + os.urandom(pad_len)


def unpad_and_decompress(padded: bytes) -> bytes:
    """還原 compress_and_pad 封裝之明文字節。"""
    if len(padded) < 4:
        raise MyStockCryptoError("密文解包格式錯誤（長度不足 4 bytes）。")
    (gz_len,) = struct.unpack(">I", padded[:4])
    if gz_len <= 0 or 4 + gz_len > len(padded):
        raise MyStockCryptoError("密文解包長度標頭不合法。")
    return gzip.decompress(padded[4 : 4 + gz_len])


def build_aad_for_path(remote_path: str) -> str:
    norm = remote_path.strip().lstrip("/").replace("\\", "/")
    return f"mystock:v{ENVELOPE_VERSION}:{norm}"


def encrypt_payload_to_envelope(
    data: Mapping[str, Any],
    passphrase: str,
    salt_hex: str,
    aad: str,
    *,
    iterations: int = DEFAULT_KDF_ITERATIONS,
    block_size: int = DEFAULT_PADDING_BLOCK_SIZE,
    _derived_key: bytes | None = None,
) -> dict[str, Any]:
    """將 JSON Payload 以 Gzip + 32 KB 隨機填充 + AES-256-GCM (AAD) 加密封裝為 `.enc` dict。"""
    try:
        salt_bytes = bytes.fromhex(salt_hex.strip())
    except ValueError as exc:
        raise MyStockCryptoError("MY_STOCK_KDF_SALT 必須為合法 Hex 字串（32 字元）。") from exc
    if len(salt_bytes) != 16:
        raise MyStockCryptoError("MY_STOCK_KDF_SALT 必須為 16 bytes（32 個 Hex 字元）。")

    raw_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    padded = compress_and_pad(raw_json, block_size=block_size)
    key = _derived_key if _derived_key is not None else derive_master_key(
        passphrase, salt_bytes, iterations=iterations
    )
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, padded, aad.encode("utf-8"))

    return {
        "v": ENVELOPE_VERSION,
        "alg": "AES-256-GCM",
        "kdf": "PBKDF2-SHA256",
        "iter": int(iterations),
        "salt": base64.b64encode(salt_bytes).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "aad": aad,
        "ct": base64.b64encode(ct).decode("ascii"),
    }


def decrypt_envelope_to_payload(
    envelope: Mapping[str, Any],
    passphrase: str,
    *,
    expected_aad: str | None = None,
) -> dict[str, Any]:
    """解密 `.enc` 封裝並驗證 AAD 與 GCM Auth Tag，驗證失敗拋出 InvalidTag。"""
    aad = str(envelope.get("aad") or "")
    if expected_aad is not None and aad != expected_aad:
        raise InvalidTag("AAD mismatch")
    salt_bytes = base64.b64decode(str(envelope["salt"]))
    iv = base64.b64decode(str(envelope["iv"]))
    ct = base64.b64decode(str(envelope["ct"]))
    iterations = int(envelope.get("iter", DEFAULT_KDF_ITERATIONS))
    key = derive_master_key(passphrase, salt_bytes, iterations=iterations)
    padded = AESGCM(key).decrypt(iv, ct, aad.encode("utf-8"))
    plain = unpad_and_decompress(padded)
    return json.loads(plain.decode("utf-8"))


# --------------------------------------------------------------------------- #
# 3. Local Morning State Cache (supports 3-phase intraday/evening refresh)
# --------------------------------------------------------------------------- #
def save_morning_brief_state(
    reports_dir: Path,
    *,
    trade_date: date | str,
    headline_summary: str,
    full_markdown: str,
    decisions_by_sno: Mapping[str, Mapping[str, Any]],
    macro_context: Mapping[str, Any] | None = None,
    model_version: str = "",
) -> Path:
    """將當日盤前晨報結果落盤於本地 `.state`，供同日 0050 評估與盤後同步刷新時無損合併。"""
    dt_str = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date)[:10]
    slim_decisions: dict[str, dict[str, Any]] = {}
    for sno, dec in (decisions_by_sno or {}).items():
        code = _clean_code(sno)
        if not code or not isinstance(dec, Mapping):
            continue
        resel = dec.get("reselection")
        slim_decisions[code] = {
            "ai_action_tag": str(dec.get("ai_action_tag") or ""),
            "trailing_stop": _safe_float_opt(dec.get("trailing_stop")),
            "order_instruction": str(dec.get("order_instruction") or ""),
            "pm_rationale": str(dec.get("pm_rationale") or ""),
            "reselection": dict(resel) if isinstance(resel, Mapping) else None,
        }
    payload = {
        "trade_date": dt_str,
        "saved_at": datetime.now(TAIPEI_TIMEZONE).isoformat(timespec="seconds"),
        "model_version": str(model_version or ""),
        "headline_summary": str(headline_summary or ""),
        "full_markdown": str(full_markdown or ""),
        "decisions_by_sno": slim_decisions,
        "macro_context": dict(macro_context) if isinstance(macro_context, Mapping) else {},
    }
    state_path = Path(reports_dir) / ".state" / MORNING_STATE_FILENAME
    _atomic_write_json(state_path, payload)
    return state_path


def load_morning_brief_state(reports_dir: Path, *, trade_date: date | str | None = None) -> dict[str, Any]:
    """讀取本地當日（或最近一次）盤前晨報快取。"""
    state_path = Path(reports_dir) / ".state" / MORNING_STATE_FILENAME
    raw = _read_json(state_path)
    if not isinstance(raw, dict):
        return {}
    if trade_date is not None:
        dt_str = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date)[:10]
        if str(raw.get("trade_date") or "") != dt_str:
            return {}
    return raw


# --------------------------------------------------------------------------- #
# 4. Settlement 3-Slot Calculator (T+2 / T+1 / T+0) — Zero Legacy Bank Keys
# --------------------------------------------------------------------------- #
def compute_settlement_slots(
    *,
    trade_date: date,
    statement_rows: Sequence[Mapping[str, Any]],
    settlement_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """拆解 T+2（今日必備）、T+1（明日）、T+0（後日）三時槽應收付交割款。

    符號規範（SASD v6.3.0 §3 Zone 1 & §7.2）：
    - `> 0`：應付交割款（銀行帳戶需準備扣款，今日 > 0 強制亮 P0 警示紅條）
    - `< 0`：應收交割款（入帳款項）
    - 嚴禁包含已廢棄之 `bank_balance` / `shortfall_alert` 鍵值。
    """
    d_today = trade_date
    d_t1 = _add_tw_business_days(trade_date, 1)
    d_t0 = _add_tw_business_days(trade_date, 2)
    target_dates = {
        d_today.isoformat(): "T+2",
        d_t1.isoformat(): "T+1",
        d_t0.isoformat(): "T+0",
    }

    slot_cashflow: dict[str, int] = {"T+2": 0, "T+1": 0, "T+0": 0}
    items: list[dict[str, Any]] = []

    # 1. 優先檢視 `03_Settlement_And_PnL` 中今日官方交割彙總（TODAY_SETTLEMENT）
    official_today_found = False
    for row in settlement_rows:
        rtype = str(row.get("record_type") or "").strip().upper()
        rdate = str(row.get("record_date") or "").strip()[:10]
        if rtype == "TODAY_SETTLEMENT" and rdate == d_today.isoformat():
            net_amt = _safe_int(row.get("net_amount"), 0)
            if net_amt != 0:
                official_today_found = True
                slot_cashflow["T+2"] += net_amt
                items.append(
                    {
                        "slot": "T+2",
                        "settlement_date": rdate,
                        "trade_date": "",
                        "stock_id": _clean_code(row.get("stock_no")),
                        "stock_name": str(row.get("stock_name") or "").strip(),
                        "action": "SETTLEMENT",
                        "qty": _safe_int(row.get("qty"), 0),
                        "due_amount": -net_amt,
                        "source": "TODAY_SETTLEMENT",
                    }
                )

    # 2. 由 `01_Account_Statement` 近 2 個交易日成交推算 T+2 / T+1 / T+0 待交割明細
    for row in statement_rows:
        t_date_str = str(row.get("trade_date") or "").strip()[:10]
        s_date_str = str(row.get("settlement_date") or "").strip()[:10]
        if not s_date_str and t_date_str:
            try:
                s_date_str = _add_tw_business_days(date.fromisoformat(t_date_str), 2).isoformat()
            except ValueError:
                continue
        slot = target_dates.get(s_date_str)
        if not slot:
            continue
        if slot == "T+2" and official_today_found:
            # 避免與官方 TODAY_SETTLEMENT 重複累加今日金額，但仍不遺漏 T+1 / T+0
            continue
        net_amt = _safe_int(row.get("net_amount"), 0)
        if net_amt == 0:
            action = str(row.get("action") or "").strip().upper()
            gross = _safe_int(row.get("gross_amount"), 0)
            fee = _safe_int(row.get("fee"), 0)
            tax = _safe_int(row.get("tax"), 0)
            if gross > 0:
                net_amt = -(gross + fee) if action == "BUY" else (gross - fee - tax)
        if net_amt == 0:
            continue
        slot_cashflow[slot] += net_amt
        items.append(
            {
                "slot": slot,
                "settlement_date": s_date_str,
                "trade_date": t_date_str,
                "stock_id": _clean_code(row.get("stock_no")),
                "stock_name": str(row.get("stock_name") or "").strip(),
                "action": str(row.get("action") or "").strip().upper(),
                "qty": _safe_int(row.get("filled_qty"), 0),
                "due_amount": -net_amt,
                "source": "STATEMENT",
            }
        )

    return {
        "t_plus_2_due_today": -slot_cashflow["T+2"],
        "t_plus_1_due_tomorrow": -slot_cashflow["T+1"],
        "t_plus_0_due_day_after": -slot_cashflow["T+0"],
        "t_plus_2_date": d_today.isoformat(),
        "t_plus_1_date": d_t1.isoformat(),
        "t_plus_0_date": d_t0.isoformat(),
        "items": items,
    }


# --------------------------------------------------------------------------- #
# 5. 0-Token Execution Discipline Checker (知行合一檢核) & Battle Log Parser
# --------------------------------------------------------------------------- #
def parse_recent_battle_log(
    raw_log: Any,
    *,
    stock_id: str,
    trailing_stop: float,
    initial_tp1: float,
    status: str,
    statement_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """將 `recent_battle_log` 文字解析為依日期成對的晨報/盤後時間軸，並附加 0-Token 紀律徽章。"""
    text = str(raw_log or "").strip()
    if not text:
        return []

    by_date: dict[str, dict[str, str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _BATTLE_LOG_PREFIX_RE.match(line)
        if m:
            dt_s, kind, _body = m.groups()
            slot = by_date.setdefault(dt_s, {"date": dt_s, "morning_plan": "", "evening_fact": ""})
            if kind == "盤前晨報":
                slot["morning_plan"] = line
            else:
                slot["evening_fact"] = line
        else:
            # fallback anonymous entry
            slot = by_date.setdefault("NOTE", {"date": "NOTE", "morning_plan": "", "evening_fact": ""})
            slot["evening_fact"] = (slot["evening_fact"] + " " + line).strip()

    # 建立該股逐日實際成交摘要
    trades_by_date: dict[str, dict[str, int]] = defaultdict(lambda: {"BUY": 0, "SELL": 0})
    for row in statement_rows:
        if _clean_code(row.get("stock_no")) != stock_id:
            continue
        td = str(row.get("trade_date") or "").strip()[:10]
        act = str(row.get("action") or "").strip().upper()
        qty = _safe_int(row.get("filled_qty"), 0)
        if td and act in ("BUY", "SELL") and qty > 0:
            trades_by_date[td][act] += qty

    timeline: list[dict[str, Any]] = []
    for dt_s in sorted(by_date.keys(), reverse=True):
        entry = by_date[dt_s]
        morning_plan = entry["morning_plan"]
        evening_fact = entry["evening_fact"]
        t_day = trades_by_date.get(dt_s, {"BUY": 0, "SELL": 0})
        sold_qty = t_day["SELL"]
        bought_qty = t_day["BUY"]

        # 萃取當日盤後收盤價（若盤後事實內有 `收盤 XXX`）
        close_match = re.search(r"收盤\s*([0-9]+(?:\.[0-9]+)?)", evening_fact)
        day_close = _safe_float(close_match.group(1), 0.0) if close_match else 0.0
        # 萃取當日晨報防守價
        stop_match = re.search(r"防守價\s*([0-9]+(?:\.[0-9]+)?)", morning_plan)
        day_stop = _safe_float(stop_match.group(1), trailing_stop) if stop_match else trailing_stop

        stop_ordered = ("觸及停損" in morning_plan) or ("邏輯失效減碼" in morning_plan)
        tp1_ordered = ("分批停利" in morning_plan) or ("達標收割" in morning_plan)
        add_ordered = "逢低加碼" in morning_plan
        stop_breached_close = day_stop > 0 and day_close > 0 and day_close <= day_stop
        tp1_hit_close = initial_tp1 > 0 and day_close > 0 and day_close >= initial_tp1 * 0.995

        if evening_fact:
            if (stop_ordered or stop_breached_close) and sold_qty > 0:
                badge = "✅ 紀律停損/減碼"
                code = "COMPLIANT_STOP"
            elif (stop_ordered or stop_breached_close) and sold_qty == 0:
                badge = "⚠️ 破線未砍"
                code = "UNEXECUTED_STOP"
            elif (tp1_ordered or (tp1_hit_close and status == "OPEN")) and sold_qty > 0:
                badge = "✅ 紀律執行鎖利"
                code = "COMPLIANT_TP"
            elif (tp1_ordered or (tp1_hit_close and status == "OPEN")) and sold_qty == 0:
                badge = "⚠️ 達標未鎖利"
                code = "UNEXECUTED_TP1"
            elif add_ordered and bought_qty > 0:
                badge = "✅ 紀律分批低接"
                code = "COMPLIANT_ADD"
            elif sold_qty > 0 or bought_qty > 0:
                badge = "✅ 已執行委託"
                code = "EXECUTED"
            else:
                badge = "🟢 劇本續抱"
                code = "HOLDING"
        else:
            badge = "☀️ 今日待執行"
            code = "PENDING"

        timeline.append(
            {
                "date": dt_s,
                "morning_plan": morning_plan,
                "evening_fact": evening_fact,
                "discipline_code": code,
                "discipline_badge": badge,
                "bought_qty": bought_qty,
                "sold_qty": sold_qty,
            }
        )
    return timeline


def evaluate_position_discipline(
    memory_row: Mapping[str, Any],
    statement_rows: Sequence[Mapping[str, Any]],
    tech_info: Mapping[str, Any] | None = None,
    *,
    latest_close: float = 0.0,
    timeline: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """0-Token 知行合一執行紀律檢核器。

    比對最近一個已收盤交易日的「早盤指令／收盤觸價事實」與「對帳單實際成交」，
    若昨日收盤破線未砍或達標 TP1 未賣半，回傳醒目警示供早盤置頂提醒。
    """
    stock_id = _clean_code(memory_row.get("stock_no"))
    status = str(memory_row.get("status") or "OPEN").strip().upper()
    trailing_stop = _safe_float(memory_row.get("trailing_stop"), 0.0) or _safe_float(
        memory_row.get("initial_sl"), 0.0
    )
    initial_tp1 = _safe_float(memory_row.get("initial_tp1"), 0.0)
    last_action = str(memory_row.get("last_ai_action") or "").strip()

    parsed_tl = (
        list(timeline)
        if timeline is not None
        else parse_recent_battle_log(
            memory_row.get("recent_battle_log"),
            stock_id=stock_id,
            trailing_stop=trailing_stop,
            initial_tp1=initial_tp1,
            status=status,
            statement_rows=statement_rows,
        )
    )

    # 1. 優先檢視最近一筆已結算（具備盤後事實）之戰略日記檢核結果
    latest_settled = next((item for item in parsed_tl if item.get("evening_fact")), None)
    if latest_settled:
        code = str(latest_settled.get("discipline_code") or "HOLDING")
        dt_s = str(latest_settled.get("date") or "")
        if code == "UNEXECUTED_STOP":
            return {
                "status": "UNEXECUTED_STOP",
                "badge": "⚠️ 破線未砍",
                "unexecuted_alert": f"🚨 昨日 ({dt_s}) 跌破防守線 {trailing_stop:.2f} 元且無賣出成交（防凹單警戒）",
            }
        if code == "UNEXECUTED_TP1":
            return {
                "status": "UNEXECUTED_TP1",
                "badge": "⚠️ 達標未鎖利",
                "unexecuted_alert": f"🔔 昨日 ({dt_s}) 達標 TP1 ({initial_tp1:.2f} 元) 尚未執行風格 B 賣半鎖利",
            }
        if code.startswith("COMPLIANT"):
            return {
                "status": "COMPLIANT",
                "badge": str(latest_settled.get("discipline_badge") or "✅ 紀律達標"),
                "unexecuted_alert": None,
            }

    # 2. 若尚無盤後日記但收盤價已低於棘輪防守線且最近交易日無賣單 -> 提醒破線未處置
    cur_close = latest_close or _safe_float((tech_info or {}).get("close"), 0.0)
    last_eval_dt = str(memory_row.get("last_eval_date") or "").strip()[:10]
    if trailing_stop > 0 and cur_close > 0 and cur_close <= trailing_stop and last_action in ("🚨 觸及停損", "⚠️ 邏輯失效減碼"):
        sold_on_eval = any(
            _clean_code(r.get("stock_no")) == stock_id
            and str(r.get("action") or "").upper() == "SELL"
            and str(r.get("trade_date") or "")[:10] >= last_eval_dt
            for r in statement_rows
            if last_eval_dt
        )
        if not sold_on_eval:
            return {
                "status": "UNEXECUTED_STOP",
                "badge": "⚠️ 破線未砍",
                "unexecuted_alert": f"🚨 昨收 {cur_close:.2f} 低於防守價 {trailing_stop:.2f} 元且尚未處置（防凹單警戒）",
            }

    return {
        "status": "HOLDING",
        "badge": "🟢 劇本續抱",
        "unexecuted_alert": None,
    }


def _compute_discipline_aggregate(
    memory_rows: Sequence[Mapping[str, Any]],
    statement_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """統計全部波段記憶卡之知行合一執行率（停損執行率、TP1 賣半執行率、破線未砍次數）。"""
    stop_triggered = 0
    stop_executed = 0
    tp1_triggered = 0
    tp1_executed = 0
    unexecuted_stop_events = 0
    compliant_days = 0
    actionable_days = 0

    for mem in memory_rows:
        sno = _clean_code(mem.get("stock_no"))
        if not sno:
            continue
        trailing_stop = _safe_float(mem.get("trailing_stop"), 0.0) or _safe_float(mem.get("initial_sl"), 0.0)
        initial_tp1 = _safe_float(mem.get("initial_tp1"), 0.0)
        status = str(mem.get("status") or "OPEN").upper()
        tl = parse_recent_battle_log(
            mem.get("recent_battle_log"),
            stock_id=sno,
            trailing_stop=trailing_stop,
            initial_tp1=initial_tp1,
            status=status,
            statement_rows=statement_rows,
        )
        for item in tl:
            code = item.get("discipline_code")
            if code == "COMPLIANT_STOP":
                stop_triggered += 1
                stop_executed += 1
                actionable_days += 1
                compliant_days += 1
            elif code == "UNEXECUTED_STOP":
                stop_triggered += 1
                unexecuted_stop_events += 1
                actionable_days += 1
            elif code == "COMPLIANT_TP":
                tp1_triggered += 1
                tp1_executed += 1
                actionable_days += 1
                compliant_days += 1
            elif code == "UNEXECUTED_TP1":
                tp1_triggered += 1
                actionable_days += 1
            elif code == "COMPLIANT_ADD":
                actionable_days += 1
                compliant_days += 1

    # 補充從已平倉波段統計風格 B 執行紀錄
    closed_total = 0
    closed_wins = 0
    closed_style_b_count = 0
    for mem in memory_rows:
        if str(mem.get("status") or "").upper() != "CLOSED":
            continue
        closed_total += 1
        pnl = _safe_int(mem.get("realized_pnl"), 0)
        if pnl > 0:
            closed_wins += 1
        peak_q = _safe_int(mem.get("peak_qty"), 0)
        init_q = _safe_int(mem.get("initial_qty"), 0)
        if "TP1" in str(mem.get("last_ai_summary") or "") or "賣半" in str(mem.get("last_ai_summary") or ""):
            closed_style_b_count += 1
        elif peak_q > 0 and init_q > 0 and "分批停利" in str(mem.get("last_ai_action") or ""):
            closed_style_b_count += 1

    overall_pct = round((compliant_days / actionable_days) * 100.0, 1) if actionable_days > 0 else 100.0
    stop_rate_pct = round((stop_executed / stop_triggered) * 100.0, 1) if stop_triggered > 0 else None
    tp1_rate_pct = round((tp1_executed / tp1_triggered) * 100.0, 1) if tp1_triggered > 0 else None

    return {
        "compliance_pct": overall_pct,
        "actionable_events": actionable_days,
        "compliant_events": compliant_days,
        "stop_triggered": stop_triggered,
        "stop_executed": stop_executed,
        "stop_execution_rate_pct": stop_rate_pct,
        "unexecuted_stop_count": unexecuted_stop_events,
        "tp1_triggered": tp1_triggered,
        "tp1_executed": tp1_executed,
        "tp1_execution_rate_pct": tp1_rate_pct,
        "closed_total": closed_total,
        "closed_wins": closed_wins,
        "closed_win_rate_pct": round((closed_wins / closed_total) * 100.0, 1) if closed_total > 0 else 0.0,
        "closed_style_b_count": closed_style_b_count,
    }


# --------------------------------------------------------------------------- #
# 6. Priority Tier, Order Ticket & Tier Blueprint Builders
# --------------------------------------------------------------------------- #
def _extract_plan_price(plan_text: Any) -> float | None:
    text = str(plan_text or "").strip()
    if not text:
        return None
    # 優先擷取價位數字（排除 30%、40%、17MA 等百分比或均線數字）
    cleaned = re.sub(r"\d+(?:\.\d+)?%", " ", text)
    cleaned = re.sub(r"\b(?:MA\d+|\d+MA|\d+日|第\d+批|Tier\s*\d)\b", " ", cleaned, flags=re.IGNORECASE)
    matches = re.findall(r"(?<![A-Za-z0-9_])(\d{1,5}(?:\.\d{1,2})?)(?![A-Za-z0-9_])", cleaned)
    nums = [float(m) for m in matches if 1.0 <= float(m) <= 20000.0]
    if not nums:
        return None
    return round(nums[0], 2)


def _count_fired_bullets(wave_buy_log: str, wave_state: Mapping[str, Any] | None = None) -> int:
    if isinstance(wave_state, Mapping) and isinstance(wave_state.get("buy_batches"), Sequence):
        cnt = len(wave_state["buy_batches"])
        if cnt > 0:
            return cnt
    m = re.search(r"已買\s*(\d+)\s*批", str(wave_buy_log or ""))
    if m:
        return int(m.group(1))
    return 1 if str(wave_buy_log or "").strip() else 0


def _build_tier_blueprint(
    mem: Mapping[str, Any],
    *,
    wave_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    wave_log = str(mem.get("wave_buy_log") or "").strip()
    fired = _count_fired_bullets(wave_log, wave_state)
    t1_txt = str(mem.get("Tier1_Entry_Plan") or "").strip()
    t2_txt = str(mem.get("Tier2_Entry_Plan") or "").strip()
    t3_txt = str(mem.get("Tier3_Entry_Plan") or "").strip()
    alloc_w = _safe_float_opt(mem.get("Allocation_Weight_Pct"))

    return {
        "allocation_weight_pct": alloc_w,
        "bullets_fired_count": fired,
        "bullets_total_tiers": 3,
        "wave_buy_log": wave_log,
        "tier1": {
            "label": "第1批 (30% 試單)",
            "plan_text": t1_txt,
            "price": _extract_plan_price(t1_txt),
            "filled": fired >= 1,
        },
        "tier2": {
            "label": "第2批 (40% 核心)",
            "plan_text": t2_txt,
            "price": _extract_plan_price(t2_txt),
            "filled": fired >= 2,
        },
        "tier3": {
            "label": "第3批 (30% 黃金坑)",
            "plan_text": t3_txt,
            "price": _extract_plan_price(t3_txt),
            "filled": fired >= 3,
        },
    }


def _classify_priority_and_ticket(
    *,
    stock_id: str,
    pool_type: str,
    qty: int,
    latest_close: float,
    action_tag: str,
    mem: Mapping[str, Any] | None,
    gk: Mapping[str, Any] | None,
    discipline_check: Mapping[str, Any] | None,
    tier_blueprint: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any] | None]:
    """決定個股急迫度分組 (P0/P1/P2/P3) 與對應之早盤建議掛單票匣項目。"""
    if pool_type != "SWING" or not mem:
        return "P3", None

    status = str(mem.get("status") or "OPEN").upper()
    gk = gk or {}
    disc = discipline_check or {}
    trailing_stop = _safe_float(mem.get("trailing_stop"), 0.0) or _safe_float(gk.get("ratchet_stop_floor"), 0.0)
    tp1 = _safe_float(mem.get("initial_tp1"), 0.0)
    tp2 = _safe_float(mem.get("initial_tp2"), 0.0)
    stop_breached = bool(gk.get("stop_breached"))
    tp1_reached = bool(gk.get("tp1_reached"))
    tp2_reached = bool(gk.get("tp2_reached"))
    trim_plan = gk.get("trim_plan") if isinstance(gk.get("trim_plan"), Mapping) else {}

    def _order_type_for(shares: int) -> str:
        return "BOARD_LOT" if (shares >= 1000 and shares % 1000 == 0) else "INTRADAY_ODD_LOT"

    # P0: 停損 / 邏輯失效 / 昨日破線未砍
    if (
        action_tag in ("🚨 觸及停損", "⚠️ 邏輯失效減碼")
        or stop_breached
        or disc.get("status") == "UNEXECUTED_STOP"
    ):
        ref_px = round_tw_tick(trailing_stop or latest_close, stock_id=stock_id)
        sell_qty = qty if action_tag == "🚨 觸及停損" or stop_breached else max(1, qty // 2)
        return "P0", {
            "side": "SELL",
            "group": "STOP_LOSS",
            "order_type": _order_type_for(sell_qty),
            "suggested_qty": sell_qty,
            "ref_price": ref_px,
            "est_gross_amount": int(round(sell_qty * ref_px)),
            "note": disc.get("unexecuted_alert") or f"觸及/跌破防守線 {ref_px} 元，執行紀律停損減碼",
        }

    # P1: 風格 B 停利鎖利（TP1 / TP2 或昨日達標未鎖利）
    if (
        action_tag in ("💰 分批停利", "🏆 達標收割")
        or (tp1_reached and status == "OPEN")
        or tp2_reached
        or disc.get("status") == "UNEXECUTED_TP1"
    ):
        if tp2_reached or action_tag == "🏆 達標收割":
            ref_px = round_tw_tick(tp2 or latest_close, stock_id=stock_id)
            sell_qty = qty
            note = f"達標 TP2 ({ref_px} 元) 執行波段獲利收割"
        else:
            ref_px = round_tw_tick(tp1 or latest_close, stock_id=stock_id)
            sell_qty = _safe_int(trim_plan.get("trim_qty"), max(1, qty // 2))
            note = str(trim_plan.get("instruction_hint") or f"達標 TP1 ({ref_px} 元) 執行風格 B 賣半鎖利")
        return "P1", {
            "side": "SELL",
            "group": "TAKE_PROFIT",
            "order_type": _order_type_for(sell_qty),
            "suggested_qty": sell_qty,
            "ref_price": ref_px,
            "est_gross_amount": int(round(sell_qty * ref_px)),
            "note": note,
        }

    # P2: 主動式雙軌決策（逢低加碼 / 破線轉價值防守 / 趁彈減碼換股）
    if action_tag in ("🎯 逢低加碼", "🛡️ 破線轉價值防守", "🔄 趁彈減碼換股"):
        if action_tag == "🔄 趁彈減碼換股":
            resist = _safe_float(gk.get("rebound_resistance_target"), 0.0) or latest_close
            ref_px = round_tw_tick(resist, stock_id=stock_id)
            sell_qty = max(1, qty // 2) if qty >= 2 else qty
            return "P2", {
                "side": "SELL",
                "group": "TRIM_SWAP",
                "order_type": _order_type_for(sell_qty),
                "suggested_qty": sell_qty,
                "ref_price": ref_px,
                "est_gross_amount": int(round(sell_qty * ref_px)),
                "note": f"反彈至第一反壓 {ref_px} 元附近減碼騰籠換鳥",
            }
        # 逢低加碼 / 價值防守：取下一批子彈目標價或左側結構支撐
        next_tier_px = None
        if isinstance(tier_blueprint, Mapping):
            fired = _safe_int(tier_blueprint.get("bullets_fired_count"), 1)
            if fired < 2 and (tier_blueprint.get("tier2") or {}).get("price"):
                next_tier_px = _safe_float((tier_blueprint.get("tier2") or {}).get("price"))
            elif fired < 3 and (tier_blueprint.get("tier3") or {}).get("price"):
                next_tier_px = _safe_float((tier_blueprint.get("tier3") or {}).get("price"))
        raw_buy_px = next_tier_px or _safe_float(gk.get("left_side_structural_floor"), 0.0) or latest_close
        ref_px = round_tw_tick(raw_buy_px, stock_id=stock_id)
        add_qty = max(100, min(qty, 1000)) if qty > 0 else 100
        return "P2", {
            "side": "BUY",
            "group": "DIP_ADD",
            "order_type": _order_type_for(add_qty),
            "suggested_qty": add_qty,
            "ref_price": ref_px,
            "est_gross_amount": int(round(add_qty * ref_px)),
            "note": (
                f"預掛下一批子彈低接參考價 {ref_px} 元"
                if action_tag == "🎯 逢低加碼"
                else f"左側結構支撐 {ref_px} 元價值防守觀察"
            ),
        }

    return "P3", None


# --------------------------------------------------------------------------- #
# 7. Bundle Builders (manifest, latest, daily, history_pnl)
# --------------------------------------------------------------------------- #
def _build_macro_section(
    macro_context: Mapping[str, Any] | None,
    market_regime: Mapping[str, Any] | None,
) -> dict[str, Any]:
    macro = macro_context or {}
    regime = market_regime or {}
    risk_tone = str(macro.get("risk_tone") or "🟡 國際中性震盪")
    if "🔴" in risk_tone or "逆風" in risk_tone:
        risk_light = "RISK_OFF"
    elif "🟢" in risk_tone or "順風" in risk_tone:
        risk_light = "RISK_ON"
    else:
        risk_light = "NEUTRAL"

    quotes = macro.get("quotes") if isinstance(macro.get("quotes"), Mapping) else {}
    us_indices: list[dict[str, Any]] = []
    symbol_map = (
        ("SOX", "^SOX", "費半"),
        ("IXIC", "^IXIC", "那指"),
        ("SP500", "^GSPC", "標普"),
        ("TSM", "TSM", "台積ADR"),
        ("NVDA", "NVDA", "NVDA"),
        ("VIX", "^VIX", "VIX"),
        ("USDTWD", "TWD=X", "美元台幣"),
    )
    for key, sym, label in symbol_map:
        q = quotes.get(key)
        if isinstance(q, Mapping):
            us_indices.append(
                {
                    "key": key,
                    "symbol": sym,
                    "label": str(q.get("label") or label),
                    "price": _safe_float_opt(q.get("price")),
                    "change_pct": round(_safe_float(q.get("change_pct"), 0.0), 2),
                    "trend_3d_str": str(q.get("trend_3d_str") or ""),
                }
            )

    def _extract_light_code(raw: Any) -> str:
        s = str(raw or "")
        if "綠" in s or "🟢" in s or "GREEN" in s.upper():
            return "GREEN"
        if "紅" in s or "🔴" in s or "RED" in s.upper():
            return "RED"
        return "YELLOW"

    exp_raw = regime.get("recommended_max_exposure_pct")
    rec_exp: int | None = None
    if isinstance(exp_raw, (int, float)):
        rec_exp = int(round(exp_raw))
    elif isinstance(exp_raw, str):
        nums = re.findall(r"\d+", exp_raw)
        if nums:
            rec_exp = int(nums[-1])

    margin_info = regime.get("margin_info") if isinstance(regime.get("margin_info"), Mapping) else {}
    margin_as_of = str(
        regime.get("margin_as_of")
        or margin_info.get("as_of_date")
        or regime.get("date")
        or ""
    )[:10]
    margin_1d = _safe_float_opt(regime.get("margin_chg_1d_yi") or margin_info.get("margin_chg_1d_yi"))
    margin_5d = _safe_float_opt(regime.get("margin_chg_5d_yi") or margin_info.get("margin_chg_5d_yi"))
    futures_net = _safe_int(
        regime.get("futures_net_oi") or regime.get("foreign_futures_net_oi") or 0
    )

    return {
        "risk_light": risk_light,
        "risk_tone": risk_tone,
        "risk_note": str(macro.get("risk_note") or ""),
        "us_as_of_date": str(macro.get("us_as_of_date") or ""),
        "compact_line": str(macro.get("compact_line") or ""),
        "us_indices": us_indices,
        "trajectory_lines": list(macro.get("trajectory_lines") or []),
        "headlines": list(macro.get("headlines") or [])[:5],
        "tw_regime": {
            "light_text": str(regime.get("light") or "🟡黃燈"),
            "taiex_light": _extract_light_code(regime.get("taiex_light") or regime.get("light")),
            "otc_light": _extract_light_code(regime.get("otc_light") or regime.get("light")),
            "regime_state": str((regime.get("regime_quota") or {}).get("regime_state") or ""),
            "summary": str(regime.get("summary") or regime.get("status") or ""),
            "futures_net_oi": futures_net,
            "margin_as_of": margin_as_of,
            "margin_chg_1d_yi": margin_1d,
            "margin_chg_5d_yi": margin_5d,
            "recommended_max_exposure_pct": rec_exp if rec_exp is not None else 60,
        },
    }


def _build_dca_0050_section(
    dca_rows: Sequence[Mapping[str, Any]],
    statement_rows: Sequence[Mapping[str, Any]],
    *,
    trade_date: date,
) -> dict[str, Any]:
    """組裝 Zone 5 左欄 0050 智慧尋底儀表（含本週五日進度燈與近 12 週微笑曲線）。"""
    iso_cal = trade_date.isocalendar()
    cur_week = f"{iso_cal.year:04d}-W{iso_cal.week:02d}"

    sorted_dca = sorted(
        [r for r in dca_rows if str(r.get("eval_date") or "").strip()],
        key=lambda r: str(r.get("eval_date") or ""),
    )

    weekday_flags = {"mon": False, "tue": False, "wed": False, "thu": False, "fri": False}
    weekday_keys = ("mon", "tue", "wed", "thu", "fri")
    bought_this_week = False

    # 從對帳單與 DCA 紀錄檢核本週五日買進狀態
    week_monday = trade_date - timedelta(days=trade_date.weekday())
    week_friday = week_monday + timedelta(days=4)
    for s in statement_rows:
        if _clean_code(s.get("stock_no")) != "0050":
            continue
        if str(s.get("action") or "").upper() != "BUY":
            continue
        td_str = str(s.get("trade_date") or "")[:10]
        try:
            td = date.fromisoformat(td_str)
        except ValueError:
            continue
        if week_monday <= td <= week_friday and td.weekday() < 5:
            weekday_flags[weekday_keys[td.weekday()]] = True
            bought_this_week = True

    latest = sorted_dca[-1] if sorted_dca else {}
    if latest and str(latest.get("iso_year_week") or "") == cur_week:
        if str(latest.get("already_bought_this_week") or "").upper() == "TRUE":
            bought_this_week = True
        if _safe_int(latest.get("order_qty_shares"), 0) > 0 and str(latest.get("order_mode") or "") == "LIVE_ORDER":
            try:
                ed = date.fromisoformat(str(latest.get("eval_date") or "")[:10])
                if ed.weekday() < 5:
                    weekday_flags[weekday_keys[ed.weekday()]] = True
                    bought_this_week = True
            except ValueError:
                pass

    close_px = _safe_float(latest.get("close_price"), 0.0)
    ma17 = _safe_float(latest.get("ma17"), 0.0)
    ma45 = _safe_float(latest.get("ma45"), 0.0)
    rsi14 = _safe_float(latest.get("rsi_14"), 50.0)
    kd_k = _safe_float(latest.get("kd_k"), 50.0)
    bias17 = _safe_float(latest.get("bias_ma17_pct"), 0.0)
    dip_score = _safe_int(latest.get("dip_score"), 0)
    reason = str(latest.get("trigger_reason") or "尚無本週評估紀錄")
    sug_shares = _safe_int(latest.get("order_qty_shares"), 0)
    mult = round(0.6 + (dip_score / 100.0) * 0.9, 2) if dip_score > 0 else 1.0

    # 近 12 週微笑曲線資料點（累計 0050 買入均價 vs 當週 0050 收盤價）
    buy_0050 = sorted(
        [
            r
            for r in statement_rows
            if _clean_code(r.get("stock_no")) == "0050" and str(r.get("action") or "").upper() == "BUY"
        ],
        key=lambda r: str(r.get("trade_date") or ""),
    )
    by_week_latest: dict[str, Mapping[str, Any]] = {}
    for r in sorted_dca:
        wk = str(r.get("iso_year_week") or "").strip()
        if wk:
            by_week_latest[wk] = r

    recent_12w: list[dict[str, Any]] = []
    for wk in sorted(by_week_latest.keys())[-12:]:
        row = by_week_latest[wk]
        ed = str(row.get("eval_date") or "")[:10]
        cum_qty = 0
        cum_cost = 0.0
        bought_in_wk = False
        for b in buy_0050:
            bd = str(b.get("trade_date") or "")[:10]
            if bd and ed and bd <= ed:
                q = _safe_int(b.get("filled_qty"), 0)
                p = _safe_float(b.get("filled_price"), 0.0)
                cum_qty += q
                cum_cost += q * p
                try:
                    b_iso = date.fromisoformat(bd).isocalendar()
                    if f"{b_iso.year:04d}-W{b_iso.week:02d}" == wk:
                        bought_in_wk = True
                except ValueError:
                    pass
        avg_cost = round(cum_cost / cum_qty, 2) if cum_qty > 0 else None
        recent_12w.append(
            {
                "week": wk,
                "date": ed,
                "price": _safe_float(row.get("close_price"), 0.0),
                "avg_cost": avg_cost,
                "bought": bought_in_wk or str(row.get("order_mode") or "") == "LIVE_ORDER",
                "dip_score": _safe_int(row.get("dip_score"), 0),
            }
        )

    week_status = "BOUGHT" if bought_this_week else ("TRIGGERED" if sug_shares > 0 else "WATCHING")
    return {
        "iso_year_week": cur_week,
        "eval_date": str(latest.get("eval_date") or "")[:10],
        "week_status": week_status,
        "already_bought_this_week": bought_this_week,
        "weekday_flags": weekday_flags,
        "close_price": close_px,
        "ma17": ma17,
        "ma45": ma45,
        "rsi_14": rsi14,
        "kd_k": kd_k,
        "bias_ma17_pct": bias17,
        "dip_score": dip_score,
        "suggested_multiplier": mult,
        "suggested_shares": sug_shares,
        "order_price": _safe_float(latest.get("order_price"), close_px),
        "trigger_reason": reason,
        "signals": {
            "ma17_neg_bias": bias17 <= -1.5,
            "ma45_support": (ma45 > 0 and close_px > 0 and close_px <= ma45 * 1.015),
            "rsi_kd_oversold": (rsi14 <= 40.0 or kd_k <= 30.0),
            "drop_10d": ("10日高點" in reason or "回落" in reason),
            "margin_washout": ("融資" in reason or "斷頭" in reason),
        },
        "recent_12w": recent_12w,
    }


def _build_exposure_and_opportunities(
    *,
    holdings_list: Sequence[Mapping[str, Any]],
    picks_bundle: Mapping[str, Any] | None,
    prices_df: pd.DataFrame | None,
    taxonomy_map: Mapping[str, Mapping[str, Any]],
    public_portal_base: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """計算庫存次產業集中度、60 日相關性矩陣，以及昨晚嚴選機會池之汰弱換股配對。"""
    from taishin_portfolio_service import (
        EXPOSURE_CORR_WARN,
        EXPOSURE_HIGH_CORR,
        EXPOSURE_SWAP_ONLY,
        _is_ai_selected_rebound_row,
        compute_return_correlation,
        evaluate_candidate_exposure,
    )

    # 1. 次產業集中度
    sector_map: dict[str, dict[str, Any]] = {}
    holding_inputs: list[dict[str, Any]] = []
    holding_name_by_id: dict[str, str] = {}
    holding_ret_by_id: dict[str, float] = {}
    for h in holdings_list:
        sid = str(h.get("stock_id") or "")
        sname = str(h.get("stock_name") or sid)
        sec = str(h.get("sub_industry") or "未分類")
        wt = _safe_float(h.get("weight_pct"), 0.0)
        ret = _safe_float(h.get("unrealized_pct"), 0.0)
        holding_name_by_id[sid] = sname
        holding_ret_by_id[sid] = ret
        holding_inputs.append({"stock_id": sid, "sector": sec, "weight_pct": wt, "return_pct": ret})
        entry = sector_map.setdefault(sec, {"sector": sec, "weight_pct": 0.0, "stocks": []})
        entry["weight_pct"] = round(entry["weight_pct"] + wt, 2)
        entry["stocks"].append(f"{sid} {sname}")

    sector_weights = sorted(sector_map.values(), key=lambda x: x["weight_pct"], reverse=True)

    # 2. 準備本地日線收盤價供 60 日相關係數計算
    prices_by_sid: dict[str, pd.DataFrame] = {}
    if prices_df is not None and not prices_df.empty and "stock_id" in prices_df.columns:
        df_p = prices_df.copy()
        df_p["stock_id"] = df_p["stock_id"].astype(str).str.strip()
        for sid in holding_name_by_id:
            sub = df_p.loc[df_p["stock_id"] == sid, ["date", "close"]]
            if not sub.empty:
                prices_by_sid[sid] = sub

    high_corr_pairs: list[dict[str, Any]] = []
    h_ids = list(holding_name_by_id.keys())
    for i in range(len(h_ids)):
        for j in range(i + 1, len(h_ids)):
            s1, s2 = h_ids[i], h_ids[j]
            if s1 not in prices_by_sid or s2 not in prices_by_sid:
                continue
            corr = compute_return_correlation(prices_by_sid[s1], prices_by_sid[s2])
            if corr is not None and corr >= EXPOSURE_CORR_WARN:
                high_corr_pairs.append(
                    {
                        "stock_a": s1,
                        "name_a": holding_name_by_id[s1],
                        "stock_b": s2,
                        "name_b": holding_name_by_id[s2],
                        "corr_60d": round(corr, 3),
                    }
                )
    high_corr_pairs.sort(key=lambda x: x["corr_60d"], reverse=True)

    exposure_matrix = {
        "sector_weights": sector_weights,
        "high_corr_pairs": high_corr_pairs,
        "max_sector_weight_pct": sector_weights[0]["weight_pct"] if sector_weights else 0.0,
        "max_sector_name": sector_weights[0]["sector"] if sector_weights else "",
    }

    # 3. 昨晚 AI 嚴選潛力新星（雙池）＋ 汰弱換股配對
    opportunity_pool: list[dict[str, Any]] = []
    if not picks_bundle:
        return exposure_matrix, opportunity_pool

    seen_opp: set[str] = set()
    raw_candidates: list[tuple[str, Mapping[str, Any]]] = []

    ai_picks = list(picks_bundle.get("ai_daily_picks") or [])
    valid_ai_dates = [str(r.get("scan_date") or "").strip() for r in ai_picks if str(r.get("scan_date") or "").strip()]
    if valid_ai_dates:
        latest_ai_dt = max(valid_ai_dates)
        for r in ai_picks:
            if (
                str(r.get("scan_date") or "").strip() == latest_ai_dt
                and str(r.get("category") or r.get("pick_category") or "").strip().upper() == "TOP_PICK"
            ):
                raw_candidates.append(("alignment_2130", r))

    reb_picks = list(picks_bundle.get("rebound_watchlist") or [])
    valid_reb_dates = [str(r.get("scan_date") or "").strip() for r in reb_picks if str(r.get("scan_date") or "").strip()]
    if valid_reb_dates:
        latest_reb_dt = max(valid_reb_dates)
        for r in reb_picks:
            if str(r.get("scan_date") or "").strip() == latest_reb_dt and _is_ai_selected_rebound_row(r):
                raw_candidates.append(("rebound_2200", r))

    df_p_cached = None
    if prices_df is not None and not prices_df.empty and "stock_id" in prices_df.columns:
        df_p_cached = prices_df.copy()
        df_p_cached["stock_id"] = df_p_cached["stock_id"].astype(str).str.strip()

    for strat_id, row in raw_candidates:
        sid = _clean_code(row.get("stock_id") or row.get("stock_no") or "")
        if not sid or sid in seen_opp:
            continue
        seen_opp.add(sid)
        tax = taxonomy_map.get(sid) or {}
        sname = str(row.get("stock_name") or tax.get("stock_name") or sid).strip()
        sub_ind = str(
            row.get("sub_industry")
            or tax.get("sub_industry")
            or row.get("industry_category")
            or "未分類"
        ).strip()
        close_px = _safe_float_opt(row.get("close_price") or row.get("close"))
        t1 = _safe_float_opt(row.get("Tier1_Entry_Price")) or _extract_plan_price(row.get("Tier1_Entry_Plan")) or _extract_plan_price(row.get("Ideal_Buy_Or_Ref"))
        t2 = _safe_float_opt(row.get("Tier2_Entry_Price")) or _extract_plan_price(row.get("Tier2_Entry_Plan"))
        t3 = _safe_float_opt(row.get("Tier3_Entry_Price")) or _extract_plan_price(row.get("Tier3_Entry_Plan"))
        tp1 = _safe_float_opt(row.get("TP1_Target_Or_POC")) or _extract_plan_price(row.get("target_price_1")) or _safe_float_opt(row.get("target_ma17"))
        sl = _safe_float_opt(row.get("Hard_Stop_Loss")) or _extract_plan_price(row.get("stop_loss_price"))
        alloc_w = _safe_float_opt(row.get("Allocation_Weight_Pct"))

        cand_prices = pd.DataFrame(columns=["date", "close"])
        if df_p_cached is not None:
            sub = df_p_cached.loc[df_p_cached["stock_id"] == sid, ["date", "close"]]
            if not sub.empty:
                cand_prices = sub

        exp_res = evaluate_candidate_exposure(
            sid,
            sub_ind,
            holding_inputs,
            candidate_prices=cand_prices,
            holding_prices=prices_by_sid,
        )
        weak_obj = None
        if exp_res.weakest_holding_to_swap:
            w_id = exp_res.weakest_holding_to_swap
            weak_obj = {
                "stock_id": w_id,
                "stock_name": holding_name_by_id.get(w_id, w_id),
                "return_pct": holding_ret_by_id.get(w_id, 0.0),
            }

        if sid in holding_name_by_id:
            exp_badge = "📌 已在庫存中（可評估既有波段加碼）"
        elif exp_res.exposure_status == EXPOSURE_SWAP_ONLY and weak_obj:
            exp_badge = f"🔄 撞車建議替換弱勢股 {weak_obj['stock_id']} {weak_obj['stock_name']} ({weak_obj['return_pct']:+.1f}%)"
        elif exp_res.exposure_status == EXPOSURE_SWAP_ONLY:
            exp_badge = "🔄 同產業/高連動已滿額（僅限汰弱換股）"
        elif exp_res.exposure_status == EXPOSURE_HIGH_CORR and exp_res.max_corr_holding_id:
            m_id = exp_res.max_corr_holding_id
            exp_badge = f"🟡 高連動注意：{m_id} {holding_name_by_id.get(m_id, '')} (Corr={exp_res.max_corr_value:.2f})"
        else:
            exp_badge = "🟢 可直接新開倉"

        opportunity_pool.append(
            {
                "stock_id": sid,
                "stock_name": sname,
                "strategy": strat_id,
                "scan_date": str(row.get("scan_date") or "")[:10],
                "rank": _safe_int(row.get("rank") or row.get("source_rank"), 0) or None,
                "sub_industry": sub_ind,
                "close_price": close_px,
                "allocation_weight_pct": alloc_w,
                "tier1_price": round_tw_tick(t1, stock_id=sid) if t1 else None,
                "tier2_price": round_tw_tick(t2, stock_id=sid) if t2 else None,
                "tier3_price": round_tw_tick(t3, stock_id=sid) if t3 else None,
                "tp1_price": round_tw_tick(tp1, stock_id=sid) if tp1 else None,
                "stop_loss_price": round_tw_tick(sl, stock_id=sid) if sl else None,
                "audit_grade": str(row.get("Audit_Grade") or ""),
                "already_held": sid in holding_name_by_id,
                "war_room_url": f"../charts/{sid}_fund.html",
                "wantgoo_url": wantgoo_url(sid),
                "exposure_check": {
                    "status": exp_res.exposure_status,
                    "badge": exp_badge,
                    "same_sector_holdings": exp_res.same_sector_holdings,
                    "max_corr_holding_id": exp_res.max_corr_holding_id,
                    "max_corr_value": exp_res.max_corr_value,
                    "weakest_holding_to_swap": weak_obj,
                },
            }
        )

    return exposure_matrix, opportunity_pool


def _build_history_pnl_bundle(
    *,
    memory_rows: Sequence[Mapping[str, Any]],
    settlement_rows: Sequence[Mapping[str, Any]],
    statement_rows: Sequence[Mapping[str, Any]],
    discipline_stats: Mapping[str, Any],
    audit_db_path: Path | None = None,
) -> dict[str, Any]:
    """組裝冷資料包 `history_pnl.json`：月度已實現損益、已平倉波段帳本、紀律統計與系統回測對照。"""
    monthly_pnl: dict[str, int] = defaultdict(int)
    for row in settlement_rows:
        rtype = str(row.get("record_type") or "").strip().upper()
        if rtype != "REALIZED_PNL":
            continue
        rdate = str(row.get("record_date") or "").strip()
        if len(rdate) >= 7:
            ym = rdate[:7]
            monthly_pnl[ym] += _safe_int(row.get("realized_pnl"), 0)

    monthly_series: list[dict[str, Any]] = []
    cum = 0
    for ym in sorted(monthly_pnl.keys()):
        val = monthly_pnl[ym]
        cum += val
        monthly_series.append({"month": ym, "realized_pnl": val, "cumulative_pnl": cum})

    closed_waves: list[dict[str, Any]] = []
    for mem in memory_rows:
        if str(mem.get("status") or "").upper() != "CLOSED":
            continue
        opened = str(mem.get("opened_date") or "")[:10]
        closed = str(mem.get("closed_date") or "")[:10]
        holding_days = 0
        try:
            if opened and closed:
                holding_days = max(1, (date.fromisoformat(closed) - date.fromisoformat(opened)).days)
        except ValueError:
            holding_days = 0
        pnl = _safe_int(mem.get("realized_pnl"), 0)
        init_qty = _safe_int(mem.get("initial_qty"), 0)
        avg_cost = _safe_float(mem.get("avg_cost_price"), 0.0)
        cost_basis = init_qty * avg_cost
        ret_pct = round((pnl / cost_basis) * 100.0, 2) if cost_basis > 0 else 0.0
        style_b_used = (
            "TP1" in str(mem.get("last_ai_summary") or "")
            or "賣半" in str(mem.get("last_ai_summary") or "")
            or "分批停利" in str(mem.get("last_ai_action") or "")
        )
        closed_waves.append(
            {
                "position_id": str(mem.get("position_id") or ""),
                "stock_id": _clean_code(mem.get("stock_no")),
                "stock_name": str(mem.get("stock_name") or "").strip(),
                "source_strategy": str(mem.get("entry_source") or "自主"),
                "opened_date": opened,
                "closed_date": closed,
                "holding_days": holding_days,
                "style_b_partial_taken": style_b_used,
                "realized_pnl": pnl,
                "return_pct": ret_pct,
                "entry_thesis": str(mem.get("entry_thesis") or ""),
                "audit_grade": str(mem.get("Audit_Grade") or ""),
            }
        )
    closed_waves.sort(key=lambda x: x.get("closed_date") or "", reverse=True)

    # 計算實盤波段 Profit Factor
    gross_win = sum(w["realized_pnl"] for w in closed_waves if w["realized_pnl"] > 0)
    gross_loss = abs(sum(w["realized_pnl"] for w in closed_waves if w["realized_pnl"] < 0))
    live_pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (9.99 if gross_win > 0 else 0.0)

    # 串接本地 signal_audit.db 取得系統 TOP_PICK 理論勝率對照
    system_benchmark = {
        "available": False,
        "settled_trades": 0,
        "win_rate_pct": 0.0,
        "profit_factor": 0.0,
        "avg_return_pct": 0.0,
    }
    try:
        from signal_audit_service import CATEGORY_TOP_PICK, DEFAULT_AUDIT_DB_PATH, OUTCOME_PENDING, OUTCOME_UNFILLED, SignalAuditStore

        db_p = audit_db_path or DEFAULT_AUDIT_DB_PATH
        if Path(db_p).is_file():
            store = SignalAuditStore(db_p)
            rows = store.fetch_all_rows()
            settled = [
                r
                for r in rows
                if str(r.get("pick_category") or "").upper() == CATEGORY_TOP_PICK
                and str(r.get("outcome_status_10d") or "") not in ("", OUTCOME_PENDING, OUTCOME_UNFILLED)
                and r.get("net_return_10d_pct") is not None
            ]
            if settled:
                rets = [float(r["net_return_10d_pct"]) for r in settled]
                wins = [x for x in rets if x > 0]
                losses = [abs(x) for x in rets if x < 0]
                pf = round(sum(wins) / sum(losses), 2) if sum(losses) > 0 else (9.99 if wins else 0.0)
                system_benchmark = {
                    "available": True,
                    "settled_trades": len(settled),
                    "win_rate_pct": round(len(wins) / len(settled) * 100.0, 1),
                    "profit_factor": pf,
                    "avg_return_pct": round(sum(rets) / len(rets), 2),
                }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load signal_audit.db benchmark: %s", exc)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(TAIPEI_TIMEZONE).isoformat(timespec="seconds"),
        "monthly_pnl": monthly_series,
        "closed_waves": closed_waves,
        "live_vs_system": {
            "live_closed_count": len(closed_waves),
            "live_win_rate_pct": discipline_stats.get("closed_win_rate_pct", 0.0),
            "live_profit_factor": live_pf,
            "system_top_pick": system_benchmark,
        },
        "discipline_stats": dict(discipline_stats),
    }


def _load_taxonomy_cache(reports_dir: Path) -> dict[str, dict[str, Any]]:
    path = Path(reports_dir) / ".state" / "stock_taxonomy.json"
    raw = _read_json(path)
    return raw if isinstance(raw, dict) else {}


def _load_latest_prices_df(reports_dir: Path) -> tuple[pd.DataFrame | None, str]:
    daily_dir = Path(reports_dir) / "daily"
    if not daily_dir.is_dir():
        return None, ""
    files = sorted(daily_dir.glob("*_prices.csv"))
    if not files:
        return None, ""
    latest_file = files[-1]
    as_of_str = latest_file.stem.split("_")[0]
    try:
        df = pd.read_csv(latest_file, dtype={"stock_id": str})
        return df, as_of_str
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read latest prices CSV %s: %s", latest_file, exc)
        return None, as_of_str


def build_my_stock_bundles(
    *,
    phase: str,
    trade_date: date,
    holdings_rows: Sequence[Mapping[str, Any]],
    memory_rows: Sequence[Mapping[str, Any]],
    statement_rows: Sequence[Mapping[str, Any]],
    settlement_rows: Sequence[Mapping[str, Any]],
    dca_rows: Sequence[Mapping[str, Any]],
    tech_map: Mapping[str, Mapping[str, Any]],
    market_regime: Mapping[str, Any],
    macro_context: Mapping[str, Any] | None = None,
    picks_bundle: Mapping[str, Any] | None = None,
    deep_fund_map: Mapping[str, Mapping[str, Any]] | None = None,
    decisions_by_sno: Mapping[str, Mapping[str, Any]] | None = None,
    headline_summary: str = "",
    full_markdown: str = "",
    reports_dir: Path | None = None,
    audit_db_path: Path | None = None,
    retention_trading_days: int = DEFAULT_RETENTION_TRADING_DAYS,
    public_portal_base_url: str | None = None,
) -> dict[str, Any]:
    """組裝完整 `latest`、`daily/{date}`、`history_pnl` 與 `manifest` 明文 dict（加密前）。"""
    from taishin_portfolio_service import (
        _aggregate_holdings_by_stock,
        _resolve_wave_buy_log,
        build_tracked_position_gatekeeper_signals,
        compute_holding_waves_from_statements,
        find_latest_pick_reselection,
    )

    root = Path(reports_dir or "finmind_reports")
    now_iso = datetime.now(TAIPEI_TIMEZONE).isoformat(timespec="seconds")
    dt_str = trade_date.isoformat()
    portal_base = public_portal_base_url or get_github_pages_base_url() or ""

    # 合併本地晨報快取（當 phase 為 DCA_0050_EVAL / MEMORY_SYNC / MANUAL_PUBLISH 時自動保留當日晨報全文）
    morning_state = load_morning_brief_state(root, trade_date=trade_date)
    merged_headline = headline_summary or str(morning_state.get("headline_summary") or "")
    merged_markdown = full_markdown or str(morning_state.get("full_markdown") or "")
    merged_decisions: dict[str, dict[str, Any]] = {}
    for k, v in (morning_state.get("decisions_by_sno") or {}).items():
        if isinstance(v, Mapping):
            merged_decisions[_clean_code(k)] = dict(v)
    for k, v in (decisions_by_sno or {}).items():
        if isinstance(v, Mapping):
            merged_decisions[_clean_code(k)] = dict(v)

    resolved_macro = macro_context or (morning_state.get("macro_context") if isinstance(morning_state.get("macro_context"), Mapping) else {})

    taxonomy_map = _load_taxonomy_cache(root)
    prices_df, price_as_of_date = _load_latest_prices_df(root)
    if not price_as_of_date:
        price_as_of_date = str(market_regime.get("date") or dt_str)[:10]

    # 1. 整理未平倉記憶卡與對帳單波段
    active_mem_by_sno: dict[str, Mapping[str, Any]] = {}
    for m in memory_rows:
        if str(m.get("status") or "").upper() in ("OPEN", "TP1_PARTIAL_TAKEN"):
            sno = _clean_code(m.get("stock_no"))
            if sno:
                active_mem_by_sno[sno] = m

    agg_holdings = _aggregate_holdings_by_stock([dict(r) for r in holdings_rows])
    waves = compute_holding_waves_from_statements([dict(r) for r in statement_rows])
    discipline_agg = _compute_discipline_aggregate(memory_rows, statement_rows)

    # 2. 組裝逐檔持股資料
    holdings_out: list[dict[str, Any]] = []
    pool_totals: dict[str, dict[str, float]] = {
        "all": {"market_value": 0.0, "cost_basis": 0.0},
        "swing_pool": {"market_value": 0.0, "cost_basis": 0.0},
        "long_term_pool": {"market_value": 0.0, "cost_basis": 0.0},
    }
    buy_ticket_total = 0
    sell_ticket_total = 0

    for sno, hold in sorted(agg_holdings.items(), key=lambda kv: -_safe_float(kv[1].get("weight_pct"), 0.0)):
        qty = _safe_int(hold.get("qty"), 0)
        if qty <= 0:
            continue
        mem = active_mem_by_sno.get(sno)
        pool_type = "SWING" if mem is not None else "LONG_TERM"
        t_info = tech_map.get(sno) or {}
        deep = (deep_fund_map or {}).get(sno) or {}
        tax = deep.get("taxonomy") or taxonomy_map.get(sno) or {}
        sub_industry = str(
            tax.get("sub_industry") or tax.get("official_industry") or ("指數ETF" if sno.startswith("00") else "未分類")
        ).strip()

        avg_cost = round(_safe_float(hold.get("avg_cost_price"), 0.0), 2)
        latest_close = round(_safe_float(hold.get("current_price"), 0.0) or _safe_float(t_info.get("close"), 0.0), 2)
        mv = _safe_int(hold.get("market_value"), int(round(qty * latest_close)))
        cost = _safe_int(hold.get("total_cost"), int(round(qty * avg_cost)))
        upnl = _safe_int(hold.get("unrealized_pnl"), mv - cost)
        upct = round(_safe_float(hold.get("return_pct"), (upnl / cost * 100.0) if cost > 0 else 0.0), 2)
        wt = round(_safe_float(hold.get("weight_pct"), 0.0), 2)

        pool_totals["all"]["market_value"] += mv
        pool_totals["all"]["cost_basis"] += cost
        pool_key = "swing_pool" if pool_type == "SWING" else "long_term_pool"
        pool_totals[pool_key]["market_value"] += mv
        pool_totals[pool_key]["cost_basis"] += cost

        dec = merged_decisions.get(sno) or {}
        gk = build_tracked_position_gatekeeper_signals(dict(mem), hold, tech_info=t_info) if mem else {}
        action_tag = (
            str(dec.get("ai_action_tag") or (mem.get("last_ai_action") if mem else "") or hold.get("ai_action_tag") or "")
            or ("🟢 順勢續抱" if pool_type == "SWING" else "📦 帳面損益觀察")
        )

        # 三階子彈藍圖 & 波段流水帳
        wave_for_sno = waves.get(sno) or {}
        mem_payload = None
        tier_blueprint = None
        discipline_check = {"status": "HOLDING", "badge": "📦 長投觀察", "unexecuted_alert": None}
        reselection_obj = {"is_reselected": False, "badge": "", "summary_line": ""}

        if mem is not None:
            resolved_wave_log = _resolve_wave_buy_log(
                mem.get("wave_buy_log"),
                wave_for_sno,
                opened_date=str(mem.get("opened_date") or ""),
                initial_qty=_safe_int(mem.get("initial_qty"), qty),
                current_qty=qty,
                avg_cost_price=avg_cost,
            )
            mem_with_log = {**mem, "wave_buy_log": resolved_wave_log}
            tier_blueprint = _build_tier_blueprint(mem_with_log, wave_state=wave_for_sno)
            trailing_stop = round(
                _safe_float(dec.get("trailing_stop"), 0.0)
                or _safe_float(mem.get("trailing_stop"), 0.0)
                or _safe_float(gk.get("ratchet_stop_floor"), 0.0),
                2,
            )
            init_tp1 = round(_safe_float(mem.get("initial_tp1"), 0.0), 2)
            init_tp2 = round(_safe_float(mem.get("initial_tp2"), 0.0), 2)
            init_sl = round(_safe_float(mem.get("initial_sl"), 0.0), 2)
            timeline = parse_recent_battle_log(
                mem.get("recent_battle_log"),
                stock_id=sno,
                trailing_stop=trailing_stop,
                initial_tp1=init_tp1,
                status=str(mem.get("status") or "OPEN").upper(),
                statement_rows=statement_rows,
            )
            discipline_check = evaluate_position_discipline(
                {**mem, "trailing_stop": trailing_stop},
                statement_rows,
                t_info,
                latest_close=latest_close,
                timeline=timeline,
            )
            # 昨晚再度入榜偵測
            resel = dec.get("reselection") or find_latest_pick_reselection(
                sno,
                str(mem.get("opened_date") or ""),
                picks_bundle,
                source_scan_date=str(mem.get("source_scan_date") or ""),
            )
            if isinstance(resel, Mapping) and resel.get("summary_line"):
                reselection_obj = {
                    "is_reselected": True,
                    "badge": "🔥 昨晚再度入榜",
                    "scan_date": str(resel.get("scan_date") or ""),
                    "strategy": str(resel.get("source") or ""),
                    "rank": resel.get("rank"),
                    "summary_line": str(resel.get("summary_line") or ""),
                }

            opened_str = str(mem.get("opened_date") or "")[:10]
            holding_days = 0
            try:
                if opened_str:
                    holding_days = max(1, (trade_date - date.fromisoformat(opened_str)).days)
            except ValueError:
                holding_days = 0

            mem_payload = {
                "position_id": str(mem.get("position_id") or f"{opened_str}_{sno}"),
                "status": str(mem.get("status") or "OPEN").upper(),
                "source_strategy": str(mem.get("entry_source") or ""),
                "source_scan_date": str(mem.get("source_scan_date") or ""),
                "source_rank": mem.get("source_rank"),
                "entry_date": opened_str,
                "holding_days": holding_days,
                "initial_tp1": init_tp1,
                "initial_tp2": init_tp2,
                "initial_sl": init_sl,
                "trailing_stop": trailing_stop,
                "left_side_structural_floor": round(_safe_float(gk.get("left_side_structural_floor"), 0.0), 2) or None,
                "rebound_resistance_target": round(_safe_float(gk.get("rebound_resistance_target"), 0.0), 2) or None,
                "entry_thesis": str(mem.get("entry_thesis") or ""),
                "invalidation_rule": str(mem.get("invalidation_rule") or ""),
                "thesis_status": "BROKEN" if action_tag in ("🚨 觸及停損", "⚠️ 邏輯失效減碼") else "INTACT",
                "tier_blueprint": tier_blueprint,
                "recent_battle_log": timeline,
                "audit": {
                    "grade": str(mem.get("Audit_Grade") or ""),
                    "tactic": str(mem.get("Execution_Tactic") or ""),
                    "deterministic_rr": _safe_float_opt(mem.get("Deterministic_RR")),
                    "bear_case_warning": str(mem.get("Bear_Case_Warning") or ""),
                    "invalidation_triggers": str(mem.get("Invalidation_Triggers") or ""),
                },
            }

        # 雙向距離
        t_stop = (mem_payload or {}).get("trailing_stop") or 0.0
        t_tp1 = (mem_payload or {}).get("initial_tp1") or 0.0
        t_tp2 = (mem_payload or {}).get("initial_tp2") or 0.0
        to_stop_pct = round(((t_stop - latest_close) / latest_close) * 100.0, 2) if (t_stop > 0 and latest_close > 0) else None
        to_tp1_pct = round(((t_tp1 - latest_close) / latest_close) * 100.0, 2) if (t_tp1 > 0 and latest_close > 0) else None
        to_tp2_pct = round(((t_tp2 - latest_close) / latest_close) * 100.0, 2) if (t_tp2 > 0 and latest_close > 0) else None

        priority_tier, order_ticket = _classify_priority_and_ticket(
            stock_id=sno,
            pool_type=pool_type,
            qty=qty,
            latest_close=latest_close,
            action_tag=action_tag,
            mem=mem,
            gk=gk,
            discipline_check=discipline_check,
            tier_blueprint=tier_blueprint,
        )
        if order_ticket:
            amt = _safe_int(order_ticket.get("est_gross_amount"), 0)
            if order_ticket["side"] == "BUY":
                buy_ticket_total += amt
            else:
                sell_ticket_total += amt

        # 技術、籌碼與 v6.2.3 下檔大量支撐微結構
        strat_pkg = deep.get("strategic_tech_pkg") if isinstance(deep.get("strategic_tech_pkg"), Mapping) else {}
        rs_beta = strat_pkg.get("rs_beta") if isinstance(strat_pkg.get("rs_beta"), Mapping) else {}
        beta_60d = _safe_float_opt(rs_beta.get("beta_60d")) or 1.0
        chip = deep.get("chip") if isinstance(deep.get("chip"), Mapping) else {}
        p2 = deep.get("phase2_features") if isinstance(deep.get("phase2_features"), Mapping) else {}
        cf = deep.get("cashflow") if isinstance(deep.get("cashflow"), Mapping) else {}
        val = deep.get("valuation") if isinstance(deep.get("valuation"), Mapping) else {}

        microstructure = {
            "ma": {
                "ma3": _safe_float_opt(t_info.get("ma3")),
                "ma17": _safe_float_opt(hold.get("ma17") or t_info.get("ma17")),
                "ma45": _safe_float_opt(hold.get("ma45") or t_info.get("ma45")),
                "ma117": _safe_float_opt(hold.get("ma117") or t_info.get("ma117")),
                "ma189": _safe_float_opt(hold.get("ma189") or t_info.get("ma189")),
            },
            "bias_ma17_pct": _safe_float_opt(gk.get("bias_ma17_pct")),
            "bias_ma189_pct": _safe_float_opt(gk.get("bias_ma189_pct")),
            "rsi14": _safe_float_opt(gk.get("rsi_14") or t_info.get("rsi_14")),
            "atr14": _safe_float_opt(gk.get("atr14") or t_info.get("atr14")),
            "daily_return_pr": _safe_float_opt(gk.get("daily_return_pr") or t_info.get("daily_return_pr")),
            "vol_poc_60d": _safe_float_opt(strat_pkg.get("poc_60d") or gk.get("vol_poc_60d") or t_info.get("vol_poc_60d")),
            "val_60d": _safe_float_opt(strat_pkg.get("val_60d")),
            "vah_60d": _safe_float_opt(strat_pkg.get("vah_60d")),
            "lower_support_nodes": list(strat_pkg.get("lower_support_nodes") or [])[:3],
            "key_bullish_bars": list(strat_pkg.get("key_bullish_bars") or [])[:2],
            "trust_vwap_20d": _safe_float_opt(strat_pkg.get("trust_vwap_20d")),
            "trust_vwap_60d": _safe_float_opt(strat_pkg.get("trust_vwap_60d")),
            "foreign_vwap_20d": _safe_float_opt(strat_pkg.get("foreign_vwap_20d")),
            "foreign_vwap_60d": _safe_float_opt(strat_pkg.get("foreign_vwap_60d")),
            "rs_beta_summary": str(rs_beta.get("summary") or ""),
            "daily_table_5d_markdown": str(strat_pkg.get("daily_table_markdown") or ""),
            "sector_breadth_pct": _safe_float_opt(t_info.get("sector_breadth_pct")),
            "disposition_tag": str(t_info.get("disposition_tag") or "NORMAL"),
            "foreign_5d_lots": _safe_float_opt(chip.get("foreign_5d_lots")),
            "trust_5d_lots": _safe_float_opt(chip.get("trust_5d_lots")),
            "margin_chg_5d_lots": _safe_float_opt(chip.get("margin_chg_5d_lots")),
            "est_margin_maint_pct": str(chip.get("est_margin_maint_pct") or ""),
            "chip_quadrant_tag": str(chip.get("chip_quadrant_tag") or ""),
            "tdcc_summary": str(chip.get("tdcc_summary") or ""),
            "recent_news": list(deep.get("recent_news") or [])[:3],
            "fundamental_brief": {
                "forward_val_line": str(p2.get("forward_val_line") or ""),
                "implied_forward_eps": _safe_float_opt(p2.get("implied_forward_eps")),
                "margin_trend_line": str(p2.get("margin_trend_line") or ""),
                "pe": _safe_float_opt(val.get("pe")),
                "ocf_yi": _safe_float_opt(cf.get("ocf_yi")),
                "fcf_yi": _safe_float_opt(cf.get("fcf_yi")),
            },
        }

        # 本波段交易流水帳
        opened_cutoff = (mem_payload or {}).get("entry_date") or ""
        wave_txs = [
            {
                "trade_date": str(r.get("trade_date") or "")[:10],
                "action": str(r.get("action") or "").upper(),
                "order_type": str(r.get("order_type") or ""),
                "qty": _safe_int(r.get("filled_qty"), 0),
                "price": round(_safe_float(r.get("filled_price"), 0.0), 2),
                "fee": _safe_int(r.get("fee"), 0),
                "tax": _safe_int(r.get("tax"), 0),
                "net_amount": _safe_int(r.get("net_amount"), 0),
            }
            for r in statement_rows
            if _clean_code(r.get("stock_no")) == sno
            and (not opened_cutoff or str(r.get("trade_date") or "")[:10] >= opened_cutoff)
        ]
        wave_txs.sort(key=lambda x: x["trade_date"], reverse=True)

        ai_note_parts = [
            str(dec.get("order_instruction") or "").strip(),
            str(dec.get("pm_rationale") or "").strip(),
        ]
        ai_morning_note = "｜".join(p for p in ai_note_parts if p) or str(
            (mem.get("last_ai_summary") if mem else "") or hold.get("ai_advice_summary") or ""
        )

        holdings_out.append(
            {
                "stock_id": sno,
                "stock_name": str(hold.get("stock_name") or (mem.get("stock_name") if mem else "") or sno).strip(),
                "pool_type": pool_type,
                "priority_tier": priority_tier,
                "action_tag": action_tag,
                "sub_industry": sub_industry,
                "qty": qty,
                "avg_cost": avg_cost,
                "latest_close": latest_close,
                "beta_60d": round(beta_60d, 2),
                "weight_pct": wt,
                "market_value": mv,
                "cost_basis": cost,
                "unrealized_pnl": upnl,
                "unrealized_pct": upct,
                "distances": {
                    "to_stop_pct": to_stop_pct,
                    "to_tp1_pct": to_tp1_pct,
                    "to_tp2_pct": to_tp2_pct,
                },
                "reselection": reselection_obj,
                "discipline_check": discipline_check,
                "order_ticket": order_ticket,
                "memory": mem_payload,
                "microstructure": microstructure,
                "ai_morning_note": ai_morning_note,
                "wave_transactions": wave_txs[:30],
                "links": {
                    "war_room": f"../charts/{sno}_fund.html",
                    "wantgoo": wantgoo_url(sno),
                },
            }
        )

    # 依急迫度與權重排序
    tier_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    holdings_out.sort(key=lambda h: (tier_order.get(h["priority_tier"], 9), -_safe_float(h["weight_pct"])))

    # 3. 彙總 KPI
    ytd_prefix = f"{trade_date.year:04d}-"
    ytd_realized_pnl = sum(
        _safe_int(r.get("realized_pnl"), 0)
        for r in settlement_rows
        if str(r.get("record_type") or "").upper() == "REALIZED_PNL"
        and str(r.get("record_date") or "").startswith(ytd_prefix)
    )

    def _finalize_pool(tot: Mapping[str, float]) -> dict[str, Any]:
        mv = int(round(tot["market_value"]))
        cb = int(round(tot["cost_basis"]))
        pnl = mv - cb
        pct = round((pnl / cb) * 100.0, 2) if cb > 0 else 0.0
        return {"market_value": mv, "cost_basis": cb, "unrealized_pnl": pnl, "unrealized_pct": pct}

    settlement_section = compute_settlement_slots(
        trade_date=trade_date,
        statement_rows=statement_rows,
        settlement_rows=settlement_rows,
    )
    exposure_matrix, opportunity_pool = _build_exposure_and_opportunities(
        holdings_list=holdings_out,
        picks_bundle=picks_bundle,
        prices_df=prices_df,
        taxonomy_map=taxonomy_map,
        public_portal_base=portal_base,
    )
    dca_section = _build_dca_0050_section(dca_rows, statement_rows, trade_date=trade_date)
    macro_section = _build_macro_section(resolved_macro, market_regime)

    latest_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso,
        "phase": phase,
        "trade_date": dt_str,
        "price_as_of_date": price_as_of_date,
        "public_portal_base_url": portal_base,
        "macro": macro_section,
        "settlement": settlement_section,
        "summary": {
            "all": _finalize_pool(pool_totals["all"]),
            "swing_pool": _finalize_pool(pool_totals["swing_pool"]),
            "long_term_pool": _finalize_pool(pool_totals["long_term_pool"]),
            "ytd_realized_pnl": ytd_realized_pnl,
            "closed_win_rate_pct": discipline_agg["closed_win_rate_pct"],
            "discipline_compliance_pct": discipline_agg["compliance_pct"],
            "unexecuted_stop_count": discipline_agg["unexecuted_stop_count"],
        },
        "order_ticket_summary": {
            "total_buy_amount": buy_ticket_total,
            "total_sell_amount": sell_ticket_total,
            "net_amount": sell_ticket_total - buy_ticket_total,
        },
        "morning_brief_overview": {
            "headline_summary": merged_headline or "目前無嚴選波段持股或尚未產出早盤總評。",
            "full_markdown": merged_markdown,
        },
        "holdings": holdings_out,
        "exposure_matrix": exposure_matrix,
        "dca_0050": dca_section,
        "opportunity_pool": opportunity_pool,
    }

    history_pnl_payload = _build_history_pnl_bundle(
        memory_rows=memory_rows,
        settlement_rows=settlement_rows,
        statement_rows=statement_rows,
        discipline_stats=discipline_agg,
        audit_db_path=audit_db_path,
    )

    # 溫資料包：保留最近 retention_trading_days 個交易日快照
    daily_archive_dir = root / ".state" / DAILY_PLAIN_ARCHIVE_DIRNAME
    daily_archive_dir.mkdir(parents=True, exist_ok=True)
    existing_daily_dates = sorted(
        [p.stem for p in daily_archive_dir.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)],
        reverse=True,
    )
    if dt_str not in existing_daily_dates:
        existing_daily_dates = sorted([dt_str, *existing_daily_dates], reverse=True)
    keep_dates = existing_daily_dates[: max(1, int(retention_trading_days))]

    latest_hash = hashlib.sha256(
        json.dumps(latest_payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]

    manifest_payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso,
        "last_phase": phase,
        "latest_trade_date": dt_str,
        "price_as_of_date": price_as_of_date,
        "available_daily_dates": keep_dates,
        "latest_checksum": latest_hash,
    }

    return {
        "manifest": manifest_payload,
        "latest": latest_payload,
        "daily": {dt_str: latest_payload},
        "history_pnl": history_pnl_payload,
        "keep_dates": keep_dates,
    }


def _assert_no_forbidden_keys(obj: Any, path: str = "") -> None:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if str(k) in _FORBIDDEN_LEGACY_KEYS:
                raise AssertionError(f"偵測到已廢棄欄位 {k!r} 於 {path}")
            _assert_no_forbidden_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _assert_no_forbidden_keys(item, f"{path}[{i}]")


# --------------------------------------------------------------------------- #
# 8. End-to-End Publisher (`publish_my_stock_portal`)
# --------------------------------------------------------------------------- #
def publish_my_stock_portal(
    *,
    phase: str = PHASE_MANUAL_PUBLISH,
    trade_date: date | None = None,
    sheet_mgr: Any | None = None,
    holdings_rows: Sequence[Mapping[str, Any]] | None = None,
    memory_rows: Sequence[Mapping[str, Any]] | None = None,
    statement_rows: Sequence[Mapping[str, Any]] | None = None,
    settlement_rows: Sequence[Mapping[str, Any]] | None = None,
    dca_rows: Sequence[Mapping[str, Any]] | None = None,
    tech_map: Mapping[str, Mapping[str, Any]] | None = None,
    market_regime: Mapping[str, Any] | None = None,
    macro_context: Mapping[str, Any] | None = None,
    picks_bundle: Mapping[str, Any] | None = None,
    deep_fund_map: Mapping[str, Mapping[str, Any]] | None = None,
    decisions_by_sno: Mapping[str, Mapping[str, Any]] | None = None,
    headline_summary: str = "",
    full_markdown: str = "",
    reports_dir: Path | None = None,
    audit_db_path: Path | None = None,
    private_web_dir: Path | None = None,
    passphrase: str | None = None,
    salt_hex: str | None = None,
    retention_trading_days: int | None = None,
    dry_run: bool = False,
    rotate_all: bool = False,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """加密打包並透過 Git Trees API (`scope='private'`) 發布至 GitHub Pages `/private/`。"""
    from taishin_config import (
        DCA_0050_SHEET_NAME,
        HOLDINGS_SHEET_NAME,
        MEMORY_SHEET_NAME,
        SETTLEMENT_SHEET_NAME,
        STATEMENT_SHEET_NAME,
    )
    from taishin_portfolio_service import (
        TaishinSheetManager,
        _aggregate_holdings_by_stock,
        load_local_technical_context,
        load_public_deep_fundamental_context,
        load_public_picks_for_attribution,
    )

    today = trade_date or datetime.now(TAIPEI_TIMEZONE).date()
    root = Path(reports_dir or "finmind_reports")
    lock_path = root / ".state" / LOCK_FILENAME
    resolved_passphrase = (
        passphrase if passphrase is not None else os.getenv("MY_STOCK_ENC_PASSPHRASE", "")
    ).strip()
    resolved_salt = (
        salt_hex if salt_hex is not None else os.getenv("MY_STOCK_KDF_SALT", "")
    ).strip()
    resolved_retention = (
        retention_trading_days
        if retention_trading_days is not None
        else _safe_int(os.getenv("MY_STOCK_RETENTION_TRADING_DAYS"), DEFAULT_RETENTION_TRADING_DAYS)
    )

    if not resolved_passphrase or not resolved_salt:
        msg = "MY_STOCK_ENC_PASSPHRASE 或 MY_STOCK_KDF_SALT 未設定，略過私人戰情頁發布。"
        if raise_on_error:
            raise MyStockCryptoError(msg)
        logger.warning(msg)
        return {"skipped": True, "reason": msg}

    try:
        with _portal_publish_lock(lock_path):
            # 補齊任何未由呼叫端傳入之資料來源
            mgr = sheet_mgr
            if any(x is None for x in (holdings_rows, memory_rows, statement_rows, settlement_rows, dca_rows)):
                mgr = mgr or TaishinSheetManager()
            resolved_holdings = list(holdings_rows) if holdings_rows is not None else mgr.read_records(HOLDINGS_SHEET_NAME)
            resolved_memories = list(memory_rows) if memory_rows is not None else mgr.read_records(MEMORY_SHEET_NAME)
            resolved_statements = list(statement_rows) if statement_rows is not None else mgr.read_records(STATEMENT_SHEET_NAME)
            resolved_settlements = list(settlement_rows) if settlement_rows is not None else mgr.read_records(SETTLEMENT_SHEET_NAME)
            resolved_dca = list(dca_rows) if dca_rows is not None else mgr.read_records(DCA_0050_SHEET_NAME)

            if tech_map is None or market_regime is None:
                loaded_tech, loaded_regime = load_local_technical_context(reports_dir=root, as_of=today)
                resolved_tech = tech_map if tech_map is not None else loaded_tech
                resolved_regime = market_regime if market_regime is not None else loaded_regime
            else:
                resolved_tech = tech_map
                resolved_regime = market_regime

            resolved_picks = (
                picks_bundle
                if picks_bundle is not None
                else load_public_picks_for_attribution(reports_dir=root)
            )

            resolved_deep = deep_fund_map
            if resolved_deep is None:
                agg = _aggregate_holdings_by_stock([dict(r) for r in resolved_holdings])
                tracked_sids = {
                    _clean_code(m.get("stock_no"))
                    for m in resolved_memories
                    if str(m.get("status") or "").upper() in ("OPEN", "TP1_PARTIAL_TAKEN")
                } - {""}
                if tracked_sids:
                    prices_by_sid = {
                        sno: _safe_float(h.get("current_price"), 0.0)
                        for sno, h in agg.items()
                        if _safe_float(h.get("current_price"), 0.0) > 0
                    }
                    try:
                        resolved_deep = load_public_deep_fundamental_context(
                            tracked_sids,
                            tech_map=resolved_tech,
                            prices_by_sid=prices_by_sid,
                            reports_dir=root,
                            as_of=today,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Failed to load deep fundamental context for MY_STOCK: %s", exc)
                        resolved_deep = {}
                else:
                    resolved_deep = {}

            bundles = build_my_stock_bundles(
                phase=phase,
                trade_date=today,
                holdings_rows=resolved_holdings,
                memory_rows=resolved_memories,
                statement_rows=resolved_statements,
                settlement_rows=resolved_settlements,
                dca_rows=resolved_dca,
                tech_map=resolved_tech,
                market_regime=resolved_regime,
                macro_context=macro_context,
                picks_bundle=resolved_picks,
                deep_fund_map=resolved_deep,
                decisions_by_sno=decisions_by_sno,
                headline_summary=headline_summary,
                full_markdown=full_markdown,
                reports_dir=root,
                audit_db_path=audit_db_path,
                retention_trading_days=resolved_retention,
            )

            _assert_no_forbidden_keys(bundles["latest"], "latest")

            # 落地今日明文溫備份於本地 .state（供未來 --rotate-all 重新加密所有近 14 日檔案）
            daily_archive_dir = root / ".state" / DAILY_PLAIN_ARCHIVE_DIRNAME
            dt_str = today.isoformat()
            _atomic_write_json(daily_archive_dir / f"{dt_str}.json", bundles["latest"])

            # 清理超過保留天數之本地溫備份
            keep_dates_set = set(bundles["keep_dates"])
            for p in daily_archive_dir.glob("*.json"):
                if p.stem not in keep_dates_set:
                    try:
                        p.unlink()
                    except OSError:
                        pass

            # 衍生一次 AES-256 金鑰，重用於本批次所有檔案（每檔仍有獨立隨機 12-byte IV）
            salt_bytes = bytes.fromhex(resolved_salt)
            derived_key = derive_master_key(resolved_passphrase, salt_bytes, iterations=DEFAULT_KDF_ITERATIONS)

            files_to_publish: dict[str, bytes] = {}

            def _pack_enc(remote_path: str, data_obj: Mapping[str, Any]) -> None:
                env = encrypt_payload_to_envelope(
                    data_obj,
                    resolved_passphrase,
                    resolved_salt,
                    build_aad_for_path(remote_path),
                    iterations=DEFAULT_KDF_ITERATIONS,
                    _derived_key=derived_key,
                )
                files_to_publish[remote_path] = json.dumps(
                    env, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")

            _pack_enc("private/data/manifest.enc", bundles["manifest"])
            _pack_enc("private/data/latest.enc", bundles["latest"])

            # 寫入當日 daily/{date}.enc（確保 manifest.available_daily_dates 中每個日期皆不 404）；
            # 若 --rotate-all 則重新加密全部保留之近 14 日快照
            if rotate_all:
                for d_keep in bundles["keep_dates"]:
                    snap = _read_json(daily_archive_dir / f"{d_keep}.json")
                    if isinstance(snap, dict):
                        _pack_enc(f"private/data/daily/{d_keep}.enc", snap)
            else:
                _pack_enc(f"private/data/daily/{dt_str}.enc", bundles["latest"])

            # 寫入 history_pnl.enc（盤後同步、手動發布、金鑰輪換或首次發布）
            if phase in (PHASE_MEMORY_SYNC, PHASE_MANUAL_PUBLISH, PHASE_MORNING_BRIEF) or rotate_all:
                _pack_enc("private/data/history_pnl.enc", bundles["history_pnl"])

            # 納入零個資純前端殼檔與 PWA Manifest
            web_dir = private_web_dir or PRIVATE_WEB_DIR
            idx_path = web_dir / "index.html"
            if idx_path.is_file():
                files_to_publish["private/index.html"] = idx_path.read_bytes()
            man_path = web_dir / "manifest.webmanifest"
            if man_path.is_file():
                files_to_publish["private/manifest.webmanifest"] = man_path.read_bytes()

            # 寫入本地密文鏡像（便於離線除錯與乾跑驗收）
            mirror_dir = root / LOCAL_MIRROR_DIRNAME
            for rel_path, content in files_to_publish.items():
                target = mirror_dir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(target.suffix + ".tmp")
                tmp.write_bytes(content)
                os.replace(tmp, target)

            my_stock_url = get_my_stock_portal_url() or ""
            if dry_run:
                return {
                    "dry_run": True,
                    "phase": phase,
                    "trade_date": dt_str,
                    "files": sorted(files_to_publish.keys()),
                    "mirror_dir": str(mirror_dir),
                    "portal_url": my_stock_url,
                    "keep_dates": bundles["keep_dates"],
                }

            sync_res = sync_portal_batch(
                files_to_publish,
                keep_daily_dates=keep_dates_set,
                commit_message=PRIVATE_COMMIT_MESSAGE,
                scope="private",
            )
            return {
                "dry_run": False,
                "phase": phase,
                "trade_date": dt_str,
                "mirror_dir": str(mirror_dir),
                "keep_dates": bundles["keep_dates"],
                **sync_res,
            }
    except Exception as exc:  # noqa: BLE001
        if raise_on_error:
            raise
        logger.warning("MY_STOCK 私人戰情中心發布失敗（不影響主帳務排程）：%s", exc)
        print(f"[warn] MY_STOCK 私人戰情中心發布失敗（不影響主帳務排程）：{exc}")
        return {"error": str(exc)}
