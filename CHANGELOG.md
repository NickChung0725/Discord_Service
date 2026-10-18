# Changelog — 個人智慧助理版本演進與修改紀錄

本文件記錄 `Discord_Service` 的所有重要功能迭代、架構升級與錯誤修正，方便隨時回顧「目前助理新增了哪些能力」。

---

## [v3.2.1] - 2026-10-18
### ✨ 新增功能與介面大升級 (Added & Improved)
* **全方位投資戰情室（基本面財報 ＋ 技術面 K 線）二合一整合 ([`fundamental_analyzer.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/fundamental_analyzer.py), [`chart_generator.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/chart_generator.py))**：
  * 在 [`chart_generator.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/chart_generator.py) 新增 `build_stock_technical_chart_html`，將「近半年日 K 線、17MA/45MA/117MA/189MA 四線均線、成交量、KD(9,3,3)、MACD 與拖曳框選高低均價計算器」無縫嵌入戰情室 HTML。
  * 戰情室頁首新增**懸浮快速導覽列 (Sticky Navbar)**，方便手機與電腦快速跳轉：`[🤖 AI 深度診斷]` ➔ `[📈 技術面 K 線與均線]` ➔ `[📊 基本面六大指標圖表]`。
* **解決「圖例擠在最底部」痛點：改為 6 大獨立互動卡片，每張圖頂部配置專屬圖例**：
  * 將原本 1500px 單一超長 Subplot 畫布重構為 6 張獨立的 Plotly 互動卡片。
  * **每張圖右上角皆有獨立 Legend 圖例**，點擊即可即時開關線條；並啟用 `hovermode="x unified"`（垂直十字對齊懸浮窗），同一月份/季度的所有指標一次對比。
  * 針對雙單位圖表（月營收 vs YoY%、單季 EPS vs TTM EPS、現金流億元 vs 獲利含金量%）配置**獨立左右雙 Y 軸**，徹底解決折線被壓扁問題。
* **貼心「💡 財報與技術專有名詞白話小抄」（琥珀金提示框）**：
  * 在每張圖表頂部直接內嵌白話教學（YoY、三率三升、ROE 資金效率、營業費用率、DIO/DSO/DPO/CCC 存貨與收現天數、單季與近四季 TTM EPS 創高意義、OCF 營業現金流、CapEx 資本支出、FCF 自由現金流、獲利含金量 OCF/NI）。
* **手機好讀與一鍵複製轉傳優化 ([`stock_ai_command.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/stock_ai_command.py))**：
  * `/stock_ai` 回傳格式由不可選取複製的 Discord Embed 改為純文字 Markdown（超過 2,000 字自動智慧分段），手機版長按 1 秒即可「複製文字」轉傳他人。
  * 移除易超時失效的「🔄 重新呼叫 AI 分析」互動按鈕，統一收斂為乾淨直覺的 **`[📊 全方位戰情室（含財報+K線）]`** 連結按鈕。
* **整頓清理舊指令**：
  * 正式從主機器人 [`bot_server.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/bot_server.py) 中卸載舊版單一功能 `/stock` 指令，所有查股需求由功能完整 10 倍的 `/stock_ai` 統一服務。

### 🔧 錯誤修正 (Fixed)
* **修正 Discord 訊息 Markdown 超連結語法 ([`alignment_ai_picker.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/alignment_ai_picker.py))**：
  * 移除網址外層多餘的 `<>`（`[文字](<url>)` ➔ `[文字](url)`），解決 Discord Markdown 無法辨識導致變成純文字的問題；並在 Webhook 帶入 `"flags": 4` (`SUPPRESS_EMBEDS`)，避免超連結產生巨型預覽卡片洗版。
* **修正 `fundamental_analyzer.py` 缺失 `asyncio` 與 `logger` 模組宣告**。
* **修正 `discord.py` 2.7+ `view=None` 造成的 `TypeError`**。

---

