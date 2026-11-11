# Changelog — 個人智慧助理版本演進與修改紀錄

本文件記錄 `Discord_Service` 的所有重要功能迭代、架構升級與錯誤修正，方便隨時回顧「目前助理新增了哪些能力」。

---

## [v5.7.2] - 2026-11-11
### 🔧 優化與錯誤修正 (Fixed & Changed) — BigQuery AI 日誌短命 CLI 寫入防護與同步保障
* **參考 `finmind-ai-pick` 健全實作，修復短命 CLI / systemd oneshot 服務的 BigQuery 歸檔 Race Condition（[`services/ai_service.py`](/services/ai_service.py)）**：
  * **背景死鎖成因**：`finmind-ai-pick` 因走 Vertex AI Batch 模式，寫入 BigQuery 永久日誌（`save_to_permanent_logs`）是同步阻塞執行（等候 `load_job.result()`），絕不掉資料；而台新晨報等在線 API 原先採 `threading.Thread(daemon=True)` 背景執行，在短命 oneshot 腳本（1~2 秒退出）中會被作業系統強制殺死，導致 BigQuery 掉記錄。
  * **實作 `flush_bq_archives()` 與 Python `atexit` 機制**：全域追蹤背景歸檔執行緒，並透過 `atexit.register(flush_bq_archives)` 確保進程退出前必定等待所有 BigQuery load jobs 寫入完成（上限 10 秒），徹底解決 daemon thread 被提前截斷的問題。
  * **擴充 `blocking_bq_log` 參數**：`ask_gemini_with_meta()`、`ask_gemini_diagnosis_with_meta()` 與 `ask_vertex_ai_with_meta()` 均支援 `blocking_bq_log: bool = False` 參數，提供呼叫端比照 Batch 模式的同步阻塞保證。
* **台新帳務與晨報日誌同步保證與時間戳記擴充（[`taishin_portfolio_service.py`](/taishin_portfolio_service.py)）**：
  * `run_morning_battle_ai_review()`（開盤作戰指令）與 `run_portfolio_ai_diagnosis()`（持股健診與建倉劇本同步）啟用 `blocking_bq_log=True`，且在函式結尾顯式調用 `flush_bq_archives()`。
  * `custom_id` 由年月日擴充為精確時分秒時間戳記（`MORNING_BRIEF_YYYYMMDD_HHMMSS` 與 `THESIS_YYYYMMDD_HHMMSS_PORTFOLIO`），消除同一天多次手動重跑時 ID 碰撞無法識別的問題。

---

## [v5.7.1] - 2026-11-11
### 🔧 優化與排程時間調整 (Changed)
* **AI 嚴選選股定時排程由 21:00 調整至 21:30**：
  * 修改 [`deploy/finmind-ai-pick.timer`](/deploy/finmind-ai-pick.timer) 觸發時間為 `21:30`（`OnCalendar=Mon..Fri *-*-* 21:30:00 Asia/Taipei`），預留充足時間讓 19:00 全市場均線與流動性護欄掃描任務（`finmind-scan`，遇 API 延遲或重試時可能耗時 2~2.5 小時）完整產出當日 `alignment_signals.csv` 與 `prices.csv`，避免盤後 AI 嚴選因掃描尚未落盤而提早略過。
* **型態 A 跌深反彈排程調整至 22:00 並移除舊資料防呆回退（[`quant/alignment_ai_picker.py`](/quant/alignment_ai_picker.py) & [`deploy/finmind-rebound-ai.timer`](/deploy/finmind-rebound-ai.timer)）**：
  * 移除 `run_rebound_pipeline` 在找不到當日 `{target_date}_prices.csv` 時自動回溯歷史候選檔案的防呆機制；若當日價格資料未落盤直接誠實略過，杜絕誤用前一交易日舊資料進行反彈判斷。
  * 修改 [`deploy/finmind-rebound-ai.timer`](/deploy/finmind-rebound-ai.timer) 觸發時間由 `20:50` 改為 `22:00`（`OnCalendar=Mon..Fri *-*-* 22:00:00 Asia/Taipei`），排於 21:30 AI 嚴選之後，確保在 19:00 全市場掃描必定完成後才執行。
  * 同步更新系統現況手冊 [`README.md`](/README.md) 與系統設計架構文件 [`discord_service_sasd.md`](/discord_service_sasd.md)。

---

## [v5.7.0] - 2026-11-09
### ✨ 新增功能 (Added) — SASD Phase 3：台股微結構過濾、雙核心大盤路由與無未來函數審計閉環（含 Phase 1~3 全鏈路接線）
* **台股微結構過濾與除權息補償（[`finmind_stock_scanner.py`](/finmind_stock_scanner.py) & [`finmind_scanner.json`](/finmind_scanner.json)）**：
  * 實作並接入 `passes_liquidity_median_guard()`（近 20 日成交金額中位數 $\ge 5,000$ 萬台幣硬門檻，防範單日爆量殭屍股）與 `evaluate_disposition_risk()`（近 5/10 日急漲與高週轉注意／處置分盤交易預警）。
  * 實作並接入 `compensate_ex_dividend_gaps()` 與 `extract_ex_dividend_dates()`（以 T 日現價為 1.0 基準對歷史 OHLC 做前複權回溯補償，消除 6～9 月除權息假死叉與假 Volume POC 套牢區）。
  * 實作三層籌碼微結構拆解：`is_mandatory_margin_covering_window()`（3/15～4/25 股東會與除權息前 7 交易日強制回補失真註記）、`fetch_or_cache_tdcc_holding()` + `evaluate_weekly_tdcc_anchor()`（依實收資本額 $\ge 30$ 億動態切換 1,000 張／400 張大戶週底氣，附 `tdcc_base_date`）與 `classify_daily_margin_efficiency()`（日融資增減 × K 線實體效率四象限分類，精準區分 `MAIN_FORCE_MARGIN_LOCK` 主力鎖碼與 `RETAIL_MARGIN_CHASING_TRAP` 散戶套牢）。
* **加權＋櫃買（OTC）雙核心紅綠燈與外資期貨避險路由（[`taishin/macro_context.py`](/taishin/macro_context.py)）**：
  * 新增 `RegimeQuotaConfig`、`evaluate_market_regime_quota()`、`fetch_yahoo_daily_closes()`、`fetch_foreign_tx_futures_net_short()` 與 `RegimeRouter`，結合加權指數、櫃買指數（OTC）MA20 位階斜率與外資台指期淨空單變化，動態輸出 `GREEN_BULL` / `YELLOW_ROTATION` / `RED_DEFENSIVE` 策略配額（右側突破上限、Style B 低接上限、族群共振門檻與相對估值上限），並接入 `fetch_or_load_market_regime()`。
* **同細分產業資金共振度（[`quant/taxonomy_service.py`](/quant/taxonomy_service.py)）**：
  * 新增 `build_sector_metrics_from_prices()` 與 `compute_sector_breadth()`，統計同細分產業站上 17MA 比例與日報酬同業百分位（PR 值），攔截孤掌難鳴之假突破。
* **無未來函數事後審計閉環與 CLI（[`quant/signal_audit_service.py`](/quant/signal_audit_service.py) & [`finmind_stock_scanner.py`](/finmind_stock_scanner.py)）**：
  * 新增 SQLite `signal_audit_log`（`data/signal_audit.db`，嚴守 Leading Commas SQL 規範）、`evaluate_forward_path()` 三條防自欺鐵律（`Open_{T+1} * 1.005` 摩擦成本與低接成交分離、同日碰撞停損優先悲觀認定 `STOP_LOSS_HIT` + `is_whipsawed`、`REJECT_TRAP` 錯殺率 `is_false_negative_miss`）、`SignalAuditStore`（含 `sync_to_gsheet` 同步至 `09_Signal_Audit_Log`）與 `format_weekly_summary_markdown()`。
  * 新增 CLI 子命令 `audit-signals`，並將事後回填與 Google Sheets 同步完整接入 19:00 `scan_configured_universe()` 與 21:00 `run_pipeline()`（每逢週五盤後自動附加 `Weekly Quantitative Audit Summary` 推播）。

### 🔧 優化與全鏈路補齊 (Changed & Verified across Phase 1~3)
* **Pass 1 紅藍軍審計全鏈路接線與原生 Structured Output（[`services/ai_service.py`](/services/ai_service.py) & [`quant/alignment_ai_picker.py`](/quant/alignment_ai_picker.py)）**：
  * `ask_vertex_ai_with_meta()` 新增 `response_schema`、`system_instruction`、`temperature` 支援，Pass 1 紅藍軍審計透過 `GenerateContentConfig(response_mime_type="application/json", response_schema=BatchAuditResponse)` 原生鎖定自回歸防偏誤順序；新增 `_filter_recent_rss_news()` 精準保留近 5 交易日新聞；`_enforce_post_audit_guards()` 與 `enforce_regime_quotas()` 完整串接處置禁追高、紅黃燈配額、族群廣度與橫斷面相對 PE 硬攔截。
* **Phase 2 橫斷面同業 PE 自動串接（[`quant/fundamental_analyzer.py`](/quant/fundamental_analyzer.py)）**：
  * 新增 `compute_fundamental_features()` 與 `compute_cross_sectional_pe_map()`，自動由 `08_Stock_Taxonomy` 計算動態同業中位數 PE，並由 `03_Balance_Sheet` 提取實收資本額供集保大戶門檻判定。
* **Google Sheets 9 個確定性審計欄位擴充與傳遞（[`quant/fundamental_config.py`](/quant/fundamental_config.py)、[`taishin/taishin_config.py`](/taishin/taishin_config.py)、[`taishin_portfolio_service.py`](/taishin_portfolio_service.py)）**：
  * `AI_Daily_Picks` 與 `05_Position_Thesis_Memory` 同步擴充 `Audit_Grade`、`Execution_Tactic`、`Ideal_Buy_Or_Ref`、`Chase_Limit_Price`、`Hard_Stop_Loss`、`TP1_Target_Or_POC`、`Deterministic_RR`、`Bear_Case_Warning`、`Invalidation_Triggers` 共 9 欄，於 22:45 `sync-memory` 自動繼承鎖定 Python 硬停損與 TP1 點位（維持 12+5 核心表數不變）。
* **測試覆蓋**：新增與擴充 [`tests/test_phase3_microstructure.py`](/tests/test_phase3_microstructure.py)、[`tests/test_stage_guards.py`](/tests/test_stage_guards.py)、[`tests/test_margin_features.py`](/tests/test_margin_features.py)、[`tests/test_finmind_stock_scanner.py`](/tests/test_finmind_stock_scanner.py)，全儲存庫單元測試 100% 通過。

---
## [v5.6.1] - 2026-11-09
### 🔧 優化與修正 (Fixed & Changed)
* **修復 08:15 晨報呼叫 Vertex AI 404 NOT_FOUND 錯誤（AI 端點與 BigQuery Location 職責分離）**：
  * **問題成因**：`services/ai_service.py` 中的 `get_vertex_location()` 先前包含了 `or os.getenv("GOOGLE_CLOUD_LOCATION")` 作為 fallback，導致當 `.env` 設定 `GOOGLE_CLOUD_LOCATION=us-central1`（原供 BigQuery 使用）且未明訂 `VERTEX_LOCATION` 時，AI 模型呼叫被導向 `us-central1` 地區端點；而在 `us-central1` 找不到 `gemini-4-argon` / `gemini-3.8-flash` 等新世代模型，導致 08:15 晨報在調用 `VERTEX_STAGE2_MODEL` 時拋出 `ClientError: 404 NOT_FOUND Publisher model ... was not found`。
  * **端點責任分離**：
    * [`services/ai_service.py`](/services/ai_service.py)：修改 `get_vertex_location()`，使其僅讀取 `VERTEX_LOCATION` 或預設 `DEFAULT_VERTEX_LOCATION = "global"`，不再回退污染至 `GOOGLE_CLOUD_LOCATION`。BigQuery 資料集與儲存（`get_bq_location()`）則維持讀取 `BQ_LOCATION` 或 `GOOGLE_CLOUD_LOCATION`（預設 `"us-central1"`）。
    * [`.env`](/.env) & [`.env.example`](/.env.example)：明確新增 `VERTEX_LOCATION=global` 與 `BQ_LOCATION=us-central1`，並保留 `GOOGLE_CLOUD_LOCATION=us-central1` 以相容其他 GCP 儲存指令。
    * [`tests/test_ai_weather.py`](/tests/test_ai_weather.py)：更新測試環境，驗證 `VERTEX_LOCATION` 設定生效，並新增測試確保在僅有 `GOOGLE_CLOUD_LOCATION=us-central1` 時，AI 模型端點仍能正確安全預設為 `"global"`。
    * 同步更新 [`README.md`](/README.md) 與 [`discord_service_sasd.md`](/discord_service_sasd.md)。

