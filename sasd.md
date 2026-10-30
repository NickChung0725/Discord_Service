# 台股全市場動態掃描、雙軌量能守門員與細分產業同業知識庫系統規格書 (SASD v5.0 - Claude Code 終極交付版)

**專案名稱**：`Discord_Service` 台股量化分析助理（v5.0 全市場掃描 ＋ 細分產業同業知識庫擴充）  
**文件版本**：v5.0（整合最終定案完整版，供 Claude AI 一次到位實作，免除 Debug 往返）  
**執行環境**：Python 3.11+（`uv` 虛擬環境）、FinMind 免費版 Token（安全水位 $540\text{ req/hr}$）、Google AI Studio 免費版（`GEMINI_MODEL=gemini-flash-lite-latest`）、Google Sheets（`gspread`）

---

## 0. 給 Claude AI (Coding Agent) 的開發鐵則与必讀契約

請在開始撰寫或修改任何程式碼前，嚴格遵守以下 7 條工程鐵則：

1. **環境與測試指令**：
   - 執行單元測試必須使用：`uv run python -m unittest discover -s tests`
   - 所有新增與修改的功能皆不可破壞現有測試，並須新增對應測試檔 `tests/test_taxonomy_and_prefilter.py`。
2. **禁止破壞既有 Google Sheets 欄位位移**：
   - `finmind_stock_scanner.py` 中的 `_format_signal_tracker_worksheet()` 對 `MA_Signal_Tracker` 的 24 個欄位設定了硬編碼的 Column Index 數值格式（例如 Index `11..14` 為整數張數、Index `15..21` 為股價與均線小數點、`21..22` 為差距百分比）。**嚴禁更改 `MA_Signal_Tracker` 既有前 24 欄的順序**！
   - 細分產業與相似同業統一儲存於獨立的第 12 張工作表 **`08_Stock_Taxonomy`** 以及本地快取檔 `finmind_reports/.state/stock_taxonomy.json`。
3. **保護 FinMind 免費版配額（$540\text{ req/hr}$）**：
   - `_rate_limit_finmind_http_requests(limiter)` 若未傳入 `only_hosts=("finmindtrade.com",)`，會攔截全系統所有 `requests` 呼叫（包含 TWSE、TPEx、Yahoo 台股爬蟲），導致白白等待 $6.7$ 秒並浪費配額紀錄！
   - **修正強制要求**：在 `finmind_stock_scanner.py` 呼叫 `_rate_limit_finmind_http_requests(limiter, only_hosts=("finmindtrade.com",))` 時，**必須傳入 `only_hosts=("finmindtrade.com",)`**（與 `alignment_ai_picker.py` 一致）。
4. **解決 `_configured_universe()` 遇到 `"ALL"` 拋出 `ValueError` 的問題**：
   - 現有 `finmind_stock_scanner.py` 的 `_configured_universe()` 會檢查 `set(configured_industries).difference(available_industries)`，若 `industries` 設為 `["ALL"]` 會直接報錯。必須在函式開頭攔截 `"ALL"` 或 `"*"`，展開為 `stock_info` 扣除排除類別後的所有產業。
5. **雙軌流動性濾網（保護中高價股與量縮回踩股）**：
   - 初篩不可單純用「當日張數 $\ge 3,000$ 張」一刀切（否則股價 $300$ 元、成交 $1,500$ 張、成交金額高達 $4.5$ 億的績優股或型態 A 回踩量縮股會被誤殺）。
   - **初篩保留條件（Stage 1 Gatekeeper）**：符合以下任一條件即保留抓取 FinMind 日線：
     $$\text{當日量} \ge 1,000\text{ 張} \quad \lor \quad \text{20日均量} \ge 1,000\text{ 張} \quad \lor \quad \text{當日成交金額} \ge 100,000,000\text{ 元（1億台幣）}$$
   - **高流動性優先標記（$\ge 3,000$ 張）**：當日成交量 $\ge 3,000$ 張者，在排序與報表中優先標示。
6. **AI 模型與防幻覺雙保險（Zero-Cost & Zero-Hallucination）**：
   - 細分產業與同業配對固定使用 `.env` 的免費輕量模型 `GEMINI_MODEL`（預設 `gemini-flash-lite-latest`）。
   - 呼叫 AI 前，先以無狀態 HTTP 抓取 Yahoo 奇摩股市個股基本資料頁（`https://tw.stock.yahoo.com/quote/{stock_id}.TW/profile`）提取 250 字內的「主要經營業務與產品比重」作為 Prompt 小抄（RAG Grounding）。若抓取失敗則優雅降級僅用股名與官方大分類。
   - AI 回傳的 `peers`（同業清單）必須經過 Python 端 `TaiwanStockInfo` 真實 4 碼股票字典校驗（剔除不存在代號、自身代號，並自動校正官方股名）。
7. **22:00 / 22:30 選股器 (`alignment_ai_picker.py`) 與 `/stock_ai` 深度整合**：
   - `alignment_ai_picker.py` 的 `limit_stage2_candidates()`（同產業最多 3 檔）與 `enforce_sector_cap()`（同產業最多 2 檔），必須改為**優先依據 `sub_industry`（細分次產業，如「被動元件-MLCC」與「PCB-IC載板」分開計算）**，避免整個「電子零組件業」或「半導體業」卡死名額！
   - `/stock_ai` 單股戰情室報告與 HTML 戰情室必須直接展示該股的「🏷️ 細分產業（主/被動元件屬性）」與「🔗 直接競爭同業（Peers）」。

---

## 1. 系統整體架構與資料流 (System Architecture)

### 1.1 全市場三階漏斗掃描架構 (3-Stage Scanning Funnel)

