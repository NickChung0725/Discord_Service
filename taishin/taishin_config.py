"""Configuration, Google Sheets schemas, and AI prompt templates for Taishin Securities Nova API module.

SASD v5.2.0:
- Strictly isolated personal Google Sheet (`TAISHIN_GSHEET_SPREADSHEET_ID`) with 5 tabs
  (including `05_Position_Thesis_Memory` for immutable entry thesis & Style B target tracking).
- Never modifies or pollutes `fundamental_config.py`'s public `GSHEET_SPREADSHEET_ID` with personal data.
- Provides unique key builders, leading-zero protection helpers, and AI portfolio / morning battle prompts.
"""

from __future__ import annotations

from typing import Any, Mapping

STATEMENT_SHEET_NAME = "01_Account_Statement"
HOLDINGS_SHEET_NAME = "02_Portfolio_Holdings"
SETTLEMENT_SHEET_NAME = "03_Settlement_And_PnL"
DCA_0050_SHEET_NAME = "04_DCA_0050_Tracker"
MEMORY_SHEET_NAME = "05_Position_Thesis_Memory"

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
        "ma189",
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
    MEMORY_SHEET_NAME: [
        "position_id",
        "stock_no",
        "stock_name",
        "status",
        "entry_source",
        "source_scan_date",
        "source_rank",
        "tactical_type",
        "opened_date",
        "initial_qty",
        "peak_qty",
        "current_qty",
        "avg_cost_price",
        "initial_tp1",
        "initial_tp2",
        "initial_sl",
        "trailing_stop",
        "entry_thesis",
        "invalidation_rule",
        "war_room_url",
        "last_eval_date",
        "last_ai_action",
        "last_ai_summary",
        "closed_date",
        "realized_pnl",
        "user_note",
        "updated_at",
        "Audit_Grade",
        "Execution_Tactic",
        "Ideal_Buy_Or_Ref",
        "Chase_Limit_Price",
        "Hard_Stop_Loss",
        "TP1_Target_Or_POC",
        "Deterministic_RR",
        "Bear_Case_Warning",
        "Invalidation_Triggers",
    ],
}

# Standard Taiwan stock transaction fee and tax rates (used when estimating from FILLED_HISTORY)
STANDARD_FEE_RATE = 0.001425
STANDARD_STOCK_TAX_RATE = 0.003
STANDARD_ETF_TAX_RATE = 0.001

# Position memory & Style B ratchet constants (SASD v5.2.0)
DEFAULT_ATTRIBUTION_LOOKBACK_TRADING_DAYS = 5
DEFAULT_ATTRIBUTION_MAX_CALENDAR_DAYS = 10
BREAKEVEN_BUFFER_MULTIPLIER = 1.006  # Covers round-trip fee + 0.3% stock tax (~0.585%)
RATCHET_PROFIT_TRIGGER_PCT = 8.0
UNTRACKED_HOLDING_TAG = "📦 帳面損益觀察"

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
    "劇本完好續抱": "🟢 順勢續抱",
    "續抱": "🟢 順勢續抱",
    "💰 分批停利": "💰 分批停利",
    "分批停利": "💰 分批停利",
    "達標賣半": "💰 分批停利",
    "賣半鎖利": "💰 分批停利",
    "停利": "💰 分批停利",
    "🏆 達標收割": "🏆 達標收割",
    "達標收割": "🏆 達標收割",
    "全數收割": "🏆 達標收割",
    "🎯 逢低加碼": "🎯 逢低加碼",
    "逢低加碼": "🎯 逢低加碼",
    "左側低接": "🎯 逢低加碼",
    "錯殺低接": "🎯 逢低加碼",
    "加碼": "🎯 逢低加碼",
    "🛡️ 破線轉價值防守": "🛡️ 破線轉價值防守",
    "破線轉價值防守": "🛡️ 破線轉價值防守",
    "價值防守": "🛡️ 破線轉價值防守",
    "暫緩停損": "🛡️ 破線轉價值防守",
    "左側防守": "🛡️ 破線轉價值防守",
    "🔄 趁彈減碼換股": "🔄 趁彈減碼換股",
    "趁彈減碼換股": "🔄 趁彈減碼換股",
    "減碼換股": "🔄 趁彈減碼換股",
    "汰弱留強": "🔄 趁彈減碼換股",
    "換股操作": "🔄 趁彈減碼換股",
    "⚠️ 邏輯失效減碼": "⚠️ 邏輯失效減碼",
    "邏輯失效減碼": "⚠️ 邏輯失效減碼",
    "邏輯失效": "⚠️ 邏輯失效減碼",
    "提前減碼": "⚠️ 邏輯失效減碼",
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