---

## [v5.6.0] - 2026-11-05
### ✨ 新增功能 (Added)
* **Phase 2：無盲點基本面特徵工程與事件驅動快取**（規格：`gemini-code-1793867722705.md`）
  * [`quant/fundamental_config.py`](/quant/fundamental_config.py)：新增 `FundamentalEngineConfig` 資料類別與 `DEFAULT_FUNDAMENTAL_ENGINE_CONFIG`，集中管理股數反推安全門檻（`MIN_SAFE_EPS_FOR_SHARE_INFERENCE = 0.25`）、本業前瞻稅後係數（`0.80`）、一般月份均線容忍度（`0.95`）、季內達成率門檻（`0.36` / `0.72`）、三率變動門檻（`±1.5` 百分點）與橫斷面估值折溢價比值（`0.85` / `1.35`）。
  * [`quant/fundamental_analyzer.py`](/quant/fundamental_analyzer.py)：
    * 實作三層防呆真實流通股數還原 `_infer_safe_weighted_shares()`（Tier 1 最新季 `|EPS| >= 0.25`、Tier 2 回溯過去 8 季、Tier 3 市值／收盤價保底），消除彈性面額股 10 倍誤差與微利轉機股除以零／四捨五入膨脹誤差。
    * 實作含農曆春節豁免之右側營收動能引擎 `_analyze_revenue_momentum()`（1～2 月自動啟動 `LUNAR_NEW_YEAR_EXEMPT_MODE` 檢驗 1～2 月累計營收 YoY 是否大於去年 Q4 YoY；3～12 月檢驗加權 3M YoY 與近 3 月均線 `* 0.95` 保護並識別高基期滑落）與本季營收達成率（QTD Run-Rate）。
    * 實作本業年化前瞻估值 `_compute_forward_valuation()`（以近兩季平均營益率 OPM 推算 `Implied Core Forward EPS` 與 `Implied Forward PE`，對齊同業中位數 PE 計算 `Sector PE Ratio`）。
    * 實作三率擴張與收縮語意工程 `_evaluate_margin_trends()`（自動標記 `MARGIN_TRIPLE_EXPANSION`、`OPERATING_LEVERAGE_BREAKOUT`、`FAKE_REVENUE_DILUTION` 殺價搶單警訊、`MARGIN_NEUTRAL`）與 `build_ai_fundamental_prompt_block()` 高密度語意特徵區塊。
  * [`quant/fundamental_cache.py`](/quant/fundamental_cache.py)：新增 `should_bypass_revenue_cache()`、`should_bypass_quarterly_cache()`、`should_bypass_cache_for_calendar_event()` 與 `record_live_check_timestamp()`，於每月 5 日起營收開獎窗口自動對落後快取觸發 Event-Driven Bypass，並具備 6 小時冷卻期保護 FinMind API Quota。
  * [`quant/taxonomy_service.py`](/quant/taxonomy_service.py)：新增 `compute_sector_median_pe()`（過濾 `PE <= 0` 或 `PE > 120` 離群值，同細分產業有效樣本 $\ge 4$ 檔取中位數，不足自動回退大分類中位數或預設基準 `20.0`）。
  * [`tests/test_fundamental_analyzer.py`](/tests/test_fundamental_analyzer.py) & [`tests/test_fundamental_cache.py`](/tests/test_fundamental_cache.py)：新增並通過全部 Phase 2 規格驗收測試。
* **Phase 1：AI 紅藍軍審計架構（Pass 1）與庫存曝險耦合**（規格：`gemini-code-1793865428763.md`）
  * [`quant/alignment_ai_picker.py`](/quant/alignment_ai_picker.py)：新增 `StockAuditItem` / `BatchAuditResponse` 自回歸防偏誤 Schema（先 `bear_case_flaws`、最後才 `verdict_grade`）、`AUDITOR_SYSTEM_PROMPT`、`run_red_blue_audit()`（每 3 檔一批、`asyncio.gather` 並行）、`_enforce_post_audit_guards()`（風報比 < 1.8 的 S 級降級／淘汰、庫存滿載強制 `PORTFOLIO_SWAP_ONLY`）與 Zero-Pick Veto（全數淘汰輸出「今日無符合高勝率標準之標的，建議空手觀望」）；`run_pipeline()` 於 Stage 2 前串接 `run_audit_stage()`，Stage 2 的 `target_price_1` / `stop_loss_price` 改沿用 Python 確定性點位。
  * [`finmind_stock_scanner.py`](/finmind_stock_scanner.py)：新增 `compute_deterministic_trade_setup()`（ATR14、60 日量 POC、`RIGHT_BREAKOUT` / `STYLE_B_PULLBACK` 雙軌追價上限／硬停損／TP1、`deterministic_rr_ratio`）與 `align_news_with_price_action()`（新聞發布日量價反應標籤、250 字摘要，逾時 3 秒降級回標題）。
  * [`taishin_portfolio_service.py`](/taishin_portfolio_service.py)：新增 `PortfolioExposureCheckResult`、`evaluate_candidate_exposure()`（同產業 ≥ 2 檔／權重 > 35%、60 日日報酬皮爾森相關係數 ≥ 0.70 警示／≥ 0.80 僅限換股）與 `load_portfolio_exposure_inputs()`。
  * [`tests/test_alignment_ai_picker.py`](/tests/test_alignment_ai_picker.py)：新增 5 項規格驗收測試與 2 項輔助測試。
  * 新增選填設定 `finmind_scanner.json` → `ai_picker_red_blue_audit`（預設 `true`，設為 `false` 可停用審計）；新增相依套件 `pydantic`。

### 🔧 優化與修正 (Changed)
* 審計流程任一環節失敗（庫存讀取、新聞抓取、AI 呼叫）一律優雅降級，不中斷 21:00 選股主流程。
* `top_picks` 與 `ai_recommendations.json` 新增 `audit` 欄位（`Audit_Grade`、`Execution_Tactic`、`Chase_Limit_Price`、`Hard_Stop_Loss`、`Deterministic_RR`、`Bear_Case_Warning`、`Invalidation_Triggers` 等）；Discord 卡片顯示審計行與 `🔄 僅限換股` 標示。

---

## [v5.5.1] - 2026-11-05
### 🔧 優化與修正 (Changed & Removed)
* **全面移除台新交割銀行餘額查詢 (`bank_balance`) 與交割餘額預警 (`shortfall_alert`)**：
  * 因台新 SDK 銀行餘額 API (`bank_balance`) 於非盤中或未開通狀態會回傳 `OA0022: 查詢作業錯誤` 導致 `22:45` 與 `08:15` 排程中斷，且使用者不需系統代查銀行餘額與交割餘額缺口，故全面移除相關邏輯：
  * [`taishin/taishin_client.py`](/taishin/taishin_client.py)：移除 `fetch_bank_balance()` 方法與 `inspect-sdk --live` 之 `bank_balance` 探測項目；同時將 `OA0022` / `查詢作業錯誤` 納入 `_is_no_data_sdk_error()` 容錯清單。
  * [`taishin/taishin_config.py`](/taishin/taishin_config.py)：從 `03_Settlement_And_PnL` 工作表 Schema 移除 `bank_balance` 與 `shortfall_alert` 欄位。
  * [`taishin_portfolio_service.py`](/taishin_portfolio_service.py)：移除 `_sync_taishin_core_accounting()`、`sync_daily()`、`run_morning_brief()`、`dump_portfolio_context()` 及 `normalize_settlement_or_pnl_record()` 中所有 `bank_balance` 與交割餘額檢查邏輯。
  * 同步更新 [`tests/test_taishin_nova.py`](/tests/test_taishin_nova.py)、[`.agent/skills/portfolio-advisor/SKILL.md`](/.agent/skills/portfolio-advisor/SKILL.md)、[`deploy/taishin-morning-brief.service`](/deploy/taishin-morning-brief.service)、[`README.md`](/README.md) 與 [`discord_service_sasd.md`](/discord_service_sasd.md)。

---

## [v5.5.0] - 2026-11-04
### ✨ 新增功能 (Added)
* **大盤層級融資籌碼風控整合**：
  * [`finmind_stock_scanner.py`](/finmind_stock_scanner.py) 新增 `fetch_market_margin_snapshot()`，直接串接 TWSE / TPEx 官方免費 OpenAPI 與官方 RWD 備援，不消耗 FinMind 配額，本地自動維護 `finmind_reports/.state/market_margin_history.json` 滾動 30 交易日紀錄。
  * 升級 `evaluate_market_regime()`：依據大盤均線位階與全市場融資增減判定 `margin_regime`（如 `🚨 散戶凹單未洗清（提防多殺多斷頭）`、`⚡ 融資斷頭宣洩（留意止穩黃金坑）`、`⚠️ 高檔散戶槓桿過熱`、`🟢 籌碼健康`）與建議總資金投入上限 (`recommended_max_exposure_pct`)、動態限制入選檔數。
* **個股層級 07_Chip_Summary 融資維持率與資法人對作標籤擴充**：
  * [`quant/fundamental_config.py`](/quant/fundamental_config.py) 擴充 `SHEET_SCHEMAS["07_Chip_Summary"]` 7 個新欄位：`margin_balance_lots`、`margin_utilization_pct`、`margin_chg_5d_lots`、`margin_chg_20d_lots`、`short_margin_ratio_pct`、`est_margin_maint_pct`、`chip_quadrant_tag`。
  * [`quant/fundamental_cache.py`](/quant/fundamental_cache.py) 的 `_sync_chip_summary()` 串接 FinMind `TaiwanStockMarginPurchaseShortSale`，精確計算近 20 日融資買進成交量加權成本（VWAP）與估算維持率 (`est_margin_maint_pct`)，並依據法人與融資動態自動分類「資法人對作四象限標籤」。
* **Stage 0 硬性排雷與 Stage 1/2 Prompt 賦能**：
  * [`quant/fundamental_analyzer.py`](/quant/fundamental_analyzer.py) 的 `get_hard_screen_metrics()` 讀取最新融資數據，`build_hard_warnings()` 新增「主力出貨／散戶接刀硬傷」與「融資維持率逼近 130% 追繳臨界區之斷頭踩踏警戒」硬性排雷；資料卡 [B] 區新增信用籌碼與維持率診斷行。
  * [`quant/fundamental_config.py`](/quant/fundamental_config.py) 的 Stage 1 `full_diagnosis` Step 4 與 Stage 2 `portfolio_selection` Prompt 全面升級，納入融資對作風控與建議持倉水位。
* **0050 智慧定投恐慌殺融資加碼**：
  * [`taishin_portfolio_service.py`](/taishin_portfolio_service.py) 的 `evaluate_0050_dip_decision()` 與 `run_eval_dca_0050()` 結合大盤融資變化：當大盤融資單日暴減 $\le -40$ 億或 5 日累計暴減 $\le -90$ 億時自動觸發「大盤融資斷頭清洗」加碼訊號（分數 +15，定投倍數逼近 1.5x）。
* **單元測試套件**：新增 [`tests/test_margin_features.py`](/tests/test_margin_features.py) 完整覆蓋大盤融資解析、四象限分類、維持率估算、硬傷警告與 0050 殺融資加碼。

### 🔧 優化與修正 (Changed & Decoupled)
* **全專案硬編碼排程時間字串徹底解耦**：
  * 清理 [`quant/alignment_ai_picker.py`](/quant/alignment_ai_picker.py)、[`quant/fundamental_analyzer.py`](/quant/fundamental_analyzer.py)、[`quant/fundamental_config.py`](/quant/fundamental_config.py)、[`services/ai_service.py`](/services/ai_service.py)、[`finmind_stock_scanner.py`](/finmind_stock_scanner.py)、[`taishin/taishin_config.py`](/taishin/taishin_config.py)、[`taishin_portfolio_service.py`](/taishin_portfolio_service.py) 與 [`taishin/macro_context.py`](/taishin/macro_context.py) 中殘留之所有硬編碼時間字串（`22:00`、`22:30`、`20:50`、`21:00`、`08:15`、`22:45`、`13:15`、`11:00`、`19:00`），全面改為功能與策略導向描述。
  * 全系統排程時間集中由 [`discord_service_sasd.md`](/discord_service_sasd.md) 第 2.4 節（Single Source of Truth）與 `systemd` 定時器控制。
  * 更新 [`tests/test_taishin_nova.py`](/tests/test_taishin_nova.py) 之保底定投斷言字串。

