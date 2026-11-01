# Discord_Service — 個人智慧生活與台股量化分析助理

這是一個部署於 **Google Cloud Platform (GCP) Compute Engine** 的全方位 Discord 機器人與台股自動化量化分析服務。系統整合了 **FinMind 台股技術面與基本面數據**、**Google Sheets 12 分頁智慧快取資料庫**、**台新證券 Nova API 個人帳務與 AI 持股健診中心 (獨立 Google Sheet 4 分頁)**、**Plotly 互動式看盤與財報戰情室自動發布 (GitHub Pages)**、**Google Gemini AI 雙層模型智慧診斷** 以及 **Open-Meteo 每日氣象推播**，透過 `systemd` 實現 24 小時常駐與每日自動化選股及帳務排程。

---

## ⏰ 助理的一天：自動化排程時間軸 (Asia/Taipei)

| 時間 / 頻率 | 任務名稱 | 觸發機制 | 執行內容與產出 |
| :--- | :--- | :--- | :--- |
| **每 6 小時** | **伺服器存活心跳** | `bot_server.py` (`scheduled_task`) | 向 `GCP_VM_SERVER_CHANNEL_ID` 發送系統時間心跳通知，確認 GCP VM 與 Bot 正常運作。 |
| **每日 08:00** | **台北早安氣象報** | `bot_server.py` (`daily_weather_task`) | 串接 Open-Meteo API，自動推播當日台北即時溫度、體感、高低溫、降雨機率與紫外線指數至 `ANNOUNCE_CHANNEL_ID`。 |
| **週一至週五 11:00** | **台新證券個人帳務同步與 AI 持股買入均價健診** | `systemd` (`taishin-statement.timer`) | **SASD v5.1.0 個人帳務中心**：<br>1. 透過 `taishin_sdk` 同步近 7 日對帳單 (`account_statment`)、成交明細 (`filled_history`)、今日應收付交割款、分戶帳餘額（自動偵測 `⚠️餘額不足` 預警）與已實現損益至獨立 Google Sheet (`TAISHIN_GSHEET_SPREADSHEET_ID`) 的 `01_Account_Statement` 與 `03_Settlement_And_PnL`<br>2. 同步最新庫存 (`inventories`)，若 API 未回傳買入均價則自動由 `01_Account_Statement` 歷史買進紀錄加權計算<br>3. 跨表串聯本地 `daily/{date}_prices.csv` 五線均線與公用財報快取 (`00_Watchlist_Master`)，呼叫 `STAGE1_MODEL`（備援 `VERTEX_STAGE1_MODEL`）針對每檔持股產出買入均價安全墊評估、操作標籤 (`🟢 順勢續抱` / `💰 分批停利` / `🎯 逢低加碼` / `🚨 觸及停損` / `🟡 觀望防守`)、保本/移動停利價與目標價，覆寫 `02_Portfolio_Holdings` 並推播至 `TAISHIN_ALERT_CHANNEL_ID`（未設定則 fallback 至 `STOCK_CHANNEL_ID`）。 |
| **週一至週五 13:15** | **0050 每週不定期定額智慧尋底評估** | `systemd` (`taishin-dca-0050.timer`) | **SASD v5.1.0 0050 智慧尋底引擎**：<br>1. 檢查本週 (`YYYY-Www`) 是否已買入 `0050`<br>2. **週一至週四**：若出現「跌破 17MA 負乖離 <= -1.5%」、「回踩 45MA 季線」、「RSI(14) <= 40 或 KD_K <= 30」、「近 10 日高點回落 >= 2.5%」任一低點訊號，立即觸發買進訊號<br>3. **週五 13:15**：若週一至週四皆未觸發，強制執行本週保底定投（依 `dip_score` 0~100 動態調整 `0.6x ~ 1.5x` 股數）<br>4. 預設 `TAISHIN_ENABLE_TRADING=false` 僅寫入 `04_DCA_0050_Tracker` 並推播 Discord 買入建議；開啟 `TAISHIN_ENABLE_TRADING=true` 且帶入 `--confirm-live-order` 時才呼叫 `sdk.stock.place_order` 下盤中零股 (`MarketType.IntradayOdd`)。 |
| **週一至週五 19:00** | **第一階段：台股五線均線＋黃金量能＋大盤紅綠燈掃描** | `systemd` (`finmind-scan.timer`) | **全市場動態掃描（SASD v5.0）**：`industries=["ALL"]`，先以 TWSE/TPEx 盤後快照做 Stage 1「雙軌量能守門員」（當日量 ≥ 1,000 張 或 20 日均量 ≥ 1,000 張 或 成交金額 ≥ 1 億即放行，≥ 3,000 張為高流動性），僅對通過者呼叫 FinMind 日線（並自動偵測下市股）；掃描完成後為入榜訊號股補齊「細分產業＋相似同業」標籤（`08_Stock_Taxonomy`）並附於快報。篩選符合 `MA3 >= 各線`、`MA45 > MA117 > MA189` 且處於 `17/45MA 即將金叉 (-1.5%~0%)` 或 `剛金叉起漲 (0%~+2.5%)` 之強勢股，並同步計算**黃金量能三指標**與**大盤環境紅綠燈（🟢綠燈 / 🟡黃燈 / 🔴紅燈）**：<br>1. 產出本地 CSV：`daily/{date}_prices.csv`、`daily/{date}_alignment_signals.csv`、`daily/{date}_failures.csv` 與 `daily/{date}_market_regime.json`<br>2. 同步至 Google Sheets `MA_Signal_Tracker`（設定鍵：`sync_alignment_to_gsheet`）<br>3. 推播精簡版均線快報（含大盤紅綠燈狀態）至 `STOCK_CHANNEL_ID`。 |
| **週一至週五 22:00** | **第二階段：右側強勢股財報＋法說會驗證＋AI 嚴選進場** | `systemd` (`finmind-ai-pick.timer`) | 讀取 19:00 產出之 `alignment_signals` 清單：<br>1. **增量同步財報與籌碼**：自動補齊月營收、三大財報、衍生財務比率、股利與法人買賣超<br>2. **Stage 1 單股 Step 1~7 審計（付費版 `STAGE1_MODEL`，預設 `gemini-3.8-flash`，不再降級 flash-lite）**：Stage 0 先以純 Python 清洗 RSS 雜訊（剔除盤中標題黨/逾 180 天舊聞/空殼摘要）、**日量 < 300 張或「營益率 < 0 且合約負債 < 1 億」者直接硬性攔截歸入 `avoid`（不送 AI）**；其餘先深度拆解再評分，產出 1~4 點動態亮點/風險、`trend_tags`、`trade_plan`（進場觸發/目標價/停損/失效條件）之 `ai_summary_json`（經 Python Sanitizer 防呆校正，內含 `model_version`）並在 Discord/戰情室顯示「分析模型」<br>3. **Stage 2 紅藍軍辯證與 Top 5 嚴選（付費版 Vertex AI：`VERTEX_MODEL`，ARGON）**：候選 > 15 檔時依 Stage 1 評分保留前 12 檔，並在 Python 端先做「天量過熱(量比>4.5x) / 已超漲(>17MA+12%)」降權處理；同時**同產業最多只取 3 檔進入 Stage 2 Prompt**（確保決賽名單產業多元）。最終 `top_picks` 仍維持**同產業最多入選 2 檔（Python 強制，超額移入 watchlist）**；每檔輸出 `entry_zone_and_trigger`、`bull_thesis`、`bear_case_and_invalidation`；固定讀取 `.env` 之 `VERTEX_MODEL` 呼叫 GCP Vertex AI（僅在 Vertex AI 內自動重試，不降級至免費版以確保決策品質一致），並在報告顯示 `response.model_version` 作為「分析模型」<br>4. **AI 呼叫日誌（`finmind_reports/logs/YYYY-MM-DD_ai_calls.log`）**：每次 AI 呼叫會以「單次一段」方式追加寫入完整 prompt 與完整 output，供事後追查與除錯<br>5. **全候選股戰情室發布與回填**：所有完成 AI 深度分析之標的（不限於嚴選 TOP_PICK，包含 WATCHLIST 與 AVOID）皆會生成 HTML 發布至 GitHub Pages，並將 `war_room_url` 同步回填至 `00_Watchlist_Master` 與寫入 `AI_Daily_Picks`、本地 JSON/MD，最後推播 AI 嚴選報告至 `STOCK_CHANNEL_ID`。 |
| **週一至週五 22:30** | **第三階段：左側「型態 A 高點回跌回踩長均線反彈」掃描與彈性 AI 診斷** | `systemd` (`finmind-rebound-ai.timer`) | 從 `daily/{date}_prices.csv` 讀取近 60 個交易日數據，捕捉績優股急跌回踩長均線之均值回歸契機：<br>1. **型態 A 五大濾網**：`MA45 >= MA117 > MA189`、近 60 日高點回跌達位階門檻（30~150跌>=10元、150~500跌>=30元、500~1000跌>=100元、>=1000跌>=300元）且跌幅 `>= 10%`、收盤回踩 `45MA` 或 `117MA`（`-1.5% ~ +2.5%`）、反彈至 `17MA` 空間 `>= 5%`、K 線收盤強度 `(Close-Min)/(Max-Min) >= 0.55`<br>2. 產出本地 `daily/{date}_rebound_signals.csv` 與 `daily/{date}_rebound_failures.csv`<br>3. 寫入獨立 Google Sheets 工作表 `Rebound_Watchlist`<br>4. **彈性決策點**：先推播「今日符合反彈條件標的總數」與清單至 Discord；支援第一階段觀測標的數量（預設不自動跑 AI），或透過 `--run-ai` / `rebound_auto_run_ai`（當檔數落於 3~15 檔合理區間）無縫啟動 AI 深度診斷與推播。<br>5. **Stage 2 Prompt 反彈模式分流**：22:30 反彈清單在跨股嚴選時不沿用「17MA 下彎扣分」規則，改以「45MA/117MA 守穩 + 收盤強度 >= 0.55 + 反彈至 17MA 空間 >= 5%」檢核，且第一目標價優先設為 17MA 反壓。 |