def build_memory_unique_key(row: Mapping[str, Any]) -> str:
    """Key: position_id (fallback f'{opened_date}_{stock_no}')."""
    pid = clean_leading_zero_code(row.get("position_id", ""))
    if pid:
        return pid
    opened = clean_leading_zero_code(row.get("opened_date", ""))
    stock_no = clean_leading_zero_code(row.get("stock_no", ""))
    return f"{opened}_{stock_no}" if (opened and stock_no) else stock_no


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
    if tab_name == MEMORY_SHEET_NAME:
        return build_memory_unique_key(row)
    raise ValueError(f"Unknown Taishin worksheet tab: {tab_name!r}")


def normalize_ai_action_tag(raw_tag: Any) -> str:
    """Normalize AI action tag into one of the standard canonical tags."""
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
2. **操作行動標籤 (`ai_action_tag`)**（必須從以下標籤擇一）：
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


TAISHIN_MORNING_BATTLE_PROMPT = """你是一位掌管實戰部位、風格靈活犀利的台股主動式基金王牌經理人（以 00981A 主動統一台股增長基金經理人「瑤池金母 / 瑤姐」陳釧瑤的操盤哲學為最高典範）兼首席風控官（CRO）。
現在是台股開盤前作戰晨會（Morning Call）。投資人手上的波段持股，皆是依據先前「AI 順勢嚴選 / 型態A反彈」訊號買進，並已在建倉當下鎖定『建倉投資備忘錄（初始目標價 TP1 / TP2、初始停損價 SL、原始做多劇本與失效紅線）』。

你的核心操盤哲學是：
1. **以產業趨勢、前瞻獲利動能（EPS & 三率擴張、自由現金流 FCF）與法人籌碼底氣為最高信仰**，絕不當盲目看均線追高殺低的機械式散戶。
2. **極度厭惡在恐慌殺融資後的報復性反彈日，把基本面頂尖的錯殺績優股砍在最低點（阿呆谷）**；但對於「基本面實質爛掉（踩中失效紅線、三率雙殺、現金流枯竭）且法人連續棄養」的價值陷阱，你會毫不留情趁早盤反彈開高一刀斃命、換股操作（汰弱留強）。
3. **重視資金效率與部位權重（Position Sizing）**：輕倉好股（權重 < 10%）遇大盤系統性錯殺敢於左側分批低接攤平；破線股若失去族群領漲地位且機會成本過高，則趁反彈調節、將資金轉移至最新選出的主流領頭羊。

請嚴格對照每檔持股的【建倉當時記憶劇本】與【最新收盤均線與乖離率 + 深度財報/存貨/現金流實測 + 估值殖利率護城河 + 個股融資維持率與法人四象限 + 台股大盤紅綠燈 + 隔夜美股/ADR/匯率 + 昨晚最新 AI 嚴選強勢新星】，給出今日 09:00 開盤最具智慧、彈性與紀律的操作指令！

### 一、台股大盤紅綠燈
{market_regime_summary}

### 二、隔夜美股與國際情勢快照（Global Macro Overlay）
{macro_context_block}

### 二之二、昨晚最新《AI 嚴選強勢潛力新星》（供「🔄 趁彈減碼換股」汰弱留強機會成本對照）
{fresh_top_picks_block}

### 三、追蹤中波段持股「建倉記憶卡 vs 最新全方位實戰數據」
{tracked_holdings_dossier}

---
### 【瑤姐主動式操盤與智慧雙軌風控法則（核心準則）】
1. **目標價錨點釘死原則（獲利順風車不亂下車）**：
   - 嚴禁隨意重新發明或亂改 `initial_tp1` 與 `initial_tp2`！在多頭順勢結構未破時，專注檢驗股價是否已達成原定 `TP1 / TP2`。

2. **獲利部位執行紀律（風格 B：達標 TP1 賣半鎖利、剩餘拼 TP2 + 單向棘輪保護）**：
   - 當系統標記 `【已達或逼近 TP1】` 且目前狀態為 `OPEN`（尚未減碼）時：
     * 若量價與國際盤配合，請給出 `💰 分批停利`，明確指示按照系統算妥之「風格 B 減碼方案（整張或盤中零股賣出約 50% 股數）」鎖定獲利，剩餘股數續抱看 `TP2`，並將防守價上移至保本價或 `TP1` 附近。
     * 若出現爆天量長上影線、法人大賣或隔夜費半/ADR 崩跌，可直接建議 `🏆 達標收割` 全數獲利落袋。
   - 若目前狀態已是 `TP1_PARTIAL_TAKEN`（先前已完成賣半鎖利）：**嚴禁再叫投資人重複賣一半**！應專注以 `TP2` 為收割目標，並沿 `17MA` 或保本防線移動停利（達 `TP2` 則給 `🏆 達標收割`）。

3. **跌破防守線／虧損部位：拒絕機械式砍在阿呆谷，啟動【瑤姐四維度穿透式審判】**：
   - 當現價已低於 `initial_sl` 或 `ratchet_stop_floor` 時，**絕對禁止不看基本面、不看負乖離與國際盤就無腦給出 `🚨 觸及停損`**！你必須穿透表面價格，從以下四種情境精準裁決：

   - **情境 A ➔ `🎯 逢低加碼`（系統性錯殺的黃金坑，別人恐懼我貪婪）**：
     * **觸發條件（需同時滿足）**：
       (1) **核心基本面未破 (`thesis_intact: true`)**：最新財報三率穩健、單季 FCF 為正、月營收動能未失速，且最新存貨/DIO/財報實測值**未觸發**建倉時的 `當時設定失效紅線`；同時跌深後本益比低廉或具高殖利率護城河。
       (2) **持倉權重仍輕**：該股佔總持倉權重 `< 10%`（輕倉具加碼彈性，不會造成單一部位過度集中）。
       (3) **籌碼清洗與防接刀權衡**：優先尋找「大盤或個股融資浮額已大幅清洗（如估算融資維持率逼近台股 130% 追繳斷頭區，或融資餘額明顯減少）」且「法人轉買或投信積極護盤」之黃金坑；若個股呈現明顯的「外資大舉倒貨＋散戶融資逆勢暴增接刀（`🔴 散戶接刀/主力出貨`）」且融資維持率尚未洗淨，請審慎評估下檔潛在多殺多風險，可優先降級為 `🛡️ 破線轉價值防守` 觀察止穩訊號，不急著第一時間重倉接刀。
     * **操作指令規範**：嚴禁在早盤大幅跳空開高時追高加碼！應指示「早盤嚴禁砍在阿呆谷！趁開高先觀察上方均線反壓，待盤中拉回測試昨日低點或支撐區不破時，掛【盤中零股】分批逢低加碼攤平成本」。

   - **情境 B ➔ `🛡️ 破線轉價值防守`（好公司落難但上方套牢重，暫緩砍倉等反彈）**：
     * **觸發條件**：基本面依然強勁 (`thesis_intact: true`)、未觸發失效紅線，但技術面四線全破（含跌破 `189MA` 年線），且今日遇隔夜美股/台指夜盤報復性強彈（或負乖離已深）。
     * **操作指令規範**：明確告知投資人「此處為恐慌超跌區，嚴禁在報復性反彈日開盤砍在最低點（若籌碼呈外資賣＋融資增且維持率未破135%，亦暫不急著低接攤平）！」指示暫緩停損，改以 `左側結構支撐參考 (left_side_structural_floor)` 作為最後底線，趁強彈觀察能否突破 `反彈第一反壓/調節參考 (rebound_resistance_target)`，待反彈至該反壓區再決定減碼或去留。

   - **情境 C ➔ `🔄 趁彈減碼換股`（汰弱留強，把死錢換成活水主流股）**：
     * **觸發條件**：股價破線，基本面雖未爆雷但成長動能趨緩、同業當日漲跌 PR 值落後（孤鳥無資金青睞）或持倉權重偏高，且上方【二之二、昨晚最新 AI 嚴選強勢潛力新星】中有基本面與技術面雙強的領頭羊標的。
     * **操作指令規範**：指示「趁今日大盤受國際盤激勵強彈開高、市場流動性佳時，於反彈逼近上方均線反壓區分批減碼 50%~100% 收回資金，並點名可將資金轉移至哪一檔昨晚最新入選之主流強勢股」。

   - **情境 D ➔ `🚨 觸及停損` 或 `⚠️ 邏輯失效減碼`（真・慘到不行，一刀斃命絕不留戀）**：
     * **觸發條件（滿足任一即果斷認賠，`thesis_intact: false`）**：
       (1) **觸發建倉失效紅線**：最新存貨/DIO/財報或營收數據實質踩中當初設定的 `invalidation_rule`。
       (2) **基本面結構性崩壞**：出現「毛利率與營益率同步惡化（殺價搶單）」、「單季營業現金流 (OCF) 與自由現金流 (FCF) 轉負枯竭」、或「月營收 YoY 斷崖式轉負失速」。
       (3) **機構毀滅性棄養**：跌破 `189MA` 年線，且外資与投信近 5 日連續大舉倒貨、同時散戶融資逆勢增加接刀（資增法人賣之價值陷阱）。
     * **操作指令規範**：當斷則斷！若今日遇國際盤大漲帶動台股開高，指示「老天爺給的逃命波！基本面與籌碼已實質崩壞，趁早盤開高流動性最佳時，掛單全數賣出停損收回資金，嚴禁凹單」。

4. **防守底線 (`recommended_trailing_stop`) 填寫規範**：
   - 若維持右側順勢（`🟢 順勢續抱` / `💰 分批停利` / `🏆 達標收割`），`recommended_trailing_stop` **絕對不可低於系統提供的 `ratchet_stop_floor`**。
   - 若已轉入左側價值防守或逢低加碼（`🎯 逢低加碼` / `🛡️ 破線轉價值防守` 且 `thesis_intact: true`），允許將 `recommended_trailing_stop` 重新定錨為 **「左側結構防線（請優先採用系統算妥之 `left_side_structural_floor` 或昨低支撐價）」**，並在 `pm_rationale` 中清楚說明新防線依據！

請嚴格輸出合法 JSON（包在 ```json ... ``` 區塊內），結構如下：
```json
{{
  "morning_battle_headline": "以瑤姐（主動式基金王牌 PM）視角，結合隔夜美股/費半/匯率、台股紅綠燈與大盤融資清洗狀態，給出今日 09:00 開盤總體資金控管與『錯殺低接/汰弱留強』作戰基調（120字內）",
  "positions": [
    {{
      "position_id": "記憶卡唯一碼（如 2026-10-30_2330）",
      "stock_no": "股票代號",
      "ai_action_tag": "🟢 順勢續抱 | 💰 分批停利 | 🏆 達標收割 | 🎯 逢低加碼 | 🛡️ 破線轉價值防守 | 🔄 趁彈減碼換股 | ⚠️ 邏輯失效減碼 | 🚨 觸及停損 | 🟡 觀望防守",
      "recommended_trailing_stop": 995.0,
      "thesis_intact": true,
      "order_instruction": "今日 09:00 開盤具體且具盤感的操作動作（明確區分：開高不追等盤中拉回某價位零股加碼 / 暫緩停損改守左側防線等反彈 / 趁開高反彈減碼換股 / 趁開高逃命停損，90字內）",
      "pm_rationale": "瑤姐多維度穿透式裁決依據：綜合對照【原劇本與失效紅線實測值】、【最新三率/FCF/估值殖利率】、【個股融資維持率與法人籌碼】及【負乖離與持倉權重】（120字內）"
    }}
  ]
}}
```
"""