---
## [v5.4.0] - 2026-11-04
### ✨ 新增功能 (Added)
* **Vertex AI Batch 改用 BigQuery 交換媒介**：[`services/ai_service.py`](/services/ai_service.py) 的 `run_vertex_batch_requests()` 改為每批建立獨立 `vertex_batch_staging.input_{seq}`（`load_table_from_json`）並由 Vertex 寫入 `predictions_{seq}`；新增 `ensure_staging_dataset()`（`default_table_expiration_ms = 432000000`，5 天自動到期）與單表 `expires` 雙重保險。
* **AI 日誌集中化**：新增 `extract_prompt_log_record()`、`save_to_permanent_logs()`；Batch（`VERTEX_BATCH`）與 Online（`ONLINE_GEMINI`，背景執行緒、非阻塞）呼叫皆以免費 Batch Load 追加至 `discord_service.ai_prompt_logs`，失敗僅記 Warning。
* `ask_gemini_with_meta` / `ask_gemini_diagnosis_with_meta` / `ask_vertex_ai_with_meta` 新增選填 `task_type`、`stock_id`、`custom_id`；[`fundamental_analyzer.py`](/quant/fundamental_analyzer.py)、[`taishin_portfolio_service.py`](/taishin_portfolio_service.py)、[`stock_ai_command.py`](/services/stock_ai_command.py)、[`alignment_ai_picker.py`](/quant/alignment_ai_picker.py) 已傳入對應 metadata。
* `.env` 新增 `BQ_STAGING_DATASET`、`BQ_PERM_DATASET`、`BQ_PERM_TABLE`、`BQ_LOG_ENABLED`。
### 🔧 優化與修正 (Changed)
* 移除 `GCS_BUCKET_NAME` / `GCS_BATCH_PREFIX` 與 `google.cloud.storage` 依賴；`pyproject.toml` 以 `google-cloud-bigquery>=3.0.0` 取代 `google-cloud-storage`。
* [`tests/test_vertex_batch.py`](/tests/test_vertex_batch.py) 改寫為 BigQuery / genai Mock 測試。

---
## [v5.3.0] - 2026-11-03
### 🔧 優化與修正 (Fixed)
* **大盤紅綠燈快取補上 189MA**：[`finmind_stock_scanner.py`](/finmind_stock_scanner.py) 的 `evaluate_market_regime()` 原本只計算並儲存 17/45/117MA，現在 `{date}_market_regime.json` 與 summary 都會包含 `MA189`（資料不足時為 null / 顯示「資料不足」）；舊快取缺少 `MA189` 時會自動重新計算。
* **持股追蹤補上 189MA**：[`taishin/taishin_config.py`](/taishin/taishin_config.py) 的 `02_Portfolio_Holdings` 新增 `ma189` 欄位（接在 `ma117` 後，開啟時自動補表頭）；[`taishin_portfolio_service.py`](/taishin_portfolio_service.py) 持股資料、盤前/盤後報告、AI 診斷提示與嚴選建倉追蹤提示均加入 189MA。
### ✨ 新增功能 (Added)
* **21:00 AI 順勢選股與 20:50 反彈選股導入 Vertex AI Batch API 離線批次 (SASD v5.3.0)**：
  * [`services/ai_service.py`](/services/ai_service.py) 新增 `run_vertex_batch_requests()`（JSONL 打包 ➔ GCS 上傳 ➔ Batch Job ➔ 原地輪詢 ➔ 依 `custom_id` 解析並寫入 `YYYY-MM-DD_ai_calls.log`）、`_ensure_gcp_credentials_env()`、`is_vertex_batch_enabled()`、`get_vertex_batch_stage1_model()` / `get_vertex_batch_stage2_model()` / `get_gcs_bucket_name()`。
  * [`quant/fundamental_analyzer.py`](/quant/fundamental_analyzer.py) 將診斷拆為 `prepare_diagnosis_context()` 與 `finalize_diagnosis_from_ai()`，`run_full_diagnosis()` 行為不變。
  * [`quant/alignment_ai_picker.py`](/quant/alignment_ai_picker.py) 的 `analyze_candidates()` 與 `run_portfolio_selection()` 支援 Batch（Cache-First，僅 Cache Miss 送批次，單股失敗隔離）。
  * [`finmind_stock_scanner.py`](/finmind_stock_scanner.py) `analyze-alignment` / `analyze-rebound` 新增 `--batch` / `--no-batch`。
  * 新增 `.env` 變數 `VERTEX_BATCH_ENABLED`、`GCS_BUCKET_NAME`、`GCS_BATCH_PREFIX`、`VERTEX_BATCH_POLL_SECONDS`、`VERTEX_BATCH_STAGE1_MODEL`、`VERTEX_BATCH_STAGE2_MODEL`；[`finmind_scanner.json`](/finmind_scanner.json) 新增 `ai_picker_use_vertex_batch`、`ai_picker_stage2_use_vertex_batch`、`vertex_batch_poll_interval_seconds`；`pyproject.toml` 新增 `google-cloud-storage>=2.14.0`。
  * 新增 [`tests/test_vertex_batch.py`](/tests/test_vertex_batch.py)（JSONL 對齊、輪詢、Location 萃取、快取混合、失敗拋錯）。

### 🔧 優化與修正 (Changed & Fixed)
* [`deploy/finmind-ai-pick.service`](/deploy/finmind-ai-pick.service) 與 [`deploy/finmind-rebound-ai.service`](/deploy/finmind-rebound-ai.service) 確認含 `TimeoutStartSec=infinity` 與 `PYTHONUNBUFFERED=1`，避免 oneshot 逾時殺掉等待中的批次。
* `_cleanup_old_daily_reports()` 同步清理 `finmind_reports/.state/batch/` 過期 `.jsonl`。

---

## [v5.2.0] - 2026-11-02
### ✨ 新增功能 (Added)
* **台新證券 AI 建倉記憶錨定、風格 B 分批移動停利與 08:15 開盤作戰晨報架構 (SASD v5.2.0)**：
  * **第 5 張個人 Google Sheet `05_Position_Thesis_Memory`（建倉記憶與目標價鎖定表）**：
    * 於 [`taishin/taishin_config.py`](/taishin/taishin_config.py) 新增第 5 張分頁 `05_Position_Thesis_Memory`（主鍵 `position_id = {stock_no}_{entry_date}_{wave_seq}`），升級 [`taishin_portfolio_service.py`](/taishin_portfolio_service.py) 之 `TaishinSheetManager.ensure_schema()` 支援不破壞歷史資料自動向右補齊新欄位。
    * 實作 `compute_holding_waves_from_statements()` 波段切分與 `match_entry_to_picks()` 嚴選限定歸因：由 `01_Account_Statement` 自動還原每次從 `0 股 -> 買進 -> 清倉` 的完整波段（加碼自動重算均價與 `peak_qty`，不覆寫初始目標價），僅回溯買進日往前 **5 個交易日內**依序配對 `22:00 AI_Daily_Picks (TOP_PICK)` ➔ `22:00 AI_Daily_Picks (WATCHLIST)` ➔ `22:30 Rebound_Watchlist`，鎖定買進當時的 `initial_tp1`、`initial_tp2`、`initial_sl`、`entry_thesis` 與 `invalidation_rule`，徹底消除「每日重算導致目標價漂移」問題。未命中嚴選榜單之長投／自主買進股（如 `0050`）不建立記憶卡、不強派目標價，僅顯示 `📦 帳面損益觀察`。
  * **風格 B 分批停利（含單張盤中零股拆賣）＋ 單向棘輪移動停利 (`trailing_stop`)**：
    * 實作 `compute_style_b_trim_plan()` 與 `compute_ratchet_trailing_stop()`：當 `OPEN` 部位達標或逼近 `initial_tp1` 時，自動觸發賣出約 50% 部位建議（`>= 2,000` 股建議賣整張現股；單張 `1,000` 股或零股自動建議掛 `盤中零股` 賣出 `qty // 2` 如 500 股鎖利）；偵測對帳單完成減碼後自動推進為 `TP1_PARTIAL_TAKEN` 並改看 `initial_tp2`。
    * `trailing_stop` 採「單向往上棘輪 (`max(prev_trailing_stop, candidate)`)」，隨獲利墊高至保本價（`avg_cost * 1.006` 含稅費）與 `17MA` 動態支撐，絕不因回檔而下修防守線。
  * **隔夜美股與國際情勢自動觀測模組 (`taishin/macro_context.py`)**：
    * 新增 [`taishin/macro_context.py`](/taishin/macro_context.py)，自動抓取費城半導體 (`^SOX`)、那斯達克 (`^IXIC`)、標普 500 (`^GSPC`)、台積電 ADR (`TSM`)、輝達 (`NVDA`)、`^VIX` 恐慌指數與美元兌台幣 (`TWD=X`) 漲跌幅，結合近 48 小時國際總經／半導體 Google News RSS 標題（經雜訊過濾），自動判定 `RISK_ON` / `NEUTRAL` / `RISK_OFF` 國際風險燈號。
  * **本地 Antigravity 專屬顧問技能 (`portfolio-advisor` Skill + `dump-context` CLI)**：
    * 新增 [`taishin_portfolio_service.py`](/taishin_portfolio_service.py) 唯讀子命令 `dump-context`（支援 `--refresh-live` 即時登入台新 API 更新庫存）與 [`.agent/skills/portfolio-advisor/SKILL.md`](/.agent/skills/portfolio-advisor/SKILL.md)，讓使用者在本地 Antigravity 對話詢問庫存時可隨時調閱精準成本均價、建倉記憶與國際盤數據進行深度互動（維持零 Discord 斜線指令暴露個人資產之資安隔離）。

### 🔧 優化與修正 (Changed & Fixed)
* **19:00 MA 均線排列訊號與 20:50 型態 A 反彈清單推播頻道改至 `alignment_signals` 並升級手機／LINE 雙行卡片版型**：
  * 於 [`finmind_scanner.json`](/finmind_scanner.json)、[`.env`](/.env) 與 [`.env.example`](/.env.example) 新增 `alignment_channel_id`、`rebound_channel_id` 與 `ALIGNMENT_SIGNALS_CHANNEL_ID=1566830891299508426`，將 19:00 均線排列訊號與 20:50 型態 A 高點回跌回踩長均線反彈清單獨立推播至專屬頻道 `alignment_signals#1566830891299508426`（21:00 AI 嚴選報告維持推播至 `STOCK_CHANNEL_ID`）。
  * 重構 [`finmind_stock_scanner.py`](/finmind_stock_scanner.py) 之 `format_alignment_report()` 與 `format_rebound_report()`，導入**精簡標頭 ＋ 數字序號 (`1.`, `2.`) ＋ 緊湊單換行（每檔股票間無空行） ＋ 完整細分產業與相似同業標籤 (`└─ 🏷️`)**，並在 `recalc_from_cache` 與 `run_rebound_pipeline` 加入細分產業自動補齊機制，大幅提升轉傳 LINE 與手機窄螢幕閱讀體驗。
  * 同步更新 [`quant/alignment_ai_picker.py`](/quant/alignment_ai_picker.py)、[`tests/test_finmind_stock_scanner.py`](/tests/test_finmind_stock_scanner.py) 與 [`README.md`](/README.md)。
* **型態 A 跌深反彈掃描定時排程由 22:30 改至 20:50**：
  * 將 [`deploy/finmind-rebound-ai.timer`](/deploy/finmind-rebound-ai.timer) 觸發時間由 `22:30` 調整為 `20:50`（`OnCalendar=Mon..Fri *-*-* 20:50:00 Asia/Taipei`），在 19:00~20:00 全市場掃描完成後、21:00 AI 嚴選選股前 10 分鐘精準產出型態 A 跌深回踩均線之績優股。
  * 強化 [`quant/alignment_ai_picker.py`](/quant/alignment_ai_picker.py) 之 `run_rebound_pipeline` 與成交量回補函式，若當日日線未及產出時自動向前回溯讀取最新交易日之 `prices.csv` 與大盤燈號快取，提供雙重防禦保護。
  * 同步更新 [`finmind_stock_scanner.py`](/finmind_stock_scanner.py)、[`taishin/taishin_config.py`](/taishin/taishin_config.py) 與 [`README.md`](/README.md) 排程時間軸、服務部署設定與說明文件。
