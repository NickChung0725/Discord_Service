"""Configuration, Google Sheets schemas, and AI prompt templates for Taishin Securities Nova API module.

SASD v5.1.0:
- Strictly isolated personal Google Sheet (`TAISHIN_GSHEET_SPREADSHEET_ID`) with 4 tabs.
- Never modifies or pollutes `fundamental_config.py` / `GSHEET_SPREADSHEET_ID`.
- Provides unique key builders, leading-zero protection helpers, and AI portfolio diagnosis prompt.
"""

from __future__ import annotations

from typing import Any, Mapping

STATEMENT_SHEET_NAME = "01_Account_Statement"
HOLDINGS_SHEET_NAME = "02_Portfolio_Holdings"
SETTLEMENT_SHEET_NAME = "03_Settlement_And_PnL"
DCA_0050_SHEET_NAME = "04_DCA_0050_Tracker"

TAISHIN_SHEET_SCHEMAS: dict[str, list[str]] = {
    STATEMENT_SHEET_NAME: [
        "trade_date",
        "settlement_date",
        "source_api",
        "account_no",
        "order_no",
        "stock_no",
        "stock_name",
        "order_type",
        "action",
        "filled_qty",
        "filled_price",
        "gross_amount",
        "fee",
        "tax",
        "net_amount",
        "raw_json",
        "synced_at",
    ],
    HOLDINGS_SHEET_NAME: [
        "snapshot_date",
        "stock_no",
        "stock_name",
        "order_type",
        "qty",
        "tradable_qty",
        "avg_cost_price",
        "total_cost",
        "current_price",
        "market_value",
        "unrealized_pnl",
        "return_pct",
        "weight_pct",
        "ma17",
        "ma45",
        "ma117",
        "ai_action_tag",
        "ai_target_price",
        "ai_stop_loss",
        "ai_advice_summary",
        "war_room_url",
        "updated_at",
    ],
    SETTLEMENT_SHEET_NAME: [
        "record_date",
        "record_type",
        "stock_no",
        "stock_name",
        "qty",
        "buy_price",
        "sell_price",
        "net_amount",
        "realized_pnl",
        "realized_return_pct",
        "bank_balance",
        "shortfall_alert",
        "raw_json",
        "synced_at",
    ],
    DCA_0050_SHEET_NAME: [
        "iso_year_week",
        "eval_date",
        "close_price",
        "intraday_low",
        "ma17",
        "ma45",
        "rsi_14",
        "kd_k",
        "bias_ma17_pct",
        "dip_score",
        "trigger_reason",
        "already_bought_this_week",
        "order_mode",
        "order_qty_shares",
        "order_price",
        "order_no",
        "updated_at",
    ],
}

# Standard Taiwan stock transaction fee and tax rates (used when estimating from FILLED_HISTORY)
STANDARD_FEE_RATE = 0.001425
STANDARD_STOCK_TAX_RATE = 0.003
STANDARD_ETF_TAX_RATE = 0.001

# Backfill & GSheet batching constants
DEFAULT_BACKFILL_FROM_DATE = "2020-01-01"
DEFAULT_BACKFILL_CHUNK_DAYS = 30
BACKFILL_CHUNK_SLEEP_SECONDS = 1.5
GSHEET_APPEND_BATCH_SIZE = 500
GSHEET_RETRY_DELAYS_SECONDS = (2.0, 4.0, 8.0)

# 0050 Smart DCA constants
DCA_0050_SYMBOL = "0050"
DEFAULT_DCA_0050_WEEKLY_BUDGET_TWD = 10000
DCA_MIN_MULTIPLIER = 0.6
DCA_MAX_MULTIPLIER = 1.5
DCA_MA17_NEG_BIAS_TRIGGER_PCT = -1.5
DCA_RSI14_TRIGGER = 40.0
DCA_KDK_TRIGGER = 30.0
DCA_10D_DROP_TRIGGER_PCT = 2.5