---

### 💬 Discord 互動斜線指令一覽 (Slash Commands)
 
| 指令 | 參數說明 | 功能與產出說明 |
| :--- | :--- | :--- |
| **`/stock_ai`** | `symbol` (單股如 `2357` 或多股如 `2357,2330`，最多 4 檔)<br>`prompt` (選填，特定問題)<br>`force_refresh` (選填，預設 `False`)<br>`tier` (選填：`標準 (3.8 Flash)`〔預設〕或 `大師 (ARGON)`〔強制走 Vertex AI Stage1：`VERTEX_STAGE1_MODEL`〕) | **【📊 全方位投資戰情室（基本面財報 ＋ 技術面 K 線）】**<br>• **手機好讀一鍵複製**：以純文字 Markdown 發送（長按 1 秒即可複製轉傳他人）。<br>• **單股體質診斷（SASD v4.0 Step 1~7，先審計後評分）**：包含 0~100 體質評分、1~4 點動態亮點與風險、趨勢標籤，並附 **交易計畫 Embed**（戰術定位、進場觸發條件、第一/二目標價、嚴格停損價、紅軍失效退場條件）；日量 < 300 張或本業虧損無合約負債者以「🚨 系統硬傷警示」注入診斷（軟性警示，不擋股票）、三率/CCC/FCF 拆解、股利與法人籌碼、表 7 均線與量能結構、法說展望真實性佐證；並在首行顯示 **分析模型：{response.model_version}**。<br>• **二合一戰情室網頁**：發布全新戰情室 (`charts/{stock_id}_fund.html`)，內嵌**近半年技術面 K 線與均線 (17/45/117/189MA) + 成交量 + KD + MACD**，以及**基本面六大財務趨勢圖（每張圖正上方皆有獨立圖例與白話名詞小抄）**；戰情室頁面亦顯示「分析模型」。<br>• **多股橫向比較 (`2357,2330`)**：改走付費 Vertex AI（Stage1：`VERTEX_STAGE1_MODEL`）進行橫向比較與結論輸出（同樣顯示「分析模型」）。<br>• **專題追問 (`prompt`)**：改走付費 Vertex AI（Stage1：`VERTEX_STAGE1_MODEL`）針對使用者問題引用財報數字精準解答（同樣顯示「分析模型」）。 |
| **`/ai`** | `prompt` (問題內容) | **【Gemini AI 萬用問答】**<br>呼叫 Google Gemini 模型回答各式投資、程式或日常問題（超過 2,000 字自動分段回覆）。 |
| **`/weather`** | 無 | **【台北即時天氣查詢】**<br>即時查詢 Open-Meteo 台北地區氣溫、體感、天氣狀況、風速、降雨機率與今日紫外線。 |
| **`/card`** | 無 | **【系統狀態卡片】** 顯示機器人名稱、API 延遲 (ms) 與伺服器成員數。 |
| **`/ping`** | 無 | **【連線延遲測試】** 回應 `Pong! 🏓` 與毫秒延遲（或在頻道直接輸入文字 `ping`）。 |
| **`/say`** | `message` | **【文字複誦】** 讓機器人代為發送指定訊息。 |
| **`/roll`** | `max_number` (預設 100) | **【隨機擲骰】** 擲出 `1 ~ max_number` 之間的隨機數字。 |
| **`/buttons`** | 無 | **【互動按鈕測試】** 發送包含按讚與警告按鈕之互動元件。 |