* **AI 嚴選選股定時排程由 22:00 提早至 21:00**：
  * 將 [`deploy/finmind-ai-pick.timer`](/deploy/finmind-ai-pick.timer) 觸發時間由 `22:00` 提前至 `21:00`（`OnCalendar=Mon..Fri *-*-* 21:00:00 Asia/Taipei`），緊隨於 19:00~20:00 全市場均線掃描完成後執行，減少盤後等待時間。
  * 同步更新 [`finmind_stock_scanner.py`](/finmind_stock_scanner.py) CLI 說明與 [`README.md`](/README.md) 時間軸、服務設定與環境變數說明。
* **22:00 `AI_Daily_Picks` 結構化交易計畫欄位擴充**：
  * 於 [`quant/fundamental_config.py`](/quant/fundamental_config.py) 的 `SHEET_SCHEMAS["AI_Daily_Picks"]` 末端新增 `tactical_type`、`target_price_1`、`target_price_2`、`stop_loss_price` 四欄，並更新 [`quant/alignment_ai_picker.py`](/quant/alignment_ai_picker.py) 的 `build_pick_rows()` 自動寫入 Stage 2 / Stage 1 `trade_plan` 目標價與停損價，供盤後建倉記憶精準錨定。
* **台新排程收斂重整（退役 `11:00` 盤中排程，升級為 `22:45` 記憶歸因 ＋ `08:15` 開盤作戰晨報）**：
  * 移除舊版 `11:00` `deploy/taishin-statement.service` 與 `deploy/taishin-statement.timer`（避免盤中追高殺低與錯失 09:00 交割補款時機）。
  * 新增 [`deploy/taishin-memory-sync.service`](/deploy/taishin-memory-sync.service) ＋ [`deploy/taishin-memory-sync.timer`](/deploy/taishin-memory-sync.timer)（週一至週五 `22:45` 執行 `sync-memory`，平時靜默同步、僅新綁定／分批停利／平倉結案時推播快訊）。
  * 新增 [`deploy/taishin-morning-brief.service`](/deploy/taishin-morning-brief.service) ＋ [`deploy/taishin-morning-brief.timer`](/deploy/taishin-morning-brief.timer)（週一至週五 `08:15` 執行 `morning-brief`，整合 09:00 前 T+2 交割款預警、隔夜美股國際盤與 `VERTEX_STAGE2_MODEL` 嚴選持股開盤掛單建議；若無嚴選記憶持股自動切換 **0-Token 純 Python 快報模式**）。
  * 擴充 [`tests/test_taishin_nova.py`](/tests/test_taishin_nova.py) 與 [`tests/test_alignment_ai_picker.py`](/tests/test_alignment_ai_picker.py) 單元測試（全專案 125 項測試全數通過）。

---

## [v5.1.0] - 2026-11-01
### ✨ 新增功能 (Added)
* **台新證券 Nova API 歷史對帳單回補、帳務同步與 AI 持股健診模組 (SASD v5.1.0)**：
  * **純背景排程與 CLI 架構（零侵入既有系統）**：完全不修改 `bot_server.py`、`fundamental_config.py` 與 `fundamental_cache.py`，新增獨立設定與提示詞模組 [`taishin_config.py`](/taishin_config.py)、SDK 封裝器 [`taishin_client.py`](/taishin_client.py) 與帳務主服務 [`taishin_portfolio_service.py`](/taishin_portfolio_service.py)。
  * **獨立個人 Google Sheet 4 大分頁 (`TAISHIN_GSHEET_SPREADSHEET_ID`)**：
    * `01_Account_Statement`（全歷史交易對帳單與成交總表）、`02_Portfolio_Holdings`（目前持股庫存與 AI 均價操作建議表）、`03_Settlement_And_PnL`（交割款預警與歷史已實現損益表）、`04_DCA_0050_Tracker`（0050 每週不定期定額智慧尋底表）。
    * 實作主鍵冪等去重、每批次最多 500 筆 `append_rows(..., value_input_option="USER_ENTERED")`、HTTP `429` 指數退避重試（`2s, 4s, 8s`），以及 ETF 股票代號與委託單號前導零保護（如 `'0050`、`'00878`）。
  * **Rust PyO3 二進位物件正規化與動態簽章探測 (`taishin_client.py`)**：
    * 實作 `normalize_sdk_obj()` 遞迴轉型無 `__dict__` 之 Rust PyO3 擴充物件；相容官方 `account_statment` 拼字與 `call_sdk_with_date_range()` 自動探測 `(acc, YYYYMMDD, YYYYMMDD)` ➔ `(acc, YYYY-MM-DD, YYYY-MM-DD)` ➔ `(acc)` 並於 Python 端精確過濾日期區間。
  * **全歷史對帳單 30 天分塊回補 (`backfill-history`)**：
    * 自動將查詢區間切分為 30 天 Chunks，單一 Process 僅呼叫 1 次 `sdk.login()`、Chunk 間 `sleep(1.5)` 秒限流保護，並交叉比對 `account_statment` 與 `filled_history` / `filled_history_detail` 自動補遺缺漏成交紀錄與估算稅費。
  * **每日 11:00 帳務同步與 AI 持股買入均價診斷 (`sync-daily`)**：
    * 同步近 7 日對帳單、今日交割款、分戶帳餘額（自動判定 `⚠️餘額不足`）與最新庫存；若 API 未回傳買入均價則自動由 `01_Account_Statement` 歷史買進紀錄加權計算。
    * 跨表串聯本地 `prices.csv` 五線均線與公用 `00_Watchlist_Master` 快取，呼叫 `STAGE1_MODEL`（備援 `VERTEX_STAGE1_MODEL`）輸出成本安全墊評估、操作標籤與具體保本停利/目標價，推播至 `TAISHIN_ALERT_CHANNEL_ID`（严禁上傳 GitHub Pages）。
  * **週一至週五 13:15 `0050` 每週不定期定額智慧尋底引擎 (`eval-dca-0050`)**：
    * 週一至週四偵測「17MA 負乖離 <= -1.5%」、「回踩 45MA」、「RSI(14)<=40 或 KD_K<=30」、「近 10 日高點回落 >= 2.5%」四大低點訊號；週五 13:15 若本週尚未買進則強制執行保底定投（依 `dip_score` 動態調整 `0.6x ~ 1.5x` 股數）。支援 `TAISHIN_ENABLE_TRADING` 與 `--confirm-live-order` 雙重下單安全閘門。
  * **新增 `systemd` 定時排程與完整單元測試**：
    * 新增 [`deploy/taishin-statement.service`](/deploy/taishin-statement.service)、[`deploy/taishin-statement.timer`](/deploy/taishin-statement.timer)、[`deploy/taishin-dca-0050.service`](/deploy/taishin-dca-0050.service)、[`deploy/taishin-dca-0050.timer`](/deploy/taishin-dca-0050.timer) 與 [`tests/test_taishin_nova.py`](/tests/test_taishin_nova.py)。

### 🔧 優化與修正 (Changed & Fixed)
* **專案目錄結構進階模組化整理（保留 3 大主入口，13 個內部模組依領域分層收納）**：
  * **根目錄精簡為 3 大主入口**：根目錄僅保留 [`bot_server.py`](/bot_server.py)（Discord Bot 常駐主程式）、[`finmind_stock_scanner.py`](/finmind_stock_scanner.py)（19:00/22:00/22:30 台股量化與 AI 選股 CLI 主入口）與 [`taishin_portfolio_service.py`](/taishin_portfolio_service.py)（11:00/13:15 台新證券個人帳務與 0050 定投 CLI 主入口），確保 GCP Linux VM 上所有 `systemd` 服務與平時習慣之 CLI 指令 100% 免修改向下相容。
  * **三大功能子套件分層 (`services/`、`quant/`、`taishin/`)**：
    * [`services/`](/services/__init__.py)：收納 [`services/ai_service.py`](/services/ai_service.py)、[`services/weather_service.py`](/services/weather_service.py)、[`services/github_uploader.py`](/services/github_uploader.py)、[`services/stock_ai_command.py`](/services/stock_ai_command.py) 與 [`services/bot.py`](/services/bot.py)。
    * [`quant/`](/quant/__init__.py)：收納 [`quant/fundamental_config.py`](/quant/fundamental_config.py)、[`quant/fundamental_cache.py`](/quant/fundamental_cache.py)、[`quant/fundamental_analyzer.py`](/quant/fundamental_analyzer.py)、[`quant/alignment_ai_picker.py`](/quant/alignment_ai_picker.py)、[`quant/taxonomy_service.py`](/quant/taxonomy_service.py) 與 [`quant/chart_generator.py`](/quant/chart_generator.py)。
    * [`taishin/`](/taishin/__init__.py)：收納 [`taishin/taishin_config.py`](/taishin/taishin_config.py) 與 [`taishin/taishin_client.py`](/taishin/taishin_client.py)。
  * **雜項檔案歸檔與冗餘清理**：將台新/富果 API 參考索引移入 [`docs/llms.txt`](/docs/llms.txt)、本地連線測試腳本移入 `scripts/test_log.py`，並移除已被 `pyproject.toml` + `uv.lock` 取代的舊版 `requirements.txt`。
* **憑證與金鑰目錄清理、集中收納與跨平台路徑智慧容錯 (`certs/sheetbot.json`)**：
  * **金鑰移入 `certs/` 集中管理**：確認官方與版控規範為 `certs/`，將 Google 服務帳號金鑰正式移入憑證目錄 [`certs/sheetbot.json`](/certs/sheetbot.json)，與台新證券 `.pfx` 憑證集中受 [`.gitignore`](/.gitignore)（阻擋 `certs/`）防護，徹底清除歷史殘留的拼錯資料夾 `crets/`。
  * **台新憑證路徑探測強化 (`taishin_client.py`)**：[`taishin_client.py`](/taishin_client.py) 的 `TaishinCredentials.from_env()` 新增多層 fallback 探測順序（`TAISHIN_CERT_PATH` ➔ `certs/taishin_cert.pfx` ➔ `certs/{person_id}.pfx` ➔ `certs/*.pfx`），確保身分證字號命名之憑證在未明確指定環境變數時亦能正確載入。
  * **Google 服務帳號金鑰跨環境容錯與向後相容**：在 [`fundamental_cache.py`](/fundamental_cache.py)、[`taishin_portfolio_service.py`](/taishin_portfolio_service.py)、[`finmind_stock_scanner.py`](/finmind_stock_scanner.py) 與 [`taxonomy_service.py`](/taxonomy_service.py) 中，實作雙向相容探測順序（`GOOGLE_APPLICATION_CREDENTIALS` ➔ `certs/sheetbot.json` ➔ `./sheetbot.json`），無論本地 Windows 將金鑰收納於 `certs/` 或 Linux 伺服器放在舊有路徑，皆能無縫自動偵測。
  * **文件與環境範例同步更新**：同步更新 [`README.md`](/README.md)（補齊 GCP VM PSCP 雙向下載與上傳指令教學）與 [`.env.example`](/.env.example) 之 `GOOGLE_APPLICATION_CREDENTIALS=certs/sheetbot.json` 說明。

---

## [v5.0.1] - 2026-10-30
### 🔧 優化與修正 (Changed & Fixed)
* **GitHub Pages 戰情室上傳自動重試與單股診斷容錯隔離**：
  * [`github_uploader.py`](/github_uploader.py) 新增 `_request_with_retry()`，針對 GitHub Contents API 的 `429/500/502/503/504` 與網路瞬斷自動執行指數退避重試（`2s, 4s, 8s`）。
  * [`fundamental_analyzer.py`](/fundamental_analyzer.py) 的 `run_full_diagnosis()` 將 `render_and_publish_war_room_html()` 包覆於 `try...except` 中，確保偶發上傳失敗時仍能將 `ai_summary_json` 寫入 `00_Watchlist_Master` 並保留該股參與 Stage 2 決選（解決 `2834 臺企銀` 因 GitHub HTTP 500 整檔報廢的問題）。