```text
[19:00 排程啟動: scan-all]
       │
       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ Stage 0: 靜態代號與黑名單清洗 (0 API 消耗, < 0.1 秒)                        │
│  1. 讀取 FinMind TaiwanStockInfo (1 次 API，並建立全市場 {stock_id: name}) │
│  2. 正則過濾：僅保留 4 碼純數字普通股 (r"^\d{4}$")，自動剔除特別股(3702A)等 │
│  3. 剔除 stock_universe_registry.json 中 status == "DELISTED" 之永久下市股 │
└────────────────────────────────────────────────────────────────────────────┘
       │ (剩餘約 1,850 檔上市櫃普通股)
       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ Stage 1: 官方盤後全市場快照 ＋ 雙軌量能守門員 (0 FinMind 額度, 耗時 ~2 秒)  │
│  1. 抓取 TWSE (MI_INDEX_ALL) 與 TPEx (daily_close_quotes) 全市場當日收盤表 │
│  2. 自動下市偵測：不在當日收盤表且累計 missing_count >= 5 -> 標記 DELISTED │
│  3. 更新本地 20 日滾動成交量快取 (stock_universe_registry.json)             │
│  4. 雙軌流動性過濾（符合任一即放行）：                                     │
│     • 今日成交張數 >= 1,000 張 (min_trading_volume_lots)                   │
│     • 或 20日均量 >= 1,000 張                                              │
│     • 或 今日成交金額 >= 1 億台幣 (min_turnover_twd = 100,000,000)         │
└────────────────────────────────────────────────────────────────────────────┘
       │ (精準濃縮至約 300 ~ 420 檔活躍股，100% 涵蓋高價績優股與熱門股！)
       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ Stage 2: FinMind 12 個月日線抓取與五線均線運算 (約 350 次 API, ~38 分鐘)    │
│  1. 僅對通過 Stage 1 的活躍股呼叫 FinMind taiwan_stock_daily               │
│  2. 計算 MA3 / MA17 / MA45 / MA117 / MA189 與黃金量能三指標                 │
│  3. 產出 alignment_signals.csv (19:00) 與 prices.csv (供 22:30 反彈掃描)   │
└────────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ Stage 3: 細分產業 (主/被動元件) 與相似同業標籤注入 (TaxonomyService)        │
│  1. 針對入選訊號股（約 10~30 檔）查詢本地 stock_taxonomy.json (TTL 90 天)  │
│  2. 若未命中：抓取 Yahoo 業務簡介 + 呼叫 gemini-flash-lite-latest 產出標籤 │
│  3. 雙向同步寫入 stock_taxonomy.json 與 Google Sheets [08_Stock_Taxonomy]  │
│  4. 注入 Discord 19:00/22:00/22:30 報表、Stage 2 同業限額分群與 /stock_ai  │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 變更檔案清單與職責總覽 (File Modification Matrix)

| 序號 | 檔案路徑 | 變更類型 | 核心修改內容摘要 |
| :--- | :--- | :--- | :--- |
| **1** | `fundamental_config.py` | 修改 | 於 `SHEET_SCHEMAS` 新增第 12 張工作表 `"08_Stock_Taxonomy"` 的 10 欄位 Schema 定義。 |
| **2** | `finmind_scanner.json` | 修改 | `"industries"` 改為 `["ALL"]`，新增雙軌量能門檻（`1000` 張 / `1億` 成交額 / `3000` 張高亮）、TWSE/TPEx 快照預篩開關、Taxonomy 與 Registry 檔案路徑。 |
| **3** | `taxonomy_service.py` | **新增檔案** | 實作：1. TWSE/TPEx 當日全市場快照抓取與 `stock_universe_registry.json` 滾動 20 日均量/下市狀態更新；2. Yahoo 業務小抄爬蟲；3. `gemini-flash-lite-latest` 細分產業與同業萃取 + 代號防幻覺清洗器；4. 本地 JSON 與 Google Sheets `08_Stock_Taxonomy` 雙向同步。 |
| **4** | `finmind_stock_scanner.py` | 修改 | 1. `_rate_limit_finmind_http_requests` 綁定 `only_hosts=("finmindtrade.com",)`；2. `load_scan_config` 與 `_configured_universe` 支援 `industries=["ALL"]`、4 碼普通股過濾與 Stage 1 量能預篩；3. 新增 CLI 子指令 `init-universe`；4. 19:00 與 22:30 Discord 快報附加細分產業與同業資訊。 |
| **5** | `alignment_ai_picker.py` | 修改 | 1. 載入 `TaxonomyService` 為候選股注入 `sub_industry`、`component_nature`、`peers`；2. `limit_stage2_candidates` 與 `enforce_sector_cap` 改用 `sub_industry`（細分次產業）做同業限額；3. `build_dossier` 與 `format_pick_report` 顯示細分產業與相似同業。 |
| **6** | `fundamental_analyzer.py` / `stock_ai_command.py` | 修改 | 當執行 `/stock_ai` 單股診斷與生成戰情室 HTML 時，呼叫 `TaxonomyService.get_or_create_taxonomy()`，在報告首段與戰情室標頭顯示「🏷️ 細分產業（主/被動元件）｜🔗 直接競爭同業」。 |
| **7** | `tests/test_taxonomy_and_prefilter.py` | **新增檔案** | 針對全市場 `"ALL"` 展開、雙軌量能過濾、下市偵測、同業代號防幻覺清洗、Stage 2 細分次產業限額進行完整單元測試（全程 Mock 外部網路請求）。 |

---

## 3. 資料結構與設定檔規格 (Schemas & Configurations)

### 3.1 `fundamental_config.py` 新增 `08_Stock_Taxonomy` Schema

請在 `fundamental_config.py` 的 `SHEET_SCHEMAS` 字典中新增以下鍵值（10 個正交欄位，內建 `非電子/不適用` 逃生門，確保 100% 涵蓋電子、傳產、金融、生技、航運全市場）：

```python
    "08_Stock_Taxonomy": [
        "stock_id",           # 股票代號 (PK, 4碼字串如 "2327")
        "stock_name",         # 股票名稱 (如 "國巨")
        "official_industry",  # L1 證交所/FinMind 官方大類 (如 "電子零組件業")
        "sector_group",       # L2 六大板塊分群 (Enum)
        "sub_industry",       # L3 細分次產業 (如 "被動元件-MLCC與晶片電阻"，用於 22:00 同業限額分群)
        "component_nature",   # 電子元件屬性 (Enum: 主動元件 / 被動元件 / 機構與模組周邊 / 系統與終端設備 / 非電子/不適用)
        "supply_chain_role",  # 價值鏈位置 (Enum: 上游 / 中游 / 下游 / 垂直整合)
        "core_products",      # 主力產品與業務簡述 (50字內)
        "theme_tags",         # 概念題材標籤 (JSON Array 字串，如 '["車用電子", "AI伺服器"]')
        "peers",              # 台股直接競爭同業 (JSON Array 字串，如 '["2492 華新科", "6173 信昌電"]')
        "updated_at",         # 更新日期 YYYY-MM-DD (TTL 90天)
    ],
