# Discord_Service — 個人智慧生活與台股量化分析助理

這是一個部署於 **Google Cloud Platform (GCP) Compute Engine** 的全方位 Discord 機器人與台股自動化量化分析服務。系統整合了 **FinMind 台股技術面與基本面數據**、**Google Sheets 10 分頁智慧快取資料庫**、**Plotly 互動式看盤與財報戰情室自動發布 (GitHub Pages)**、**Google Gemini AI 雙層模型智慧診斷** 以及 **Open-Meteo 每日氣象推播**，透過 `systemd` 實現 24 小時常駐與每日雙階段選股排程。

---

## ⏰ 助理的一天：自動化排程時間軸 (Asia/Taipei)

| 時間 / 頻率 | 任務名稱 | 觸發機制 | 執行內容與產出 |
| :--- | :--- | :--- | :--- |
| **每 6 小時** | **伺服器存活心跳** | `bot_server.py` (`scheduled_task`) | 向 `GCP_VM_SERVER_CHANNEL_ID` 發送系統時間心跳通知，確認 GCP VM 與 Bot 正常運作。 |
| **每日 08:00** | **台北早安氣象報** | `bot_server.py` (`daily_weather_task`) | 串接 Open-Meteo API，自動推播當日台北即時溫度、體感、高低溫、降雨機率與紫外線指數至 `ANNOUNCE_CHANNEL_ID`。 |
| **週一至週五 19:00** | **第一階段：台股五線均線＋黃金量能掃描** | `systemd` (`finmind-scan.timer`) | 掃描五大重點產業日 K 線，篩選符合 `MA3 >= 各線`、`MA45 > MA117 > MA189` 且 `MA45` 已超過或接近 `MA17` (`<=1%`) 之強勢股，並同步計算**黃金量能三指標（今日成交量張數、5日均量、20日均量與 20日量比狀態）**：<br>1. 產出本地 `daily/{date}_alignment_signals.csv` 等 4 份報表<br>2. 同步至 Google Sheets `MA_Signal_Tracker`（自動計算「🆕當日新進」、「連續天數」、「17MA近期走勢」、「成交量/5日/20日均量」與「量能狀態(量比)」並自動條件上色）<br>3. 推播均線與量能強勢股清單至 `STOCK_CHANNEL_ID`。 |
| **週一至週五 22:00** | **第二階段：強勢股財報＋法說會驗證＋AI 嚴選進場** | `systemd` (`finmind-ai-pick.timer`) | 讀取 19:00 產出之 `alignment_signals` 清單：<br>1. **增量同步財報與籌碼**：自動補齊月營收、三大財報、衍生財務比率、股利與法人買賣超<br>2. **法說展望擷取與吹牛檢測 (`gemini-flash-lite-latest`)**：定向檢索近 180 天法說會報導，並以合約負債、CapEx、存貨週轉、月營收與法人籌碼交叉佐證真實性，產出結構化 `ai_summary_json`<br>3. **跨股總決策嚴選 (`gemini-3.8-flash`)**：綜合基本面(40%)＋均線位階與量價結構(35%，優先挑放量點火/溫和放量且排除 <500 張低量冷門股)＋籌碼(25%)評選出 Top 進場標的、觀察名單與排雷名單<br>4. 寫入 `08_AI_Daily_Picks`、本地 JSON/MD，並為入選股發布全方位戰情室網頁後推播至 `STOCK_CHANNEL_ID`。 |

---

### 💬 Discord 互動斜線指令一覽 (Slash Commands)
 