# Valid AI action tags and mapping
AI_ACTION_TAG_MAP: dict[str, str] = {
    "🟢 順勢續抱": "🟢 順勢續抱",
    "順勢續抱": "🟢 順勢續抱",
    "續抱": "🟢 順勢續抱",
    "💰 分批停利": "💰 分批停利",
    "分批停利": "💰 分批停利",
    "停利": "💰 分批停利",
    "🎯 逢低加碼": "🎯 逢低加碼",
    "逢低加碼": "🎯 逢低加碼",
    "加碼": "🎯 逢低加碼",
    "🚨 觸及停損": "🚨 觸及停損",
    "觸及停損": "🚨 觸及停損",
    "停損": "🚨 觸及停損",
    "🟡 觀望防守": "🟡 觀望防守",
    "觀望防守": "🟡 觀望防守",
    "觀望": "🟡 觀望防守",
}

LEADING_ZERO_PROTECTED_FIELDS: frozenset[str] = frozenset({"stock_no", "order_no"})


def clean_leading_zero_code(value: Any) -> str:
    """Strip any leading single-quote added for Google Sheets USER_ENTERED protection."""
    if value is None:
        return ""
    text = str(value).strip()
    while text.startswith("'"):
        text = text[1:].strip()
    return text


def protect_leading_zero(value: Any) -> str:
    """Prefix a single quote if `value` starts with '0' so Google Sheets USER_ENTERED keeps '0050'."""
    cleaned = clean_leading_zero_code(value)
    if cleaned.startswith("0"):
        return f"'{cleaned}"
    return cleaned


def _norm_num_for_key(value: Any) -> str:
    """Normalize numeric values in unique keys so '1000', 1000, and 1000.0 produce identical keys."""
    if value is None:
        return "0"
    text = clean_leading_zero_code(value).replace(",", "")
    if not text:
        return "0"
    try:
        num = float(text)
    except ValueError:
        return text
    if num.is_integer():
        return str(int(num))
    return f"{num:.4f}".rstrip("0").rstrip(".")


def build_statement_unique_key(row: Mapping[str, Any]) -> str:
    """Key: f'{trade_date}_{order_no}_{stock_no}_{action}_{filled_qty}_{filled_price}'."""
    trade_date = clean_leading_zero_code(row.get("trade_date", ""))
    order_no = clean_leading_zero_code(row.get("order_no", ""))
    stock_no = clean_leading_zero_code(row.get("stock_no", ""))
    action = clean_leading_zero_code(row.get("action", "")).upper()
    filled_qty = _norm_num_for_key(row.get("filled_qty", 0))
    filled_price = _norm_num_for_key(row.get("filled_price", 0))
    return f"{trade_date}_{order_no}_{stock_no}_{action}_{filled_qty}_{filled_price}"


def build_holding_unique_key(row: Mapping[str, Any]) -> str:
    """Key: f'{stock_no}_{order_type}'."""
    stock_no = clean_leading_zero_code(row.get("stock_no", ""))
    order_type = clean_leading_zero_code(row.get("order_type", "")) or "現股"
    return f"{stock_no}_{order_type}"


def build_settlement_unique_key(row: Mapping[str, Any]) -> str:
    """Key: f'{record_date}_{record_type}_{stock_no}_{qty}_{net_amount}_{realized_pnl}'."""
    record_date = clean_leading_zero_code(row.get("record_date", ""))
    record_type = clean_leading_zero_code(row.get("record_type", "")).upper()
    stock_no = clean_leading_zero_code(row.get("stock_no", ""))
    qty = _norm_num_for_key(row.get("qty", 0))
    net_amount = _norm_num_for_key(row.get("net_amount", 0))
    realized_pnl = _norm_num_for_key(row.get("realized_pnl", 0))
    return f"{record_date}_{record_type}_{stock_no}_{qty}_{net_amount}_{realized_pnl}"


def build_dca_unique_key(row: Mapping[str, Any]) -> str:
    """Key: eval_date."""
    return clean_leading_zero_code(row.get("eval_date", ""))


