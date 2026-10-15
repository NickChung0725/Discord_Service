# Discord_Service — 個人智慧生活與台股量化分析助理

這是一個部署於 **Google Cloud Platform (GCP) Compute Engine** 的全方位 Discord 機器人與台股自動化掃描服務。系統整合了 **FinMind 台股數據分析**、**Plotly 互動式看盤圖表自動發布 (GitHub Pages)**、**Google Gemini AI 智慧問答** 以及 **Open-Meteo 每日氣象推播**，透過 `systemd` 實現 24 小時常駐與每日自動化排程。

---

## 核心功能一覽（目前已實作現況）

### 1. 台股互動式技術分析圖表 (`/stock`)
* **即時指標運算**：輸入台股代號（如 `2330`），自動透過 FinMind API 抓取歷史日 K 資料，計算以下核心技術指標：
  * **均線系統 (MA)**：`17MA`、`45MA`、`117MA`、`189MA`
  * **動能指標 (KD)**：9 日 RSV，K=3、D=3 平滑移動平均
  * **趨勢指標 (MACD)**：12 日與 26 日 EMA，9 日訊號線 (Signal) 與柱狀圖 (Histogram)
  * **量能分析**：成交量與 5 日均量比、漲跌幅與振幅統計
* **自動發布 GitHub Pages**：使用 Plotly 繪製可縮放、懸停查價的 HTML 互動式 K 線圖，透過 **GitHub Contents API** 自動上傳至指定的 GitHub Pages 分支。
* **Discord 視覺化圖卡**：於頻道回傳結構化 Embed 摘要（含收盤價、漲跌、各期均線乖離、KD/MACD 狀態）及專屬互動式網頁連結。

### 2. 台股產業均線多頭自動掃描 (`finmind_stock_scanner.py`)
* **目標產業過濾**：預設鎖定台股五大重點產業進行批次掃描：
  * `半導體業`、`電子通路業`、`綠能環保`、`航運業`、`電腦及週邊設備業`
* **多頭排列與糾結篩選**：自動檢視個股是否符合 `17 / 45 / 117 / 189` 均線多頭排列與量價突破訊號。
* **本地 CSV 報表與 Discord 推播**：
  * 每次執行自動於本地 `./finmind_reports/` 產出 4 份結構化 CSV 檔案（價格快照 `prices`、技術訊號 `signals`、均線多頭 `alignment_signals`、異常紀錄 `failures`）。
  * 支援透過 `--post-to-discord` 參數將當日強勢股清單直接推播至指定的 Discord 股票頻道 (`STOCK_CHANNEL_ID`)。

### 3. Gemini AI 智慧助理 (`/ai`)
* 串接 Google `google-genai` SDK，支援於 Discord 頻道內直接呼叫 Gemini 模型（可由環境變數指定模型版本，如 `gemini-2.5-flash`）進行程式解答、數據分析與日常問答。

### 4. 即時氣象查詢與每日早安推播 (`/weather`)
* **手動查詢**：透過 `/weather` 串接 Open-Meteo API，即時回傳當前溫度、體感溫度、降雨機率與天氣狀況。
* **每日定時推播**：於 `bot_server.py` 內建 `discord.ext.tasks` 排程，每日上午 **08:00 (Asia/Taipei)** 自動發送台北地區早安天氣預報至公告頻道 (`ANNOUNCE_CHANNEL_ID`)。

### 5. 台股基本面財報快取 + AI 戰情室 (`/stock_ai`)
* **Google Sheets 六張固定 Tabs 快取**：自動建立/補齊以下分頁（不做每股一張表）：
  * `00_Watchlist_Master`、`01_Monthly_Revenue`、`02_Income_Statement`、`03_Balance_Sheet`、`04_Cash_Flow`、`05_AI_Metrics_Wide`
