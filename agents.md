# AI Agent 專案開發與維護守則 (agent.md)

## 1. 本地開發虛擬環境設定 (Windows PowerShell)
此專案使用 `uv` 管理獨立虛擬環境，執行任何 Python 或測試指令前請先設定：
```powershell
$env:UV_PROJECT_ENVIRONMENT = "C:\Users\Hung-PC\.venvs\Discord_Service"
$env:PYTHONIOENCODING = "utf-8"
```

## 2. 文件長期同步與維護規範 (Documentation Sync Rule)
為了確保使用者隨時清楚目前個人助理具備的所有能力與近期變更，**每當 AI 助理完成以下任一修改時，必須在同一次任務結束前主動同步更新對應文件**：

1. **更新 [`README.md`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/README.md)（系統現況手冊）**：
   * 新增、修改或移除任何 **自動化排程時間 / 任務**（見「⏰ 助理的一天：自動化排程時間軸」）。
   * 新增、修改或移除任何 **Discord 斜線指令 (Slash Commands)** 或 **CLI 子命令**。
   * 新增或變更 **Google Sheets 工作表 (`SHEET_SCHEMAS`)** 或 **`.env` 環境變數**。
   * 新增或刪除專案頂層 Python 模組或 `deploy/` 服務檔。
2. **更新 [`CHANGELOG.md`](file:///h:/我的雲端硬碟/Work_Document/Discord_Service/CHANGELOG.md)（版本演進日誌）**：
   * 每次完成功能開發、邏輯調整或 Bug 修正後，於 `CHANGELOG.md` 最上方新增或擴充當日版本區塊（標註日期、✨ 新增功能、🔧 優化與修正、涉及之檔案連結）。