* **控股／金控／特殊產業資產負債表總權益自動校正與科目別名擴充**：
  * [`fundamental_config.py`](/fundamental_config.py) 的 `ACCOUNT_ANCHOR_MAP` 調整將 `"TotalEquity"` 與 `"TotalLiabilities"` 優先於 `"Equity"` 與 `"Liabilities"`，並補齊 `資產合計`、`負債合計`、`權益合計`、`股東權益總計` 等別名。
  * [`fundamental_cache.py`](/fundamental_cache.py) 的 `_normalize_quarterly_statement()` 在處理資產負債表時，若資產與負債總額皆有效但 `total_equity` 缺漏或因非控制權益差額導致失衡（如 `2905 三商`），自動以 `total_assets - total_liabilities` 校正 `total_equity`（同時保留原始 `parent_equity` 供母公司 ROE 計算）。
* **股利政策表 30 天 TTL 判定修正與 `DataLoader` 登入複用**：
  * [`fundamental_cache.py`](/fundamental_cache.py) 的 `need_div` 改以 `06_Dividend_Policy.updated_at`（上次同步時間）判定 30 天 TTL，並在到期重查無新配息時刷新最新一筆 `updated_at`，解決除息旺季過後每日重複下載 `TaiwanStockDividend` 的問題。
  * [`chart_generator.py`](/chart_generator.py) 新增 `_get_finmind_loader()` 複用已登入的 `DataLoader` 實例，消除每檔股票繪製戰情室 K 線時重複呼叫 `login_by_token` 與觸發限流等待的額外耗時。
* **Google AI Studio 請求逾時調整**：
  * [`ai_service.py`](/ai_service.py) 將 `GOOGLE_AI_HTTP_TIMEOUT_MS` 設定為 `300_000`（300 秒），並同步更新 [`tests/test_ai_weather.py`](/tests/test_ai_weather.py)、[`tests/test_stock_features.py`](/tests/test_stock_features.py)、[`tests/test_fundamental_analyzer.py`](/tests/test_fundamental_analyzer.py)、[`tests/test_fundamental_cache.py`](/tests/test_fundamental_cache.py) 單元測試。

---

## [v5.0.0] - 2026-10-29
### ✨ 新增功能 (Added)
* **SASD v5.0 全市場動態掃描與雙軌量能守門員**：
  * 新增 [`taxonomy_service.py`](/taxonomy_service.py)：`fetch_twse_tpex_daily_snapshot`（TWSE/TPEx 盤後快照）、`update_registry_with_daily_snapshot`（20 日滾動均量＋連續缺漏自動標記 DELISTED）、`passes_dual_track_liquidity`（當日量 / 20 日均量 / 成交金額任一達標即放行）。
  * `finmind_scanner.json` 改為 `industries: ["ALL"]` 並新增量能門檻、Taxonomy / Registry 路徑等設定鍵（[`finmind_scanner.json`](/finmind_scanner.json)）。
  * 新增 CLI 子命令 `init-universe`：以 FinMind 近 30 日日線初始化 `stock_universe_registry.json`（7 天內已處理者跳過，支援斷點續傳；無近期日線標記下市）。
* **細分產業與同業知識庫**：新增第 12 張工作表 `08_Stock_Taxonomy`（[`fundamental_config.py`](/fundamental_config.py)）與本地快取 `finmind_reports/.state/stock_taxonomy.json`（TTL 90 天）；Yahoo 業務簡介 RAG ＋ `GEMINI_MODEL` 產生標籤，同業代號經真實股票字典防幻覺校驗；AI 失敗時不寫入快取。
* **報表整合**：19:00 / 22:30 Discord 快報、22:00 Stage 2 dossier 與 AI 嚴選報告附加「🏷️ 細分產業｜🔗 相似同業」；`/stock_ai` 報告首段與 `{stock_id}_fund.html` 戰情室標頭同步顯示（[`fundamental_analyzer.py`](/fundamental_analyzer.py)、[`alignment_ai_picker.py`](/alignment_ai_picker.py)）。

### 🔧 優化與修正 (Changed & Fixed)
* `_configured_universe()` 支援 `ALL` / `*`、4 碼普通股過濾與 Stage 1 預篩，並加入**跨產業全域去重 (seen_stock_ids)**，徹底消除 FinMind 電子工業與細分次產業重複掛名導致的個股雙重掃描；所有 FinMind 限流 context 統一加上 `only_hosts=("finmindtrade.com",)`，避免 TWSE/TPEx/Yahoo 呼叫誤耗配額（[`finmind_stock_scanner.py`](/finmind_stock_scanner.py)）。
* `limit_stage2_candidates()`（同細分最多 3 檔）與 `enforce_sector_cap()`（同細分最多 2 檔）改以 `sub_industry` 為優先分群鍵，缺值退回官方 `industry_category`。
* 新增測試 [`tests/test_taxonomy_and_prefilter.py`](/tests/test_taxonomy_and_prefilter.py)。

---

## [v4.0.0] - 2026-10-29
### ✨ 新增功能與架構升級 (Added & Changed)
* **SASD v4.0 實戰邏輯邊界補丁**：
  * **AI 模型路由更新（Stage1/Stage2 分離）**：`/stock_ai` 的「專題問答」與「多股比較」改走 Vertex AI（`VERTEX_STAGE1_MODEL`）；22:00 單股診斷仍先走 Google AI Studio（`STAGE1_MODEL`）重試，失敗後自動升級至 Vertex AI（`VERTEX_STAGE1_MODEL`）；跨股 Stage 2 決策固定走 Vertex AI（`VERTEX_STAGE2_MODEL`）（[`ai_service.py`](/ai_service.py), [`stock_ai_command.py`](/stock_ai_command.py), [`alignment_ai_picker.py`](/alignment_ai_picker.py), [`README.md`](/README.md)）。
  * **Stage 2 Prompt 分流（22:30 反彈 vs 22:00 趨勢）**：反彈模式不再套用「17MA 持續下彎扣 15 分」條款，改以「45MA/117MA 守穩 + 收盤強度 >= 0.55 + 反彈至 17MA 空間 >= 5%」檢核，且第一目標價優先設為 17MA 反壓（[`alignment_ai_picker.py`](/alignment_ai_picker.py)）。
  * **Stage 2 前 12 檔 Python 排序改良**：裁剪不再僅依 Stage 1 score，新增「天量過熱(>4.5x) / 已超漲(>17MA+12%)」降權，並在進入 Stage 2 Prompt 前強制同產業最多 3 檔（[`alignment_ai_picker.py`](/alignment_ai_picker.py), [`tests/test_alignment_ai_picker.py`](/tests/test_alignment_ai_picker.py)）。
  * **舊快取自動升級（Schema Check）**：命中快取時若 `ai_summary_json` 缺少 `trade_plan` 或 `trend_tags`，視為 cache miss 重新呼叫 Stage 1 更新快取（[`fundamental_analyzer.py`](/fundamental_analyzer.py), [`tests/test_fundamental_analyzer.py`](/tests/test_fundamental_analyzer.py)）。

* **SASD v4.0 雙軌 AI 診斷與嚴選升級**（Stage 0 Python 預篩 → Stage 1 單股審計 → Stage 2 紅藍軍辯證）：
  * **Stage 1 Prompt 全面改寫**：Step 1~7 先審計、最後才評分（避免錨定偏誤）；亮點/風險改為 1~4 點動態（重質不重量，天量過熱/法人倒貨/業外扭曲不得遺漏）；`SUMMARY_JSON` 新增 `trend_tags`（含 `fcf_nature` 區分戰略擴產 vs 結構性失血）與 `trade_plan`（進場觸發、目標價、停損、紅軍失效條件）。
  * **Stage 0 RSS 清洗與 Sanitizer**：新增 `filter_and_clean_rss()`（剔除盤中標題黨、逾 180 天舊聞、標題與摘要重複 > 85% 之空殼並標註無內文）、`sanitize_stage1_json()`（法說無內容強制 `無/無資料`、亮點/風險 1~4 點、補齊結構、`2026Q9` → `2026-09`、目標價不高於收盤加註警示）。
  * **Stage 0 硬性排雷**：`/stock_ai` 為軟性「🚨 系統硬傷警示」；22:00 排程對「日量 < 300 張」或「營益率 < 0 且合約負債 < 1 億」直接硬性攔截並併入最終 `avoid`（`hard_prefilter` / `merge_pre_filtered_avoid`，日量攔截於 Stage 1 之前執行以節省 Token）。
  * **Stage 1 模型路由**：預設改為付費版 `gemini-3.8-flash`（`.env` `STAGE1_MODEL`），不再降級 flash-lite（`STAGE1_FALLBACK_MODEL` 可選填）。
  * **`/stock_ai` 新增 `tier` 參數**：`標準 (3.8 Flash)`（預設）或 `大師 (ARGON)`（Vertex AI `VERTEX_MODEL`）；回覆最後附「交易計畫」Embed。
  * **Stage 2 Prompt 改寫（CIO/CRO 紅藍軍）**：條件式進場觸發 `entry_zone_and_trigger`、目標價防呆、景氣循環股 P/E 上限、FCF 擴產豁免與一票否決；輸出欄位改為 `bull_thesis` / `bear_case_and_invalidation` / `sector`。Discord 推播與 `AI_Daily_Picks`（`entry_thesis` / `risk_note`）同步改讀新欄位（保留舊欄位 fallback）。
  * **Stage 2 組合風控**：候選 > 15 檔時依 Stage 1 評分保留前 12 檔（`limit_stage2_candidates`）；**同產業最多入選 2 檔**由 Python 強制執行（`enforce_sector_cap`，超額移入 watchlist）。
  * **資料卡 [B] 擴充**：新增 PE/PB、毛利率/營益率 QoQ·YoY、近 3 月平均營收 YoY 與動能判讀（`build_snapshot_trend_lines`）。
  * 相關檔案：[`fundamental_config.py`](/fundamental_config.py), [`fundamental_analyzer.py`](/fundamental_analyzer.py), [`ai_service.py`](/ai_service.py), [`stock_ai_command.py`](/stock_ai_command.py), [`alignment_ai_picker.py`](/alignment_ai_picker.py), [`tests/test_alignment_ai_picker.py`](/tests/test_alignment_ai_picker.py), [`tests/test_stage_guards.py`](/tests/test_stage_guards.py)

### 🔧 優化與修正 (Changed)
* `/stock_ai` 人類可讀診斷文字截斷上限由 3800 提高至 6000 字（Step 1~7 內容較長）。

---

## [v3.5.1] - 2026-10-28
### ✨ 新增功能與架構升級 (Added & Changed)
* **Google AI Studio 呼叫升級為 `models.generate_content` + ThinkingConfig(128)，並新增單次 AI 呼叫日誌（每日單檔）**：
  * Google AI Studio 由舊版 `client.interactions.create` 全面切換為 `client.models.generate_content(..., config=GenerateContentConfig(thinking_config=ThinkingConfig(thinking_budget=128)))`。
  * 新增 `finmind_reports/logs/YYYY-MM-DD_ai_calls.log`：每次 AI 呼叫以「完整 Prompt + 完整 Output」寫入，並透過鎖定確保不互相穿插（供追查與除錯）。
  * 相關檔案：[`ai_service.py`](/ai_service.py), [`deploy/finmind-ai-pick.service`](/deploy/finmind-ai-pick.service)

* **22:00 單股診斷升級：主模型 `gemini-flash-latest` + 429/503 長等待重試 + 降級 `gemini-flash-lite-latest`**：
  * 依 SASD v3.5.1 規格，將單股基本面診斷改為「主模型 → 重試 → 降級模型 → 重試」的策略，降低 free-tier 額度波動造成的失敗率。
  * 相關檔案：[`ai_service.py`](/ai_service.py), [`fundamental_analyzer.py`](/fundamental_analyzer.py)

* **「分析模型：{response.model_version}」全面寫入 Discord 回覆、戰情室與 Google Sheets 快取**：
  * 單股診斷、專題問答、多股比較與 22:00/22:30 Vertex AI 決策報告皆會顯示實際 `model_version`。
  * 新增 `00_Watchlist_Master.ai_model_version` 欄位，並將 `model_version` 寫入 `ai_summary_json`（快取命中仍可顯示）。
  * 相關檔案：[`fundamental_config.py`](/fundamental_config.py), [`fundamental_analyzer.py`](/fundamental_analyzer.py), [`stock_ai_command.py`](/stock_ai_command.py), [`alignment_ai_picker.py`](/alignment_ai_picker.py)