| 指令 | 參數說明 | 功能與產出說明 |
| :--- | :--- | :--- |
| **`/stock_ai`** | `symbol` (單股如 `2357` 或多股如 `2357,2330`，最多 4 檔)<br>`prompt` (選填，特定問題)<br>`force_refresh` (選填，預設 `False`) | **【📊 全方位投資戰情室（基本面財報 ＋ 技術面 K 線）】**<br>• **手機好讀一鍵複製**：以純文字 Markdown 發送（長按 1 秒即可複製轉傳他人）。<br>• **單股體質診斷**：包含 0~100 體質評分、3 大亮點、2 大風險、三率/CCC/FCF 拆解、股利與法人籌碼、表 7 均線與量能結構、法說展望真實性佐證。<br>• **二合一戰情室網頁**：發布全新戰情室 (`charts/{stock_id}_fund.html`)，內嵌**近半年技術面 K 線與均線 (17/45/117/189MA) + 成交量 + KD + MACD**，以及**基本面六大財務趨勢圖（每張圖正上方皆有獨立圖例與白話名詞小抄）**。<br>• **多股橫向比較 (`2357,2330`)**：自動橫向提取多檔財報數據並給出優劣排序結論。<br>• **專題追問 (`prompt`)**：針對使用者問題引用財報數字精準解答。 |
| **`/ai`** | `prompt` (問題內容) | **【Gemini AI 萬用問答】**<br>呼叫 Google Gemini 模型回答各式投資、程式或日常問題（超過 2,000 字自動分段回覆）。 |
| **`/weather`** | 無 | **【台北即時天氣查詢】**<br>即時查詢 Open-Meteo 台北地區氣溫、體感、天氣狀況、風速、降雨機率與今日紫外線。 |
| **`/card`** | 無 | **【系統狀態卡片】** 顯示機器人名稱、API 延遲 (ms) 與伺服器成員數。 |
| **`/ping`** | 無 | **【連線延遲測試】** 回應 `Pong! 🏓` 與毫秒延遲（或在頻道直接輸入文字 `ping`）。 |
| **`/say`** | `message` | **【文字複誦】** 讓機器人代為發送指定訊息。 |
| **`/roll`** | `max_number` (預設 100) | **【隨機擲骰】** 擲出 `1 ~ max_number` 之間的隨機數字。 |
| **`/buttons`** | 無 | **【互動按鈕測試】** 發送包含按讚與警告按鈕之互動元件。 |

---

## 🗄️ Google Sheets 智慧快取資料庫架構 (共 10 張工作表)

系統採用 Google Sheets 作為雲端結構化資料庫（定義於 [`fundamental_config.py`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/fundamental_config.py) 的 `SHEET_SCHEMAS`），自動建立並維護以下 10 張分頁：

