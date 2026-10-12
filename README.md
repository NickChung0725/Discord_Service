# Discord 台股圖表機器人

使用 Discord `/stock` 指令取得台股近半年 K 線、均線、成交量、KD 與 MACD 互動圖表，並將 HTML 發布至 GitHub Pages。

## 需求

- Python 3.10 或更新版本
- Discord Bot Token，並邀請機器人加入伺服器及授予使用應用程式指令的權限
- 公開 GitHub 儲存庫，且已設定 GitHub Pages
- FinMind API Token 為選填；未設定時使用 FinMind SDK 的未登入存取

## 安裝與設定

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell 啟用虛擬環境請使用 `.venv\Scripts\Activate.ps1`，複製範例設定檔請使用 `Copy-Item .env.example .env`。

編輯 `.env`，設定以下變數：

| 變數 | 說明 |
| --- | --- |
| `DISCORD_TOKEN` | Discord Bot Token（必要；舊的 `BOT_TOKEN` 仍可使用） |
| `GEMINI_API_KEY` | Google AI Studio API Key（使用 `/ai` 時必要） |
| `GEMINI_MODEL` | Gemini 模型名稱（選填，預設 `gemini-flash-lite-latest`） |
| `FINMIND_API_TOKEN` | FinMind API Token（選填） |
| `GITHUB_TOKEN` | 有目標 repo Contents 讀寫權限的 GitHub fine-grained PAT |
| `GITHUB_REPO` | 儲存庫，格式為 `username/repo_name` |
| `GITHUB_BRANCH` | Pages 發布分支，預設 `gh-pages` |

GitHub Token 建議僅授予目標 repo 的 **Contents: Read and write** 權限（GitHub 可能同時要求 Metadata 讀取權限）。請勿將 `.env` 或 Token 提交到 Git。

如果目前 `.env` 已有 `GITHUB_BRANCH=main`，請改成 `GITHUB_BRANCH=gh-pages`；程式會優先採用 `.env` 中明確設定的分支。接著在 GitHub 儲存庫的 **Settings → Pages**，將發布來源設為 `gh-pages` branch 及根目錄 `/`；首次使用前，請先在 GitHub 建立 `gh-pages` 分支。上傳器會將 HTML 放在該分支的 `charts/` 資料夾中；若 repo 名稱不是 `<username>.github.io`，網址格式為 `https://<username>.github.io/<repo_name>/charts/<股票代號>.html`。此流程會在 `gh-pages` 分支建立或更新單一股票檔案，不會再於 `main` 分支新增圖表提交。重複查詢會先取得既有檔案 SHA 再更新，不會建立多份同名 HTML；每次內容更新仍會在 `gh-pages` 新增一筆 commit。Pages 初次啟用或更新可能需要數分鐘才會公開。

## 啟動

`bot.py` 是同步全域斜線指令的獨立入口，提供 `/stock`、`/ai` 和 `/weather`：

```bash
python bot.py
```

`bot_server.py` 提供相同斜線指令，並保留原有 ping、say、roll、buttons、card 及排程功能：

```bash
python bot_server.py
```

使用 `uv` 時，在更新依賴後執行 `uv sync`，再用 `uv run python bot_server.py` 啟動。

使用既有的 `bot_server.py` 前，請先將檔案頂端的 `GUILD_ID` 改成你的 Discord 伺服器 ID；此入口會同步伺服器限定指令。獨立 `bot.py` 則同步全域指令，首次發布可能需要一些時間才會顯示。

## Gemini AI 與每日天氣