## [v3.2.0] - 2026-10-18
### ✨ 新增功能 (Added)
* **每日 22:00 強勢股財報批次分析與 AI 嚴選進場決策系統 ([`alignment_ai_picker.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/alignment_ai_picker.py))**：
  * 新增 CLI 子命令 `python finmind_stock_scanner.py analyze-alignment --config finmind_scanner.json`（支援 `--date`、`--force-ai`、`--dry-run`）。
  * 自動讀取當日 19:00 產出之 `{date}_alignment_signals.csv`，結合 `MA_Signal_Tracker` 的「當日新進 (`is_new`)」、「連續符合天數 (`streak`)」與「每日執行狀況」，並在超過候選上限（預設 25 檔）時依「新進/剛突破 ➔ 17MA 上揚 ➔ 17/45MA 差距最小」自動預篩。
  * 新增 `systemd` 排程設定 [`deploy/finmind-ai-pick.service`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/deploy/finmind-ai-pick.service) 與 [`deploy/finmind-ai-pick.timer`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/deploy/finmind-ai-pick.timer)（週一至週五 22:00 自動執行）。
* **法說會未來展望定向檢索與「財報領先指標交叉佐證」([`fundamental_analyzer.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/fundamental_analyzer.py))**：
  * 新增 `fetch_earnings_call_and_outlook(stock_id, stock_name)`：透過 Google News RSS 定向檢索近 180 天內該公司之「法說、展望、訂單、產能、資本支出、毛利率、財測」報導與摘要（純 Python 執行，0 AI 額度消耗）。
  * 升級單股診斷 Prompt (`PROMPT_TEMPLATES["full_diagnosis"]`)：要求 AI 先提煉法說展望 (Step A)，再強制對照 **表 2 合約負債、表 3 資本支出、表 1 毛利率/營益率、表 5 月營收 YoY、表 6 法人買賣超** 進行真實性驗證 (Step B)，給出 `🟢高度可信 / 🟡待驗證 / 🔴存疑` 評級。
* **雙層 Gemini 模型分工與免費額度 429 自動降級保護 ([`ai_weather.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/ai_weather.py))**：
  * 第一階段（單股財報＋法說驗證）維持使用高配額 `gemini-flash-lite-latest`（15 RPM / 500 RPD）。
  * 第二階段（22:00 跨股綜合評選，每晚僅 1 次）指定使用高階推理模型 `gemini-3.8-flash`（5 RPM / 20 RPD），若遇 `429 RESOURCE_EXHAUSTED` 或 `503` 自動無縫降級至 `gemini-flash-lite-latest`。
* **Google Sheets 第 10 張工作表 `08_AI_Daily_Picks` ([`fundamental_config.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/fundamental_config.py))**：
  * 自動記錄每日 22:00 AI 嚴選出的 `TOP_PICK`、`WATCHLIST` 與 `AVOID` 清單、推薦時收盤價、17MA/45MA 支撐防守價、單股體質分、總決策推薦分與法說展望評級，方便長期回測追蹤。

### 🔧 優化與修正 (Changed & Fixed)
* **結構化 `ai_summary_json`**：單股 AI 輸出新增 `===SUMMARY_JSON===` 結構化區塊，完整保留結論、評分、亮點、風險、ROE/FCF、籌碼、法說驗證與技術面觀察，解決原本 `ai_text[:1500]` 硬截斷導致後半段資訊遺失的問題。
* **批次模式 Google Sheets 記憶體快取就地更新 (`enable_batch_mode`)**：修正每次寫入即清空整張表快取的問題，避免批次同步 20+ 檔股票時觸發 Google Sheets 每分鐘 60 次讀取上限 (`429 Too Many Requests`)。
* **修正單股 AI 快取指紋 (`cache_period`)**：移除 `check_and_sync_stock` 提前寫入 `ai_cache_day` 的邏輯，改以純資料水位 (`last_fin_qtr` + `last_rev_ym` + `max_div_raw_date` + `max_chip_raw_date`) 判定快取命中，且空白摘要不再誤判為命中。
* **FinMind 限流器支援 Host 過濾 (`only_hosts`)**：22:00 批次執行時僅對 `finmindtrade.com` 計入 FinMind 滑動視窗配額，不誤擋 Google Sheets、Gemini 與 Discord 請求。