| 分頁名稱 | 定位與儲存內容 | 更新時機與快取策略 |
| :--- | :--- | :--- |
| **`00_Watchlist_Master`** | **主控索引與 AI 快取表**：記錄股名、產業、各表最新資料水位 (`last_rev_ym`, `last_fin_qtr`, `max_div_raw_date`, `max_chip_raw_date`)、探測冷卻時間 (`last_probe_at`)、結構化 `ai_summary_json` 與 `war_room_url`。 | 每次 `/stock_ai` 或 22:00 批次分析時自動維護。 |
| **`01_Monthly_Revenue`** | **月營收動能表**：單月營收、`mom_pct` (月增率)、`yoy_pct` (年增率)。 | 依每月 10 日法定水位增量抓取（自動回頭重抓 1 期以吸收更正公告）。 |
| **`02_Income_Statement`** | **綜合損益表 (單季)**：營收、成本、毛利、營業費用、營業利益、業外收支、稅前/稅後淨利、母公司淨利、EPS。 | 依法定財報截止日（5/15、8/14、11/14、3/31）增量抓取（回補 1 季）。 |
| **`03_Balance_Sheet`** | **資產負債表 (季底)**：現金、金融資產、應收帳款、存貨、不動產廠房設備(PPE)、短長期借款、合約負債、應付帳款、股本、保留盈餘、權益總額（含資產=負債+權益自動驗算）。 | 同季報水位增量同步。 |
| **`04_Cash_Flow`** | **現金流量表 (自動累季轉單季)**：自動將 FinMind Q2/Q3/Q4 累計值拆解為**單季真實流量**（含折舊攤銷、AR/存貨/AP 營運資金變動、營業現金流 OCF、資本支出 CapEx、自由現金流 FCF）。 | 同季報水位增量同步。 |
| **`05_AI_Metrics_Wide`** | **AI 衍生財務比率寬表**：後端 Pandas 精確算妥之毛利率、費用率、營益率、業外佔比、淨利率、TTM EPS、年化 ROE、DIO/DSO/DPO/CCC 週轉天數、OCF/NI 含金量、EBITDA、淨負債比、流動/速動比、利息保障倍數。 | 每次季報更新時由系統後端自動重算寫入（嚴禁 AI 自行心算）。 |
| **`06_Dividend_Policy`** | **歷年股利政策表**：民國年自動轉西元年、現金股利、股票股利、總股利、現金股利總額(億)、年度 EPS、配息率 (`payout_ratio_pct`)、除息日。 | 30 天 TTL 增量更新。 |
| **`07_Chip_Summary`** | **三大法人與外資籌碼表**：外資持股比例、20 日持股變化、外資/投信/自營商及三大法人合計之近 5 日與近 20 日買賣超（張）。 | 每日 TTL（>= 1 天）增量更新。 |
| **`MA_Signal_Tracker`** | **19:00 均線與量能強勢股追蹤表**：記錄每日符合五線排列之個股、標註「🆕 是（新進榜）/ 🔄 否（持續符合）」、「連續天數」、「每日執行狀況」、「17MA 近期走勢」、**「成交量(張) / 5日均量(張) / 20日均量(張) / 量能狀態(量比)」**並自動套用條件格式底色（放量點火自動亮橘紅加粗、低量冷門灰字警示）。 | 每日 19:00 `scan-all` 自動同步。 |
| **`08_AI_Daily_Picks`** | **22:00 AI 嚴選進場歷史追蹤表**：記錄每日 AI 評選之 `TOP_PICK`、`WATCHLIST`、`AVOID` 清單、推薦當下收盤價、17MA/45MA 支撐價、單股財報分、總決策推薦分、法說展望評級與進場理由，供長期回測勝率。 | 每日 22:00 `analyze-alignment` 自動寫入。 |

---

## 📁 專案目錄結構