* **FinMind 增量更新 + 冷卻保護**：以 `MAX(raw_date)` 做真實水位，落後時採 `start_date = max_raw_date + 1 day` 增量抓取；並使用 `FINMIND_PROBE_COOLDOWN_HOURS` 避免空窗期重複探測。
* **Gemini AI 分析快取**：以 `ai_cached_period=last_fin_qtr_last_rev_ym` 做快取鍵，資料未變且未強制刷新時可 0 token 秒回。
* **Plotly 戰情室頁面**：產生 `{stock_id}_fund.html` 並發布到 GitHub Pages `charts/{stock_id}_fund.html`。

### 6. 伺服器實用工具指令
* `/ping`：檢測機器人與 Discord 伺服器的連線延遲 (Latency)。
* `/say`：讓機器人代為發送指定文字訊息。
* `/roll`：隨機擲骰小工具。

---

## 專案目錄結構

```text
Discord_Service/
├── bot_server.py               # [主程式] 綁定特定伺服器 (Guild) 之完整版機器人（含 Slash 指令與每日 08:00 天氣排程）
├── finmind_stock_scanner.py    # [掃描器] 台股五大產業均線與技術指標批次掃描腳本（支援 CLI 與 Discord 推播）
├── bot.py                      # [備用入口] 全域 (Global) Slash 指令版機器人入口
├── stock_bot.py                # [開發測試] 獨立測試股票圖表生成與 GitHub Pages 上傳之腳本
├── TEST.PY                     # [連線測試] 驗證 Gemini API Key 與模型回應之最小化測試腳本
├── .env                        # [環境變數] 存放 API Keys、Tokens 與頻道 ID（不納入版控）
├── requirements.txt            # [相依套件] Python 執行環境套件清單
├── deploy/                     # [部署設定] Linux systemd 常駐與定時任務設定檔範本
│   ├── finmind-scan.service    # 產業掃描單次執行服務定義檔
│   └── finmind-scan.timer      # 每日 00:00 定時觸發掃描之 Timer 設定檔
└── finmind_reports/            # [輸出目錄] 存放每日掃描產出的 CSV 報表檔案
```

---

## 環境需求與安裝步驟

### 1. 系統與 Python 環境
* **作業系統**：Debian / Ubuntu Linux（推薦部署於 GCP Compute Engine e2-micro 免費方案）或 Windows/macOS 本地開發環境
* **Python 版本**：Python 3.10 以上

### 2. 建立虛擬環境並安裝套件

```bash
# 進入專案目錄
cd ~/Discord_Service

# 建立 Python 虛擬環境
python3 -m venv .venv

# 啟用虛擬環境 (Linux / macOS)
source .venv/bin/activate

# 安裝相依套件
pip install -r requirements.txt
```

*(若尚未建立 `requirements.txt`，核心依賴包含：`discord.py`、`python-dotenv`、`requests`、`pandas`、`plotly`、`google-genai`、`FinMind`。)*

---

## 環境變數設定 (`.env`)

請於專案根目錄建立 `.env` 檔案，並填入以下設定值：

```ini
# ==========================================
# 1. Discord Bot 設定
# ==========================================
DISCORD_TOKEN=你的_Discord_Bot_Token

# 每日 08:00 早安天氣推播目標頻道 ID
ANNOUNCE_CHANNEL_ID=123456789012345678

# 每日 00:00 FinMind 產業掃描報告推播頻道 ID
STOCK_CHANNEL_ID=123456789012345678

# ==========================================
# 2. Google Gemini AI 設定
# ==========================================
GEMINI_API_KEY=你的_Gemini_API_Key
GEMINI_MODEL=gemini-2.5-flash

# ==========================================
# 3. FinMind 台股數據 API 設定
# ==========================================
FINMIND_API_TOKEN=你的_FinMind_API_Token

# ==========================================
# 4. GitHub Pages 圖表自動發布設定
# ==========================================
GITHUB_TOKEN=你的_GitHub_Fine_Grained_PAT
GITHUB_REPO=你的帳號/你的儲存庫名稱
GITHUB_BRANCH=gh-pages

# ==========================================
# 5. Google Sheets 財報快取資料庫設定 (/stock_ai)
# ==========================================
GOOGLE_APPLICATION_CREDENTIALS=/home/你的帳號/Discord_Service/service_account.json
GSHEET_SPREADSHEET_ID=你的_Google_Sheet_ID

FINMIND_PROBE_COOLDOWN_HOURS=6
AI_WINDOW_QUARTERS=8
AI_WINDOW_MONTHS=12
CHART_WINDOW_QUARTERS=16
CHART_WINDOW_MONTHS=36
```