---

## 🗄️ Google Sheets 智慧快取資料庫架構 (共 12 張工作表)

系統採用 Google Sheets 作為雲端結構化資料庫（定義於 [`quant/fundamental_config.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/quant/fundamental_config.py) 的 `SHEET_SCHEMAS`），自動建立並維護以下 12 張分頁：

| 分頁名稱 | 定位與儲存內容 | 更新時機與快取策略 |
| :--- | :--- | :--- |
| **`00_Watchlist_Master`** | **主控索引與 AI 快取表**：記錄股名、產業、各表最新資料水位 (`last_rev_ym`, `last_fin_qtr`, `max_div_raw_date`, `max_chip_raw_date`)、探測冷卻時間 (`last_probe_at`)、結構化 `ai_summary_json` 與 `war_room_url`。 | 每次 `/stock_ai` 或 22:00 / 22:30 批次分析時自動維護。 |
| **`01_Monthly_Revenue`** | **月營收動能表**：單月營收、`mom_pct` (月增率)、`yoy_pct` (年增率)。 | 依每月 10 日法定水位增量抓取（自動回頭重抓 1 期以吸收更正公告）。 |
| **`02_Income_Statement`** | **綜合損益表 (單季)**：營收、成本、毛利、營業費用、營業利益、業外收支、稅前/稅後淨利、母公司淨利、EPS。 | 依法定財報截止日（5/15、8/14、11/14、3/31）增量抓取（回補 1 季）。 |
| **`03_Balance_Sheet`** | **資產負債表 (季底)**：現金、金融資產、應收帳款、存貨、不動產廠房設備(PPE)、短長期借款、合約負債、應付帳款、股本、保留盈餘、權益總額（含資產=負債+權益自動驗算）。 | 同季報水位增量同步。 |
| **`04_Cash_Flow`** | **現金流量表 (自動累季轉單季)**：自動將 FinMind Q2/Q3/Q4 累計值拆解為**單季真實流量**（含折舊攤銷、AR/存貨/AP 營運資金變動、營業現金流 OCF、資本支出 CapEx、自由現金流 FCF）。 | 同季報水位增量同步。 |
| **`05_AI_Metrics_Wide`** | **AI 衍生財務比率寬表**：後端 Pandas 精確算妥之毛利率、費用率、營益率、業外佔比、淨利率、TTM EPS、年化 ROE、DIO/DSO/DPO/CCC 週轉天數、OCF/NI 含金量、EBITDA、淨負債比、流動/速動比、利息保障倍數。 | 每次季報更新時由系統後端自動重算寫入（嚴禁 AI 自行心算）。 |
| **`06_Dividend_Policy`** | **歷年股利政策表**：民國年自動轉西元年、現金股利、股票股利、總股利、現金股利總額(億)、年度 EPS、配息率 (`payout_ratio_pct`)、除息日。 | 30 天 TTL 增量更新。 |
| **`07_Chip_Summary`** | **三大法人與外資籌碼表**：外資持股比例、20 日持股變化、外資/投信/自營商及三大法人合計之近 5 日與近 20 日買賣超（張）。 | 每日 TTL（>= 1 天）增量更新。 |
| **`08_Stock_Taxonomy`** | **細分產業與同業知識庫**（SASD v5.0）：`stock_id`、`stock_name`、`official_industry`、`sector_group`、`sub_industry`（細分次產業，供 22:00 同業限額分群）、`component_nature`（主動/被動元件等）、`supply_chain_role`、`core_products`、`theme_tags`、`peers`（經 Python 真實代號防幻覺校驗）、`updated_at`。同步本地快取 `finmind_reports/.state/stock_taxonomy.json`。 | TTL 90 天；由 `GEMINI_MODEL`（免費 flash-lite）搭配 Yahoo 業務簡介產生，入榜訊號股與 `/stock_ai` 查詢時補齊。 |
| **`MA_Signal_Tracker`** | **19:00 均線與量能強勢股追蹤表**：記錄每日符合五線排列之個股、標註「🆕 是（新進榜）/ 🔄 否（持續符合）」、「連續天數」、「每日執行狀況」、「17MA 近期走勢」、**「成交量(張) / 5日均量(張) / 20日均量(張) / 量能狀態(量比)」**並自動套用條件格式底色（放量點火自動亮橘紅加粗、低量冷門灰字警示）。 | 每日 19:00 `scan-all` 自動同步。 |
| **`AI_Daily_Picks`** | **22:00 AI 嚴選進場歷史追蹤表**：記錄每日 AI 評選之 `TOP_PICK`、`WATCHLIST`、`AVOID` 清單、推薦當下收盤價、17MA/45MA 支撐價、單股財報分、總決策推薦分、法說展望評級與進場理由，供長期回測勝率。 | 每日 22:00 `analyze-alignment` 自動寫入。 |
| **`Rebound_Watchlist`** | **22:30 型態 A 長均線回踩反彈清單**：記錄 `pk_key`、`stock_id`、`stock_name`、`industry_category`、`scan_date`、`close_price`、`high_60d`、`drop_amount`、`drop_pct`、`support_ma`、`support_price`、`target_ma17`、`potential_return_pct`、`volume_status`、`market_light`、`ai_summary_json`、`war_room_url`。 | 每日 22:30 `analyze-rebound` 自動同步。 |

---

## 🏦 台新證券個人帳務與 AI 持股健診專屬 Google Sheets 架構 (獨立 4 張工作表)

個人對帳單與持股庫存完全隔離於獨立 Google Sheet（`TAISHIN_GSHEET_SPREADSHEET_ID`，定義於 [`taishin/taishin_config.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/taishin/taishin_config.py) 的 `TAISHIN_SHEET_SCHEMAS`），絕不污染公用 12 張財報快取表，且嚴禁上傳至 GitHub Pages：