在 [Google AI Studio](https://aistudio.google.com/) 建立 API Key，將它存入 `.env` 的 `GEMINI_API_KEY`，然後重新啟動機器人。Discord 的 `/ai` 指令會使用 Gemini `generate_content` API 與 `gemini-flash-lite-latest` 模型；此組合已依專案的 `TEST.PY` 成功範例設定。如需更換模型，可設定 `GEMINI_MODEL`。請勿將 API Key 提交到 Git。

`bot_server.py` 每天台灣時間早上 8 點會將台北天氣預報發送至 `ANNOUNCE_CHANNEL_ID` 指定的文字頻道。天氣資料來自 Open-Meteo，不需要額外 API Key。也可以使用 `/weather` 立即查詢；`/ai` 和 `/weather` 在 `bot.py`、`bot_server.py` 兩個入口都可使用，但每日排程只由 `bot_server.py` 執行。

## `/stock` 流程與指標

收到指令後會先 defer Discord interaction，再以背景執行緒取得至少近一年 FinMind `TaiwanStockPrice` 資料計算均線，圖表只顯示近六個月，並呼叫 GitHub Contents API 建立或更新頁面。K 線與價格使用 FinMind；成交量使用 Yahoo Finance 日線 API，並以 1,000 股換算為張。若 Yahoo 缺少對應交易日的成交量，程式會明確回報錯誤，不會靜默改用不同統計口徑的 FinMind 數字。KD 以 9 期 RSV 和 3 期平滑計算，MACD 使用 12/26 EMA 及 9 期訊號線。圖表採用 17/45/117/189 日均線、`plotly_dark`、四個共用日期軸的子圖和 CDN 載入 Plotly.js，以減少儲存庫檔案大小。游標移至圖表時會顯示橫跨圖面的十字線，日期軸會隱藏沒有交易資料的日期（包含週末與休市日），讓交易日連續呈現。拖曳 K 線圖放大任一日期區間時，頁面右上方會即時顯示該區間的最高價、最低價，以及兩者的平均值；雙擊圖表還原全區間後，數值也會回到目前整張圖表的範圍。

成交量數字會因資料來源的統計口徑而異：例如 2026-09-01 台積電，FinMind/TWSE 日成交資料為 31,855,287 股，而 Yahoo Finance 日線 API 為 17,216,484 股（與 Yahoo 歷史資料頁顯示值相同）。FinMind 的 `TotalDealVolume` 是全市場每 5 秒彙總，不含個股代號，不能用來取代台積電個股成交量。Yahoo Finance chart API 為非官方介面，可能受其服務可用性或流量限制；失敗時機器人會回報查詢錯誤，不會悄悄切換成交量來源。

如果 Discord 指令尚未出現，請確認 Bot 已成功登入，並依使用的入口檢查伺服器限定指令（`bot_server.py`）或等待全域指令（`bot.py`）同步完成。

## FinMind 市場／產業均線掃描

`finmind_stock_scanner.py` 是獨立命令列工具，可先檢視 FinMind 回傳的市場與產業，再掃描指定市場、產業近六個月的個股日線資料。`FINMIND_API_TOKEN` 可沿用 `.env` 設定；沒有 Token 時會嘗試使用 FinMind SDK 的未登入存取。

```bash
python finmind_stock_scanner.py list-markets
python finmind_stock_scanner.py list-industries --market 上市
python finmind_stock_scanner.py scan --market 上市 --industry "半導體業" --post-to-discord
```

市場選項為 `上市`、`上櫃`、`興櫃`。掃描下載近 12 個月日線計算 MA17、MA45、MA117、MA189，再將價格與均線資料裁切為近六個月輸出。一般訊號清單保留最新交易日 **MA45 >= MA17** 或 **MA45 低於 MA17 不超過 1%** 的股票；「接近」只表示均線距離，不是未來交叉的預測。另產生 `alignment_signals_*.csv`，條件為 **MA17、MA45 都高於 MA117、MA189，且 MA17 與 MA45 的差距不超過 1%**。加上 `--post-to-discord` 後，會把這份特別清單傳到 `.env` 的 `STOCK_CHANNEL_ID`；也可以用 `--channel-id` 指定頻道。傳送需要 `DISCORD_TOKEN`（或舊名稱 `BOT_TOKEN`），Bot 必須對該頻道有檢視及發送訊息權限；Discord 失敗會明確回報，不影響已產生的 CSV。

### 每日上市／上櫃指定產業掃描

`finmind_scanner.json` 設定上市與上櫃的半導體業、電子通路業、綠能環保、航運業、電腦及週邊設備業；排除 ETF／ETN 等非個股類別，以及設定檔列出的 19 檔已下市股票。每日批次使用：

```bash
python finmind_stock_scanner.py scan-all --config finmind_scanner.json
```

掃描以台北時間午夜為排程時間，近 12 個月資料用於計算 MA17、MA45、MA117、MA189，報表保留近六個月。所有指定市場與產業合併到 `finmind_reports/daily/YYYY-MM-DD_prices.csv`、`..._signals.csv`、`..._alignment_signals.csv`、`..._failures.csv`；空失敗表也會輸出。報表採原子寫入，僅清除超過設定保留天數的這四類日報，限流資料庫保留於 `finmind_reports/.state/`。

FinMind SDK 會在請求錯誤時重試，且登入、取得股票清單也會發出 HTTP 請求。批次限流器在每一次 `requests` HTTP adapter 實際送出請求前記錄 SQLite 滾動視窗，因此 SDK 每次重試及登入／股票清單請求都各自計數；預設最多每小時 540 次，且相鄰請求至少間隔 6.7 秒。設定的五產業目前上市／上櫃約 475 檔（排除清單與 ETF 後依當日股票資料略少），順利時約需 51–54 分鐘；重試會計入額度並可能延長時間。SQLite 可跨程式重啟保存歷史，但只協調使用同一個輸出目錄／資料庫的程序；其他 VM 或程式若共用同一 FinMind Token，仍須另行合併計算額度。請勿用同一 Token 同時從其他主機查詢，否則無法保證總額低於 600 次／小時。

### GCP VM systemd 部署

部署範本位於 `deploy/finmind-scan.service` 與 `deploy/finmind-scan.timer`。在 VM 將專案放在 `/home/<Linux使用者>/Discord_Service`，建立 Python 虛擬環境並安裝 `requirements.txt`；將 `.env.example` 複製成 `.env`，至少設定 `FINMIND_API_TOKEN`、`DISCORD_TOKEN`。`.env` 僅供服務讀取，請設為 `chmod 600 .env`，不要提交 Token。系統需安裝 systemd，且 FinMind 與 Discord 網域須可連線。

```bash
cd /home/<Linux使用者>/Discord_Service
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
nano .env
```

以下以專案路徑 `/home/alice/Discord_Service` 為例，先複製 unit 範本，再將 service 檔案中的 `YOUR_LINUX_USER` 全部換成 VM 的 Linux 使用者名稱。timer 檔案不需替換。使用 `sudoedit` 修改 systemd 檔案；不要把實際 Token 寫入 unit。

```bash
sudo cp deploy/finmind-scan.service /etc/systemd/system/finmind-scan.service
sudo cp deploy/finmind-scan.timer /etc/systemd/system/finmind-scan.timer
sudoedit /etc/systemd/system/finmind-scan.service
sudo systemctl daemon-reload
sudo systemctl start finmind-scan.service
sudo systemctl status finmind-scan.service
sudo journalctl -u finmind-scan.service -n 100 --no-pager
sudo systemctl enable --now finmind-scan.timer
systemctl list-timers finmind-scan.timer
```

服務範本明確呼叫虛擬環境的 Python，並讀取專案 `.env`。Timer 指定 `Asia/Taipei` 每日 00:00 啟動；執行需約一小時內，但重試可延長。啟用 timer 前可先手動跑一次 service 驗證 Token、Discord 權限及目錄寫入。檢查報表：`ls -l finmind_reports/daily/`。如果改了設定檔啟動時間，目前程式會拒絕非 `00:00`，以免與 timer 設定不一致。

這些範本與測試可確認本機部署設定格式，但無法代替實際 GCP VM 驗證。上傳到 VM 後仍需依該 VM 的使用者、專案路徑、Python 版本、網路、Token 及 Discord 權限完成上面手動測試；本專案目前尚未實作 Google Drive 自動上傳。

### 將日報 SIGNALS 備份至 Google Drive

日報訊號檔是 `finmind_reports/daily/YYYY-MM-DD_signals.csv`，均線排列特別訊號則是 `..._alignment_signals.csv`。可在 GCP VM 安裝並設定 `rclone` 的 Google Drive remote（`rclone config`），授權的 Google 帳號必須能寫入目標 Drive 資料夾。VM 無瀏覽器時，可在可信任的管理電腦完成 OAuth 授權後，安全地將 rclone 設定傳到 VM；該設定含敏感憑證，須限制檔案權限，不能放進 repo。亦可使用 Google Workspace Shared Drive 與具備相應權限的服務帳號；個人雲端硬碟並非自動授權給 VM 的 GCP service account。

設定好 remote（以下假設名稱為 `gdrive`、目的資料夾為 `FinMind/signals`）後，可手動上傳並核對：

```bash
REPORT_DATE=$(TZ=Asia/Taipei date +%F)
rclone copyto "finmind_reports/daily/${REPORT_DATE}_signals.csv" \
  "gdrive:FinMind/signals/${REPORT_DATE}_signals.csv"
rclone check "finmind_reports/daily/${REPORT_DATE}_signals.csv" \
  "gdrive:FinMind/signals/${REPORT_DATE}_signals.csv" --one-way
```

此命令是手動備份示例，不會由 `finmind-scan.service` 自動執行；要自動化需在確認 Drive 認證方式後，另行加入上傳步驟並讓備份失敗明確告警，再調整本機報表清理期限，避免只剩雲端副本前就刪除本機資料。

## Linux VM 背景執行

使用 `systemd` 可在 VM 重開機後自動啟動並於程式意外結束時重啟。將下列範例存成 `/etc/systemd/system/discordbot.service`，並替換 Linux 使用者名稱與專案路徑：

```ini
[Unit]
Description=Discord Stock Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_LINUX_USER
WorkingDirectory=/home/YOUR_LINUX_USER/Discord_Service
ExecStart=/home/YOUR_LINUX_USER/Discord_Service/.venv/bin/python /home/YOUR_LINUX_USER/Discord_Service/bot_server.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discordbot
sudo systemctl status discordbot
journalctl -u discordbot -f
```

也可用 `nohup .venv/bin/python bot_server.py > bot.log 2>&1 &` 暫時背景執行；正式長期部署建議使用 `systemd`。
