"""無未來函數事後審計閉環 (`quant/signal_audit_service.py`).

SASD v5.4.0-Phase3：
- SQLite `signal_audit_log` 審計表（預設 `data/signal_audit.db`）。
- `evaluate_forward_path()`：三條防自欺鐵律（Open_{T+1}+0.5% 摩擦成本、停損優先悲觀認定、
  REJECT_TRAP 錯殺率追蹤）。純函式，不依賴網路，方便單元測試。
- `SignalAuditStore`：記錄訊號、盤後回填、週報統計。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_DB_PATH = Path("data") / "signal_audit.db"
SIGNAL_AUDIT_GSHEET_TAB = "09_Signal_Audit_Log"
SIGNAL_AUDIT_GSHEET_HEADERS: list[str] = [
    "audit_id",
    "signal_date",
    "stock_id",
    "stock_name",
    "sector_name",
    "regime_state",
    "scanner_signal_type",
    "ai_verdict_grade",
    "execution_strategy_type",
    "close_t",
    "ideal_buy_zone_low",
    "ideal_buy_zone_high",
    "chase_limit_price",
    "hard_stop_loss",
    "tp1_price",
    "deterministic_rr_ratio",
    "open_t1",
    "effective_entry_price",
    "entry_fill_status",
    "was_buy_zone_filled",
    "outcome_status_10d",
    "net_return_5d_pct",
    "net_return_10d_pct",
    "mfe_10d_pct",
    "mae_10d_pct",
    "is_whipsawed",
    "is_false_negative_miss",
    "created_at",
    "updated_at",
]

FRICTION_MULTIPLIER = 1.005  # 0.5% 雙邊摩擦成本（手續費、證交稅、滑價）
HORIZON_BARS = 10  # 追蹤 T+1 ~ T+10
PULLBACK_FILL_WINDOW = 3  # Style B 限價單有效 T+1 ~ T+3
FALSE_NEGATIVE_MFE_PCT = 8.0

SIGNAL_RIGHT_BREAKOUT = "RIGHT_BREAKOUT"
SIGNAL_STYLE_B_PULLBACK = "STYLE_B_PULLBACK"
GRADE_REJECT_TRAP = "REJECT_TRAP"
GRADE_S_TIER = "S_TIER_SNIPER"

FILL_PENDING = "PENDING"
FILL_T1 = "FILLED_T1"
FILL_PULLBACK = "FILLED_PULLBACK"
FILL_UNFILLED = "UNFILLED_MISSED"
FILL_SKIPPED = "SKIPPED_GAP_TOO_HIGH"

OUTCOME_PENDING = "PENDING"
OUTCOME_TP1 = "TP1_HIT_WIN"
OUTCOME_STOP = "STOP_LOSS_HIT"
OUTCOME_TIME_EXIT = "TIME_EXIT_HOLD"
OUTCOME_UNFILLED = "UNFILLED"

SIGNAL_AUDIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signal_audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT
    , signal_date TEXT NOT NULL
    , stock_id TEXT NOT NULL
    , stock_name TEXT NOT NULL
    , sector_name TEXT
    , regime_state TEXT NOT NULL
    , scanner_signal_type TEXT NOT NULL
    , ai_verdict_grade TEXT NOT NULL
    , execution_strategy_type TEXT NOT NULL
    , close_t REAL NOT NULL
    , ideal_buy_zone_low REAL
    , ideal_buy_zone_high REAL
    , chase_limit_price REAL NOT NULL
    , hard_stop_loss REAL NOT NULL
    , tp1_price REAL NOT NULL
    , deterministic_rr_ratio REAL NOT NULL
    , open_t1 REAL
    , effective_entry_price REAL
    , entry_fill_status TEXT DEFAULT 'PENDING'
    , was_buy_zone_filled INTEGER DEFAULT 0
    , outcome_status_10d TEXT DEFAULT 'PENDING'
    , net_return_5d_pct REAL
    , net_return_10d_pct REAL
    , mfe_10d_pct REAL
    , mae_10d_pct REAL
    , is_whipsawed INTEGER DEFAULT 0
    , is_false_negative_miss INTEGER DEFAULT 0
    , created_at TEXT NOT NULL
    , updated_at TEXT NOT NULL
    , UNIQUE (signal_date, stock_id)
);

CREATE INDEX IF NOT EXISTS idx_signal_audit_date_grade
    ON signal_audit_log (signal_date, ai_verdict_grade);
"""