```text
Discord_Service/
├── bot_server.py               # [Bot 主程式] 綁定伺服器 (Guild) 之機器人入口（含 Slash 指令、08:00 天氣與 6h 心跳排程）
├── bot.py                      # [技術面指令] /stock 指令註冊與獨立啟動入口
├── stock_ai_command.py         # [基本面指令] /stock_ai 指令註冊（支援單股診斷、專題問答、多股比較與戰情室互動按鈕）
├── chart_generator.py          # [技術面圖表] FinMind 日 K + Yahoo 成交量 + MA/KD/MACD Plotly 互動圖表生成器
├── fundamental_config.py       # [結構與Prompt] SASD v3.2.0 10 張 Google Sheets Schema、會計科目錨點與 AI Prompt 模板
├── fundamental_cache.py        # [財報快取引擎] Google Sheets 增量同步、累季轉單季、衍生指標計算与批次記憶體快取管理
├── fundamental_analyzer.py     # [單股分析引擎] 滑動視窗切片、7 大財報表+新聞+法說展望檢索、結構化 ai_summary_json 與戰情室發布
├── finmind_stock_scanner.py    # [19:00 掃描器] 五線均線 (MA3/17/45/117/189) 批次掃描、SQLite 限流器、MA_Signal_Tracker 同步與 CLI 入口
├── alignment_ai_picker.py      # [22:00 選股器] 訊號股預篩、批次財報同步、雙層模型 (Flash-Lite + 3.8-Flash) 跨股決策與推播
├── ai_weather.py               # [AI 與氣象] Gemini API 呼叫封裝（含雙層模型指定與 429 自動降級 Fallback）與 Open-Meteo 天氣報表
├── github_uploader.py          # [網頁發布器] 透過 GitHub Contents API 將 HTML 圖表與戰情室上傳至 GitHub Pages
├── finmind_scanner.json        # [掃描設定檔] 掃描市場/產業、排除清單、API 速率限制、19:00 與 22:00 排程開關設定
├── deploy/                     # [systemd 部署] Linux 常駐與定時排程設定檔
│   ├── finmind-scan.service    # 19:00 均線掃描服務
│   ├── finmind-scan.timer      # 週一至週五 19:00 定時器
│   ├── finmind-ai-pick.service # 22:00 AI 財報分析與選股服務
│   └── finmind-ai-pick.timer   # 週一至週五 22:00 定時器
├── tests/                      # [單元測試] 涵蓋氣象/AI、均線掃描、財報快取、單股分析與 22:00 AI 選股之自動化測試
├── CHANGELOG.md                # [變更日誌] 記錄每次功能迭代與架構演進歷史
└── agent.md                    # [AI 開發守則] 記錄本地 uv 環境路徑與文件自動同步規範
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

# 2. Google Gemini AI 設定（雙層模型分工）
GEMINI_API_KEY=你的_Gemini_API_Key
GEMINI_MODEL=gemini-flash-lite-latest

# 3. FinMind 台股數據 API 設定
FINMIND_API_TOKEN=你的_FinMind_API_Token

# 4. GitHub Pages 圖表自動發布設定
GITHUB_TOKEN=你的_GitHub_PAT
GITHUB_REPO=你的帳號/你的儲存庫名稱
GITHUB_BRANCH=main

# 5. Google Sheets 財報快取資料庫設定
GOOGLE_APPLICATION_CREDENTIALS=sheetbot.json
GSHEET_SPREADSHEET_ID=你的_Google_Sheet_ID
FINMIND_PROBE_COOLDOWN_HOURS=6
AI_WINDOW_QUARTERS=8
AI_WINDOW_MONTHS=12
CHART_WINDOW_QUARTERS=16
CHART_WINDOW_MONTHS=36
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

# 手動預覽 22:00 AI 財報選股（--dry-run 不寫入 08 表、不推播 Discord，僅印在終端機）
uv run python finmind_stock_scanner.py analyze-alignment --date YYYY-MM-DD --dry-run

# 正式執行 22:00 AI 財報選股並推播至 Discord 與 Google Sheets
uv run python finmind_stock_scanner.py analyze-alignment --date YYYY-MM-DD
```

### 2. GCP Linux VM 部署與 `systemd` 排程管理

```bash
# 1. 部署 19:00 均線掃描與 22:00 AI 選股排程
sudo cp ~/Discord_Service/deploy/finmind-scan.service /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-scan.timer /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-ai-pick.service /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-ai-pick.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now finmind-scan.timer
sudo systemctl enable --now finmind-ai-pick.timer

# 2. 檢查所有排程下次觸發時間
systemctl list-timers --all | grep finmind

# 3. 查看排程執行日誌
journalctl -u finmind-scan.service -n 50 -f
journalctl -u finmind-ai-pick.service -n 50 -f
```

### 手動測試觸發一次掃描任務
sudo systemctl start finmind-scan.service
sudo systemctl start finmind-ai-pick.service


### 2. 常用 Discord Bot 常駐服務管理指令

若已將 `bot_server.py` 配置為系統常駐服務（如 `discordbot.service`），可使用以下指令維護：

```bash
# 查看機器人運行狀態
sudo systemctl status discordbot.service

# 修改程式碼或 .env 後重啟機器人
sudo systemctl restart discordbot.service

# 查看即時執行日誌
journalctl -u discordbot.service -f

# 一鍵更新改版部署
cd ~/Discord_Service && git pull && uv sync && sudo systemctl restart discordbot

# 停止機器人服務
sudo systemctl stop discordbot
```