def get_unique_key_for_tab(tab_name: str, row: Mapping[str, Any]) -> str:
    """Dispatch unique key generation by worksheet name."""
    if tab_name == STATEMENT_SHEET_NAME:
        return build_statement_unique_key(row)
    if tab_name == HOLDINGS_SHEET_NAME:
        return build_holding_unique_key(row)
    if tab_name == SETTLEMENT_SHEET_NAME:
        return build_settlement_unique_key(row)
    if tab_name == DCA_0050_SHEET_NAME:
        return build_dca_unique_key(row)
    raise ValueError(f"Unknown Taishin worksheet tab: {tab_name!r}")


def normalize_ai_action_tag(raw_tag: Any) -> str:
    """Normalize AI action tag into one of the 5 standard tags."""
    text = str(raw_tag or "").strip()
    if text in AI_ACTION_TAG_MAP:
        return AI_ACTION_TAG_MAP[text]
    for key, canonical in AI_ACTION_TAG_MAP.items():
        if key in text:
            return canonical
    return "🟡 觀望防守"


TAISHIN_PORTFOLIO_AI_PROMPT = """你是一位專業的台股投資組合風險控管長（CRO）與實戰操盤顧問。
請根據以下投資人的「台新證券目前真實持股庫存（含買入均價、現價、未實現損益%、持倉權重%）」、「五線均線技術位階（MA3/17/45/117/189）」、「基本面財報體質快取摘要（若有）」以及「近期歷史對帳單買賣節奏」，為每一檔持股提供精準的買入均價安全墊診斷與後續操作計畫。

### 大盤環境紅綠燈
{market_regime_summary}

### 投資人目前持股庫存與技術/基本面數據
{holdings_dossier}

### 近期歷史對帳單買賣節奏摘要
{recent_trades_summary}

---
### 診斷核心原則
1. **成本優勢評估（安全墊厚度）**：
   - 比較「目前現價 (`current_price`)」與「買入均價 (`avg_cost_price`)」：計算是否已拉開安全墊（如獲利 > 8~10%），或仍處於成本區震盪（-3% ~ +5%），或已陷入帳面套牢（< -5%）。
2. **操作行動標籤 (`ai_action_tag`)**（必須從以下 5 個標籤擇一）：
   - `🟢 順勢續抱`：股價站穩 17MA / 45MA 之上、多頭排列完好，且具成本優勢。
   - `💰 分批停利`：股價正乖離過大、獲利豐厚且出現高檔爆量或逼近前波壓力，建議鎖定部分獲利並上移保本停利線。
   - `🎯 逢低加碼`：基本面優良、長線均線（45MA/117MA）向上，股價回踩均線支撐且持倉權重尚未過高（如 < 25%）。
   - `🚨 觸及停損`：跌破買入成本與重要均線（17MA/45MA）且基本面或趨勢轉弱，應嚴格執行停損控管風險。
   - `🟡 觀望防守`：處於成本區附近整理或大盤偏弱，暫不加碼，嚴守關鍵支撐價。
3. **具體防守與目標價位**：
   - 依據買入均價 (`avg_cost_price`) 與 `17MA / 45MA / 117MA` 給出精確數值的 **保本/移動停利或停損價 (`ai_stop_loss`)** 與 **波段目標價 (`ai_target_price`)**。
   - 若帳面已有明顯獲利（如 > 8%），`ai_stop_loss` 應優先設於買入均價之上（保本停利）或 17MA/45MA 支撐位。

請嚴格輸出 JSON 格式（包在 ```json ... ``` 區塊內），結構如下：
```json
{{
  "portfolio_overview": "整體投組健康度、資金集中度與交割風險總評（100字內）",
  "holdings": [
    {{
      "stock_no": "股票代號（如 2330 或 0050，保留前導零）",
      "order_type": "委託類別（如 現股 或 盤中零股）",
      "ai_action_tag": "🟢 順勢續抱 | 💰 分批停利 | 🎯 逢低加碼 | 🚨 觸及停損 | 🟡 觀望防守",
      "ai_target_price": 1150.0,
      "ai_stop_loss": 980.0,
      "ai_advice_summary": "具體評估買入均價安全墊厚度、均線支撐與操作建議（80字內）"
    }}
  ]
}}
```
"""
