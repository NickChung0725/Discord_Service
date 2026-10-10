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
| `FINMIND_API_TOKEN` | FinMind API Token（選填） |
| `GITHUB_TOKEN` | 有目標 repo Contents 讀寫權限的 GitHub fine-grained PAT |
| `GITHUB_REPO` | 儲存庫，格式為 `username/repo_name` |
| `GITHUB_BRANCH` | Pages 發布分支，預設 `gh-pages` |

GitHub Token 建議僅授予目標 repo 的 **Contents: Read and write** 權限（GitHub 可能同時要求 Metadata 讀取權限）。請勿將 `.env` 或 Token 提交到 Git。

如果目前 `.env` 已有 `GITHUB_BRANCH=main`，請改成 `GITHUB_BRANCH=gh-pages`；程式會優先採用 `.env` 中明確設定的分支。接著在 GitHub 儲存庫的 **Settings → Pages**，將發布來源設為 `gh-pages` branch 及根目錄 `/`；首次使用前，請先在 GitHub 建立 `gh-pages` 分支。上傳器會將 HTML 放在該分支的 `charts/` 資料夾中；若 repo 名稱不是 `<username>.github.io`，網址格式為 `https://<username>.github.io/<repo_name>/charts/<股票代號>.html`。此流程會在 `gh-pages` 分支建立或更新單一股票檔案，不會再於 `main` 分支新增圖表提交。重複查詢會先取得既有檔案 SHA 再更新，不會建立多份同名 HTML；每次內容更新仍會在 `gh-pages` 新增一筆 commit。Pages 初次啟用或更新可能需要數分鐘才會公開。

## 啟動

`bot.py` 是僅含股票功能的獨立入口：

```bash
python bot.py
```

既有 `bot_server.py` 也已加入 `/stock`，並保留原有 ping、say、roll、buttons、card 及排程功能：

```bash
python bot_server.py
```

使用 `uv` 時，在更新依賴後執行 `uv sync`，再用 `uv run python bot_server.py` 啟動。

使用既有的 `bot_server.py` 前，請先將檔案頂端的 `GUILD_ID` 改成你的 Discord 伺服器 ID；此入口會同步伺服器限定指令。獨立 `bot.py` 則同步全域指令，首次發布可能需要一些時間才會顯示。

## `/stock` 流程與指標

收到指令後會先 defer Discord interaction，再以背景執行緒取得至少近一年 FinMind `TaiwanStockPrice` 資料計算均線，圖表只顯示近六個月，並呼叫 GitHub Contents API 建立或更新頁面。K 線與價格使用 FinMind；成交量使用 Yahoo Finance 日線 API，並以 1,000 股換算為張。若 Yahoo 缺少對應交易日的成交量，程式會明確回報錯誤，不會靜默改用不同統計口徑的 FinMind 數字。KD 以 9 期 RSV 和 3 期平滑計算，MACD 使用 12/26 EMA 及 9 期訊號線。圖表採用 17/45/117/189 日均線、`plotly_dark`、四個共用日期軸的子圖和 CDN 載入 Plotly.js，以減少儲存庫檔案大小。游標移至圖表時會顯示橫跨圖面的十字線，日期軸會隱藏沒有交易資料的日期（包含週末與休市日），讓交易日連續呈現。拖曳 K 線圖放大任一日期區間時，頁面右上方會即時顯示該區間的最高價、最低價，以及兩者的平均值；雙擊圖表還原全區間後，數值也會回到目前整張圖表的範圍。

成交量數字會因資料來源的統計口徑而異：例如 2026-09-01 台積電，FinMind/TWSE 日成交資料為 31,855,287 股，而 Yahoo Finance 日線 API 為 17,216,484 股（與 Yahoo 歷史資料頁顯示值相同）。FinMind 的 `TotalDealVolume` 是全市場每 5 秒彙總，不含個股代號，不能用來取代台積電個股成交量。Yahoo Finance chart API 為非官方介面，可能受其服務可用性或流量限制；失敗時機器人會回報查詢錯誤，不會悄悄切換成交量來源。

如果 Discord 指令尚未出現，請確認 Bot 已成功登入，並依使用的入口檢查伺服器限定指令（`bot_server.py`）或等待全域指令（`bot.py`）同步完成。

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