```

#### 欄位 Enum 嚴格規範（定義於 `taxonomy_service.py` 並由 Sanitizer 強制校驗）：
1. **`VALID_SECTOR_GROUPS` (L2 六大板塊)**：
   - `"半導體"`
   - `"電子硬體與零組件"`
   - `"軟體、網通與資安"`
   - `"傳產製造與原物料"`
   - `"生技醫療與綠能"`
   - `"金融、航運與內需服務"`
2. **`VALID_COMPONENT_NATURES` (主/被動元件與非電子逃生門)**：
   - `"主動元件"`（IC設計、晶圓製造、封測、功率半導體 MOSFET/IGBT/二極體、雷射光電晶片等需外部電源主動控制訊號者）
   - `"被動元件"`（MLCC 積層陶瓷電容、鋁質/固態電容、晶片電阻、電感、石英元件、變壓器、保護元件等被動儲能/濾波者）
   - `"機構與模組周邊"`（PCB、IC載板、CCL銅箔基板、散熱模組、機殼、連接器、線材、電源供應器、光學鏡頭、面板模組）
   - `"系統與終端設備"`（AI伺服器組裝、筆電/PC品牌與代工、網通設備整機、工業電腦 IPC、半導體設備機台）
   - `"非電子/不適用"`（航運、重電、鋼鐵、塑化、化工、生技新藥/醫材、金融、營建、食品、觀光、純軟體服務等非電子硬體公司）
3. **`VALID_SUPPLY_CHAIN_ROLES` (價值鏈位置)**：
   - `"上游(IP/材料/晶圓/原物料)"`
   - `"中游(製造/封測/零組件/加工)"`
   - `"下游(品牌/系統組裝/通路/終端服務)"`
   - `"垂直整合(IDM/一條龍)"`

---

### 3.2 `finmind_scanner.json` 完整升級配置

請將 `finmind_scanner.json` 更新為以下結構（向下相容所有既有設定鍵）：

```json
{
  "markets": [
    "上市",
    "上櫃"
  ],
  "industries": [
    "ALL"
  ],
  "only_common_stocks_4digits": true,
  "enable_twse_tpex_prefilter": true,
  "min_trading_volume_lots": 1000,
  "highlight_volume_lots": 3000,
  "min_turnover_twd": 100000000,
  "volume_filter_mode": "today_or_ma20_or_turnover",
  "delisted_missing_days_threshold": 5,
  "dormant_recheck_days": 14,
  "taxonomy_ttl_days": 90,
  "taxonomy_cache_path": "finmind_reports/.state/stock_taxonomy.json",
  "universe_registry_path": "finmind_reports/.state/stock_universe_registry.json",
  "taxonomy_gsheet_tab_name": "08_Stock_Taxonomy",
  "excluded_industry_categories": [
    "ETF",
    "ETN",
    "Index",
    "大盤",
    "所有證券",
    "受益證券",
    "上櫃ETF",
    "上櫃指數股票型基金(ETF)",
    "指數投資證券(ETN)",
    "存託憑證",
    "創新板"
  ],
  "excluded_stocks": [
    {
      "stock_id": "2473",
      "stock_name": "思源",
      "reason": "已下市"
    }
  ],
  "max_requests_per_hour": 540,
  "min_request_interval_seconds": 6.7,
  "daily_start_time": "19:00",
  "timezone": "Asia/Taipei",
  "post_alignment_to_discord": true,
  "stock_channel_id": "1557778164124622918",
  "sync_alignment_to_gsheet": true,
  "gsheet_tab_name": "MA_Signal_Tracker",
  "output_directory": "finmind_reports",
  "local_retention_days": 30,
  "ai_picker_max_candidates": 25,
  "ai_picker_max_top_picks": 5,
  "ai_picker_portfolio_model": "gemini-3.8-flash",
  "ai_picker_call_interval_seconds": 13.0,
  "publish_war_room_for_all_candidates": true,
  "post_ai_picks_to_discord": true,
  "save_ai_picks_to_gsheet": true,
  "enable_market_regime": true,
  "market_benchmark_id": "TAIEX",
  "rebound_gsheet_tab_name": "Rebound_Watchlist",
  "sync_rebound_to_gsheet": true,
  "post_rebound_to_discord": true,
  "rebound_auto_run_ai": false,
  "rebound_ai_min_candidates": 3,
  "rebound_ai_max_candidates": 15
}
```

---

## 4. 新增核心模組設計：`taxonomy_service.py`

請在專案根目錄建立 `taxonomy_service.py`，包含兩大核心元件：
1. **`MarketUniversePrefilter`**：負責 TWSE/TPEx 官方全市場盤後快照抓取、`stock_universe_registry.json` 滾動 20 日均量維護、自動下市偵測與雙軌量能過濾。
2. **`TaxonomyService`**：負責 Yahoo 台股業務簡介抓取、呼叫免費版 `GEMINI_MODEL` (`gemini-flash-lite-latest`)、台股代號防幻覺校驗、以及本地 `stock_taxonomy.json` 與 Google Sheets `08_Stock_Taxonomy` 的雙向讀寫快取。

### 4.1 `taxonomy_service.py` 完整實作規格

```python
"""全市場盤後快照預篩器 (MarketUniversePrefilter) 與 細分產業同業知識庫服務 (TaxonomyService)。"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import requests

from ai_service import ask_google_ai_studio_with_meta
from fundamental_config import SHEET_SCHEMAS

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
FOUR_DIGIT_RE = re.compile(r"^\d{4}$")

VALID_SECTOR_GROUPS = (
    "半導體",
    "電子硬體與零組件",
    "軟體、網通與資安",
    "傳產製造與原物料",
    "生技醫療與綠能",
    "金融、航運與內需服務",
)

VALID_COMPONENT_NATURES = (
    "主動元件",
    "被動元件",
    "機構與模組周邊",
    "系統與終端設備",
    "非電子/不適用",
)

VALID_SUPPLY_CHAIN_ROLES = (
    "上游(IP/材料/晶圓/原物料)",
    "中游(製造/封測/零組件/加工)",
    "下游(品牌/系統組裝/通路/終端服務)",
    "垂直整合(IDM/一條龍)",
)

TAXONOMY_SHEET_NAME = "08_Stock_Taxonomy"
TAXONOMY_HEADERS = list(SHEET_SCHEMAS[TAXONOMY_SHEET_NAME])


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _parse_int_clean(val: object) -> int:
    if val is None:
        return 0
    text = str(val).replace(",", "").strip()
    if not text or text in ("--", "-", "N/A"):
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _parse_float_clean(val: object) -> float:
    if val is None:
        return 0.0
    text = str(val).replace(",", "").strip()
    if not text or text in ("--", "-", "N/A"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# Part 1: TWSE / TPEx 全市場當日收盤快照與雙軌流動性預篩器
# --------------------------------------------------------------------------- #
def fetch_twse_tpex_daily_snapshot(timeout: int = 15) -> dict[str, dict[str, float]]:
    """從證交所與櫃買中心 OpenAPI 抓取當日全市場收盤快照（0 FinMind 額度，耗時約 2 秒）。

    回傳格式:
    {
        "2330": {
            "vol_lots": 35200.0,
            "turnover_twd": 38000000000.0,
            "close": 1080.0,
            "market": "上市",
        },
        ...
    }
    """
    snapshot: dict[str, dict[str, float]] = {}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DiscordStockAssistant/5.0)"}

    # 1. 上市 (TWSE) 全市場當日收盤表
    twse_url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX_ALL"
    try:
        resp = requests.get(twse_url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        for row in resp.json():
            code = str(row.get("Code", "")).strip()
            if not FOUR_DIGIT_RE.match(code):
                continue
            shares = _parse_int_clean(row.get("TradeVolume"))
            turnover = _parse_float_clean(row.get("TradeValue"))
            close_price = _parse_float_clean(row.get("ClosingPrice"))
            if turnover <= 0 and shares > 0 and close_price > 0:
                turnover = shares * close_price
            snapshot[code] = {
                "vol_lots": round(shares / 1000.0, 2),
                "turnover_twd": turnover,
                "close": close_price,
                "market": "上市",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("TWSE OpenAPI 快照抓取失敗: %s", exc)

    # 2. 上櫃 (TPEx) 全市場當日收盤表
    tpex_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    try:
        resp = requests.get(tpex_url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        for row in resp.json():
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            if not FOUR_DIGIT_RE.match(code):
                continue
            shares = _parse_int_clean(row.get("TradingShares"))
            turnover = _parse_float_clean(row.get("TransactionAmount"))
            close_price = _parse_float_clean(row.get("Close"))
            if turnover <= 0 and shares > 0 and close_price > 0:
                turnover = shares * close_price
            snapshot[code] = {
                "vol_lots": round(shares / 1000.0, 2),
                "turnover_twd": turnover,
                "close": close_price,
                "market": "上櫃",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("TPEx OpenAPI 快照抓取失敗: %s", exc)

    return snapshot


def load_universe_registry(registry_path: Path) -> dict[str, Any]:
    if registry_path.is_file():
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("stocks"), dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    return {"updated_at": "", "last_snapshot_date": "", "stocks": {}}


def update_registry_with_daily_snapshot(
    registry_path: Path,
    stock_info: pd.DataFrame,
    snapshot: Mapping[str, Mapping[str, float]],
    as_of: date,
    delisted_threshold: int = 5,
) -> dict[str, Any]:
    """用當日 TWSE/TPEx 快照更新本地 20 日滾動成交量與下市偵測計數器。"""
    reg = load_universe_registry(registry_path)
    stocks_map: dict[str, dict[str, Any]] = reg.setdefault("stocks", {})
    today_iso = as_of.isoformat()
    is_new_day = reg.get("last_snapshot_date") != today_iso

    # 若快照總檔數 < 800，代表當日休市或官網 API 異常，不遞增 missing_count
    snapshot_valid = len(snapshot) >= 800
    if not snapshot_valid:
        return reg

    info_df = stock_info.drop_duplicates(subset=["stock_id"])
    for row in info_df.itertuples(index=False):
        sid = str(row.stock_id).strip()
        if not FOUR_DIGIT_RE.match(sid):
            continue
        entry = stocks_map.setdefault(
            sid,
            {
                "stock_name": str(row.stock_name).strip(),
                "industry_category": str(row.industry_category).strip(),
                "status": "ACTIVE",
                "missing_count": 0,
                "vol_history_20d": [],
                "vol_ma20_lots": 0.0,
                "latest_vol_lots": 0.0,
                "latest_turnover_twd": 0.0,
                "last_seen": "",
            },
        )
        entry["stock_name"] = str(row.stock_name).strip()
        entry["industry_category"] = str(row.industry_category).strip()

        snap_item = snapshot.get(sid)
        if snap_item is not None and snap_item.get("close", 0.0) > 0:
            vol_lots = float(snap_item.get("vol_lots", 0.0))
            turnover = float(snap_item.get("turnover_twd", 0.0))
            entry["status"] = "ACTIVE"
            entry["missing_count"] = 0
            entry["latest_vol_lots"] = vol_lots
            entry["latest_turnover_twd"] = turnover
            entry["last_seen"] = today_iso
            hist: list[float] = list(entry.get("vol_history_20d") or [])
            if is_new_day or not hist:
                hist.append(vol_lots)
            else:
                hist[-1] = vol_lots
            hist = hist[-20:]
            entry["vol_history_20d"] = hist
            entry["vol_ma20_lots"] = round(sum(hist) / len(hist), 1) if hist else vol_lots
        else:
            if is_new_day and entry.get("status") != "DELISTED":
                entry["missing_count"] = int(entry.get("missing_count", 0)) + 1
                if entry["missing_count"] >= delisted_threshold:
                    entry["status"] = "DELISTED"
                    entry["reason"] = f"連續 {entry['missing_count']} 個交易日無收盤報價（自動判定下市/終止買賣）"

    reg["updated_at"] = datetime.now(TAIPEI_TZ).isoformat()
    reg["last_snapshot_date"] = today_iso
    _atomic_write_json(registry_path, reg)
    return reg


def passes_dual_track_liquidity(
    stock_id: str,
    snapshot: Mapping[str, Mapping[str, float]],
    registry_stocks: Mapping[str, Mapping[str, Any]],
    *,
    min_lots: int = 1000,
    min_turnover_twd: float = 100_000_000.0,
    mode: str = "today_or_ma20_or_turnover",
) -> bool:
    """雙軌流動性守門員：排除已下市與低量殭屍股，同時保護中高價績優股與回踩量縮股。"""
    reg_info = registry_stocks.get(stock_id, {})
    if reg_info.get("status") == "DELISTED":
        return False

    snap_valid = len(snapshot) >= 800
    snap_item = snapshot.get(stock_id)

    if snap_valid:
        if snap_item is None or snap_item.get("close", 0.0) <= 0:
            # 當日無收盤價（已下市或暫停交易）
            return False
        today_vol = float(snap_item.get("vol_lots", 0.0))
        today_turnover = float(snap_item.get("turnover_twd", 0.0))
    else:
        # 當日官方快照連線失敗時，Fallback 使用本地 Registry 快取
        if reg_info.get("status") == "DORMANT":
            return False
        if not reg_info:
            return True
        today_vol = float(reg_info.get("latest_vol_lots", 0.0))
        today_turnover = float(reg_info.get("latest_turnover_twd", 0.0))

    ma20_vol = float(reg_info.get("vol_ma20_lots", 0.0))

    if mode == "today":
        return today_vol >= min_lots
    if mode == "today_or_ma20":
        return today_vol >= min_lots or ma20_vol >= min_lots
    # 預設 "today_or_ma20_or_turnover"
    return (
        today_vol >= min_lots
        or ma20_vol >= min_lots
        or today_turnover >= min_turnover_twd
    )


# --------------------------------------------------------------------------- #
# Part 2: Yahoo 台股業務簡介抓取 + Gemini Lite 細分產業與同業知識庫
# --------------------------------------------------------------------------- #
def fetch_yahoo_company_profile(stock_id: str, timeout: int = 8) -> str:
    """從 Yahoo 奇摩股市抓取公司『主要經營業務』簡述（250字內），作為 Gemini Lite 的 RAG 依據。"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for suffix in (".TW", ".TWO"):
        url = f"https://tw.stock.yahoo.com/quote/{stock_id}{suffix}/profile"
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                continue
            text = html.unescape(resp.text)
            # 嘗試擷取 meta description 或「主要經營業務」段落
            meta_match = re.search(
                r'<meta\s+name="description"\s+content="([^"]+)"', text
            )
            desc_parts: list[str] = []
            if meta_match:
                desc_parts.append(meta_match.group(1).strip())
            biz_match = re.search(
                r"主要經營業務[</\w\s\"=-]*?>([^<]{10,300})<", text
            )
            if biz_match:
                desc_parts.append(biz_match.group(1).strip())
            combined = "｜".join(desc_parts).strip()
            if combined:
                return combined[:250]
        except Exception:  # noqa: BLE001
            continue
    return ""