### 🔧 優化與修正 (Changed)
* **修正 22:00/22:30 候選股戰情室發布範圍：落實「只要經過 AI 分析即上傳戰情室 HTML 並回填 Google Sheets」**：
  * **問題成因**：原先 `finmind_scanner.json` 中 `publish_war_room_for_all_candidates` 設為 `false`，且決策後僅對 `decision["top_picks"]`（5 檔）執行 HTML 生成與上傳，導致其餘完成 AI 深度分析的標的（如次佳名單 WATCHLIST、避開名單 AVOID 等 6 檔）未生成戰情室網頁，其 `war_room_url` 亦未寫入 `00_Watchlist_Master` 與 `AI_Daily_Picks`。
  * **修正邏輯**：
    1. 將 `finmind_scanner.json` 的 `publish_war_room_for_all_candidates` 預設值調整為 `true`，`alignment_ai_picker.py` 讀取 config 之 fallback 亦改為 `True`。
    2. 深度分析階段讓所有進入個股診斷的標的皆自動發布戰情室 HTML 並上傳 GitHub。
    3. 跨股決策後加入安全巡檢機制，遍歷 `results`（及 22:30 `ai_results`）中所有完成分析之標的，若尚未具備網址則自動補發布戰情室並同步回填 `00_Watchlist_Master`，確保寫入 `AI_Daily_Picks` / `Rebound_Watchlist` 時所有分析過個股均帶有完整 GitHub Pages URL。
  * 相關檔案：[`alignment_ai_picker.py`](/alignment_ai_picker.py), [`finmind_scanner.json`](/finmind_scanner.json), [`README.md`](/README.md), [`tests/test_alignment_ai_picker.py`](/tests/test_alignment_ai_picker.py)

* **22:00 AI 單股分析呼叫節流時間調整為 13 秒**：
  * `ai_picker_call_interval_seconds: 4.5 → 13.0`，避免連續多檔診斷觸發 429。
  * 相關檔案：[`finmind_scanner.json`](/finmind_scanner.json), [`alignment_ai_picker.py`](/alignment_ai_picker.py)

* **保留期清理擴充：同步刪除過期 AI 呼叫日誌**：
  * `_cleanup_old_daily_reports` 會一併清理 `logs/*_ai_calls.log`（超過 retention_days）。
  * 相關檔案：[`finmind_stock_scanner.py`](/finmind_stock_scanner.py)

* **systemd service 日誌同步輸出到 Serial Console**：
  * `StandardOutput=journal+console` / `StandardError=journal+console`，讓 GCP serial console 可看到每次 AI 呼叫的單行 telemetry。
  * 相關檔案：[`deploy/finmind-scan.service`](/deploy/finmind-scan.service), [`deploy/finmind-ai-pick.service`](/deploy/finmind-ai-pick.service), [`deploy/finmind-rebound-ai.service`](/deploy/finmind-rebound-ai.service)

* **單元測試同步更新**：
  * 更新 AI 呼叫 mock（`interactions.create` → `models.generate_content`）與新增 log cleanup/model_version 相關測試。
  * 相關檔案：[`tests/test_ai_weather.py`](/tests/test_ai_weather.py), [`tests/test_alignment_ai_picker.py`](/tests/test_alignment_ai_picker.py), [`tests/test_fundamental_analyzer.py`](/tests/test_fundamental_analyzer.py), [`tests/test_finmind_stock_scanner.py`](/tests/test_finmind_stock_scanner.py)

---

## [v3.4.1] - 2026-10-27
### 🔧 優化與修正 (Changed)
* **關閉財報 Markdown 表格科學記號顯示 ([`fundamental_analyzer.py`](/fundamental_analyzer.py))**：
  * 將 `build_markdown_context`（表 1 ~ 表 6）與 `build_latest_ma_context`（表 7）中所有 `to_markdown(index=False)` 呼叫全面加上 `floatfmt=".2f"`，強制關閉 `tabulate` 預設對大數值轉為科學記號（如 `2.89105e+08`）的行為，改以固定小數點後兩位完整呈現原始數值。

---

## [v3.4.0] - 2026-10-26
### ✨ 新增功能与架構升級 (Added & Changed)
* **解耦舊版 `ai_weather.py`，徹底拆分為獨立之 [`ai_service.py`](/ai_service.py) 與 [`weather_service.py`](/weather_service.py)**：
  * 移除舊時代混合檔案 `ai_weather.py`，將台北氣象預報（Open-Meteo API 與 `/weather` 指令）完全獨立至 [`weather_service.py`](/weather_service.py)。
  * 新增 [`ai_service.py`](/ai_service.py) 統一管理所有 AI 模型呼叫與 `/ai` 指令，並全面更新 [`bot_server.py`](/bot_server.py)、[`bot.py`](/bot.py)、[`fundamental_analyzer.py`](/fundamental_analyzer.py)、[`stock_ai_command.py`](/stock_ai_command.py) 與 [`alignment_ai_picker.py`](/alignment_ai_picker.py) 之模組引用。
* **導入「雙軌 AI 供應商分流架構」：22:00 跨股彙總選股升級使用付費版 Vertex AI，其餘維持免費版 Google AI Studio ([`ai_service.py`](/ai_service.py), [`alignment_ai_picker.py`](/alignment_ai_picker.py))**：
  * **付費版 Vertex AI (`ask_vertex_ai` / `get_vertex_model`)**：專用於 22:00（及 22:30 反彈開啟 AI 時）的 `run_portfolio_selection` 跨股總決策選股。
    * 固定以 `.env` 中的 `VERTEX_MODEL` 為主（覆蓋 `finmind_scanner.json` 的 `ai_picker_portfolio_model`），搭配 `GOOGLE_CLOUD_PROJECT_ID`、`GOOGLE_CLOUD_LOCATION` 與 `VERTEX_API_KEY` 初始化 `genai.Client(vertexai=True, ...)`。
    * **堅持決策品質不降級**：遇到網路或 429/500/502/503/504 (`deadline_exceeded`) 暫時性錯誤時，僅在 Vertex AI 內部進行指數退避重試（3s / 6s / 12s）；若仍失敗則直接報錯並推播提醒至 Discord，絕不降級至免費版模型。
  * **免費版 Google AI Studio (`ask_gemini` / `get_gemini_model`)**：用於 22:00 第一階段逐股基本面財報診斷 (`FundamentalAnalyzer`)、`/stock_ai` 單股/多股問答與 `/ai` 日常問答，兼顧零成本與高效率；即使未指定 `fallback_model`，遇到 `504 deadline_exceeded` 等暫時性錯誤亦會自動退避重試。
* **擴充暫時性錯誤重試特徵與單元測試 ([`ai_service.py`](/ai_service.py), [`tests/test_ai_weather.py`](/tests/test_ai_weather.py), [`tests/test_alignment_ai_picker.py`](/tests/test_alignment_ai_picker.py))**：
  * 將 `502`、`504`、`deadline_exceeded`、`deadline expired`、`timed out`、`timeout`、`internalservererror` 納入 `_RETRYABLE_ERROR_MARKERS`，解決長 Prompt 偶發 `504 Deadline expired before operation could complete` 時未觸發重試即中斷的問題。
  * 新增 `VertexAiTests` 與 `504 deadline_exceeded` 自動重試測試（全專案 82 項單元測試全數通過）。

---

## [v3.3.1] - 2026-10-22
### 🔧 優化與修正 (Changed)
* **Discord 分段推播在「關聯回覆」續篇不再重複標題，直接接續股票條目 ([`alignment_ai_picker.py`](/alignment_ai_picker.py), [`finmind_stock_scanner.py`](/finmind_stock_scanner.py))**：
  * 當第 2 段起訊息以 `message_reference` 回覆第 1 段時，自動移除續篇內重複的標題與 `（續）`，讓閱讀更聚焦。
  * 同步擴充測試覆蓋續篇 trim 行為（[`tests/test_alignment_ai_picker.py`](/tests/test_alignment_ai_picker.py), [`tests/test_finmind_stock_scanner.py`](/tests/test_finmind_stock_scanner.py)）。
* **22:30 型態 A 反彈清單移除 Markdown 粗體符號 `**`，改善複製到 LINE 的可讀性 ([`finmind_stock_scanner.py`](/finmind_stock_scanner.py))**：
  * 將標題區與個股條目中的 `**...**` 改為純文字，避免跨平台貼上出現 `**` 雜訊。

---

## [v3.3.0] - 2026-10-21
### ✨ 新增功能 (Added)
* **補足左側反彈契機：「型態 A（高點急跌回踩長均線反彈）」量化掃描系統 ([`finmind_stock_scanner.py`](/finmind_stock_scanner.py), [`alignment_ai_picker.py`](/alignment_ai_picker.py))**：
  * 新增 `classify_rebound_signal` 與 `scan_rebound_from_prices`：從 `daily/{date}_prices.csv` 讀取近 60 個交易日數據，嚴格篩選同時滿足五大條件之均值回歸標的：
    1. **長線護體**：`MA45 >= MA117 > MA189`（長期多頭結構未破壞）。
    2. **高點回跌門檻（位階分級 ＋ 跌幅 >= 10%）**：`30~150元` 回跌 $\ge 10$ 元、`150~500元` 回跌 $\ge 30$ 元、`500~1,000元` 回跌 $\ge 100$ 元、`>=1,000元` 回跌 $\ge 300$ 元，且自近 60 日高點 (`high_60d`) 跌幅 $\ge 10\%$。
    3. **下有長均線支撐**：今日收盤價落在下方 `45MA`（季線）或 `117MA`（半年線）的 `-1.5% ~ +2.5%` 區間內。
    4. **上有反彈空間**：收盤價低於 `17MA`，且反彈至 `17MA` 的空間 $\ge 5\%$。
    5. **K 線收盤強度濾網**：計算 `收盤強度 = (Close - Min) / (Max - Min)`，硬性要求 $\ge 0.55$（排除長上影線假反彈股）。
  * 產出本地檔案：`daily/{date}_rebound_signals.csv` 與 `daily/{date}_rebound_failures.csv`。
* **大盤總經視角連動：大盤環境紅綠燈機制 (`Market Regime Filter`) ([`finmind_stock_scanner.py`](/finmind_stock_scanner.py), [`alignment_ai_picker.py`](/alignment_ai_picker.py))**：
  * 新增 `evaluate_market_regime` 與 `fetch_or_load_market_regime`（預設以加權指數 **`TAIEX`** 為大盤基準並快取至 `daily/{date}_market_regime.json`）：
    * `🟢綠燈`（大盤 $\ge 17\text{MA}$ 與 $45\text{MA}$ 且 $17\text{MA}$ 向上）：偏多環境，順勢主推「17/45MA 金叉突破」。
    * `🟡黃燈`（大盤跌破 $17\text{MA}$ 但守住 $45\text{MA}/117\text{MA}$）：震盪盤整，系統提高「型態 A 反彈清單」推薦權重。
    * `🔴紅燈`（大盤跌破 $45\text{MA}$ 且季線下彎）：空頭警戒，強制在 AI 報告與 Discord 標註防禦警示，嚴格過濾基本面。
  * **免除產業排除干擾並自動快取日線**：大盤紅綠燈採定向獨立抓取，完全不受 `excluded_industry_categories`（排除 ETF/大盤）影響；並在 19:00 自動將 `TAIEX` 日線追加進 `prices.csv`，使 22:00 與 22:30 秒級重算零額外 API 消耗。
* **Google Sheets 擴充第 11 張工作表 `Rebound_Watchlist` ([`fundamental_config.py`](/fundamental_config.py), [`finmind_stock_scanner.py`](/finmind_stock_scanner.py))**：
  * 新增 `Rebound_Watchlist` Schema 與 `sync_rebound_watchlist_to_gsheet`，完整記錄 `pk_key`, `stock_id`, `stock_name`, `industry_category`, `scan_date`, `close_price`, `high_60d`, `drop_amount`, `drop_pct`, `support_ma`, `support_price`, `target_ma17`, `potential_return_pct`, `volume_status`, `market_light`, `ai_summary_json`, `war_room_url`。