| 分頁名稱 | 定位與去重主鍵 (Unique Key) | 更新時機與策略 |
| :--- | :--- | :--- |
| **`01_Account_Statement`** | **全歷史交易對帳單與成交總表**<br>Key: `f"{trade_date}_{order_no}_{stock_no}_{action}_{filled_qty}_{filled_price}"` | `backfill-history` 30 天分塊全量回補（對帳單 `STATEMENT` ＋ 成交明細交叉補遺 `FILLED_HISTORY`）與每日 11:00 增量批次寫入（自動保護 ETF 如 `'0050` 前導零）。 |
| **`02_Portfolio_Holdings`** | **目前持股庫存與 AI 均價操作建議表**<br>Key: `f"{stock_no}_{order_type}"` | 每日 11:00 `sync-daily` 整表覆寫最新庫存、買入均價（若 API 未帶則由 `01_Account_Statement` 加權回算）、五線均線與 AI 持股診斷結果。 |
| **`03_Settlement_And_PnL`** | **交割款預警與歷史已實現損益表**<br>Key: `f"{record_date}_{record_type}_{stock_no}_{qty}_{net_amount}_{realized_pnl}"` | `backfill-history` 回補歷史已實現損益與交割紀錄，及每日 11:00 增量更新今日交割款與分戶帳餘額不足預警 (`✅正常` / `⚠️餘額不足`)。 |
| **`04_DCA_0050_Tracker`** | **0050 每週不定期定額智慧尋底表**<br>Key: `eval_date` | 週一至週五 13:15 `eval-dca-0050` 評估寫入（記錄 `dip_score`、觸發原因、`DRY_RUN` / `LIVE_ORDER` 與建議股數）。 |