def build_taxonomy_prompt(
    stock_id: str,
    stock_name: str,
    official_industry: str,
    business_summary: str = "",
) -> str:
    biz_line = f"\n- 公開主要業務與產品摘要：{business_summary}" if business_summary else ""
    return f"""你是一位精通台灣上市櫃供應鏈的資深產業研究員。請針對台股「{stock_id} {stock_name}」進行細分產業與同業競爭對手定義。
- 證交所官方分類：{official_industry}{biz_line}

請嚴格輸出單一 JSON 物件（不要加上 Markdown 語法），必須包含以下欄位：
{{
  "sector_group": "請從 {list(VALID_SECTOR_GROUPS)} 擇一",
  "sub_industry": "具體細分次產業名稱（15字內，例如：被動元件-MLCC與晶片電阻、IC設計-電源管理IC、PCB-ABF載板、貨櫃航運、重電設備與變壓器）",
  "component_nature": "請嚴格從 {list(VALID_COMPONENT_NATURES)} 擇一。判準：IC/二極體/MOSFET/雷射等需外部電源主動控制訊號者選『主動元件』；MLCC電容/電阻/電感/石英元件等選『被動元件』；PCB/散熱/機殼/連接器/電源供應器選『機構與模組周邊』；整機/伺服器/設備廠選『系統與終端設備』；傳產/航運/金融/生技/純軟體等非電子硬體選『非電子/不適用』",
  "supply_chain_role": "請從 {list(VALID_SUPPLY_CHAIN_ROLES)} 擇一",
  "core_products": "主力產品或核心業務（以頓號分隔，35字內）",
  "theme_tags": ["核心題材標籤1", "核心題材標籤2"],
  "peers": ["同業4碼代號1 同業股名1", "同業4碼代號2 同業股名2", "同業4碼代號3 同業股名3"]
}}
注意：peers 必須為台灣上市櫃真實存在的 2~4 家最直接競爭同業（4碼數字代號+空白+股名）。"""