def _f(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _ret_pct(exit_price: float, entry_price: float) -> float:
    return round((exit_price - entry_price) / entry_price * 100.0, 4)


def _walk_path(
    bars: Sequence[Mapping[str, Any]],
    entry_price: float,
    hard_stop: float,
    tp1: float,
    *,
    bars_before_entry: int,
) -> dict[str, Any]:
    """自成交日起逐日走停損優先路徑。`bars` 為成交日(含)起至 T+10 的 K 線。"""
    outcome = OUTCOME_TIME_EXIT
    exit_price: float | None = None
    exit_day: int | None = None  # 以 T+N 計
    whipsawed = 0
    mfe_high = entry_price
    mae_low = entry_price

    for idx, bar in enumerate(bars):
        high = float(bar["high"])
        low = float(bar["low"])
        mfe_high = max(mfe_high, high)
        mae_low = min(mae_low, low)
        day_no = bars_before_entry + idx + 1  # T+N

        # 鐵律 2：同日同時碰到停損與停利 → 悲觀認定停損
        if low <= hard_stop:
            outcome = OUTCOME_STOP
            exit_price = hard_stop
            exit_day = day_no
            if max((float(b["high"]) for b in bars[idx + 1:]), default=0.0) >= tp1:
                whipsawed = 1
            break
        if high >= tp1:
            outcome = OUTCOME_TP1
            exit_price = tp1
            exit_day = day_no
            break

    if exit_price is None and bars:
        exit_price = float(bars[-1]["close"])
        exit_day = bars_before_entry + len(bars)

    return {
        "outcome": outcome,
        "exit_price": exit_price,
        "exit_day": exit_day,
        "whipsawed": whipsawed,
        "mfe_pct": _ret_pct(mfe_high, entry_price),
        "mae_pct": _ret_pct(mae_low, entry_price),
    }


def evaluate_forward_path(
    signal: Mapping[str, Any],
    forward_bars: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """依三條鐵律計算單筆訊號的事後績效。

    `forward_bars`：T+1 起升冪的日 K（open/high/low/close）。回傳可直接寫入
    `signal_audit_log` 對應欄位之 dict。資料不足以完成成交判定時回傳 PENDING。
    """
    bars = list(forward_bars)[:HORIZON_BARS]
    result: dict[str, Any] = {
        "open_t1": None,
        "effective_entry_price": None,
        "entry_fill_status": FILL_PENDING,
        "was_buy_zone_filled": 0,
        "outcome_status_10d": OUTCOME_PENDING,
        "net_return_5d_pct": None,
        "net_return_10d_pct": None,
        "mfe_10d_pct": None,
        "mae_10d_pct": None,
        "is_whipsawed": 0,
        "is_false_negative_miss": 0,
    }
    if not bars:
        return result

    open_t1 = float(bars[0]["open"])
    result["open_t1"] = open_t1
    signal_type = str(signal.get("scanner_signal_type") or "")
    grade = str(signal.get("ai_verdict_grade") or "")
    chase_limit = float(signal["chase_limit_price"])
    hard_stop = float(signal["hard_stop_loss"])
    tp1 = float(signal["tp1_price"])
    is_reject = grade == GRADE_REJECT_TRAP

    entry_idx: int | None = None
    entry_price: float | None = None

    if is_reject:
        # 鐵律 3：淘汰股以 Open_{T+1} * 1.005 為假設成本
        entry_idx = 0
        entry_price = round(open_t1 * FRICTION_MULTIPLIER, 2)
        result["entry_fill_status"] = FILL_T1
    elif signal_type == SIGNAL_STYLE_B_PULLBACK:
        zone_high = _f(signal.get("ideal_buy_zone_high"))
        if zone_high is None:
            zone_high = chase_limit
        window = bars[:PULLBACK_FILL_WINDOW]
        for idx, bar in enumerate(window):
            if float(bar["low"]) <= zone_high:
                entry_idx = idx
                entry_price = round(min(float(bar["open"]), zone_high) * FRICTION_MULTIPLIER, 2)
                result["entry_fill_status"] = FILL_PULLBACK
                result["was_buy_zone_filled"] = 1
                break
        if entry_idx is None:
            if len(window) < PULLBACK_FILL_WINDOW:
                return result  # 尚未滿 3 日，仍待回填
            result["entry_fill_status"] = FILL_UNFILLED
            result["outcome_status_10d"] = OUTCOME_UNFILLED
            # 逆向選擇檢驗：未成交者以 Open_{T+1} 假設進場之報酬（僅供統計，不計入實盤損益）
            hypo_entry = round(open_t1 * FRICTION_MULTIPLIER, 2)
            hypo = _walk_path(bars, hypo_entry, hard_stop, tp1, bars_before_entry=0)
            if hypo["exit_price"] is not None and (
                hypo["outcome"] != OUTCOME_TIME_EXIT or len(bars) >= HORIZON_BARS
            ):
                result["net_return_10d_pct"] = _ret_pct(hypo["exit_price"], hypo_entry)
                result["mfe_10d_pct"] = hypo["mfe_pct"]
                result["mae_10d_pct"] = hypo["mae_pct"]
            return result
    else:  # RIGHT_BREAKOUT
        if open_t1 <= chase_limit:
            entry_idx = 0
            entry_price = round(open_t1 * FRICTION_MULTIPLIER, 2)
            result["entry_fill_status"] = FILL_T1
        elif float(bars[0]["low"]) <= chase_limit:
            entry_idx = 0
            entry_price = round(chase_limit * FRICTION_MULTIPLIER, 2)
            result["entry_fill_status"] = FILL_T1
        else:
            result["entry_fill_status"] = FILL_SKIPPED
            result["outcome_status_10d"] = OUTCOME_UNFILLED
            return result

    assert entry_idx is not None and entry_price is not None
    result["effective_entry_price"] = entry_price
    path_bars = bars[entry_idx:]
    walk = _walk_path(path_bars, entry_price, hard_stop, tp1, bars_before_entry=entry_idx)

    result["outcome_status_10d"] = walk["outcome"]
    result["mfe_10d_pct"] = walk["mfe_pct"]
    result["mae_10d_pct"] = walk["mae_pct"]
    result["is_whipsawed"] = walk["whipsawed"]

    finished = walk["outcome"] != OUTCOME_TIME_EXIT or len(bars) >= HORIZON_BARS
    if not finished:
        result["outcome_status_10d"] = OUTCOME_PENDING

    if walk["exit_price"] is not None and finished:
        # 觸發停損／停利即鎖定報酬，不以第 10 日收盤美化
        result["net_return_10d_pct"] = _ret_pct(walk["exit_price"], entry_price)

    # T+5 報酬：若 T+5 前已出場則鎖定出場報酬，否則取第 5 根收盤
    if walk["outcome"] != OUTCOME_TIME_EXIT and walk["exit_day"] is not None and walk["exit_day"] <= 5:
        result["net_return_5d_pct"] = _ret_pct(walk["exit_price"], entry_price)
    elif len(bars) >= 5 and entry_idx <= 4:
        result["net_return_5d_pct"] = _ret_pct(float(bars[4]["close"]), entry_price)

    if is_reject and finished:
        never_stopped = walk["outcome"] != OUTCOME_STOP
        hit_big = walk["outcome"] == OUTCOME_TP1 or walk["mfe_pct"] >= FALSE_NEGATIVE_MFE_PCT
        result["is_false_negative_miss"] = 1 if (never_stopped and hit_big) else 0

    return result


BarsFetcher = Callable[[str, str], Sequence[Mapping[str, Any]]]

_UPDATE_COLUMNS = (
    "open_t1", "effective_entry_price", "entry_fill_status", "was_buy_zone_filled",
    "outcome_status_10d", "net_return_5d_pct", "net_return_10d_pct", "mfe_10d_pct",
    "mae_10d_pct", "is_whipsawed", "is_false_negative_miss",
)


class SignalAuditStore:
    """`signal_audit_log` 的存取、盤後回填與週報統計。"""

    def __init__(self, db_path: str | Path = DEFAULT_AUDIT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if str(self.db_path) == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:")
                self._memory_conn.row_factory = sqlite3.Row
            return self._memory_conn
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        with closing(conn) if str(self.db_path) != ":memory:" else _noclose(conn):
            conn.executescript(SIGNAL_AUDIT_SCHEMA_SQL)
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def record_signal(self, signal: Mapping[str, Any]) -> None:
        """寫入（或更新未回填的）訊號；同 (signal_date, stock_id) 不重複。"""
        now = self._now()
        conn = self._connect()
        with closing(conn) if str(self.db_path) != ":memory:" else _noclose(conn):
            conn.execute(
                """
                INSERT INTO signal_audit_log (
                    signal_date, stock_id, stock_name, sector_name, regime_state
                    , scanner_signal_type, ai_verdict_grade, execution_strategy_type
                    , close_t, ideal_buy_zone_low, ideal_buy_zone_high, chase_limit_price
                    , hard_stop_loss, tp1_price, deterministic_rr_ratio, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (signal_date, stock_id) DO UPDATE SET
                    ai_verdict_grade = excluded.ai_verdict_grade
                    , execution_strategy_type = excluded.execution_strategy_type
                    , updated_at = excluded.updated_at
                """,
                (
                    str(signal["signal_date"]), str(signal["stock_id"]),
                    str(signal.get("stock_name") or signal["stock_id"]),
                    signal.get("sector_name"), str(signal.get("regime_state") or "YELLOW_ROTATION"),
                    str(signal["scanner_signal_type"]), str(signal["ai_verdict_grade"]),
                    str(signal.get("execution_strategy_type") or "NO_TRADE"),
                    float(signal["close_t"]), _f(signal.get("ideal_buy_zone_low")),
                    _f(signal.get("ideal_buy_zone_high")), float(signal["chase_limit_price"]),
                    float(signal["hard_stop_loss"]), float(signal["tp1_price"]),
                    float(_f(signal.get("deterministic_rr_ratio")) or 0.0), now, now,
                ),
            )
            conn.commit()

    def backfill_pending(self, bars_fetcher: BarsFetcher) -> int:
        """回填尚未結算的訊號。`bars_fetcher(stock_id, signal_date)` 回傳 T+1 起之日 K。"""
        conn = self._connect()
        updated = 0
        with closing(conn) if str(self.db_path) != ":memory:" else _noclose(conn):
            rows = conn.execute(
                "SELECT * FROM signal_audit_log WHERE outcome_status_10d = ? "
                "OR entry_fill_status = ?",
                (OUTCOME_PENDING, FILL_PENDING),
            ).fetchall()
            for row in rows:
                try:
                    bars = bars_fetcher(row["stock_id"], row["signal_date"])
                except Exception:
                    logger.exception("audit backfill fetch failed: %s", row["stock_id"])
                    continue
                result = evaluate_forward_path(dict(row), bars)
                sets = ", ".join(f"{c} = ?" for c in _UPDATE_COLUMNS)
                conn.execute(
                    f"UPDATE signal_audit_log SET {sets}, updated_at = ? WHERE audit_id = ?",
                    (*[result[c] for c in _UPDATE_COLUMNS], self._now(), row["audit_id"]),
                )
                updated += 1
            conn.commit()
        return updated

    def build_weekly_summary(self) -> dict[str, Any]:
        """產出系統健康度週報：獲利因子、逆向選擇 Delta、AI 錯殺率與調整建議。"""
        conn = self._connect()
        with closing(conn) if str(self.db_path) != ":memory:" else _noclose(conn):
            rows = [dict(r) for r in conn.execute("SELECT * FROM signal_audit_log").fetchall()]

        settled = [
            r for r in rows
            if r["ai_verdict_grade"] != GRADE_REJECT_TRAP
            and r["entry_fill_status"] in (FILL_T1, FILL_PULLBACK)
            and r["outcome_status_10d"] in (OUTCOME_TP1, OUTCOME_STOP, OUTCOME_TIME_EXIT)
            and r["net_return_10d_pct"] is not None
        ]
        returns = [r["net_return_10d_pct"] for r in settled]
        gains = sum(x for x in returns if x > 0)
        losses = abs(sum(x for x in returns if x < 0))
        profit_factor = (gains / losses) if losses > 0 else (None if gains == 0 else float("inf"))

        def _win_rate(items: list[dict[str, Any]]) -> float | None:
            vals = [r["net_return_10d_pct"] for r in items if r["net_return_10d_pct"] is not None]
            return (sum(1 for v in vals if v > 0) / len(vals)) if vals else None

        filled_pullback = [r for r in settled if r["entry_fill_status"] == FILL_PULLBACK]
        unfilled = [r for r in rows if r["entry_fill_status"] == FILL_UNFILLED]
        filled_wr = _win_rate(filled_pullback)
        unfilled_wr = _win_rate(unfilled)
        adverse_delta = (
            unfilled_wr - filled_wr if filled_wr is not None and unfilled_wr is not None else None
        )

        rejects = [
            r for r in rows
            if r["ai_verdict_grade"] == GRADE_REJECT_TRAP and r["outcome_status_10d"] != OUTCOME_PENDING
        ]
        fn_rate = (
            sum(r["is_false_negative_miss"] for r in rejects) / len(rejects) if rejects else None
        )

        s_tier = [r for r in settled if r["ai_verdict_grade"] == GRADE_S_TIER]
        s_stop_rate = (
            sum(1 for r in s_tier if r["outcome_status_10d"] == OUTCOME_STOP) / len(s_tier)
            if s_tier else None
        )

        suggestions: list[str] = []
        if adverse_delta is not None and adverse_delta > 0.10:
            suggestions.append("未成交標的勝率顯著高於低接成交者，建議將 ideal_buy_zone_high 由 MA17+0.4ATR 放寬至 MA17+0.6ATR。")
        if fn_rate is not None and fn_rate > 0.30:
            suggestions.append("AI 錯殺率 > 30%，建議放寬 Prompt 空頭審計標準。")
        if s_stop_rate is not None and s_stop_rate > 0.35:
            suggestions.append("S_TIER_SNIPER 停損率 > 35%，建議收緊微結構過濾。")
        if profit_factor is not None and profit_factor < 2.0:
            suggestions.append("實戰獲利因子低於目標 2.0，請檢視停損與目標價設定。")

        return {
            "settled_trades": len(settled),
            "profit_factor": profit_factor,
            "filled_win_rate": filled_wr,
            "unfilled_hypothetical_win_rate": unfilled_wr,
            "adverse_selection_delta": adverse_delta,
            "false_negative_rate": fn_rate,
            "s_tier_stop_rate": s_stop_rate,
            "suggestions": suggestions,
        }

    def fetch_all_rows(self) -> list[dict[str, Any]]:
        """依 `signal_date DESC, stock_id ASC` 回傳完整審計表列。"""
        conn = self._connect()
        with closing(conn) if str(self.db_path) != ":memory:" else _noclose(conn):
            rows = conn.execute(
                "SELECT * FROM signal_audit_log ORDER BY signal_date DESC, stock_id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def sync_to_gsheet(
        self,
        *,
        spreadsheet_id: str | None = None,
        credentials_path: str | None = None,
        tab_name: str = SIGNAL_AUDIT_GSHEET_TAB,
    ) -> int:
        """將 `signal_audit_log` 增量／全表同步至 Google Sheets 專屬分頁（預設 `09_Signal_Audit_Log`）。"""
        rows = self.fetch_all_rows()
        if not rows:
            return 0
        creds = (
            credentials_path
            if credentials_path is not None
            else os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        )
        if creds and Path(creds).is_file():
            pass
        elif Path("certs/sheetbot.json").is_file():
            creds = "certs/sheetbot.json"
        elif Path("sheetbot.json").is_file():
            creds = "sheetbot.json"
        sheet_id = (
            spreadsheet_id
            if spreadsheet_id is not None
            else os.getenv("GSHEET_SPREADSHEET_ID", "").strip()
        )
        if not creds or not sheet_id:
            return 0

        try:
            import gspread

            gc = gspread.service_account(filename=creds)
            sh = gc.open_by_key(sheet_id)
            existing_tabs = {ws.title: ws for ws in sh.worksheets()}
            headers = list(SIGNAL_AUDIT_GSHEET_HEADERS)
            if tab_name not in existing_tabs:
                ws = sh.add_worksheet(
                    title=tab_name,
                    rows=max(len(rows) + 100, 500),
                    cols=len(headers),
                )
            else:
                ws = existing_tabs[tab_name]
            matrix: list[list[Any]] = [headers]
            for r in rows:
                matrix.append(["" if r.get(col) is None else r.get(col) for col in headers])
            if ws.row_count < len(matrix) + 20 or ws.col_count < len(headers):
                ws.resize(rows=max(ws.row_count, len(matrix) + 50), cols=max(ws.col_count, len(headers)))
            ws.clear()
            end_cell = gspread.utils.rowcol_to_a1(len(matrix), len(headers))
            ws.update(range_name=f"A1:{end_cell}", values=matrix, value_input_option="USER_ENTERED")
            return len(rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync %s to Google Sheets: %s", tab_name, exc)
            return 0


def format_weekly_summary_markdown(summary: Mapping[str, Any], *, as_of_str: str = "") -> str:
    """格式化每週五盤後 `Weekly Quantitative Audit Summary` 報告區塊。"""
    def _pct(val: Any) -> str:
        return f"{float(val) * 100:.1f}%" if isinstance(val, (int, float)) else "N/A"

    pf = summary.get("profit_factor")
    if pf is None:
        pf_str = "N/A"
    elif pf == float("inf"):
        pf_str = "∞（無虧損交易）"
    else:
        pf_str = f"{float(pf):.2f}"

    title_date = f" ({as_of_str})" if as_of_str else ""
    lines = [
        f"📊 【Weekly Quantitative Audit Summary 事後審計週報】{title_date}",
        f"- 已結算實戰訊號：{summary.get('settled_trades', 0)} 筆｜實戰淨獲利因子 (Profit Factor)：{pf_str}",
        (
            f"- 逆向選擇監控：低接成交勝率 {_pct(summary.get('filled_win_rate'))} vs "
            f"未成交飆股假設勝率 {_pct(summary.get('unfilled_hypothetical_win_rate'))} "
            f"（Delta: {_pct(summary.get('adverse_selection_delta'))}）"
        ),
        (
            f"- AI 審計校準：REJECT_TRAP 錯殺率 {_pct(summary.get('false_negative_rate'))}｜"
            f"S_TIER_SNIPER 停損率 {_pct(summary.get('s_tier_stop_rate'))}"
        ),
    ]
    suggestions = list(summary.get("suggestions") or [])
    if suggestions:
        lines.append("- 🔧 系統自適應調參建議：")
        for s in suggestions:
            lines.append(f"  - {s}")
    else:
        lines.append("- ✅ 系統健康度診斷：各項核心指標均在健康區間內，維持現行參數。")
    return "\n".join(lines)


def _df_to_ohlc_bars(df: pd.DataFrame, after_date_iso: str) -> list[dict[str, float]]:
    if df is None or df.empty or "date" not in df.columns:
        return []
    work = df.copy()
    work["_dt"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    work = work.dropna(subset=["_dt"])
    work = work.loc[work["_dt"] > str(after_date_iso)[:10]].sort_values("_dt").drop_duplicates(subset=["_dt"], keep="last")
    if work.empty:
        return []
    high_col = "high" if "high" in work.columns else ("max" if "max" in work.columns else "close")
    low_col = "low" if "low" in work.columns else ("min" if "min" in work.columns else "close")
    open_col = "open" if "open" in work.columns else "close"
    bars: list[dict[str, float]] = []
    for row in work.head(HORIZON_BARS).to_dict("records"):
        o, h, l, c = _f(row.get(open_col)), _f(row.get(high_col)), _f(row.get(low_col)), _f(row.get("close"))
        if None in (o, h, l, c):
            continue
        bars.append({"open": float(o), "high": float(h), "low": float(l), "close": float(c)})
    return bars


def build_local_and_finmind_bars_fetcher(
    daily_dir: Path | None,
    *,
    loader: Any | None = None,
    extra_prices_df: pd.DataFrame | None = None,
) -> BarsFetcher:
    """建立優先查詢本地 `daily/*_prices.csv`、缺漏才退回 FinMind 的 T+1~T+10 日 K 擷取器。"""
    frames: list[pd.DataFrame] = []
    if isinstance(extra_prices_df, pd.DataFrame) and not extra_prices_df.empty:
        frames.append(extra_prices_df)
    if daily_dir is not None and daily_dir.is_dir():
        # 取最新幾個 prices.csv（每個皆含近 6 個月日線，讀最新 2 份即可涵蓋所有近期與過往交易日）
        price_files = sorted(daily_dir.glob("*_prices.csv"))[-2:]
        for p in price_files:
            try:
                frames.append(pd.read_csv(p, dtype={"stock_id": str}))
            except Exception:  # noqa: BLE001
                continue

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    by_stock: dict[str, pd.DataFrame] = {}
    if not combined.empty and "stock_id" in combined.columns:
        combined["stock_id"] = combined["stock_id"].astype(str).str.strip()
        for sid, grp in combined.groupby("stock_id", sort=False):
            by_stock[str(sid)] = grp

    def _fetch(stock_id: str, signal_date: str) -> Sequence[Mapping[str, Any]]:
        sid = str(stock_id).strip()
        local_df = by_stock.get(sid)
        bars = _df_to_ohlc_bars(local_df, signal_date) if local_df is not None else []
        if bars or loader is None or not hasattr(loader, "taiwan_stock_daily"):
            return bars
        try:
            start_dt = (datetime.strptime(signal_date[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            end_dt = (datetime.strptime(signal_date[:10], "%Y-%m-%d") + timedelta(days=25)).strftime("%Y-%m-%d")
            remote_df = loader.taiwan_stock_daily(stock_id=sid, start_date=start_dt, end_date=end_dt)
            if isinstance(remote_df, pd.DataFrame) and not remote_df.empty:
                by_stock[sid] = remote_df
                return _df_to_ohlc_bars(remote_df, signal_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("FinMind fallback bars fetch failed for %s: %s", sid, exc)
        return bars

    return _fetch


class _noclose:
    """讓 in-memory 連線能共用 `with` 區塊而不被關閉。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, *exc: object) -> None:
        return None