---

## 📁 專案目錄結構

```text
Discord_Service/
├── bot_server.py                # [常駐主入口] 綁定伺服器 (Guild) 之 Discord 機器人入口（含 Slash 指令、08:00 天氣與 6h 心跳排程）
├── finmind_stock_scanner.py     # [量化主入口] 19:00 五線順勢掃描、22:00 AI 選股、22:30 型態A反彈掃描、大盤紅綠燈與 CLI 入口
├── taishin_portfolio_service.py # [帳務主入口] 11:00 台新帳務同步、全歷史對帳單回補、AI 持股均價診斷、13:15 0050 智慧定投與 CLI 入口
├── services/                    # [Discord 指令與共用外部服務層]
│   ├── __init__.py
│   ├── ai_service.py            # 雙軌 AI 服務：免費版 Google AI Studio 與付費版 Vertex AI 呼叫封裝與 /ai 指令
│   ├── weather_service.py       # Open-Meteo 台北即時與預報天氣抓取、格式化與 /weather 指令
│   ├── github_uploader.py       # 透過 GitHub Contents API 將 HTML 圖表與戰情室上傳至 GitHub Pages
│   ├── stock_ai_command.py      # /stock_ai 指令註冊（單股診斷、專題問答、多股比較與戰情室互動按鈕）
│   └── bot.py                   # /stock 技術面 K 線指令註冊與獨立啟動入口
├── quant/                       # [台股量化分析、財報快取與 AI 選股核心引擎]
│   ├── __init__.py
│   ├── fundamental_config.py    # 12 張 Google Sheets Schema、會計科目錨點與法人級 AI Prompt 模板
│   ├── fundamental_cache.py     # Google Sheets 增量同步、累季轉單季、衍生指標計算與批次記憶體快取管理
│   ├── fundamental_analyzer.py  # 單股 Step 1~7 財報診斷、新聞雜訊清洗、結構化 ai_summary_json 與戰情室發布
│   ├── alignment_ai_picker.py   # 22:00/22:30 訊號預篩、批次財報同步、Vertex AI 跨股紅藍軍辯證總決策與推播
│   ├── taxonomy_service.py      # TWSE/TPEx 盤後快照雙軌量能預篩、下市偵測与 08_Stock_Taxonomy 細分產業知識庫
│   └── chart_generator.py       # FinMind 日 K + Yahoo 成交量 + MA/KD/MACD Plotly 互動圖表生成器
├── taishin/                     # [台新證券 Nova API 內部模組]
│   ├── __init__.py
│   ├── taishin_config.py        # 個人帳務 Google Sheet Schema (4張表)、前導零保護與 AI 持股健診 Prompt 模板
│   └── taishin_client.py        # TaishinSDK 登入、PyO3 物件正規化、動態日期簽章探測、30天分塊與 0050 下單安全閘門
├── docs/                        # [開發參考文件]
│   └── llms.txt                 # 台新/富果 Nova API 參考文件索引
├── scripts/                     # [開發與診斷輔助腳本]
│   └── test_log.py              # 本地 Google AI Studio / Vertex AI 連線測試腳本
├── certs/                       # [憑證與金鑰目錄] 存放台新 .pfx 憑證與 Google sheetbot.json (列入 .gitignore 嚴禁版控)
├── finmind_scanner.json         # [掃描設定檔] 掃描市場/產業、排除清單、API 速率限制、19:00 / 22:00 / 22:30 排程開關設定
├── deploy/                      # [systemd 部署] Linux 常駐與定時排程設定檔
│   ├── taishin-statement.service  # 11:00 台新帳務同步與 AI 持股健診服務
│   ├── taishin-statement.timer    # 週一至週五 11:00 定時器
│   ├── taishin-dca-0050.service   # 13:15 0050 智慧尋底定投評估服務
│   ├── taishin-dca-0050.timer     # 週一至週五 13:15 定時器
│   ├── finmind-scan.service       # 19:00 均線掃描服務
│   ├── finmind-scan.timer         # 週一至週五 19:00 定時器
│   ├── finmind-ai-pick.service    # 22:00 AI 財報分析與選股服務
│   ├── finmind-ai-pick.timer      # 週一至週五 22:00 定時器
│   ├── finmind-rebound-ai.service # 22:30 型態 A 長均線回踩反彈掃描與彈性 AI 診斷服務
│   └── finmind-rebound-ai.timer   # 週一至週五 22:30 定時器
├── tests/                       # [單元測試] 涵蓋氣象/AI、均線與反彈掃描、大盤紅綠燈、財報快取、AI 選股與台新模組之自動化測試
├── pyproject.toml               # [套件定義] uv 專案依賴設定檔
├── uv.lock                      # [套件鎖定] uv 環境版本鎖定檔
├── CHANGELOG.md                 # [變更日誌] 記錄每次功能迭代與架構演進歷史
└── AGENTS.md                    # [AI 開發守則] 記錄本地 uv 環境路徑與文件自動同步規範
```