def sanitize_taxonomy_record(
    raw_data: Mapping[str, Any],
    *,
    stock_id: str,
    stock_name: str,
    official_industry: str,
    valid_stock_map: Mapping[str, str],
    as_of: date | None = None,
) -> dict[str, Any]:
    """對 AI 回傳的分類結果執行 Enum 防呆與真實台股代號防幻覺清洗。"""
    today_iso = (as_of or datetime.now(TAIPEI_TZ).date()).isoformat()

    sector_group = str(raw_data.get("sector_group", "")).strip()
    if sector_group not in VALID_SECTOR_GROUPS:
        if "半導體" in official_industry:
            sector_group = "半導體"
        elif any(k in official_industry for k in ("電子", "電腦", "光電", "通信")):
            sector_group = "電子硬體與零組件"
        elif any(k in official_industry for k in ("軟體", "資訊", "數位")):
            sector_group = "軟體、網通與資安"
        elif any(k in official_industry for k in ("生技", "綠能", "環保")):
            sector_group = "生技醫療與綠能"
        elif any(k in official_industry for k in ("金融", "航運", "貿易", "居家", "觀光")):
            sector_group = "金融、航運與內需服務"
        else:
            sector_group = "傳產製造與原物料"

    component_nature = str(raw_data.get("component_nature", "")).strip()
    if component_nature not in VALID_COMPONENT_NATURES:
        if "半導體" in official_industry:
            component_nature = "主動元件"
        elif any(k in official_industry for k in ("電子", "電腦", "光電", "通信")):
            component_nature = "機構與模組周邊"
        else:
            component_nature = "非電子/不適用"

    supply_chain_role = str(raw_data.get("supply_chain_role", "")).strip()
    if supply_chain_role not in VALID_SUPPLY_CHAIN_ROLES:
        supply_chain_role = "中游(製造/封測/零組件/加工)"

    sub_industry = str(raw_data.get("sub_industry", "")).strip() or official_industry
    core_products = str(raw_data.get("core_products", "")).strip()[:60]

    raw_tags = raw_data.get("theme_tags")
    if isinstance(raw_tags, str):
        try:
            raw_tags = json.loads(raw_tags)
        except Exception:  # noqa: BLE001
            raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    theme_tags = [str(t).strip() for t in (raw_tags or []) if str(t).strip()][:3]

    # 防幻覺校驗 peers：必須存在於 valid_stock_map 且不可為自己
    raw_peers = raw_data.get("peers")
    if isinstance(raw_peers, str):
        try:
            raw_peers = json.loads(raw_peers)
        except Exception:  # noqa: BLE001
            raw_peers = [p.strip() for p in raw_peers.split(",") if p.strip()]
    verified_peers: list[str] = []
    seen_codes: set[str] = {stock_id}
    for item in raw_peers or []:
        match = re.search(r"\b(\d{4})\b", str(item))
        if not match:
            continue
        code = match.group(1)
        if code not in seen_codes and code in valid_stock_map:
            seen_codes.add(code)
            verified_peers.append(f"{code} {valid_stock_map[code]}")
        if len(verified_peers) >= 4:
            break

    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "official_industry": official_industry,
        "sector_group": sector_group,
        "sub_industry": sub_industry,
        "component_nature": component_nature,
        "supply_chain_role": supply_chain_role,
        "core_products": core_products,
        "theme_tags": json.dumps(theme_tags, ensure_ascii=False),
        "peers": json.dumps(verified_peers, ensure_ascii=False),
        "updated_at": today_iso,
    }


def format_taxonomy_badge(rec: Mapping[str, Any] | None) -> str:
    """產生供 Discord 報表與戰情室嵌入的單行標籤字串。"""
    if not rec:
        return ""
    nature = str(rec.get("component_nature", "")).strip()
    sub_ind = str(rec.get("sub_industry", "")).strip()
    products = str(rec.get("core_products", "")).strip()
    peers_raw = rec.get("peers", "[]")
    if isinstance(peers_raw, str):
        try:
            peers_list = json.loads(peers_raw)
        except Exception:  # noqa: BLE001
            peers_list = []
    elif isinstance(peers_raw, list):
        peers_list = peers_raw
    else:
        peers_list = []

    nature_tag = f"【{nature}】" if nature and nature != "非電子/不適用" else ""
    prod_part = f"（{products}）" if products else ""
    peers_str = "、".join(str(p) for p in peers_list[:4]) if peers_list else "無直接對標"
    return f"🏷️ 細分產業：{nature_tag}{sub_ind}{prod_part} ｜ 🔗 相似同業：{peers_str}"