---

## [v3.1.5] - 2026-10-16
### ✨ 新增功能 (Added)
* **五線均線強勢排列升級 (`MA3 / MA17 / MA45 / MA117 / MA189`) ([`finmind_stock_scanner.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/finmind_stock_scanner.py))**：
  * 新增 `3MA` 條件（`MA3` 必須 `>=` 所有均線），確保短線動能維持強勢。
  * 新增 `describe_ma17_trend`：比對今日 `17MA` 與前 1 日、前 5 日變化，自動分類為 `📈 連漲上揚`、`↗️ 轉折向上`、`➡️ 走平整理`、`↘️ 漲勢收斂`、`📉 持續下彎`。
* **Google Sheets `MA_Signal_Tracker` 訊號追蹤工作表**：
  * 每日 19:00 掃描後自動與歷史紀錄比對，計算「當日新進（🆕 是 / 🔄 否）」、「連續天數 (`streak`)」與「每日執行狀況（如 🔥 轉強突破、⚠️ 訊號收斂）」，並透過 Google Sheets API 自動套用條件格式底色與欄寬美化。
* **單股基本面 AI 診斷整合「表 7：近期日線與均線排列動態」([`fundamental_analyzer.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/fundamental_analyzer.py))**：
  * `/stock_ai` 自動搜尋本地最新 `prices.csv`，將個股最新收盤價、五條均線與 `17MA` 走勢注入 AI 數據包。

---

## [v3.1.0] - 2026-10-14
### ✨ 新增功能 (Added)
* **`/stock_ai` 台股基本面三大財報、股利與籌碼智慧快取分析系統 ([`stock_ai_command.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/stock_ai_command.py), [`fundamental_cache.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/fundamental_cache.py), [`fundamental_analyzer.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/fundamental_analyzer.py))**：
  * 建立 Google Sheets 8 張核心財報與籌碼分頁（`00_Watchlist_Master` ~ `07_Chip_Summary`），支援法定截止日水位檢查與回補 1 期增量更新。
  * 實作現金流量表 Q2/Q3/Q4 累季轉單季拆解演算法、資產負債表平衡驗證與自由現金流驗算。
  * 支援單股完整體質診斷、特定財報問題追問 (`prompt`)、以及多股橫向比較（如 `/stock_ai symbol:2357,2330`，最多 4 檔）。
  * 自動生成 Plotly 6 宮格互動式財報戰情室網頁 (`{stock_id}_fund.html`) 並上傳至 GitHub Pages，於 Discord 提供三顆互動按鈕。

---

## [v2.0.0] - 早期基礎版本
* **`/stock` 技術面互動式 K 線圖 ([`chart_generator.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/chart_generator.py), [`github_uploader.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/github_uploader.py))**：整合 FinMind 日 K 與 Yahoo Finance 成交量，繪製 MA/KD/MACD 互動圖表並自動發布至 GitHub Pages。
* **每日 19:00 五大產業均線批次掃描器 ([`finmind_stock_scanner.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/finmind_stock_scanner.py))**：含 SQLite 滑動視窗限流器（540 次/小時）與 Discord 自動分段推播。
* **生活與系統助理 ([`ai_weather.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/ai_weather.py), [`bot_server.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/bot_server.py))**：提供 `/ai` 萬用問答、`/weather` 即時天氣、每日 08:00 早安台北天氣排程、每 6 小時伺服器心跳通知與 `/card`、`/ping`、`/say`、`/roll`、`/buttons` 工具指令。