* **新增每日 22:30 獨立排程與 CLI 子命令 `analyze-rebound` ([`deploy/finmind-rebound-ai.service`](/deploy/finmind-rebound-ai.service), [`deploy/finmind-rebound-ai.timer`](/deploy/finmind-rebound-ai.timer), [`finmind_scanner.json`](/finmind_scanner.json))**：
  * 新增 `python finmind_stock_scanner.py analyze-rebound`（支援 `--date`、`--run-ai`、`--no-ai`、`--force-ai`、`--dry-run`）。
  * 支援兩階段彈性決策：第一階段預設 `rebound_auto_run_ai: false`，先推播「今日符合反彈條件標的總數」與清單至 Discord 並寫入 `Rebound_Watchlist`；當開啟 `rebound_auto_run_ai: true`（且檔數落於 `3 ~ 15` 檔區間）或傳入 `--run-ai` 時，自動銜接 AI 深度財報診斷與推播。

### 🔧 優化與修正 (Changed)
* **AI 嚴選總決策 Prompt (`portfolio_selection`) 四大法人操盤規範全面升級 ([`fundamental_config.py`](/fundamental_config.py), [`alignment_ai_picker.py`](/alignment_ai_picker.py))**：
  1. **大盤環境背景宣告**：動態注入當日 `🟢綠燈 / 🟡黃燈 / 🔴紅燈` 狀態，要求 AI 動態調整進場防守係數。
  2. **戰術操作定位分流 (`tactical_type`)**：強制將每檔入選股歸類為 `🎯 短中期波段持有 (5~20日)`、`⚡ 短線價差/當沖` 或 `🏦 長線價值抱單` 並標註依附均線。
  3. **法人測幅三刀流 (`target_price_1`, `target_price_2`, `stop_loss_price`)**：結合 `05_AI_Metrics_Wide` 之 `TTM_EPS` 與本益比區間、`17MA 正乖離極值 (+12%)` 及 `N 字箱型等幅測距`，在結構化 JSON 與 Discord 報告具體輸出第一目標價、第二目標價與嚴格停損價（-4% 或跌破支撐）。
  4. **勝率優化與排雷確認**：強制審視近 5 日法人買賣超與自由現金流（FCF），若技術面反彈但法人連續大賣且 FCF 為負，強制列入風險警示或降級 (`avoid` / `watchlist`)。
* **Discord 超長文字自動分段推播支援「關聯回覆 (Message Reference)」機制 ([`alignment_ai_picker.py`](/alignment_ai_picker.py), [`finmind_stock_scanner.py`](/finmind_stock_scanner.py))**：
  * 解決 22:00 AI 總決策、22:30 型態 A 反彈與 19:00 均線快報在文字超過 1,800 字元被截斷分段發送時，接續訊息與首則訊息各自獨立、無關聯性的問題。
  * 系統自動擷取首則推播訊息之 Discord Message ID，後續接續分段一律帶入 `message_reference: {"message_id": first_id, "fail_if_not_exists": False}` 與 `allowed_mentions: {"replied_user": False}`，呈現與 `/stock_ai` 完全一致的回覆串連線條，確保閱讀體驗統一。
* **單元測試擴充**：於 [`tests/test_finmind_stock_scanner.py`](/tests/test_finmind_stock_scanner.py) 與 [`tests/test_alignment_ai_picker.py`](/tests/test_alignment_ai_picker.py) 新增型態 A 篩選、K 線收盤強度濾網、大盤紅綠燈、`Rebound_Watchlist`、戰術定位測幅渲染與 Discord 超長分段關聯回覆測試（共 75 項測試全數通過）。

---

## [v3.2.4] - 2026-10-21
### 🔧 優化與修正 (Changed)
* **移除舊版 `{date}_signals.csv` 產出，統一以 `{date}_alignment_signals.csv` 作為均線清單資料源 ([`finmind_stock_scanner.py`](/finmind_stock_scanner.py), [`alignment_ai_picker.py`](/alignment_ai_picker.py))**：
  * 19:00 掃描不再產生 `daily/{date}_signals.csv`（含每市場/產業中繼 `signals_*.csv`），僅保留 `prices`／`alignment_signals`／`failures`。
  * `MA_Signal_Tracker` 同步資料來源改為 `alignment_signals`（自動欄位相容：`MA17_MA45_gap_percent` → `gap_percent`），並將設定鍵更名為 `sync_alignment_to_gsheet`（`sync_signals_to_gsheet` 保留向下相容）。
  * 調整本地保留期清理規則：不再自動刪除歷史 `daily/*_signals.csv` 舊檔，避免誤刪。
  * 測試同步更新：[`tests/test_finmind_stock_scanner.py`](/tests/test_finmind_stock_scanner.py)。

* **縮減 19:00 Discord 均線排列快報每檔輸出欄位，提高可讀性 ([`finmind_stock_scanner.py`](/finmind_stock_scanner.py))**：
  * `format_alignment_report` 移除每檔股票後段的 `MA3/MA17/MA45/MA117/MA189` 數值與 `17/45 差`，僅保留「訊號類型、收盤、量能狀態、17MA 走勢」等關鍵資訊。

---

## [v3.2.3] - 2026-10-20
### ✨ 新增功能：資料層／策略層拆分，秒級重算 (Added)
* **新增 CLI 子命令 `recalc-signals` ([`finmind_stock_scanner.py`](/finmind_stock_scanner.py))**：直接讀取 19:00 已存檔的 `daily/{date}_prices.csv`（內含近一年算好的 MA 與成交量），以純本地運算重新套用訊號規則，**0 次 FinMind 請求、約 2 秒完成**（原本需重抓約 1 小時）。
  * 新增 `recalc_signals_from_prices`（純函式）與 `recalc_from_cache`（讀檔／寫檔／Sheets／Discord 編排），並抽出 `SIGNAL_CSV_COLUMNS`、`ALIGNMENT_CSV_COLUMNS` 常數。
  * 參數：`--date`、`--dry-run`（只預覽不寫入）、`--no-gsheet`、`--no-discord`、`--output-dir`、`--config`。
  * 19:00／22:00 `systemd` 兩班車流程與服務檔完全不變；調整門檻後可 `recalc-signals` ➔ `analyze-alignment`，22:00 對已分析股票直接命中 Sheets 快取。
  * 測試：[`tests/test_finmind_stock_scanner.py`](/tests/test_finmind_stock_scanner.py) 新增 `RecalcFromCacheTests`。

### ✨ 均線排列訊號邏輯升級 (Improved)
* **17/45MA 雙區間訊號判讀全面重構（「-1.5% ~ 0% 即將金叉」＋「0% ~ +2.5% 剛金叉起漲」）([`finmind_stock_scanner.py`](/finmind_stock_scanner.py))**：
  * **解決舊版 `1%` 門檻過於嚴苛之痛點**：舊版限制 `1%` 導致強勢股剛黃金交叉 2~3 天（17MA 與 45MA 開口拉開至 1.2%~2.5%）即過早自追蹤表剔除，無法追蹤「初升段」與「首次量縮回測 17MA 支撐」之安全甜蜜買點。
  * **雙區間訊號標準化**：
    * `即將金叉`（差距 `-1.5% <= gap < 0%`）：17MA 處於 45MA 下方 1.5% 內且 3MA 已率先站上所有均線，蓄勢即將金叉。同時排除落後超過 -1.5%（如 -5%）之假反彈弱勢股。
    * `剛金叉起漲`（差距 `0% <= gap <= +2.5%`）：17MA 正式站上 45MA，五線標準多頭排列（`3MA >= 17MA >= 45MA > 117MA > 189MA`），開口處於 +2.5% 內之初升段安全起漲區。
  * **Google Sheets 與 Discord 視覺化同步升級**：
    * `MA_Signal_Tracker` 每日執行狀況智慧辨識：當訊號由「即將金叉」轉為「剛金叉起漲」，自動標記 `🔥 轉強突破（即將金叉 ➔ 剛金叉起漲｜連 X 日｜差距 X.XX%）`；反之標記 `⚠️ 均線拉回`。
    * 條件格式上色：`剛金叉起漲`（含舊版 `已超過`）標記為**暖橘紅底深橘字**；`即將金叉`（含舊版 `接近`）標記為**柔和藍底深藍字**。
    * 19:00 Discord 快報條件標題與篩選規則同步更新為雙區間標準。

### 🔧 錯誤修正 (Fixed)
* **22:00 AI 嚴選報告導入多階自我修復式 JSON 解析器 (`parse_portfolio_json`) ([`alignment_ai_picker.py`](/alignment_ai_picker.py), [`fundamental_config.py`](/fundamental_config.py))**：
  * **根治 LLM 結尾幻覺與陣列未閉合造成的解析中斷**：針對 Gemini 生成長篇 JSON 在末端偶發漏閉合 `]` 或附加尾隨單詞（如 `} questionnaires}`）的情況，導入四階容錯修復流程（精確切片 ➔ 尾部雜字與漏閉合修復 ➔ 尾隨逗號清洗 ➔ 括號平衡重構），徹底避免前段 99% 精準分析因末尾微小語法問題而降級為未結構化文字。
  * **Prompt 約束升級**：在 `portfolio_selection` 提示詞中強化結尾結構規範（嚴禁額外雜字、註解，陣列必須以 `]` 正確閉合）。


---

## [v3.2.2] - 2026-10-19
### ✨ 新增功能 (Added)
* **專業操盤手視角「黃金量能三維度」全面導入 ([`finmind_stock_scanner.py`](/finmind_stock_scanner.py), [`fundamental_config.py`](/fundamental_config.py), [`alignment_ai_picker.py`](/alignment_ai_picker.py), [`fundamental_analyzer.py`](/fundamental_analyzer.py))**：
  * **零額外 API 額度消耗**：直接從 FinMind `taiwan_stock_daily` 既有 `Trading_Volume`（股數）換算「張數 (`/ 1000`)」，同步計算 **`成交量(張)`**、**`5日均量(張)`（1 週短線動能）**、**`20日均量(張)`（1 個月機構基準量）** 與 **`量能狀態(量比)`（今日量 ÷ 20MA 均量 ＋ 5MA vs 20MA 多頭量構）**。
  * **6 大量能狀態智慧判讀 (`describe_volume_status`)**：
    * `🔥 放量點火 (>= 2.0x)`：主力真金白銀進場點火，突破最具可信度。
    * `📈 溫和放量 (1.2x ~ 2.0x)`：健康推升量，籌碼穩定換手。
    * `➡️ 量能平穩 (0.8x ~ 1.2x)`：正常量能。
    * `💧 量縮整理 (< 0.8x)`：拉回不破 `17MA` 屬主力鎖碼惜售；突破日若無量則提防假突破。
    * `⚠️ 天量過熱 (> 4.5x)`：爆出超過月均量 4.5 倍以上天量，警示短線隔日沖或主力趁利多倒貨風險。
    * `⚠️ 低量冷門 (< 500張)`：20 日均量與今日量皆低於 500 張，警示流動性不足與滑價風險。
  * **Google Sheets `MA_Signal_Tracker` 擴充與視覺化上色**：
    * 新增 4 欄：`成交量(張)`、`5日均量(張)`、`20日均量(張)`、`量能狀態(量比)`（共 22 欄，支援既有歷史資料與 `{date}_prices.csv` 自動回填）。
    * 自動套用千分位整數格式 `#,##0`；`量能狀態(量比)` 含 `🔥` 或 `📈` 自動標示**橘紅底粗體**，含 `低量冷門` 自動標示**淺灰底深灰字**。
  * **19:00 Discord 強勢股快報、22:00 AI 嚴選與 `/stock_ai` 全面連動**：
    * **19:00 快報 (`format_alignment_report`)**：每檔標的直接顯示 `量能: 🔥 放量點火 (2.45x｜5MA>20MA) [今 5,820張 / 5MA 3,410張 / 20MA 2,380張]`。
    * **22:00 AI 嚴選 (`alignment_ai_picker.py`)**：Dossier 與 Discord 推播卡片同步納入量能結構，並升級 `portfolio_selection` Prompt，引導 AI 優先加分「帶量突破 (`1.5x ~ 3.5x`) 或強勢股量縮回測 17MA 支撐」，自動降評 `< 500 張` 殭屍股與 `> 4.5x` 天量爆量股。
    * **`/stock_ai` 單股診斷 (`fundamental_analyzer.py`)**：表 7 新增當日量能結構摘要與近 5 日成交量（張）明細，Prompt 第 6 節升級為「技術面與量價配合綜合結論」。