class TaxonomyService:
    """管理本地 stock_taxonomy.json 與 Google Sheets [08_Stock_Taxonomy] 雙向快取與 AI 生成。"""

    def __init__(
        self,
        cache_path: Path = Path("finmind_reports/.state/stock_taxonomy.json"),
        ttl_days: int = 90,
        sheet_tab_name: str = TAXONOMY_SHEET_NAME,
    ) -> None:
        self.cache_path = cache_path
        self.ttl_days = ttl_days
        self.sheet_tab_name = sheet_tab_name
        self._records: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def load(self) -> dict[str, dict[str, Any]]:
        if self._loaded:
            return self._records
        if self.cache_path.is_file():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._records = data
                    self._loaded = True
                    return self._records
            except Exception:  # noqa: BLE001
                logger.warning("Failed to load local taxonomy JSON, trying Google Sheets")
        # Fallback: 從 Google Sheets 08_Stock_Taxonomy 拉取重建
        self._pull_from_gsheet()
        self._loaded = True
        return self._records

    def save_local(self) -> None:
        _atomic_write_json(self.cache_path, self._records)

    def _is_fresh(self, rec: Mapping[str, Any], as_of: date) -> bool:
        updated_str = str(rec.get("updated_at", "")).strip()
        if not updated_str or not rec.get("sub_industry"):
            return False
        try:
            updated_date = date.fromisoformat(updated_str[:10])
            return (as_of - updated_date).days <= self.ttl_days
        except ValueError:
            return False

    def get_cached(self, stock_id: str) -> dict[str, Any] | None:
        self.load()
        return self._records.get(str(stock_id).strip())

    def get_or_create_sync(
        self,
        stock_id: str,
        stock_name: str = "",
        official_industry: str = "",
        valid_stock_map: Mapping[str, str] | None = None,
        as_of: date | None = None,
        sync_sheet: bool = False,
    ) -> dict[str, Any]:
        """同步取得單股細分產業與同業資訊（若快取未命中則呼叫免費版 Gemini Lite）。"""
        self.load()
        sid = str(stock_id).strip()
        today = as_of or datetime.now(TAIPEI_TZ).date()
        existing = self._records.get(sid)
        if existing and self._is_fresh(existing, today):
            return existing

        s_name = stock_name or (existing or {}).get("stock_name", "") or (valid_stock_map or {}).get(sid, sid)
        off_ind = official_industry or (existing or {}).get("official_industry", "其他")
        biz_desc = fetch_yahoo_company_profile(sid)
        prompt = build_taxonomy_prompt(sid, s_name, off_ind, biz_desc)

        model_name = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest").strip() or "gemini-flash-lite-latest"
        raw_dict: dict[str, Any] = {}
        try:
            meta = asyncio.run(
                ask_google_ai_studio_with_meta(prompt, model=model_name, tag=f"taxonomy_{sid}")
            )
            text = re.sub(r"^```(?:json)?|```$", "", str(meta.text or "").strip(), flags=re.MULTILINE).strip()
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                raw_dict = json.loads(text[start : end + 1])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini taxonomy generation failed for %s: %s", sid, exc)

        stock_map = valid_stock_map or {k: v.get("stock_name", k) for k, v in self._records.items()}
        record = sanitize_taxonomy_record(
            raw_dict,
            stock_id=sid,
            stock_name=s_name,
            official_industry=off_ind,
            valid_stock_map=stock_map,
            as_of=today,
        )
        self._records[sid] = record
        self.save_local()
        if sync_sheet:
            self.sync_to_gsheet()
        return record

    def ensure_batch(
        self,
        stocks_df: pd.DataFrame,
        valid_stock_map: Mapping[str, str],
        *,
        as_of: date | None = None,
        call_interval_sec: float = 4.2,
        sync_sheet: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """批次補齊訊號股之細分產業與同業標籤，若有任何新增則同步寫回本地 JSON 與 Google Sheets。"""
        self.load()
        today = as_of or datetime.now(TAIPEI_TZ).date()
        updated_any = False

        if stocks_df is not None and not stocks_df.empty:
            dedup = stocks_df.drop_duplicates(subset=["stock_id"])
            for row in dedup.itertuples(index=False):
                sid = str(row.stock_id).strip()
                if not sid or sid == "-":
                    continue
                existing = self._records.get(sid)
                if existing and self._is_fresh(existing, today):
                    continue
                s_name = str(getattr(row, "stock_name", "") or valid_stock_map.get(sid, sid)).strip()
                off_ind = str(getattr(row, "industry_category", "") or "其他").strip()
                self.get_or_create_sync(
                    sid,
                    stock_name=s_name,
                    official_industry=off_ind,
                    valid_stock_map=valid_stock_map,
                    as_of=today,
                    sync_sheet=False,
                )
                updated_any = True
                time.sleep(call_interval_sec)

        if updated_any and sync_sheet:
            self.sync_to_gsheet()
        return self._records

    def _pull_from_gsheet(self) -> None:
        creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        sheet_id = os.getenv("GSHEET_SPREADSHEET_ID", "").strip()
        if not creds or not sheet_id:
            return
        try:
            gc = gspread.service_account(filename=creds)
            sh = gc.open_by_key(sheet_id)
            ws = sh.worksheet(self.sheet_tab_name)
            rows = ws.get_all_records()
            for r in rows:
                sid = str(r.get("stock_id", "")).strip()
                if sid:
                    self._records[sid] = {col: r.get(col, "") for col in TAXONOMY_HEADERS}
            if self._records:
                self.save_local()
        except Exception:  # noqa: BLE001
            pass

    def sync_to_gsheet(self) -> int:
        creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        sheet_id = os.getenv("GSHEET_SPREADSHEET_ID", "").strip()
        if not creds or not sheet_id or not self._records:
            return 0
        try:
            gc = gspread.service_account(filename=creds)
            sh = gc.open_by_key(sheet_id)
            existing_tabs = {ws.title: ws for ws in sh.worksheets()}
            if self.sheet_tab_name not in existing_tabs:
                ws = sh.add_worksheet(
                    title=self.sheet_tab_name,
                    rows=max(len(self._records) + 100, 500),
                    cols=len(TAXONOMY_HEADERS),
                )
            else:
                ws = existing_tabs[self.sheet_tab_name]

            sorted_recs = [
                self._records[k] for k in sorted(self._records.keys())
            ]
            matrix: list[list[Any]] = [TAXONOMY_HEADERS]
            for rec in sorted_recs:
                matrix.append([rec.get(col, "") for col in TAXONOMY_HEADERS])

            ws.clear()
            end_cell = gspread.utils.rowcol_to_a1(len(matrix), len(TAXONOMY_HEADERS))
            ws.update(
                range_name=f"A1:{end_cell}",
                values=matrix,
                value_input_option="USER_ENTERED",
            )
            return len(sorted_recs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync 08_Stock_Taxonomy to Google Sheets: %s", exc)
            return 0
```

---

## 5. 既有模組修改規格（逐檔精準指引）

### 5.1 修改 `finmind_stock_scanner.py`

#### 修改點 A：修正 `_rate_limit_finmind_http_requests` 只攔截 FinMind 網域
在 `scan_configured_universe()` 與 `main()` 中，所有 `with _rate_limit_finmind_http_requests(limiter):` 呼叫點，**統一加上 `only_hosts=("finmindtrade.com",)`**：
```python
with _rate_limit_finmind_http_requests(limiter, only_hosts=("finmindtrade.com",)):
```

#### 修改點 B：` _configured_universe()` 支援 `industries=["ALL"]`、4 碼普通股與雙軌流動性預篩
將 `_configured_universe(stock_info: pd.DataFrame, config: Mapping[str, object])` 擴充為支援傳入 `daily_snapshot` 與 `registry_stocks`：

```python
def _configured_universe(
    stock_info: pd.DataFrame,
    config: Mapping[str, object],
    daily_snapshot: Mapping[str, Mapping[str, float]] | None = None,
    registry_stocks: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[tuple[str, str], pd.DataFrame]:
    _validate_stock_info(stock_info)
    excluded_ids = {item["stock_id"] for item in config["excluded_stocks"]}
    if registry_stocks:
        for sid, info in registry_stocks.items():
            if isinstance(info, Mapping) and info.get("status") == "DELISTED":
                excluded_ids.add(str(sid))

    excluded_categories = EXCLUDED_INDUSTRY_CATEGORIES.union(
        config["excluded_industry_categories"]
    )
    only_4digits = bool(config.get("only_common_stocks_4digits", False))
    enable_prefilter = bool(config.get("enable_twse_tpex_prefilter", False))
    min_lots = int(config.get("min_trading_volume_lots", 1000))
    min_turnover = float(config.get("min_turnover_twd", 100_000_000.0))
    vol_mode = str(config.get("volume_filter_mode", "today_or_ma20_or_turnover"))

    configured_industries = list(config["industries"])
    available_industries = sorted(
        set(stock_info["industry_category"].dropna().astype(str).str.strip())
        - excluded_categories
    )
    if "ALL" in configured_industries or "*" in configured_industries:
        target_industries = available_industries
    else:
        missing_industries = sorted(
            set(configured_industries).difference(
                set(stock_info["industry_category"].dropna().astype(str))
            )
        )
        if missing_industries:
            raise ValueError(
                "FinMind 股票清單不存在設定的產業：" + "、".join(missing_industries)
            )
        target_industries = configured_industries

    from taxonomy_service import FOUR_DIGIT_RE, passes_dual_track_liquidity

    universe: dict[tuple[str, str], pd.DataFrame] = {}
    for market in config["markets"]:
        market_stocks = _filter_market(stock_info, market)
        sid_series = market_stocks["stock_id"].astype(str).str.strip()
        mask = (
            ~market_stocks["industry_category"].isin(excluded_categories)
            & ~sid_series.isin(excluded_ids)
        )
        if only_4digits:
            mask = mask & sid_series.str.match(FOUR_DIGIT_RE)
        market_stocks = market_stocks.loc[mask].copy()

        if enable_prefilter and (daily_snapshot is not None or registry_stocks is not None):
            snap = daily_snapshot or {}
            reg_map = registry_stocks or {}
            keep_ids = {
                str(sid).strip()
                for sid in market_stocks["stock_id"]
                if passes_dual_track_liquidity(
                    str(sid).strip(),
                    snap,
                    reg_map,
                    min_lots=min_lots,
                    min_turnover_twd=min_turnover,
                    mode=vol_mode,
                )
            }
            market_stocks = market_stocks.loc[
                market_stocks["stock_id"].astype(str).str.strip().isin(keep_ids)
            ].copy()

        for industry in target_industries:
            selected = market_stocks.loc[
                market_stocks["industry_category"].astype(str).str.strip() == industry
            ].copy()
            universe[(market, industry)] = (
                selected.drop_duplicates(subset=["stock_id"]).reset_index(drop=True)
            )
    return universe
```

#### 修改點 C：在 `scan_configured_universe()` 串接 Stage 1 快照與 Stage 3 Taxonomy 補齊
1. 在 `scan_configured_universe()` 建立 `universe` 之前：
   - 若 `config.get("enable_twse_tpex_prefilter")` 為 `True`，呼叫 `fetch_twse_tpex_daily_snapshot()` 與 `update_registry_with_daily_snapshot()` 更新 `stock_universe_registry.json`，並傳入 `_configured_universe()`。
2. 在產出 `alignment_signals` 後：
   - 建立全市場股名字典 `valid_stock_map = dict(zip(stock_info["stock_id"].astype(str).str.strip(), stock_info["stock_name"].astype(str).str.strip()))`。
   - 呼叫 `TaxonomyService.ensure_batch(alignment_signals, valid_stock_map, as_of=today)` 自動為今日進榜的強勢股補齊細分產業與同業標籤。
3. 在 `format_alignment_report()` 與 `format_rebound_report()` 中：
   - 讀取 `TaxonomyService().get_cached(row.stock_id)`，若存在則於該股行尾換行縮排印出 `format_taxonomy_badge(tax_rec)`：
     ```text
     2327 國巨 | 剛金叉起漲 | 收盤 625.00 | 量 8,450張 (🔥 放量點火...) | 17MA走勢 📈 連漲上揚
       └─ 🏷️ 細分產業：【被動元件】被動元件-MLCC與晶片電阻 ｜ 🔗 相似同業：2492 華新科、6173 信昌電
     ```

#### 修改點 D：新增 `init-universe` CLI 子指令（支援中斷續傳與每季自動重整）
在 `_make_parser()` 新增子指令 `init-universe`：
- 功能：掃描所有上市櫃 4 碼普通股近 30 日日線（受 `FinMindRequestLimiter` 保護，已存在於 `stock_universe_registry.json` 且 `last_seen` 在 7 天內者自動跳過，達成 100% 斷點續傳）。
- 若無日線或最後交易日距今 $> 15$ 天 $\rightarrow$ 標記 `status = "DELISTED"`。
- 計算近 20 日均量寫入 `vol_ma20_lots` 與 `vol_history_20d`。

---

### 5.2 修改 `alignment_ai_picker.py`（針對您提供的程式碼精準改寫）

您提供的 `alignment_ai_picker.py` 中，原本的同業限額邏輯只看 FinMind 粗分類 `industry_category`（例如把 MLCC 被動元件、PCB 載板、散熱模組全部當成同一個 `"電子零組件業"`，導致整個大類最多只能選 2 檔）。

請在 `alignment_ai_picker.py` 執行以下 **4 項具體改造**：

#### 改造 1：新增 `enrich_with_taxonomy()` 輔助函式
在 `enrich_with_tracker()` 下方新增 `enrich_with_taxonomy()`，為候選股 DataFrame 注入 `sub_industry`、`component_nature`、`taxonomy_badge`：

```python
def enrich_with_taxonomy(
    frame: pd.DataFrame,
    config: Mapping[str, object],
    target_date: date,
) -> pd.DataFrame:
    """為候選股補齊 08_Stock_Taxonomy 的細分次產業 (sub_industry)、主被動元件屬性與同業標籤。"""
    result = frame.copy()
    result["sub_industry"] = result.get("industry_category", "")
    result["component_nature"] = ""
    result["taxonomy_badge"] = ""
    if result.empty:
        return result

    try:
        from taxonomy_service import TaxonomyService, format_taxonomy_badge

        tax_path = Path(
            str(config.get("taxonomy_cache_path", "finmind_reports/.state/stock_taxonomy.json"))
        )
        tax_tab = str(config.get("taxonomy_gsheet_tab_name", "08_Stock_Taxonomy"))
        tax_svc = TaxonomyService(cache_path=tax_path, sheet_tab_name=tax_tab)
        tax_map = tax_svc.load()

        # 建立 valid_stock_map (先用本地已快取 + 候選股名單)
        valid_map = {k: str(v.get("stock_name", k)) for k, v in tax_map.items()}
        for r in result.itertuples(index=False):
            valid_map[str(r.stock_id).strip()] = str(getattr(r, "stock_name", "")).strip()

        tax_svc.ensure_batch(result, valid_map, as_of=target_date, sync_sheet=True)
        for idx, sid in result["stock_id"].items():
            rec = tax_svc.get_cached(str(sid))
            if rec:
                result.at[idx, "sub_industry"] = (
                    str(rec.get("sub_industry") or result.at[idx, "industry_category"]).strip()
                )
                result.at[idx, "component_nature"] = str(rec.get("component_nature", "")).strip()
                result.at[idx, "taxonomy_badge"] = format_taxonomy_badge(rec)
    except Exception as error:  # noqa: BLE001
        print(f"[warn] 無法載入細分產業知識庫，退回使用官方分類：{error}")
    return result
```

#### 改造 2：升級 `limit_stage2_candidates()` 與 `enforce_sector_cap()` 優先使用 `sub_industry`（細分次產業）
在 `alignment_ai_picker.py` 第 384~463 行：
1. 新增一個提取細分產業鍵值的 Helper：
   ```python
   def _effective_sector_key(row: Mapping[str, object]) -> str:
       """優先回傳細分次產業 sub_industry（如『被動元件-MLCC』），若無才退回官方 industry_category。"""
       sub = str(row.get("sub_industry") or "").strip()
       if sub:
           return sub
       ind = str(row.get("industry_category") or "").strip()
       return ind or f"__NO_SECTOR__:{row.get('stock_id')}"
   ```
2. 在 `limit_stage2_candidates()` 中，將原本：
   ```python
   sector = str(row.get("industry_category") or "").strip() or f"__NO_SECTOR__:{row.get('stock_id')}"
   ```
   改為：
   ```python
   sector = _effective_sector_key(row)
   ```
3. 在 `enforce_sector_cap()` 中，將原本：
   ```python
   industries = {
       str(r.get("stock_id")): str(r.get("industry_category", "") or "").strip()
       for r in frame.to_dict("records")
   }
   ```
   改為優先取 `sub_industry`：
   ```python
   industries = {
       str(r.get("stock_id")): _effective_sector_key(r)
       for r in frame.to_dict("records")
   }
   ```
   這樣「被動元件-MLCC」最多可選 2 檔、「PCB-IC載板」也最多可選 2 檔，既能精準控管同細分領域風險，又不會被官方粗分類誤卡名額！

#### 改造 3：在 `build_dossier()` 將細分產業與同業資訊餵給 Stage 2 (Vertex AI)
在 `build_dossier()`（約第 196 行）標頭與 `[A. 均線訊號、位階動態與量能結構]` 中加入 `taxonomy_badge`：
```python
    tax_badge = str(row.get("taxonomy_badge", "") or "").strip()
    if tax_badge:
        tech_lines.insert(0, f"  - {tax_badge}")
```

#### 改造 4：在 `format_pick_report()`、`run_pipeline()` 與 `run_rebound_pipeline()` 顯示細分產業與同業
1. 在 `run_pipeline()`（約第 643 行 `selected, skipped = prefilter_candidates(...)` 之後）與 `run_rebound_pipeline()`（約第 863 行 `selected = rebound_df.head(...)` 之後）呼叫：
   ```python
   selected = enrich_with_taxonomy(selected, config, target_date)
   ```
2. 在 `format_pick_report()` 組裝每個 `top_picks` 區塊時（約第 548 行），若 `row.get("taxonomy_badge")` 非空，直接插入一行顯示細分產業與同業：
   ```python
   tax_line = [str(row["taxonomy_badge"])] if row.get("taxonomy_badge") else []
   ```
   並放進 `blocks.append("\n".join([..., price_line, *tax_line, *vol_line, ...]))`。

---

### 5.3 修改 `/stock_ai` 單股戰情室 (`fundamental_analyzer.py` & `stock_ai_command.py`)

當使用者在 Discord 執行 `/stock_ai 2327`（或 22:00 批次生成個股戰情室 HTML）時：
1. 在 `FundamentalAnalyzer.run_full_diagnosis(stock_id, ...)` 或報告組裝處：
   - 呼叫 `TaxonomyService().get_or_create_sync(stock_id, stock_name=..., official_industry=..., sync_sheet=True)` 取得該股的細分產業與同業紀錄 `tax_rec`。
2. **Discord 純文字 Markdown 報告首段注入**：
   - 在 `分析模型：{model_version}` 下一行，直接印出：
     `🏷️ 細分產業：【被動元件】被動元件-MLCC與晶片電阻（MLCC、晶片電阻、電感） ｜ 🔗 相似同業：2492 華新科、6173 信昌電、2472 立隆電`
   - 這讓使用者在手機看到單股報告時，能立刻知道該公司是主動還被動元件，並直接複製同業代號下達 `/stock_ai 2327,2492` 進行多股橫向比較！
3. **GitHub Pages 二合一戰情室 (`{stock_id}_fund.html`) 標頭注入**：
   - 在戰情室網頁頂部的個股資訊橫幅（Banner）中，加入細分產業膠囊標籤（Badge）與相似同業清單。

---

## 6. 單元測試規格 (`tests/test_taxonomy_and_prefilter.py`)

Claude AI 實作完畢後，必須建立 `tests/test_taxonomy_and_prefilter.py` 並通過以下 6 項核心測試案例（使用 `unittest.mock.patch` 隔離外部網路呼叫）：

1. **`test_configured_universe_supports_all_and_4digit_filter`**：
   - 驗證 `industries=["ALL"]` 不會觸發 `ValueError`，且能自動剔除 `3702A`、`3036A` 等非 4 碼特別股與 `excluded_industry_categories`。
2. **`test_dual_track_liquidity_protects_high_price_stocks`**：
   - 測試股價 $500$ 元、當日成交量僅 $400$ 張（但成交金額達 $2$ 億 $\ge 1$ 億台幣門檻）的高價績優股，`passes_dual_track_liquidity()` 必須回傳 `True`（不誤殺）。
   - 測試股價 $15$ 元、當日量 $200$ 張、20日均量 $180$ 張（成交額 $300$ 萬）的殭屍股，必須回傳 `False`。
3. **`test_delisted_auto_detection_after_missing_threshold`**：
   - 模擬某股票連續 $5$ 個交易日不在 TWSE/TPEx 收盤快照中，`update_registry_with_daily_snapshot()` 自動將其標記為 `status == "DELISTED"`，後續自動排除。
4. **`test_sanitize_taxonomy_hallucinated_peers`**：
   - 模擬 `gemini-flash-lite-latest` 回傳 `peers: ["2492 華新科", "9999 虛構科技", "2327 國巨"]`（含不存在代號 `9999` 與自身代號 `2327`）及無效的 `component_nature`，驗證 `sanitize_taxonomy_record()` 能剔除 `9999` 與 `2327`，只保留 `"2492 華新科"`，並將 Enum 導正為合法值。
5. **`test_stage2_sub_industry_cap_allows_different_sub_industries`**：
   - 準備 4 檔官方分類皆為 `"電子零組件業"` 的股票，其中 2 檔 `sub_industry` 為 `"被動元件-MLCC"`，另外 2 檔為 `"PCB-ABF載板"`。
   - 驗證 `alignment_ai_picker.enforce_sector_cap(decision, frame, cap=2)` 能讓這 4 檔全部保留在 `top_picks`（因為細分次產業各自未超過 2 檔），而第 3 檔 `"被動元件-MLCC"` 則會被正確移入 `watchlist`。
6. **`test_rate_limiter_only_throttles_finmind_host`**：
   - 驗證 `_rate_limit_finmind_http_requests(limiter, only_hosts=("finmindtrade.com",))` 不會對 `openapi.twse.com.tw` 或 `tw.stock.yahoo.com` 觸發 `limiter.wait_for_slot()`。

---

## 7. 交付給 Claude AI 的驗收清單 (Definition of Done)

請 Claude AI 完成實作後，依序執行以下指令確認 100% 通過：

```bash
# 1. 執行全套單元測試（含既有測試與新增測試）
uv run python -m unittest discover -s tests

# 2. 乾跑測試 19:00 快取重算與細分產業注入
uv run python finmind_stock_scanner.py recalc-signals --dry-run

# 3. 乾跑測試 22:00 AI 嚴選（驗證細分產業同業限額與報告格式）
uv run python finmind_stock_scanner.py analyze-alignment --dry-run

# 4. 乾跑測試 22:30 型態 A 反彈掃描
uv run python finmind_stock_scanner.py analyze-rebound --dry-run
```