> **注意**：`bot_server.py` 頂端設有 `GUILD_ID` 常數，部署至新 Discord 伺服器時，請確認將其修改為你的 Discord 伺服器 ID，以確保 Slash 指令能立即同步生效。

---

## 執行與使用方式

### 1. 啟動 Discord 機器人主服務

```bash
source .venv/bin/activate
python bot_server.py
```

啟動成功後，即可在 Discord 頻道內使用以下 Slash 指令：
* `/stock stock_id:2330` — 產生台積電 (2330) 互動式技術分析 K 線圖並取得 GitHub Pages 網址
* `/stock_ai symbol:2330` — 產生台積電 (2330) 基本面財報快取 + AI 診斷（並發布基本面戰情室到 GitHub Pages）
* `/stock_ai symbol:2330 prompt:為什麼最新一季自由現金流減少？` — 針對該股做財報專題追問
* `/ai prompt:請幫我解釋 KD 指標鈍化的意義` — 呼叫 Gemini AI 進行解答
* `/weather` — 查詢台北即時天氣狀況
* `/ping`、`/say`、`/roll` — 基礎互動指令

### 2. 手動執行 FinMind 台股產業掃描

```bash
source .venv/bin/activate

# 執行預設五大產業掃描，並將結果推播至 Discord STOCK_CHANNEL_ID
python finmind_stock_scanner.py --post-to-discord

# 指定特定市場或單一產業進行掃描（不推播至 Discord，僅輸出本地 CSV）
python finmind_stock_scanner.py --market twse --industry 半導體業
```

掃描完成後，會在 `./finmind_reports/` 目錄下生成當日 CSV 報表：
* `finmind_prices_YYYYMMDD.csv`（個股收盤與量價快照）
* `finmind_signals_YYYYMMDD.csv`（全市場技術指標訊號）
* `finmind_alignment_signals_YYYYMMDD.csv`（符合 17/45/117/189 均線多頭排列之標的）
* `finmind_failures_YYYYMMDD.csv`（API 抓取失敗或資料不足之紀錄）

---

## GCP VM 自動化部署與 `systemd` 維運

在 GCP Linux VM 上，本專案透過 `systemd` 實現「機器人 24 小時背景常駐」與「每日 00:00 自動化選股掃描」。

### 1. 部署每日 00:00 產業掃描排程 (`finmind-scan`)

將 `deploy/` 目錄下的設定檔複製至系統目錄並啟用 Timer：

```bash
sudo cp ~/Discord_Service/deploy/finmind-scan.service /etc/systemd/system/
sudo cp ~/Discord_Service/deploy/finmind-scan.timer /etc/systemd/system/

# 重新載入 systemd 並啟用每日排程
sudo systemctl daemon-reload
sudo systemctl enable --now finmind-scan.timer

# 檢查排程下次觸發時間
systemctl list-timers --all | grep finmind-scan

# 手動測試觸發一次掃描任務並查看即時 Log
sudo systemctl start finmind-scan.service
journalctl -u finmind-scan.service -n 50 -f
```

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

---

## 未來架構演進規劃 (Roadmap)

* **專案入口與模組重構**
  * 整合 `bot.py`、`stock_bot.py` 與 `bot_server.py`，將共用看盤圖表邏輯抽離為獨立模組，保留單一主程式入口以降低維護成本。