### 🔧 錯誤修正 (Fixed)
* **修正 22:00 AI 嚴選個股戰情室網址未回寫與過期網頁未覆蓋問題 ([`alignment_ai_picker.py`](/alignment_ai_picker.py), [`fundamental_analyzer.py`](/fundamental_analyzer.py))**：
  * **修正未寫回 `00_Watchlist_Master.war_room_url`**：過去 22:00 選出 `TOP_PICK` 時，在產生並上傳 HTML 後未呼叫 `cache._update_watchlist_master`，導致 Google Sheets 欄位維持空白（僅 Discord 與 `AI_Daily_Picks` 有網址）。已修正為發布成功後立即自動同步回寫。
  * **修正「已曾分析過之個股」戰情室 HTML 停留在舊日期未更新**：過去因判斷 `if report.war_room_url: continue`，只要該股先前曾分析過，便會跳過發布，導致網頁停留在舊版。已修正為若當日重新執行 AI 診斷（`not report.cached`），一律強制重新渲染最新全方位戰情室 HTML 並推播至 GitHub Pages。
  * **已立即手動回補**：將今日 22:00 嚴選出的長榮航 (2618)、樺漢 (6414)、宏正 (6277) 之 `war_room_url` 完整寫回 Google Sheets，並重新發布威鋒電子 (6756) 之全方位戰情室 HTML 至最新版本。


---

## [v3.2.1] - 2026-10-18
### ✨ 新增功能與介面大升級 (Added & Improved)
* **全方位投資戰情室（基本面財報 ＋ 技術面 K 線）二合一整合 ([`fundamental_analyzer.py`](/fundamental_analyzer.py), [`chart_generator.py`](/chart_generator.py))**：
  * 在 [`chart_generator.py`](/chart_generator.py) 新增 `build_stock_technical_chart_html`，將「近半年日 K 線、17MA/45MA/117MA/189MA 四線均線、成交量、KD(9,3,3)、MACD 與拖曳框選高低均價計算器」無縫嵌入戰情室 HTML。
  * 戰情室頁首新增**懸浮快速導覽列 (Sticky Navbar)**，方便手機與電腦快速跳轉：`[🤖 AI 深度診斷]` ➔ `[📈 技術面 K 線與均線]` ➔ `[📊 基本面六大指標圖表]`。
* **解決「圖例擠在最底部」痛點：改為 6 大獨立互動卡片，每張圖頂部配置專屬圖例**：
  * 將原本 1500px 單一超長 Subplot 畫布重構為 6 張獨立的 Plotly 互動卡片。
  * **每張圖右上角皆有獨立 Legend 圖例**，點擊即可即時開關線條；並啟用 `hovermode="x unified"`（垂直十字對齊懸浮窗），同一月份/季度的所有指標一次對比。
  * 針對雙單位圖表（月營收 vs YoY%、單季 EPS vs TTM EPS、現金流億元 vs 獲利含金量%）配置**獨立左右雙 Y 軸**，徹底解決折線被壓扁問題。
* **貼心「💡 財報與技術專有名詞白話小抄」（琥珀金提示框）**：
  * 在每張圖表頂部直接內嵌白話教學（YoY、三率三升、ROE 資金效率、營業費用率、DIO/DSO/DPO/CCC 存貨與收現天數、單季與近四季 TTM EPS 創高意義、OCF 營業現金流、CapEx 資本支出、FCF 自由現金流、獲利含金量 OCF/NI）。
* **手機好讀與一鍵複製轉傳優化 ([`stock_ai_command.py`](/stock_ai_command.py))**：
  * `/stock_ai` 回傳格式由不可選取複製的 Discord Embed 改為純文字 Markdown（超過 2,000 字自動智慧分段），手機版長按 1 秒即可「複製文字」轉傳他人。
  * 移除易超時失效的「🔄 重新呼叫 AI 分析」互動按鈕，統一收斂為乾淨直覺的 **`[📊 全方位戰情室（含財報+K線）]`** 連結按鈕。
* **整頓清理舊指令**：
  * 正式從主機器人 [`bot_server.py`](/bot_server.py) 中卸載舊版單一功能 `/stock` 指令，所有查股需求由功能完整 10 倍的 `/stock_ai` 統一服務。

### 🔧 錯誤修正 (Fixed)
* **修正 Discord 訊息 Markdown 超連結語法 ([`alignment_ai_picker.py`](/alignment_ai_picker.py))**：
  * 移除網址外層多餘的 `<>`（`[文字](<url>)` ➔ `[文字](url)`），解決 Discord Markdown 無法辨識導致變成純文字的問題；並在 Webhook 帶入 `"flags": 4` (`SUPPRESS_EMBEDS`)，避免超連結產生巨型預覽卡片洗版。
* **修正 `fundamental_analyzer.py` 缺失 `asyncio` 與 `logger` 模組宣告**。
* **修正 `discord.py` 2.7+ `view=None` 造成的 `TypeError`**。

---

## [v3.2.0] - 2026-10-18
### ✨ 新增功能 (Added)
* **每日 22:00 強勢股財報批次分析與 AI 嚴選進場決策系統 ([`alignment_ai_picker.py`](/alignment_ai_picker.py))**：
  * 新增 CLI 子命令 `python finmind_stock_scanner.py analyze-alignment --config finmind_scanner.json`（支援 `--date`、`--force-ai`、`--dry-run`）。
  * 自動讀取當日 19:00 產出之 `{date}_alignment_signals.csv`，結合 `MA_Signal_Tracker` 的「當日新進 (`is_new`)」、「連續符合天數 (`streak`)」與「每日執行狀況」，並在超過候選上限（預設 25 檔）時依「新進/剛突破 ➔ 17MA 上揚 ➔ 17/45MA 差距最小」自動預篩。
  * 新增 `systemd` 排程設定 [`deploy/finmind-ai-pick.service`](/deploy/finmind-ai-pick.service) 與 [`deploy/finmind-ai-pick.timer`](/deploy/finmind-ai-pick.timer)（週一至週五 22:00 自動執行）。
* **法說會未來展望定向檢索與「財報領先指標交叉佐證」([`fundamental_analyzer.py`](/fundamental_analyzer.py))**：
  * 新增 `fetch_earnings_call_and_outlook(stock_id, stock_name)`：透過 Google News RSS 定向檢索近 180 天內該公司之「法說、展望、訂單、產能、資本支出、毛利率、財測」報導與摘要（純 Python 執行，0 AI 額度消耗）。
  * 升級單股診斷 Prompt (`PROMPT_TEMPLATES["full_diagnosis"]`)：要求 AI 先提煉法說展望 (Step A)，再強制對照 **表 2 合約負債、表 3 資本支出、表 1 毛利率/營益率、表 5 月營收 YoY、表 6 法人買賣超** 進行真實性驗證 (Step B)，給出 `🟢高度可信 / 🟡待驗證 / 🔴存疑` 評級。
* **雙層 Gemini 模型分工與免費額度 429 自動降級保護 ([`ai_weather.py`](/ai_weather.py))**：
  * 第一階段（單股財報＋法說驗證）維持使用高配額 `gemini-flash-lite-latest`（15 RPM / 500 RPD）。
  * 第二階段（22:00 跨股綜合評選，每晚僅 1 次）指定使用高階推理模型 `gemini-3.8-flash`（5 RPM / 20 RPD），若遇 `429 RESOURCE_EXHAUSTED` 或 `503` 自動無縫降級至 `gemini-flash-lite-latest`。
* **Google Sheets 第 10 張工作表 `AI_Daily_Picks` ([`fundamental_config.py`](/fundamental_config.py))**：
  * 自動記錄每日 22:00 AI 嚴選出的 `TOP_PICK`、`WATCHLIST` 與 `AVOID` 清單、推薦時收盤價、17MA/45MA 支撐防守價、單股體質分、總決策推薦分與法說展望評級，方便長期回測追蹤。

### 🔧 優化與修正 (Changed & Fixed)
* **結構化 `ai_summary_json`**：單股 AI 輸出新增 `===SUMMARY_JSON===` 結構化區塊，完整保留結論、評分、亮點、風險、ROE/FCF、籌碼、法說驗證與技術面觀察，解決原本 `ai_text[:1500]` 硬截斷導致後半段資訊遺失的問題。
* **批次模式 Google Sheets 記憶體快取就地更新 (`enable_batch_mode`)**：修正每次寫入即清空整張表快取的問題，避免批次同步 20+ 檔股票時觸發 Google Sheets 每分鐘 60 次讀取上限 (`429 Too Many Requests`)。
* **修正單股 AI 快取指紋 (`cache_period`)**：移除 `check_and_sync_stock` 提前寫入 `ai_cache_day` 的邏輯，改以純資料水位 (`last_fin_qtr` + `last_rev_ym` + `max_div_raw_date` + `max_chip_raw_date`) 判定快取命中，且空白摘要不再誤判為命中。
* **FinMind 限流器支援 Host 過濾 (`only_hosts`)**：22:00 批次執行時僅對 `finmindtrade.com` 計入 FinMind 滑動視窗配額，不誤擋 Google Sheets、Gemini 與 Discord 請求。

---

## [v3.1.5] - 2026-10-16
### ✨ 新增功能 (Added)
* **五線均線強勢排列升級 (`MA3 / MA17 / MA45 / MA117 / MA189`) ([`finmind_stock_scanner.py`](/finmind_stock_scanner.py))**：
  * 新增 `3MA` 條件（`MA3` 必須 `>=` 所有均線），確保短線動能維持強勢。
  * 新增 `describe_ma17_trend`：比對今日 `17MA` 與前 1 日、前 5 日變化，自動分類為 `📈 連漲上揚`、`↗️ 轉折向上`、`➡️ 走平整理`、`↘️ 漲勢收斂`、`📉 持續下彎`。
* **Google Sheets `MA_Signal_Tracker` 訊號追蹤工作表**：
  * 每日 19:00 掃描後自動與歷史紀錄比對，計算「當日新進（🆕 是 / 🔄 否）」、「連續天數 (`streak`)」與「每日執行狀況（如 🔥 轉強突破、⚠️ 訊號收斂）」，並透過 Google Sheets API 自動套用條件格式底色與欄寬美化。
* **單股基本面 AI 診斷整合「表 7：近期日線與均線排列動態」([`fundamental_analyzer.py`](/fundamental_analyzer.py))**：
  * `/stock_ai` 自動搜尋本地最新 `prices.csv`，將個股最新收盤價、五條均線與 `17MA` 走勢注入 AI 數據包。

---

## [v3.1.0] - 2026-10-14
### ✨ 新增功能 (Added)
* **`/stock_ai` 台股基本面三大財報、股利與籌碼智慧快取分析系統 ([`stock_ai_command.py`](/stock_ai_command.py), [`fundamental_cache.py`](/fundamental_cache.py), [`fundamental_analyzer.py`](/fundamental_analyzer.py))**：
  * 建立 Google Sheets 8 張核心財報與籌碼分頁（`00_Watchlist_Master` ~ `07_Chip_Summary`），支援法定截止日水位檢查與回補 1 期增量更新。
  * 實作現金流量表 Q2/Q3/Q4 累季轉單季拆解演算法、資產負債表平衡驗證與自由現金流驗算。
  * 支援單股完整體質診斷、特定財報問題追問 (`prompt`)、以及多股橫向比較（如 `/stock_ai symbol:2357,2330`，最多 4 檔）。
  * 自動生成 Plotly 6 宮格互動式財報戰情室網頁 (`{stock_id}_fund.html`) 並上傳至 GitHub Pages，於 Discord 提供三顆互動按鈕。

---

## [v2.0.0] - 早期基礎版本
* **`/stock` 技術面互動式 K 線圖 ([`chart_generator.py`](/chart_generator.py), [`github_uploader.py`](/github_uploader.py))**：整合 FinMind 日 K 與 Yahoo Finance 成交量，繪製 MA/KD/MACD 互動圖表並自動發布至 GitHub Pages。
* **每日 19:00 五大產業均線批次掃描器 ([`finmind_stock_scanner.py`](/finmind_stock_scanner.py))**：含 SQLite 滑動視窗限流器（540 次/小時）與 Discord 自動分段推播。
* **生活與系統助理 ([`ai_weather.py`](/ai_weather.py), [`bot_server.py`](/bot_server.py))**：提供 `/ai` 萬用問答、`/weather` 即時天氣、每日 08:00 早安台北天氣排程、每 6 小時伺服器心跳通知與 `/card`、`/ping`、`/say`、`/roll`、`/buttons` 工具指令。