---

## ⚙️ 環境變數設定 (`.env`)

```ini
# 1. Discord Bot 與頻道設定
BOT_TOKEN=你的_Discord_Bot_Token
DISCORD_TOKEN=你的_Discord_Bot_Token
GUILD_ID=你的_Discord_伺服器_ID
ANNOUNCE_CHANNEL_ID=每日08:00天氣推播頻道ID
STOCK_CHANNEL_ID=每日19:00與22:00股票推播頻道ID
GCP_VM_SERVER_CHANNEL_ID=每6小時心跳通知頻道ID

# 2. 雙軌 AI 設定（Google AI Studio 免費版 ＋ GCP Vertex AI 付費版）
# 2-1. Google AI Studio（免費版：用於 /ai、/stock_ai 與單股基本面財報診斷）
GEMINI_API_KEY=你的_Gemini_API_Key
GEMINI_MODEL=gemini-flash-lite-latest

# （SASD v4.0）Stage 1 單股診斷（22:00 排程）預設先走 Google AI Studio（可能排隊），使用 STAGE1_MODEL；
# 若重試後仍失敗，會自動升級改走 Vertex AI（VERTEX_STAGE1_MODEL）以確保排程可完成。
STAGE1_MODEL=gemini-3.8-flash
# STAGE1_FALLBACK_MODEL=            # （選填）Google AI Studio 端的備援模型；如不填則只重試不降級
# 並且每次 AI 呼叫會把完整 Prompt/回覆寫入 finmind_reports/logs/YYYY-MM-DD_ai_calls.log

# 2-2. GCP Vertex AI（付費版）
# - Stage 1（/stock_ai 專題問答、多股比較、以及 22:00 單股診斷在免費層失敗時的升級備援）：VERTEX_STAGE1_MODEL
# - Stage 2（22:00 / 22:30 跨股彙總決策選股）：VERTEX_STAGE2_MODEL
# - VERTEX_MODEL：相容舊版設定，當 stage 專用變數未設定時才會 fallback
GOOGLE_CLOUD_PROJECT_ID=你的_GCP_Project_ID
GOOGLE_CLOUD_LOCATION=us-central1
VERTEX_API_KEY=你的_Vertex_API_Key
VERTEX_MODEL=gemini-2.5-flash-lite
VERTEX_STAGE1_MODEL=gemini-3.8-flash
VERTEX_STAGE2_MODEL=gemini-2.5-pro

# 3. FinMind 台股數據 API 設定
FINMIND_API_TOKEN=你的_FinMind_API_Token

# 4. GitHub Pages 圖表自動發布設定
GITHUB_TOKEN=你的_GitHub_PAT
GITHUB_REPO=你的帳號/你的儲存庫名稱
GITHUB_BRANCH=main

# 5. Google Sheets 財報快取資料庫設定
GOOGLE_APPLICATION_CREDENTIALS=certs/sheetbot.json
GSHEET_SPREADSHEET_ID=你的_Google_Sheet_ID
FINMIND_PROBE_COOLDOWN_HOURS=6
AI_WINDOW_QUARTERS=8
AI_WINDOW_MONTHS=12
CHART_WINDOW_QUARTERS=16
CHART_WINDOW_MONTHS=36

# 6. 台新證券 Nova API 與個人帳務中心設定 (SASD v5.1.0)
TAISHIN_PERSON_ID=你的身分證字號
TAISHIN_PASSWORD=你的台新登入密碼
TAISHIN_CERT_PASSWORD=你的憑證密碼
TAISHIN_CERT_PATH=certs/taishin_cert.pfx
TAISHIN_ACCOUNT_NO=

# 個人對帳單與庫存專屬 Google Sheet ID (與公用財報 GSHEET_SPREADSHEET_ID 徹底分離)
TAISHIN_GSHEET_SPREADSHEET_ID=你的個人帳務專用_Google_Sheet_ID

# 帳務與持股健診推播頻道 ID (若未填則 fallback 至 STOCK_CHANNEL_ID)
TAISHIN_ALERT_CHANNEL_ID=

# 交易安全開關與 0050 每週不定期定額設定 (預設 false 僅推播訊號不真下單)
TAISHIN_ENABLE_TRADING=false
DCA_0050_WEEKLY_BUDGET_TWD=10000
```

---

## 🚀 常用執行與測試指令

### 1. 本機 Windows PowerShell 開發與測試

```powershell
# 設定此專案的 uv 虛擬環境與 UTF-8 輸出編碼
$env:UV_PROJECT_ENVIRONMENT = "C:\Users\Hung-PC\.venvs\Discord_Service"
$env:PYTHONIOENCODING = "utf-8"

# 執行完整單元測試
uv run python -m unittest discover -s tests

# 手動執行 19:00 全產業均線掃描
uv run python finmind_stock_scanner.py scan-all --config finmind_scanner.json

# 秒級重算（調參／回測用，不呼叫 FinMind）：讀當日 prices.csv 重新套用均線與量能規則
# 初始化全市場 stock_universe_registry.json（近 30 日日線、20 日均量、下市標記；支援斷點續傳，建議每季執行一次）
uv run python finmind_stock_scanner.py init-universe

uv run python finmind_stock_scanner.py recalc-signals --date YYYY-MM-DD --dry-run   # 只預覽名單
uv run python finmind_stock_scanner.py recalc-signals --date YYYY-MM-DD --no-discord # 覆寫 alignment_signals 與 MA_Signal_Tracker，不重發快報
uv run python finmind_stock_scanner.py recalc-signals --date YYYY-MM-DD              # 完整覆寫 alignment_signals／Sheets／Discord

# 手動預覽 22:00 AI 財報選股（--dry-run 不寫入 08 表、不推播 Discord，僅印在終端機）
uv run python finmind_stock_scanner.py analyze-alignment --date YYYY-MM-DD --dry-run

# 正式執行 22:00 AI 財報選股並推播至 Discord 與 Google Sheets
uv run python finmind_stock_scanner.py analyze-alignment --date YYYY-MM-DD

# ※ 22:00 單股診斷呼叫節流：ai_picker_call_interval_seconds 已調整為 13.0 秒（避免免費額度 429）

# 手動預覽 22:30 型態 A 長均線回踩反彈清單（--dry-run 僅在終端機印出標的數量與清單）
uv run python finmind_stock_scanner.py analyze-rebound --date YYYY-MM-DD --dry-run

# 正式執行 22:30 型態 A 反彈掃描（寫入 Rebound_Watchlist 並推播標的數量至 Discord）
uv run python finmind_stock_scanner.py analyze-rebound --date YYYY-MM-DD

# 強制對 22:30 型態 A 反彈清單啟動 AI 深度財報診斷與嚴選推播
uv run python finmind_stock_scanner.py analyze-rebound --date YYYY-MM-DD --run-ai

# --- 台新證券 Nova API 個人帳務與 AI 持股健診指令 (SASD v5.1.0) ---
# 1. 探測已安裝之 taishin_sdk 所有方法簽章；加 --live 則實際登入印出各 API 原始回傳欄位結構
uv run python taishin_portfolio_service.py inspect-sdk
uv run python taishin_portfolio_service.py inspect-sdk --live

# 2. 首次開通台新 API 認證 (執行 login + register_api_auth，收到開通通知 15 日內執行一次)
uv run python taishin_portfolio_service.py auth-init

# 3. 【全歷史對帳單大回補】分段撈取歷史所有對帳單、歷史成交與已實現損益並寫入個人 Google Sheet
uv run python taishin_portfolio_service.py backfill-history --from-date 2020-01-01 --dry-run
uv run python taishin_portfolio_service.py backfill-history --from-date 2020-01-01

# 4. 【每日 11:00 排程主任務】同步近期對帳單 + 最新庫存 + 交割款預警 + AI 持股均價與操作建議
uv run python taishin_portfolio_service.py sync-daily --dry-run
uv run python taishin_portfolio_service.py sync-daily
uv run python taishin_portfolio_service.py sync-daily --no-ai       # 僅同步帳務與庫存至 Sheet，不跑 AI 診斷
uv run python taishin_portfolio_service.py sync-daily --no-discord  # 僅寫入 Sheet，不發送 Discord 推播

# 5. 【週一至週五 13:15 排程任務】0050 每週不定期定額低點評估
uv run python taishin_portfolio_service.py eval-dca-0050 --dry-run
uv run python taishin_portfolio_service.py eval-dca-0050
```

> ⚠️ **部署與套件同步重要提醒 (`uv sync --inexact`)**：`taishin_sdk` 係直接透過 `pip` 安裝於 Python 虛擬環境中，在更新環境或執行 `uv sync` 時，請務必加上 `--inexact`（即 `uv sync --inexact`），避免 `uv` 自動移除手動安裝之 `taishin_sdk`。

### 2. GCP Linux VM 部署與 `systemd` 排程管理

```bash
# 1. 部署 11:00 台新帳務健診、13:15 0050 智慧定投、19:00 均線掃描、22:00 AI 順勢選股與 22:30 型態 A 反彈掃描排程
sudo cp ~/Discord_Service/deploy/taishin-statement.service /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/taishin-statement.timer /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/taishin-dca-0050.service /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/taishin-dca-0050.timer /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-scan.service /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-scan.timer /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-ai-pick.service /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-ai-pick.timer /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-rebound-ai.service /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-rebound-ai.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now taishin-statement.timer
sudo systemctl enable --now taishin-dca-0050.timer
sudo systemctl enable --now finmind-scan.timer
sudo systemctl enable --now finmind-ai-pick.timer
sudo systemctl enable --now finmind-rebound-ai.timer

# 2. 檢查所有排程下次觸發時間
systemctl list-timers --all | grep -E "finmind|taishin"

# 3. 查看排程執行日誌
journalctl -u taishin-statement.service -n 50 -f
journalctl -u taishin-dca-0050.service -n 50 -f
journalctl -u finmind-scan.service -n 50 -f
journalctl -u finmind-ai-pick.service -n 50 -f
journalctl -u finmind-rebound-ai.service -n 50 -f
```

### 手動測試觸發一次掃描或帳務任務
```bash
sudo systemctl start taishin-statement.service
sudo systemctl start taishin-dca-0050.service
sudo systemctl start finmind-scan.service
sudo systemctl start finmind-ai-pick.service
sudo systemctl start finmind-rebound-ai.service
```

### 3. 常用 Discord Bot 常駐服務管理指令

若已將 `bot_server.py` 配置為系統常駐服務（如 `discordbot.service`），可使用以下指令維護：

```bash
# 查看機器人運行狀態
sudo systemctl status discordbot.service

# 修改程式碼或 .env 後重啟機器人
sudo systemctl restart discordbot.service

# 查看即時執行日誌
journalctl -u discordbot.service -f

# 一鍵更新改版部署（使用 --inexact 保留手動 pip 安裝之 taishin_sdk）
cd ~/Discord_Service && git pull && uv sync --inexact && sudo systemctl restart discordbot

# 停止機器人服務
sudo systemctl stop discordbot
```

### 4. 與 GCP VM 檔案傳輸 (PSCP)

#### 4.1 從 GCP VM 下載檔案至本地 Windows (Download)

```powershell
# 下載單一檔案
pscp -i "G:\我的雲端硬碟\Google cloud platform\Hung-PC.ppk" nick_chung0725@8.229.251.176:/home/nick_chung0725/Discord_Service/finmind_reports/logs/2026-10-30_ai_calls.log "C:\Users\Hung-PC\Downloads"

# 下載整個資料夾（加 -r）
pscp -i "G:\我的雲端硬碟\Google cloud platform\Hung-PC.ppk" -r nick_chung0725@8.229.251.176:/home/nick_chung0725/Discord_Service/certs "C:\Users\Hung-PC\Downloads\certs"
```

#### 4.2 從本地 Windows 上傳檔案至 GCP VM (Upload)

將「本地路徑」放在前面、「遠端目的地路徑」放在後面即可：

```powershell
# 上傳單一檔案至 VM 的專案目錄
pscp -i "G:\我的雲端硬碟\Google cloud platform\Hung-PC.ppk" "C:\path\to\local_file.ext" nick_chung0725@8.229.251.176:/home/nick_chung0725/Discord_Service/

# 上傳整個資料夾（加 -r，例如上傳憑證或資料庫）
pscp -i "G:\我的雲端硬碟\Google cloud platform\Hung-PC.ppk" -r "C:\path\to\certs" nick_chung0725@8.229.251.176:/home/nick_chung0725/Discord_Service/certs
```
