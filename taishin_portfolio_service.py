"""Taishin Securities Nova API Historical Backfill, Daily Accounting Sync, AI Portfolio Diagnosis & 0050 Smart DCA Service.

SASD v5.1.0:
- Pure background scheduled & CLI service (zero Discord slash commands; never modifies `bot_server.py`).
- Writes exclusively to personal Google Sheet (`TAISHIN_GSHEET_SPREADSHEET_ID`), never polluting `GSHEET_SPREADSHEET_ID`.
- Never uploads personal accounting or holdings data to GitHub Pages.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import gspread
from gspread.exceptions import APIError
import pandas as pd

import taishin  # noqa: F401 - registers services/, quant/, taishin/ on sys.path
from ai_service import (
    ask_gemini_diagnosis_with_meta,
    ask_vertex_ai_with_meta,
    flush_bq_archives,
    get_vertex_stage2_model,
)
from macro_context import fetch_global_macro_context
from taishin_client import (
    TaishinClient,
    TaishinClientError,
    extract_record_date,
    format_iso_date,
    inspect_sdk_capabilities,
    iter_date_chunks,
    normalize_sdk_obj,
    parse_flexible_date,
)
from taishin_config import (
    BACKFILL_CHUNK_SLEEP_SECONDS,
    BREAKEVEN_BUFFER_MULTIPLIER,
    DCA_0050_SHEET_NAME,
    DCA_0050_SYMBOL,
    DCA_10D_DROP_TRIGGER_PCT,
    DCA_KDK_TRIGGER,
    DCA_MA17_NEG_BIAS_TRIGGER_PCT,
    DCA_MAX_MULTIPLIER,
    DCA_MIN_MULTIPLIER,
    DCA_RSI14_TRIGGER,
    DEFAULT_ATTRIBUTION_LOOKBACK_TRADING_DAYS,
    DEFAULT_ATTRIBUTION_MAX_CALENDAR_DAYS,
    DEFAULT_BACKFILL_CHUNK_DAYS,
    DEFAULT_BACKFILL_FROM_DATE,
    DEFAULT_DCA_0050_WEEKLY_BUDGET_TWD,
    GSHEET_APPEND_BATCH_SIZE,
    GSHEET_RETRY_DELAYS_SECONDS,
    HOLDINGS_SHEET_NAME,
    LEADING_ZERO_PROTECTED_FIELDS,
    MEMORY_SHEET_NAME,
    RATCHET_PROFIT_TRIGGER_PCT,
    SETTLEMENT_SHEET_NAME,
    STANDARD_ETF_TAX_RATE,
    STANDARD_FEE_RATE,
    STANDARD_STOCK_TAX_RATE,
    STATEMENT_SHEET_NAME,
    TAISHIN_MORNING_BATTLE_PROMPT,
    TAISHIN_PORTFOLIO_AI_PROMPT,
    TAISHIN_SHEET_SCHEMAS,
    UNTRACKED_HOLDING_TAG,
    clean_leading_zero_code,
    get_unique_key_for_tab,
    normalize_ai_action_tag,
    protect_leading_zero,
)

logger = logging.getLogger(__name__)

TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
DISCORD_API_BASE_URL = "https://discord.com/api/v10"
DISCORD_CHUNK_LIMIT = 1800
REQUEST_TIMEOUT_SECONDS = 30


class TaishinPortfolioError(RuntimeError):
    """Raised when Taishin portfolio service encounters a configuration or data error."""


def _today_in_taipei() -> date:
    return datetime.now(TAIPEI_TIMEZONE).date()


def _now_iso_in_taipei() -> str:
    return datetime.now(TAIPEI_TIMEZONE).isoformat(timespec="seconds")


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        if math.isnan(value) or math.isinf(value):
            return default
        return float(value)
    text = str(value).strip().replace(",", "").rstrip("%").strip()
    if not text or text == "None":
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    return int(round(_to_float(value, float(default))))


# --------------------------------------------------------------------------- #
# Guardrail 5: Isolated Personal Google Sheet Manager with Batching & Dedup
# --------------------------------------------------------------------------- #
class TaishinSheetManager:
    """Manage the 4 tabs in `TAISHIN_GSHEET_SPREADSHEET_ID` with batch writes and leading-zero protection."""

    def __init__(
        self,
        spreadsheet_id: str | None = None,
        credentials_path: str | None = None,
        gspread_client: Any | None = None,
        spreadsheet: Any | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved_sheet_id = (
            spreadsheet_id
            if spreadsheet_id is not None
            else (os.getenv("TAISHIN_GSHEET_SPREADSHEET_ID") or "").strip()
        )
        public_sheet_id = (os.getenv("GSHEET_SPREADSHEET_ID") or "").strip()
        if not resolved_sheet_id:
            raise TaishinPortfolioError("TAISHIN_GSHEET_SPREADSHEET_ID 未設定，無法寫入個人帳務 Google Sheet。")
        if public_sheet_id and resolved_sheet_id == public_sheet_id:
            raise TaishinPortfolioError(
                "安全攔截：TAISHIN_GSHEET_SPREADSHEET_ID 不可與公用財報表 GSHEET_SPREADSHEET_ID 相同！"
            )

        resolved_creds = (
            credentials_path
            if credentials_path is not None
            else (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "certs/sheetbot.json").strip()
        )
        if resolved_creds and Path(resolved_creds).is_file():
            pass
        elif Path("certs/sheetbot.json").is_file():
            resolved_creds = "certs/sheetbot.json"
        elif Path("sheetbot.json").is_file():
            resolved_creds = "sheetbot.json"

        self.spreadsheet_id = resolved_sheet_id
        self.credentials_path = resolved_creds
        self._sleep = sleep_fn
        self._gc = gspread_client
        self._spreadsheet = spreadsheet
        self._worksheet_objs: dict[str, Any] = {}
        self._schema_ensured = False

    @property
    def spreadsheet(self) -> Any:
        if self._spreadsheet is None:
            if self._gc is None:
                self._gc = gspread.service_account(filename=self.credentials_path)
            self._spreadsheet = self._gspread_call(self._gc.open_by_key, self.spreadsheet_id)
        return self._spreadsheet

    def _gspread_call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute a gspread API call with 2s, 4s, 8s exponential backoff on HTTP 429 / 5xx."""
        delays = (*GSHEET_RETRY_DELAYS_SECONDS, None)
        for attempt, delay in enumerate(delays, start=1):
            try:
                return func(*args, **kwargs)
            except APIError as err:
                status = getattr(getattr(err, "response", None), "status_code", None)
                if status is None and "429" in str(err):
                    status = 429
                if status in (429, 500, 502, 503, 504) and delay is not None:
                    logger.warning(
                        "Taishin GSheet API %s rate-limited; retrying in %.1fs (attempt %d)...",
                        status,
                        delay,
                        attempt,
                    )
                    self._sleep(delay)
                    continue
                raise

    def ensure_schema(self) -> None:
        if self._schema_ensured:
            return
        worksheets = self._gspread_call(self.spreadsheet.worksheets)
        existing = {ws.title: ws for ws in worksheets}
        self._worksheet_objs.update(existing)

        for tab_name, headers in TAISHIN_SHEET_SCHEMAS.items():
            if tab_name not in existing:
                ws = self._gspread_call(
                    self.spreadsheet.add_worksheet,
                    title=tab_name,
                    rows=1000,
                    cols=len(headers),
                )
                self._gspread_call(
                    ws.update,
                    range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(headers))}",
                    values=[headers],
                )
                self._worksheet_objs[tab_name] = ws
                continue

            ws = existing[tab_name]
            first_row = [str(h).strip() for h in (self._gspread_call(ws.row_values, 1) or [])]
            if not first_row:
                self._gspread_call(
                    ws.update,
                    range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(headers))}",
                    values=[headers],
                )
            else:
                missing = [h for h in headers if h not in first_row]
                if missing:
                    merged_headers = first_row + missing
                    col_cnt = getattr(ws, "col_count", None)
                    if isinstance(col_cnt, int) and col_cnt < len(merged_headers):
                        self._gspread_call(ws.resize, cols=len(merged_headers))
                    self._gspread_call(
                        ws.update,
                        range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(merged_headers))}",
                        values=[merged_headers],
                    )
        self._schema_ensured = True

    def get_worksheet(self, tab_name: str) -> Any:
        self.ensure_schema()
        if tab_name not in self._worksheet_objs:
            self._worksheet_objs[tab_name] = self._gspread_call(self.spreadsheet.worksheet, tab_name)
        return self._worksheet_objs[tab_name]

    def read_records(self, tab_name: str) -> list[dict[str, Any]]:
        """Read all records from `tab_name` using unformatted values and clean leading-zero quotes."""
        ws = self.get_worksheet(tab_name)
        rows = self._gspread_call(
            ws.get_all_values,
            value_render_option=gspread.utils.ValueRenderOption.unformatted,
            date_time_render_option=gspread.utils.DateTimeOption.formatted_string,
        )
        if not rows or len(rows) < 2:
            return []
        headers = [str(h).strip() for h in rows[0]]
        records: list[dict[str, Any]] = []
        for raw_row in rows[1:]:
            padded = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
            rec: dict[str, Any] = {}
            for i, col in enumerate(headers):
                if not col:
                    continue
                val = padded[i]
                if col in LEADING_ZERO_PROTECTED_FIELDS:
                    val = clean_leading_zero_code(val)
                rec[col] = val
            if any(v not in ("", None) for v in rec.values()):
                records.append(rec)
        return records

    @staticmethod
    def _format_cell_for_sheet(col: str, val: Any) -> Any:
        if val is None:
            return ""
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return ""
        if col in LEADING_ZERO_PROTECTED_FIELDS:
            return protect_leading_zero(val)
        return val

    def append_deduplicated_rows(
        self,
        tab_name: str,
        rows: list[dict[str, Any]],
        *,
        sort_key: str | None = None,
        batch_size: int = GSHEET_APPEND_BATCH_SIZE,
    ) -> int:
        """Deduplicate against existing Unique Keys in `tab_name` and batch-append at most 500 rows per call."""
        if not rows:
            return 0

        headers = TAISHIN_SHEET_SCHEMAS[tab_name]
        existing_records = self.read_records(tab_name)
        seen_keys: set[str] = {
            get_unique_key_for_tab(tab_name, rec)
            for rec in existing_records
        }

        new_rows: list[dict[str, Any]] = []
        for row in rows:
            ukey = get_unique_key_for_tab(tab_name, row)
            if not ukey or ukey in seen_keys:
                continue
            seen_keys.add(ukey)
            new_rows.append(row)

        if not new_rows:
            return 0

        if sort_key:
            new_rows.sort(key=lambda r: (str(r.get(sort_key, "")), str(r.get("order_no", ""))))

        matrix = [
            [self._format_cell_for_sheet(col, r.get(col, "")) for col in headers]
            for r in new_rows
        ]

        ws = self.get_worksheet(tab_name)
        for idx in range(0, len(matrix), batch_size):
            batch = matrix[idx : idx + batch_size]
            self._gspread_call(
                ws.append_rows,
                batch,
                value_input_option="USER_ENTERED",
            )
        return len(new_rows)

    def overwrite_tab(self, tab_name: str, rows: list[dict[str, Any]]) -> int:
        """Overwrite an entire tab (used by `02_Portfolio_Holdings`) with leading-zero protection."""
        headers = TAISHIN_SHEET_SCHEMAS[tab_name]
        ws = self.get_worksheet(tab_name)
        self._gspread_call(ws.clear)

        matrix: list[list[Any]] = [list(headers)]
        for r in rows:
            matrix.append([self._format_cell_for_sheet(col, r.get(col, "")) for col in headers])

        self._gspread_call(
            ws.update,
            range_name=f"A1:{gspread.utils.rowcol_to_a1(len(matrix), len(headers))}",
            values=matrix,
            value_input_option="USER_ENTERED",
        )
        return len(rows)


# --------------------------------------------------------------------------- #
# Record Mapping & Cross-Verification Logic
# --------------------------------------------------------------------------- #
def _normalize_action(raw_action: Any) -> str:
    text = str(raw_action or "").strip().upper()
    if any(tok in text for tok in ("BUY", "B", "買", "買進", "現買", "資買", "券買")):
        return "BUY"
    if any(tok in text for tok in ("SELL", "S", "賣", "賣出", "現賣", "資賣", "券賣")):
        return "SELL"
    return text or "BUY"


def _normalize_order_type(raw_type: Any, filled_qty: int = 0) -> str:
    text = str(raw_type or "").strip()
    upper = text.upper()
    if any(tok in upper for tok in ("INTRADAYODD", "ODD", "零股")):
        return "盤中零股"
    if any(tok in upper for tok in ("MARGIN", "融資")):
        return "融資"
    if any(tok in upper for tok in ("SHORT", "融券")):
        return "融券"
    if 0 < filled_qty < 1000 and not text:
        return "盤中零股"
    return "現股"


def _pick_first(rec: Mapping[str, Any], *keys: str, default: Any = "") -> Any:
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return default


def normalize_statement_record(
    raw: Mapping[str, Any],
    *,
    source_api: str = "STATEMENT",
    default_account_no: str = "",
    synced_at: str | None = None,
) -> dict[str, Any]:
    """Normalize a raw SDK statement or filled_history record into `01_Account_Statement` schema."""
    now_iso = synced_at or _now_iso_in_taipei()
    trade_date_dt = extract_record_date(raw)
    trade_date = trade_date_dt.isoformat() if trade_date_dt else format_iso_date(
        _pick_first(raw, "trade_date", "filled_date", "t_date", "tdate", "ord_date", "order_date", "date")
    )
    settlement_date = format_iso_date(
        _pick_first(raw, "settlement_date", "settle_date", "s_date", "sdate"),
        default=trade_date,
    )
    account_no = clean_leading_zero_code(
        _pick_first(raw, "account_no", "account", "acct_no", "cust_id", default=default_account_no)
    )
    order_no = clean_leading_zero_code(
        _pick_first(raw, "order_no", "ord_no", "seq_no", "mkt_seq_no", "dseq", "filled_no", default="")
    )
    stock_no = clean_leading_zero_code(
        _pick_first(raw, "stock_no", "symbol", "stock_id", "code", default="")
    )
    stock_name = str(
        _pick_first(raw, "stock_name", "symbol_name", "sname", "name", default="")
    ).strip()
    filled_qty = _to_int(
        _pick_first(raw, "filled_qty", "filled_quantity", "qty", "quantity", "mat_qty", "shares", default=0)
    )
    order_type = _normalize_order_type(
        _pick_first(raw, "order_type_name", "market_type", "order_type", "trade_type", default=""),
        filled_qty=filled_qty,
    )
    action = _normalize_action(
        _pick_first(raw, "action", "buy_sell", "buy_sell_name", "bs", "side", default="BUY")
    )
    filled_price = round(
        _to_float(
            _pick_first(raw, "filled_price", "filled_avg_price", "price", "mat_price", "deal_price", default=0.0)
        ),
        4,
    )

    gross_raw = _pick_first(raw, "gross_amount", "filled_amount", "payment", "amount", "deal_amt", default=None)
    gross_amount = _to_int(gross_raw) if gross_raw is not None else int(round(filled_qty * filled_price))

    fee_raw = _pick_first(raw, "fee", "commission", default=None)
    if fee_raw is not None:
        fee = _to_int(fee_raw)
    elif source_api == "FILLED_HISTORY" and gross_amount > 0:
        fee = max(1, int(round(gross_amount * STANDARD_FEE_RATE)))
    else:
        fee = 0

    tax_raw = _pick_first(raw, "tax", "trans_tax", default=None)
    if tax_raw is not None:
        tax = _to_int(tax_raw)
    elif source_api == "FILLED_HISTORY" and action == "SELL" and gross_amount > 0:
        tax_rate = STANDARD_ETF_TAX_RATE if stock_no.startswith("00") else STANDARD_STOCK_TAX_RATE
        tax = int(round(gross_amount * tax_rate))
    else:
        tax = 0

    net_raw = _pick_first(raw, "net_amount", "net_amt", "settle_amt", default=None)
    if net_raw is not None and _to_int(net_raw) != 0:
        net_amount = _to_int(net_raw)
        if action == "BUY" and net_amount > 0:
            net_amount = -net_amount
    else:
        net_amount = -(gross_amount + fee) if action == "BUY" else (gross_amount - fee - tax)

    return {
        "trade_date": trade_date,
        "settlement_date": settlement_date,
        "source_api": source_api,
        "account_no": account_no,
        "order_no": order_no,
        "stock_no": stock_no,
        "stock_name": stock_name,
        "order_type": order_type,
        "action": action,
        "filled_qty": filled_qty,
        "filled_price": filled_price,
        "gross_amount": gross_amount,
        "fee": fee,
        "tax": tax,
        "net_amount": net_amount,
        "raw_json": json.dumps(normalize_sdk_obj(raw), ensure_ascii=False, default=str),
        "synced_at": now_iso,
    }


def merge_statement_and_fills(
    statement_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Cross-compare `STATEMENT` rows with `FILLED_HISTORY` rows; supplement missing trades."""
    merged = list(statement_rows)
    exact_keys: set[str] = set()
    loose_keys: set[tuple[str, str, str, int, float]] = set()
    statement_order_keys: set[tuple[str, str, str, str]] = set()

    for r in statement_rows:
        exact_keys.add(get_unique_key_for_tab(STATEMENT_SHEET_NAME, r))
        t_date = str(r.get("trade_date", ""))
        ord_no = clean_leading_zero_code(r.get("order_no", ""))
        stk_no = clean_leading_zero_code(r.get("stock_no", ""))
        act = str(r.get("action", "")).upper()
        if ord_no:
            statement_order_keys.add((t_date, ord_no, stk_no, act))
        loose_keys.add(
            (
                t_date,
                stk_no,
                act,
                _to_int(r.get("filled_qty", 0)),
                round(_to_float(r.get("filled_price", 0.0)), 4),
            )
        )

    supplemented = 0
    for f in fill_rows:
        ukey = get_unique_key_for_tab(STATEMENT_SHEET_NAME, f)
        t_date = str(f.get("trade_date", ""))
        ord_no = clean_leading_zero_code(f.get("order_no", ""))
        stk_no = clean_leading_zero_code(f.get("stock_no", ""))
        act = str(f.get("action", "")).upper()
        lkey = (
            t_date,
            stk_no,
            act,
            _to_int(f.get("filled_qty", 0)),
            round(_to_float(f.get("filled_price", 0.0)), 4),
        )
        if (
            ukey in exact_keys
            or lkey in loose_keys
            or (ord_no and (t_date, ord_no, stk_no, act) in statement_order_keys)
        ):
            continue
        exact_keys.add(ukey)
        loose_keys.add(lkey)
        merged.append(f)
        supplemented += 1

    return merged, supplemented


def normalize_settlement_or_pnl_record(
    raw: Mapping[str, Any],
    *,
    record_type: str,
    synced_at: str | None = None,
) -> dict[str, Any]:
    """Normalize a settlement or realized PnL record into `03_Settlement_And_PnL` schema."""
    now_iso = synced_at or _now_iso_in_taipei()
    rec_dt = extract_record_date(raw)
    record_date = rec_dt.isoformat() if rec_dt else (
        format_iso_date(_pick_first(raw, "record_date", "s_date", "t_date", "settlement_date", "settle_date", "date"))
        or _today_in_taipei().isoformat()
    )
    stock_no = clean_leading_zero_code(_pick_first(raw, "stock_no", "symbol", "stock_id", "code", default=""))
    stock_name = str(_pick_first(raw, "stock_name", "symbol_name", "sname", "name", default="")).strip()
    if stock_name and not stock_no and stock_name == str(raw.get("name", "")).strip():
        stock_name = ""
    qty = _to_int(_pick_first(raw, "qty", "closed_quantity", "quantity", "filled_qty", "shares", default=0))
    buy_detail = raw.get("buy_detail") if isinstance(raw.get("buy_detail"), Mapping) else {}
    sell_detail = raw.get("sell_detail") if isinstance(raw.get("sell_detail"), Mapping) else {}
    buy_price = round(
        _to_float(
            _pick_first(raw, "buy_price", "cost_price", "avg_price", default=None)
            or _pick_first(buy_detail, "filled_price", "price", default=0.0)
        ),
        4,
    )
    if buy_price <= 0 and qty > 0 and _to_float(raw.get("cost"), 0.0) > 0:
        buy_price = round(_to_float(raw.get("cost")) / qty, 2)
    sell_price = round(
        _to_float(
            _pick_first(raw, "sell_price", "filled_price", "deal_price", "price", default=None)
            or _pick_first(sell_detail, "filled_price", "price", default=0.0)
        ),
        4,
    )
    net_amount = _to_int(
        _pick_first(raw, "net_amount", "income", "settle_amount", "net_amt", "amount", default=0)
    )
    realized_pnl = _to_int(_pick_first(raw, "realized_pnl", "profit_loss", "pnl", "profit", default=0))
    realized_return_pct = round(
        _to_float(_pick_first(raw, "realized_return_pct", "profit_loss_rate", "return_pct", "profit_rate", default=0.0)),
        2,
    )

    return {
        "record_date": record_date,
        "record_type": record_type,
        "stock_no": stock_no,
        "stock_name": stock_name,
        "qty": qty,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "net_amount": net_amount,
        "realized_pnl": realized_pnl,
        "realized_return_pct": realized_return_pct,
        "raw_json": json.dumps(normalize_sdk_obj(raw), ensure_ascii=False, default=str),
        "synced_at": now_iso,
    }


def compute_weighted_avg_cost_from_statements(
    statements: list[dict[str, Any]],
) -> dict[str, float]:
    """Compute weighted average buy cost per `stock_no` from historical `01_Account_Statement` records."""
    sorted_rows = sorted(
        statements,
        key=lambda r: (str(r.get("trade_date", "")), str(r.get("order_no", ""))),
    )
    # Track both running position cost and cumulative BUY-only fallback
    pos_qty: dict[str, int] = {}
    pos_cost: dict[str, float] = {}
    buy_qty_total: dict[str, int] = {}
    buy_cost_total: dict[str, float] = {}

    for r in sorted_rows:
        stock_no = clean_leading_zero_code(r.get("stock_no", ""))
        if not stock_no:
            continue
        action = str(r.get("action", "")).upper()
        qty = _to_int(r.get("filled_qty", 0))
        price = _to_float(r.get("filled_price", 0.0))
        if qty <= 0 or price <= 0:
            continue

        if action == "BUY":
            pos_qty[stock_no] = pos_qty.get(stock_no, 0) + qty
            pos_cost[stock_no] = pos_cost.get(stock_no, 0.0) + (qty * price)
            buy_qty_total[stock_no] = buy_qty_total.get(stock_no, 0) + qty
            buy_cost_total[stock_no] = buy_cost_total.get(stock_no, 0.0) + (qty * price)
        elif action == "SELL":
            cur_q = pos_qty.get(stock_no, 0)
            if cur_q > 0:
                avg_p = pos_cost[stock_no] / cur_q
                rem_q = max(0, cur_q - qty)
                pos_qty[stock_no] = rem_q
                pos_cost[stock_no] = rem_q * avg_p

    result: dict[str, float] = {}
    all_stocks = set(buy_qty_total.keys()) | set(pos_qty.keys())
    for stock_no in all_stocks:
        if pos_qty.get(stock_no, 0) > 0:
            result[stock_no] = round(pos_cost[stock_no] / pos_qty[stock_no], 2)
        elif buy_qty_total.get(stock_no, 0) > 0:
            result[stock_no] = round(buy_cost_total[stock_no] / buy_qty_total[stock_no], 2)
    return result


# --------------------------------------------------------------------------- #
# Section 4.1: Chunked Historical Backfill (`backfill-history`)
# --------------------------------------------------------------------------- #
def backfill_history(
    *,
    client: TaishinClient,
    sheet_mgr: TaishinSheetManager | None,
    from_date: date | str = DEFAULT_BACKFILL_FROM_DATE,
    to_date: date | str | None = None,
    chunk_days: int = DEFAULT_BACKFILL_CHUNK_DAYS,
    sleep_seconds: float = BACKFILL_CHUNK_SLEEP_SECONDS,
    dry_run: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch all historical statements, fills, settlements, and realized PnL in 30-day chunks."""
    end_dt = parse_flexible_date(to_date) if to_date else _today_in_taipei()
    start_dt = parse_flexible_date(from_date)
    if start_dt is None or end_dt is None:
        raise ValueError(f"Invalid backfill date range: {from_date!r} ~ {to_date!r}")

    chunks = iter_date_chunks(start_dt, end_dt, chunk_days=chunk_days)
    # Guardrail 4: Only 1 sdk.login() across the entire process
    client.ensure_logged_in()
    account_no = client.get_account_no_str()
    now_iso = _now_iso_in_taipei()

    all_statement_rows: list[dict[str, Any]] = []
    all_settlement_rows: list[dict[str, Any]] = []
    chunk_summaries: list[dict[str, Any]] = []

    for idx, (c_start, c_end) in enumerate(chunks):
        raw_stmt = client.fetch_account_statement(c_start, c_end)
        raw_fills = client.fetch_filled_history(c_start, c_end)
        raw_fill_details = client.fetch_filled_history_detail(c_start, c_end)
        raw_pnl = client.fetch_realized_profit_and_losses(c_start, c_end)
        raw_hist_settle = client.fetch_history_settlement(c_start, c_end)

        stmt_rows = [
            normalize_statement_record(
                r,
                source_api="STATEMENT",
                default_account_no=account_no,
                synced_at=now_iso,
            )
            for r in raw_stmt
        ]
        fill_rows = [
            normalize_statement_record(
                r,
                source_api="FILLED_HISTORY",
                default_account_no=account_no,
                synced_at=now_iso,
            )
            for r in [*raw_fills, *raw_fill_details]
        ]
        merged_stmt, supp_count = merge_statement_and_fills(stmt_rows, fill_rows)
        all_statement_rows.extend(merged_stmt)

        pnl_rows = [
            normalize_settlement_or_pnl_record(r, record_type="REALIZED_PNL", synced_at=now_iso)
            for r in raw_pnl
        ]
        settle_rows = [
            normalize_settlement_or_pnl_record(r, record_type="HISTORY_SETTLEMENT", synced_at=now_iso)
            for r in raw_hist_settle
        ]
        all_settlement_rows.extend(pnl_rows)
        all_settlement_rows.extend(settle_rows)

        summary = {
            "chunk_start": c_start.isoformat(),
            "chunk_end": c_end.isoformat(),
            "statement_fetched": len(stmt_rows),
            "fill_supplemented": supp_count,
            "pnl_and_settlement_fetched": len(pnl_rows) + len(settle_rows),
        }
        chunk_summaries.append(summary)
        print(
            f"[backfill] 區間 {c_start.isoformat()} ~ {c_end.isoformat()}："
            f"對帳單 {len(stmt_rows)} 筆、成交明細補遺 {supp_count} 筆、"
            f"已實現損益/交割 {len(pnl_rows) + len(settle_rows)} 筆"
        )

        if idx < len(chunks) - 1 and sleep_seconds > 0:
            sleep_fn(sleep_seconds)

    added_statements = 0
    added_settlements = 0
    if not dry_run and sheet_mgr is not None:
        added_statements = sheet_mgr.append_deduplicated_rows(
            STATEMENT_SHEET_NAME,
            all_statement_rows,
            sort_key="trade_date",
        )
        added_settlements = sheet_mgr.append_deduplicated_rows(
            SETTLEMENT_SHEET_NAME,
            all_settlement_rows,
            sort_key="record_date",
        )
        print(
            f"[backfill] 寫入完成：01_Account_Statement 新增 {added_statements} 筆、"
            f"03_Settlement_And_PnL 新增 {added_settlements} 筆。"
        )
    else:
        print(
            f"[backfill][DRY-RUN] 共擷取對帳單 {len(all_statement_rows)} 筆、"
            f"交割與已實現損益 {len(all_settlement_rows)} 筆（未寫入 Google Sheet）。"
        )

    return {
        "chunks_count": len(chunks),
        "login_count": client.login_count,
        "fetched_statements": len(all_statement_rows),
        "fetched_settlements": len(all_settlement_rows),
        "added_statements": added_statements,
        "added_settlements": added_settlements,
        "chunks": chunk_summaries,
    }


# --------------------------------------------------------------------------- #
# Section 4.2: Daily Accounting Sync & AI Holdings Diagnosis (`sync-daily`)
# --------------------------------------------------------------------------- #
def load_local_technical_context(
    reports_dir: Path | None = None,
    as_of: date | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load per-stock MA3/17/45/117/189, microstructure (ATR14, 60d POC, RSI14, bias), and market regime."""
    root = reports_dir or Path(os.getenv("FINMIND_REPORTS_DIR") or "finmind_reports")
    daily_dir = root / "daily"
    if not daily_dir.is_dir():
        return {}, {"light": "🟡黃燈", "summary": "無本地大盤紅綠燈快取"}

    target_iso = (as_of or _today_in_taipei()).isoformat()
    prices_path = daily_dir / f"{target_iso}_prices.csv"
    if not prices_path.is_file():
        candidates = sorted(daily_dir.glob("*_prices.csv"))
        prices_path = candidates[-1] if candidates else prices_path

    regime_path = daily_dir / f"{target_iso}_market_regime.json"
    if not regime_path.is_file():
        r_candidates = sorted(daily_dir.glob("*_market_regime.json"))
        regime_path = r_candidates[-1] if r_candidates else regime_path

    market_regime: dict[str, Any] = {"light": "🟡黃燈", "summary": "無本地大盤紅綠燈快取"}
    if regime_path.is_file():
        try:
            market_regime = json.loads(regime_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    tech_map: dict[str, dict[str, Any]] = {}
    if prices_path.is_file():
        try:
            df = pd.read_csv(prices_path, dtype={"stock_id": str})
            if not df.empty and "stock_id" in df.columns and "close" in df.columns:
                df["stock_id"] = df["stock_id"].astype(str).str.strip()
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                high_col = "max" if "max" in df.columns else ("high" if "high" in df.columns else None)
                low_col = "min" if "min" in df.columns else ("low" if "low" in df.columns else None)
                vol_col = (
                    "Trading_Volume"
                    if "Trading_Volume" in df.columns
                    else ("volume" if "volume" in df.columns else None)
                )
                if high_col:
                    df[high_col] = pd.to_numeric(df[high_col], errors="coerce")
                if low_col:
                    df[low_col] = pd.to_numeric(df[low_col], errors="coerce")
                if vol_col:
                    df[vol_col] = pd.to_numeric(df[vol_col], errors="coerce")

                daily_ret_map: dict[str, float] = {}
                for sid, group in df.groupby("stock_id"):
                    g = group.sort_values("date").dropna(subset=["close"])
                    if g.empty:
                        continue
                    closes = g["close"]
                    latest_close = float(closes.iloc[-1])
                    prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else latest_close
                    daily_ret = ((latest_close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
                    daily_ret_map[str(sid)] = daily_ret

                    ma3 = round(float(closes.tail(3).mean()), 2)
                    ma17 = round(float(closes.tail(17).mean()), 2)
                    ma45 = round(float(closes.tail(45).mean()), 2)
                    ma117 = round(float(closes.tail(117).mean()), 2)
                    ma189 = round(float(closes.tail(189).mean()), 2)

                    bias_17 = round(((latest_close - ma17) / ma17 * 100.0), 2) if ma17 > 0 else 0.0
                    bias_189 = round(((latest_close - ma189) / ma189 * 100.0), 2) if ma189 > 0 else 0.0

                    highs = g[high_col].fillna(closes) if high_col else closes
                    lows = g[low_col].fillna(closes) if low_col else closes
                    prev_high = round(float(highs.iloc[-1]), 2)
                    prev_low = round(float(lows.iloc[-1]), 2)

                    # ATR(14) calculation
                    prev_closes = closes.shift(1).fillna(closes)
                    tr1 = (highs - lows).abs()
                    tr2 = (highs - prev_closes).abs()
                    tr3 = (lows - prev_closes).abs()
                    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                    atr14 = round(float(tr.tail(14).mean()), 2) if not tr.empty else round(latest_close * 0.025, 2)

                    # 60-day Volume POC (weighted average price of top-5 highest volume bars in last 60 days)
                    vol_poc_60d = round(latest_close, 2)
                    if vol_col:
                        g60 = g.tail(60).copy()
                        g60_valid = g60[g60[vol_col] > 0]
                        if not g60_valid.empty:
                            top_vol = g60_valid.nlargest(min(5, len(g60_valid)), vol_col)
                            v_sum = float(top_vol[vol_col].sum())
                            if v_sum > 0:
                                vol_poc_60d = round(
                                    float((top_vol["close"] * top_vol[vol_col]).sum() / v_sum),
                                    2,
                                )

                    # RSI(14) calculation
                    delta = closes.diff()
                    gain = delta.clip(lower=0.0)
                    loss = -delta.clip(upper=0.0)
                    avg_gain = float(gain.rolling(window=14, min_periods=1).mean().iloc[-1])
                    avg_loss = float(loss.rolling(window=14, min_periods=1).mean().iloc[-1])
                    if avg_loss == 0:
                        rsi_14 = 100.0 if avg_gain > 0 else 50.0
                    else:
                        rs = avg_gain / avg_loss
                        rsi_14 = 100.0 - (100.0 / (1.0 + rs))

                    tech_map[str(sid)] = {
                        "close": round(latest_close, 2),
                        "ma3": ma3,
                        "ma17": ma17,
                        "ma45": ma45,
                        "ma117": ma117,
                        "ma189": ma189,
                        "bias_ma17_pct": bias_17,
                        "bias_ma189_pct": bias_189,
                        "prev_high": prev_high,
                        "prev_low": prev_low,
                        "atr14": atr14,
                        "vol_poc_60d": vol_poc_60d,
                        "rsi_14": round(float(rsi_14), 1),
                        "daily_return_pct": round(daily_ret, 2),
                        "daily_return_pr": 50.0,
                    }

                # Compute market-wide daily return percentile (PR 0~99)
                if len(daily_ret_map) >= 2:
                    s_rets = pd.Series(daily_ret_map)
                    ranks = (s_rets.rank(pct=True) * 100.0).round(0)
                    for sid, pr_val in ranks.items():
                        if sid in tech_map and not pd.isna(pr_val):
                            tech_map[sid]["daily_return_pr"] = float(pr_val)
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to read local prices.csv for technical enrichment: %s", err)

    return tech_map, market_regime


def load_public_watchlist_cache(stock_ids: set[str]) -> dict[str, dict[str, str]]:
    """Read `ai_summary_json` and `war_room_url` from public `GSHEET_SPREADSHEET_ID` (`00_Watchlist_Master`) read-only."""
    if not stock_ids:
        return {}
    public_sheet_id = (os.getenv("GSHEET_SPREADSHEET_ID") or "").strip()
    creds_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "certs/sheetbot.json").strip()
    if creds_path and Path(creds_path).is_file():
        pass
    elif Path("certs/sheetbot.json").is_file():
        creds_path = "certs/sheetbot.json"
    elif Path("sheetbot.json").is_file():
        creds_path = "sheetbot.json"
    if not public_sheet_id or not Path(creds_path).is_file():
        return {}
    try:
        gc = gspread.service_account(filename=creds_path)
        sp = gc.open_by_key(public_sheet_id)
        ws = sp.worksheet("00_Watchlist_Master")
        rows = ws.get_all_values()
        if not rows or len(rows) < 2:
            return {}
        headers = [str(h).strip() for h in rows[0]]
        result: dict[str, dict[str, str]] = {}
        for r in rows[1:]:
            padded = list(r) + [""] * max(0, len(headers) - len(r))
            rec = {headers[i]: padded[i] for i in range(len(headers)) if headers[i]}
            sid = clean_leading_zero_code(rec.get("stock_id", ""))
            if sid in stock_ids:
                result[sid] = {
                    "stock_name": str(rec.get("stock_name", "")).strip(),
                    "ai_summary_json": str(rec.get("ai_summary_json", "")).strip(),
                    "war_room_url": str(rec.get("war_room_url", "")).strip(),
                }
        return result
    except Exception as err:  # noqa: BLE001
        logger.warning("Failed to read public 00_Watchlist_Master cache: %s", err)
        return {}


def load_public_deep_fundamental_context(
    stock_ids: set[str],
    prices_by_sid: Mapping[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    """Read latest fundamental, valuation, chip margin maintenance, and taxonomy records for tracked stocks.

    Aligned with `quant/fundamental_config.py` SHEET_SCHEMAS (plus legacy tab/column fallbacks):
    - `05_AI_Metrics_Wide`: quarterly EPS/TTM EPS, margins, DIO/DSO, `contract_liab_亿元`, `ocf_亿元`, `fcf_亿元`
    - `03_Balance_Sheet`: `inventory`, `contract_liab_current`, `parent_equity`, `capital_stock` (for PB)
    - `04_Cash_Flow`: `operating_cf`, `free_cf` fallback
    - `01_Monthly_Revenue`: latest `rev_ym`, `mom_pct`, `yoy_pct`
    - `06_Dividend_Policy`: `cash_dividend` (for cash dividend yield)
    - `07_Chip_Summary`: `foreign_net_buy_5d_lots`, `trust_net_buy_5d_lots`, `margin_chg_5d_lots`, `est_margin_maint_pct`, `chip_quadrant_tag`
    - `08_Stock_Taxonomy`: `sector_group`, `sub_industry`, `valuation_regime`, `peers`
    """
    clean_ids = {clean_leading_zero_code(s) for s in stock_ids if clean_leading_zero_code(s)}
    if not clean_ids:
        return {}

    public_sheet_id = (os.getenv("GSHEET_SPREADSHEET_ID") or "").strip()
    creds_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "certs/sheetbot.json").strip()
    if creds_path and Path(creds_path).is_file():
        pass
    elif Path("certs/sheetbot.json").is_file():
        creds_path = "certs/sheetbot.json"
    elif Path("sheetbot.json").is_file():
        creds_path = "sheetbot.json"

    if not public_sheet_id or not Path(creds_path).is_file():
        return {}

    result: dict[str, dict[str, Any]] = {sid: {} for sid in clean_ids}
    try:
        gc = gspread.service_account(filename=creds_path)
        sp = gc.open_by_key(public_sheet_id)

        def _read_latest_by_stock(
            tab_candidates: tuple[str, ...],
            sort_keys: tuple[str, ...],
        ) -> dict[str, list[dict[str, Any]]]:
            ws = None
            for tname in tab_candidates:
                try:
                    ws = sp.worksheet(tname)
                    break
                except Exception:  # noqa: BLE001
                    continue
            if ws is None:
                return {}
            try:
                rows = ws.get_all_values()
            except Exception:  # noqa: BLE001
                return {}
            if not rows or len(rows) < 2:
                return {}
            hdrs = [str(h).strip() for h in rows[0]]
            grouped: dict[str, list[dict[str, Any]]] = {}
            for raw in rows[1:]:
                padded = list(raw) + [""] * max(0, len(hdrs) - len(raw))
                rec = {hdrs[i]: padded[i] for i in range(len(hdrs)) if hdrs[i]}
                sid = clean_leading_zero_code(rec.get("stock_id") or rec.get("stock_no") or "")
                if sid in clean_ids:
                    grouped.setdefault(sid, []).append(rec)
            for sid, items in grouped.items():
                items.sort(
                    key=lambda r: next((str(r.get(sk, "")).strip() for sk in sort_keys if r.get(sk)), ""),
                )
            return grouped

        def _opt_float(val: Any) -> float | None:
            if val in ("", None, "N/A", "nan", "NaN"):
                return None
            try:
                return float(str(val).replace(",", "").strip())
            except (TypeError, ValueError):
                return None

        def _opt_yi(val: Any, *, already_yi: bool = False) -> float | str:
            num = _opt_float(val)
            if num is None:
                return "N/A"
            if already_yi:
                return round(num, 2)
            return round(num / 1e8, 2) if abs(num) >= 1e5 else round(num, 2)

        def _pick_yi(row: Mapping[str, Any], *yi_keys: str, raw_keys: tuple[str, ...] = ()) -> float | str:
            for k in yi_keys:
                v = row.get(k)
                if v not in ("", None, "N/A", "nan", "NaN"):
                    return _opt_yi(v, already_yi=True)
            for k in raw_keys:
                v = row.get(k)
                if v not in ("", None, "N/A", "nan", "NaN"):
                    return _opt_yi(v, already_yi=False)
            return "N/A"

        ttm_eps_by_sid: dict[str, float] = {}

        # 1. Quarterly Wide Metrics (`05_AI_Metrics_Wide`)
        wide_map = _read_latest_by_stock(
            ("05_AI_Metrics_Wide",),
            ("year_qtr", "raw_date", "period", "report_date", "updated_at"),
        )
        for sid, items in wide_map.items():
            last = items[-1]
            prev = items[-2] if len(items) >= 2 else {}
            q_label = str(_pick_first(last, "year_qtr", "period", "year_quarter", "report_date", default="最新季")).strip()
            eps = round(_to_float(_pick_first(last, "eps", "EPS", default=0.0)), 2)
            gm = round(_to_float(_pick_first(last, "gross_margin_pct", default=0.0)), 2)
            opm = round(_to_float(_pick_first(last, "op_margin_pct", "operating_margin_pct", default=0.0)), 2)
            prev_gm = _to_float(_pick_first(prev, "gross_margin_pct", default=gm))
            gm_qoq = round(gm - prev_gm, 2) if prev else 0.0

            ttm_eps = _opt_float(_pick_first(last, "ttm_eps", default=None))
            if ttm_eps is not None and ttm_eps > 0:
                ttm_eps_by_sid[sid] = ttm_eps

            result[sid]["income"] = {
                "period": q_label,
                "eps": eps,
                "gross_margin_pct": gm,
                "operating_margin_pct": opm,
                "gm_qoq_pp": gm_qoq,
            }

            inv_yi = _pick_yi(last, raw_keys=("inventories", "inventory"))
            cl_yi = _pick_yi(last, "contract_liab_亿元", raw_keys=("contract_liab_current", "contract_liabilities"))
            dio = _pick_first(last, "dio_days", default="N/A")
            dso = _pick_first(last, "dso_days", default="N/A")
            result[sid]["balance"] = {
                "period": q_label,
                "inventory_yi": inv_yi,
                "contract_liabilities_yi": cl_yi,
                "dio_days": dio,
                "dso_days": dso,
            }

            ocf_yi = _pick_yi(last, "ocf_亿元", raw_keys=("ocf", "operating_cash_flow", "operating_cf"))
            fcf_yi = _pick_yi(last, "fcf_亿元", raw_keys=("fcf", "free_cash_flow", "free_cf"))
            result[sid]["cashflow"] = {
                "ocf_yi": ocf_yi,
                "fcf_yi": fcf_yi,
            }

            pe_val = _pick_first(last, "pe_ttm", "pe", "per", default="")
            pb_val = _pick_first(last, "pb_ratio", "pb", "pbr", default="")
            dy_val = _pick_first(last, "cash_dividend_yield_pct", "dividend_yield", default="")
            roe_val = _pick_first(last, "parent_roe_pct", "roe_annualized_pct", "roe_q_pct", default="")
            result[sid]["valuation"] = {
                "pe": round(_to_float(pe_val), 2) if pe_val not in ("", None, "N/A") else "N/A",
                "pb": round(_to_float(pb_val), 2) if pb_val not in ("", None, "N/A") else "N/A",
                "dividend_yield": round(_to_float(dy_val), 2) if dy_val not in ("", None, "N/A") else "N/A",
                "roe_pct": round(_to_float(roe_val), 2) if roe_val not in ("", None, "N/A") else "N/A",
            }

        # 1b. Balance Sheet (`03_Balance_Sheet` / `02_Balance_Sheet`) for inventory, contract_liab, and PB
        bs_map = _read_latest_by_stock(
            ("03_Balance_Sheet", "02_Balance_Sheet"),
            ("year_qtr", "raw_date", "period", "report_date", "updated_at"),
        )
        for sid, items in bs_map.items():
            last = items[-1]
            q_label = str(_pick_first(last, "year_qtr", "period", "report_date", default="最新季")).strip()
            bal = result[sid].setdefault(
                "balance",
                {"period": q_label, "inventory_yi": "N/A", "contract_liabilities_yi": "N/A", "dio_days": "N/A", "dso_days": "N/A"},
            )
            if bal.get("inventory_yi") in ("N/A", "", None, 0.0):
                bs_inv = _pick_yi(last, raw_keys=("inventory", "inventories"))
                if bs_inv != "N/A":
                    bal["inventory_yi"] = bs_inv
            if bal.get("contract_liabilities_yi") in ("N/A", "", None, 0.0):
                bs_cl = _pick_yi(last, raw_keys=("contract_liab_current", "contract_liabilities"))
                if bs_cl != "N/A":
                    bal["contract_liabilities_yi"] = bs_cl

            # Compute PB if missing and live price is available
            val_dict = result[sid].setdefault("valuation", {"pe": "N/A", "pb": "N/A", "dividend_yield": "N/A", "roe_pct": "N/A"})
            px = _to_float((prices_by_sid or {}).get(sid), 0.0)
            if val_dict.get("pb") == "N/A" and px > 0:
                p_eq = _opt_float(last.get("parent_equity"))
                cap_stk = _opt_float(last.get("capital_stock"))
                if p_eq and cap_stk and p_eq > 0 and cap_stk > 0:
                    bps = p_eq / (cap_stk / 10.0)
                    if bps > 0:
                        val_dict["pb"] = round(px / bps, 2)

        # 1c. Cash Flow fallback (`04_Cash_Flow` / `03_Cash_Flow_Statement` / `03_Cash_Flow`)
        cf_map = _read_latest_by_stock(
            ("04_Cash_Flow", "03_Cash_Flow_Statement", "03_Cash_Flow"),
            ("year_qtr", "raw_date", "period", "report_date", "updated_at"),
        )
        for sid, items in cf_map.items():
            last = items[-1]
            cf_dict = result[sid].setdefault("cashflow", {"ocf_yi": "N/A", "fcf_yi": "N/A"})
            if cf_dict.get("ocf_yi") in ("N/A", "", None, 0.0):
                cf_ocf = _pick_yi(last, raw_keys=("operating_cf", "operating_cash_flow", "ocf"))
                if cf_ocf != "N/A":
                    cf_dict["ocf_yi"] = cf_ocf
            if cf_dict.get("fcf_yi") in ("N/A", "", None, 0.0):
                cf_fcf = _pick_yi(last, raw_keys=("free_cf", "free_cash_flow", "fcf"))
                if cf_fcf != "N/A":
                    cf_dict["fcf_yi"] = cf_fcf

        # 1d. Valuation fallback: PE from TTM EPS & dividend yield from `06_Dividend_Policy` / `05_Valuation_PER_PBR`
        if prices_by_sid:
            for sid, px_raw in prices_by_sid.items():
                px = _to_float(px_raw, 0.0)
                if px <= 0 or sid not in result:
                    continue
                val_dict = result[sid].setdefault("valuation", {"pe": "N/A", "pb": "N/A", "dividend_yield": "N/A", "roe_pct": "N/A"})
                if val_dict.get("pe") == "N/A" and ttm_eps_by_sid.get(sid, 0.0) > 0:
                    val_dict["pe"] = round(px / ttm_eps_by_sid[sid], 2)

        div_map = _read_latest_by_stock(
            ("06_Dividend_Policy",),
            ("div_year", "raw_date", "ex_dividend_date", "updated_at"),
        )
        for sid, items in div_map.items():
            last = items[-1]
            px = _to_float((prices_by_sid or {}).get(sid), 0.0)
            val_dict = result[sid].setdefault("valuation", {"pe": "N/A", "pb": "N/A", "dividend_yield": "N/A", "roe_pct": "N/A"})
            if val_dict.get("dividend_yield") == "N/A" and px > 0:
                cash_div = _opt_float(_pick_first(last, "cash_dividend", "total_dividend", default=None))
                if cash_div is not None and cash_div >= 0:
                    val_dict["dividend_yield"] = round(cash_div / px * 100.0, 2)

        # 2. Monthly Revenue (`01_Monthly_Revenue` + legacy `04_Monthly_Revenue_YoY`)
        rev_map = _read_latest_by_stock(
            ("01_Monthly_Revenue", "04_Monthly_Revenue_YoY", "04_Monthly_Revenue"),
            ("rev_ym", "raw_date", "rev_year_month", "revenue_date"),
        )
        for sid, items in rev_map.items():
            last = items[-1]
            ym = str(_pick_first(last, "rev_ym", "rev_year_month", "revenue_date", default="")).strip()
            mom = _opt_float(_pick_first(last, "mom_pct", default=None))
            yoy = _opt_float(_pick_first(last, "yoy_pct", default=None))
            cum_yoy = _opt_float(_pick_first(last, "cum_yoy_pct", "cumulative_yoy_pct", default=None))
            result[sid]["revenue"] = {
                "rev_ym": ym,
                "mom_pct": round(mom, 2) if mom is not None else None,
                "yoy_pct": round(yoy, 2) if yoy is not None else None,
                "cum_yoy_pct": round(cum_yoy, 2) if cum_yoy is not None else None,
            }

        # 3. Chip & Margin Maintenance (`07_Chip_Summary`)
        chip_map = _read_latest_by_stock(
            ("07_Chip_Summary",),
            ("raw_date", "trade_week", "as_of_date", "updated_at"),
        )
        for sid, items in chip_map.items():
            last = items[-1]
            f5_raw = _pick_first(last, "foreign_net_buy_5d_lots", "foreign_net_5d_lots", "foreign_5d_net_lots", default=None)
            it5_raw = _pick_first(last, "trust_net_buy_5d_lots", "it_net_5d_lots", "trust_net_5d_lots", "trust_5d_net_lots", default=None)
            m5_raw = _pick_first(last, "margin_chg_5d_lots", default=None)
            maint_raw = _pick_first(last, "est_margin_maint_pct", default="")
            maint_num = _opt_float(maint_raw)
            maint_str = f"{maint_num:.1f}" if maint_num is not None else "N/A"
            quad_tag = str(_pick_first(last, "chip_quadrant_tag", "inst_Flow_Tag", default="未分類")).strip() or "未分類"
            result[sid]["chip"] = {
                "foreign_5d_lots": f"{_to_int(f5_raw):+d}" if _opt_float(f5_raw) is not None else "N/A",
                "trust_5d_lots": f"{_to_int(it5_raw):+d}" if _opt_float(it5_raw) is not None else "N/A",
                "margin_chg_5d_lots": f"{_to_int(m5_raw):+d}" if _opt_float(m5_raw) is not None else "N/A",
                "est_margin_maint_pct": maint_str,
                "chip_quadrant_tag": quad_tag,
            }

        # 4. Taxonomy (`08_Stock_Taxonomy` + fallback to `00_Watchlist_Master`)
        tax_map = _read_latest_by_stock(("08_Stock_Taxonomy", "00_Watchlist_Master"), ("updated_at", "stock_id"))
        for sid, items in tax_map.items():
            last = items[-1]
            result[sid]["taxonomy"] = {
                "sector_group": str(_pick_first(last, "sector_group", "industry_category", default="")).strip(),
                "sub_industry": str(_pick_first(last, "sub_industry", "industry_category", default="")).strip(),
                "valuation_regime": str(_pick_first(last, "valuation_regime", default="標準估值")).strip() or "標準估值",
                "peers": str(_pick_first(last, "peers", default="")).strip(),
            }
    except Exception as err:  # noqa: BLE001
        logger.warning("Failed to load deep fundamental context from public GSheet: %s", err)

    return result


def build_fresh_top_picks_block(picks_bundle: Mapping[str, Any] | None, max_picks: int = 3) -> str:
    """Format the latest scan_date TOP_PICK candidates from `picks_bundle` as an opportunity-cost reference."""
    if not picks_bundle:
        return "- 暫無昨晚最新 AI 嚴選清單快取（以既有部位控管為主）。"

    ai_picks = list(picks_bundle.get("ai_daily_picks") or [])
    if not ai_picks:
        return "- 暫無昨晚最新 AI 嚴選紀錄（若大盤紅燈空手觀望則無推薦）。"

    valid_dates = [str(r.get("scan_date") or "").strip() for r in ai_picks if str(r.get("scan_date") or "").strip()]
    if not valid_dates:
        return "- 暫無昨晚最新 AI 嚴選紀錄。"

    latest_date = max(valid_dates)
    top_rows = [
        r
        for r in ai_picks
        if str(r.get("scan_date") or "").strip() == latest_date
        and str(r.get("category") or r.get("pick_category") or "").strip().upper() == "TOP_PICK"
    ][:max_picks]

    if not top_rows:
        return f"- 最新掃描日 ({latest_date}) 無 TOP_PICK 入選（Zero-Pick Veto 空手防禦中，暫不建議換股追買）。"

    lines: list[str] = [f"- 最新嚴選基準日：`{latest_date}`（若手中破線股動能落後，可趁反彈減碼換入以下領頭羊）："]
    for idx, row in enumerate(top_rows, start=1):
        sid = clean_leading_zero_code(row.get("stock_id") or row.get("stock_no") or "")
        sname = str(row.get("stock_name") or "").strip()
        tactic = str(row.get("tactical_type") or "波段嚴選").strip()
        tp1 = _pick_first(row, "TP1_Target_Or_POC", "target_price_1", default="N/A")
        sl = _pick_first(row, "Hard_Stop_Loss", "stop_loss_price", default="N/A")
        rr = _pick_first(row, "Deterministic_RR", default="")
        rr_str = f"｜風報比 `{rr}`" if rr not in ("", None) else ""
        thesis = str(_pick_first(row, "entry_thesis", "bull_thesis", default="基本面與籌碼共振突破")).strip()
        lines.append(
            f"  {idx}. **`{sid} {sname}`**（{tactic}｜🎯 TP1 `{tp1}` / 🛡️ SL `{sl}`{rr_str}）：{thesis[:85]}"
        )
    return "\n".join(lines)


def build_portfolio_holdings_rows(
    raw_inventories: list[dict[str, Any]],
    *,
    statement_records: list[dict[str, Any]],
    tech_map: Mapping[str, Mapping[str, Any]],
    public_cache: Mapping[str, Mapping[str, str]],
    snapshot_date: date | None = None,
    updated_at: str | None = None,
) -> list[dict[str, Any]]:
    """Build normalized rows for `02_Portfolio_Holdings`, backfilling missing `avg_cost_price` from statements."""
    snap_str = (snapshot_date or _today_in_taipei()).isoformat()
    now_iso = updated_at or _now_iso_in_taipei()
    fallback_avg_costs = compute_weighted_avg_cost_from_statements(statement_records)

    rows: list[dict[str, Any]] = []
    total_portfolio_mv = 0.0

    for inv in raw_inventories:
        stock_no = clean_leading_zero_code(
            _pick_first(inv, "stock_no", "symbol", "stock_id", "code", default="")
        )
        if not stock_no:
            continue
        qty = _to_int(
            _pick_first(
                inv,
                "qty",
                "current_quantity",
                "quantity",
                "current_qty",
                "hold_qty",
                "prev_day_quantity",
                "shares",
                default=0,
            )
        )
        if qty <= 0:
            continue
        tradable_qty = _to_int(
            _pick_first(inv, "tradable_qty", "avail_qty", "can_sell_qty", "prev_day_quantity", default=qty)
        )
        order_type = _normalize_order_type(
            _pick_first(inv, "order_type_name", "order_type", "trade_type", "market_type", default=""),
            filled_qty=qty,
        )

        tech = tech_map.get(stock_no, {})
        pub = public_cache.get(stock_no, {})
        stock_name = (
            str(_pick_first(inv, "stock_name", "symbol_name", "sname", "name", default="")).strip()
            or str(pub.get("stock_name", "")).strip()
            or stock_no
        )

        api_avg_cost = _to_float(
            _pick_first(
                inv,
                "avg_cost_price",
                "average_price",
                "average_filled_price",
                "avg_price",
                "cost_price",
                "buy_avg_price",
                default=0.0,
            )
        )
        avg_cost_price = round(
            api_avg_cost if api_avg_cost > 0 else fallback_avg_costs.get(stock_no, 0.0),
            2,
        )

        api_total_cost = _to_float(_pick_first(inv, "total_cost", "cost_amount", "cost", default=0.0))
        total_cost = int(round(api_total_cost if api_total_cost > 0 else (qty * avg_cost_price)))

        api_cur_price = _to_float(
            _pick_first(inv, "current_price", "last_price", "close_price", "market_price", "price", default=0.0)
        )
        current_price = round(
            api_cur_price if api_cur_price > 0 else _to_float(tech.get("close"), avg_cost_price),
            2,
        )

        api_mv = _to_float(_pick_first(inv, "market_value", "market_val", default=0.0))
        market_value = int(round(api_mv if api_mv > 0 else (qty * current_price)))
        total_portfolio_mv += market_value

        unrealized_pnl = _to_int(
            _pick_first(
                inv,
                "unrealized_pnl",
                "total_profit",
                "unrealized_profit_loss",
                "unrealized_profit",
                "pnl",
                default=(market_value - total_cost),
            )
        )
        return_pct = round(
            ((market_value - total_cost) / total_cost * 100.0) if total_cost > 0 else 0.0,
            2,
        )

        ma17 = round(_to_float(tech.get("ma17"), current_price), 2)
        ma45 = round(_to_float(tech.get("ma45"), current_price), 2)
        ma117 = round(_to_float(tech.get("ma117"), current_price), 2)
        ma189 = round(_to_float(tech.get("ma189"), current_price), 2)

        # Deterministic baseline advice (replaced by AI when AI diagnosis runs)
        default_tag, default_target, default_stop, default_advice = _build_fallback_holding_advice(
            avg_cost_price=avg_cost_price,
            current_price=current_price,
            return_pct=return_pct,
            ma17=ma17,
            ma45=ma45,
        )

        rows.append(
            {
                "snapshot_date": snap_str,
                "stock_no": stock_no,
                "stock_name": stock_name,
                "order_type": order_type,
                "qty": qty,
                "tradable_qty": tradable_qty,
                "avg_cost_price": avg_cost_price,
                "total_cost": total_cost,
                "current_price": current_price,
                "market_value": market_value,
                "unrealized_pnl": unrealized_pnl,
                "return_pct": return_pct,
                "weight_pct": 0.0,
                "ma17": ma17,
                "ma45": ma45,
                "ma117": ma117,
                "ma189": ma189,
                "ai_action_tag": default_tag,
                "ai_target_price": default_target,
                "ai_stop_loss": default_stop,
                "ai_advice_summary": default_advice,
                "war_room_url": str(pub.get("war_room_url", "")).strip(),
                "updated_at": now_iso,
            }
        )

    for r in rows:
        mv = _to_float(r["market_value"], 0.0)
        r["weight_pct"] = round((mv / total_portfolio_mv * 100.0) if total_portfolio_mv > 0 else 0.0, 2)

    rows.sort(key=lambda r: _to_float(r.get("weight_pct", 0.0)), reverse=True)
    return rows


def _build_fallback_holding_advice(
    *,
    avg_cost_price: float,
    current_price: float,
    return_pct: float,
    ma17: float,
    ma45: float,
) -> tuple[str, float, float, str]:
    """Generate rule-based baseline diagnosis when `--no-ai` is set or AI omits a holding."""
    base_price = current_price if current_price > 0 else avg_cost_price
    if return_pct >= 15.0:
        tag = "💰 分批停利"
        stop = round(max(avg_cost_price * 1.05, min(base_price * 0.96, ma17)), 2)
        target = round(max(base_price * 1.08, ma17), 2)
        advice = f"脫離買入均價安全墊豐厚（{return_pct:+.1f}%），可分批鎖利並以 {stop} 元作為移動保本停利。"
    elif return_pct <= -7.0 or (return_pct < 0 and ma45 > 0 and current_price < ma45 * 0.97):
        tag = "🚨 觸及停損"
        stop = round(min(avg_cost_price * 0.93, base_price * 0.98), 2)
        target = round(max(avg_cost_price, ma17), 2)
        advice = f"現價跌破均線與成本防線（報酬 {return_pct:+.1f}%），建議嚴守停損 {stop} 元控制風險。"
    elif current_price >= ma17 >= ma45 and return_pct >= 2.0:
        tag = "🟢 順勢續抱"
        stop = round(max(avg_cost_price, ma17 * 0.98), 2)
        target = round(base_price * 1.10, 2)
        advice = f"站穩 17MA/45MA 多頭排列且具成本優勢（{return_pct:+.1f}%），防守價設於 {stop} 元續抱。"
    elif ma45 > 0 and abs(current_price - ma45) / ma45 <= 0.025 and current_price >= ma45 * 0.99:
        tag = "🎯 逢低加碼"
        stop = round(ma45 * 0.97, 2)
        target = round(max(ma17, base_price * 1.08), 2)
        advice = f"回踩 45MA 季線支撐區，若基本面無虞可逢低分批布局，跌破 {stop} 元停損。"
    else:
        tag = "🟡 觀望防守"
        stop = round(max(avg_cost_price, ma45 * 0.98) if return_pct > 3.0 else avg_cost_price * 0.95, 2)
        target = round(base_price * 1.06, 2)
        advice = f"處於區間整理（{return_pct:+.1f}%），暫不追高，觀察 {stop} 元支撐力道。"
    return tag, target, stop, advice


def parse_portfolio_ai_response(raw_text: str) -> dict[str, Any]:
    """Extract and parse JSON from AI holdings diagnosis response."""
    text = (raw_text or "").strip()
    if not text:
        return {"portfolio_overview": "", "holdings": []}

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = m.group(1) if m else text
    if not m:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {"portfolio_overview": "", "holdings": []}

    if not isinstance(parsed, dict):
        return {"portfolio_overview": "", "holdings": []}

    holdings_list = parsed.get("holdings")
    if not isinstance(holdings_list, list):
        holdings_list = []

    normalized_items: list[dict[str, Any]] = []
    for item in holdings_list:
        if not isinstance(item, dict):
            continue
        stock_no = clean_leading_zero_code(item.get("stock_no") or item.get("stock_id") or "")
        if not stock_no:
            continue
        normalized_items.append(
            {
                "stock_no": stock_no,
                "order_type": str(item.get("order_type") or "").strip(),
                "ai_action_tag": normalize_ai_action_tag(item.get("ai_action_tag")),
                "ai_target_price": round(_to_float(item.get("ai_target_price"), 0.0), 2),
                "ai_stop_loss": round(_to_float(item.get("ai_stop_loss"), 0.0), 2),
                "ai_advice_summary": str(item.get("ai_advice_summary") or "").strip(),
            }
        )

    return {
        "portfolio_overview": str(parsed.get("portfolio_overview") or "").strip(),
        "holdings": normalized_items,
    }


def apply_ai_diagnosis_to_holdings(
    holdings_rows: list[dict[str, Any]],
    ai_parsed: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Merge structured AI diagnosis fields into `02_Portfolio_Holdings` rows."""
    ai_items = ai_parsed.get("holdings") or []
    by_stock_and_type: dict[tuple[str, str], dict[str, Any]] = {}
    by_stock: dict[str, dict[str, Any]] = {}
    for item in ai_items:
        if not isinstance(item, dict):
            continue
        sno = clean_leading_zero_code(item.get("stock_no", ""))
        otype = str(item.get("order_type", "")).strip()
        if sno:
            by_stock[sno] = item
            if otype:
                by_stock_and_type[(sno, otype)] = item

    updated: list[dict[str, Any]] = []
    for row in holdings_rows:
        r = dict(row)
        sno = clean_leading_zero_code(r.get("stock_no", ""))
        otype = str(r.get("order_type", "")).strip()
        matched = by_stock_and_type.get((sno, otype)) or by_stock.get(sno)
        if matched:
            r["ai_action_tag"] = normalize_ai_action_tag(matched.get("ai_action_tag") or r["ai_action_tag"])
            if _to_float(matched.get("ai_target_price"), 0.0) > 0:
                r["ai_target_price"] = round(_to_float(matched["ai_target_price"]), 2)
            if _to_float(matched.get("ai_stop_loss"), 0.0) > 0:
                r["ai_stop_loss"] = round(_to_float(matched["ai_stop_loss"]), 2)
            if matched.get("ai_advice_summary"):
                r["ai_advice_summary"] = str(matched["ai_advice_summary"]).strip()
        updated.append(r)
    return updated


async def run_portfolio_ai_diagnosis(
    holdings_rows: list[dict[str, Any]],
    *,
    public_cache: Mapping[str, Mapping[str, str]],
    recent_statements: list[dict[str, Any]],
    market_regime: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], str, str]:
    """Invoke `STAGE1_MODEL` -> `VERTEX_STAGE1_MODEL` to diagnose all current holdings."""
    if not holdings_rows:
        return holdings_rows, "目前無庫存持股。", ""

    dossier_lines: list[str] = []
    for idx, h in enumerate(holdings_rows, start=1):
        sno = clean_leading_zero_code(h["stock_no"])
        pub = public_cache.get(sno, {})
        ai_sum_raw = pub.get("ai_summary_json", "")
        fund_brief = "無公用財報快取"
        if ai_sum_raw:
            try:
                ai_sum = json.loads(ai_sum_raw)
                score = ai_sum.get("overall_score", "-")
                verdict = ai_sum.get("one_line_verdict", "")
                fund_brief = f"財報評分={score}｜{verdict}"
            except Exception:  # noqa: BLE001
                fund_brief = ai_sum_raw[:120]

        dossier_lines.append(
            f"{idx}. 【{sno} {h['stock_name']}】({h['order_type']}) "
            f"股數={h['qty']}｜買入均價={h['avg_cost_price']}｜現價={h['current_price']}｜"
            f"未實現損益={h['unrealized_pnl']} ({h['return_pct']:+.2f}%)｜權重={h['weight_pct']:.1f}%｜"
            f"均線：17MA={h['ma17']} / 45MA={h['ma45']} / 117MA={h['ma117']} / 189MA={h['ma189']}｜"
            f"基本面快取：{fund_brief}"
        )

    recent_trades_lines: list[str] = []
    for t in recent_statements[-15:]:
        recent_trades_lines.append(
            f"- {t.get('trade_date')} {t.get('action')} {clean_leading_zero_code(t.get('stock_no'))} "
            f"{t.get('stock_name', '')} {t.get('filled_qty')}股 @ {t.get('filled_price')} "
            f"(淨收付 {t.get('net_amount')})"
        )

    prompt = TAISHIN_PORTFOLIO_AI_PROMPT.format(
        market_regime_summary=str(market_regime.get("summary") or market_regime.get("light") or "🟡黃燈"),
        holdings_dossier="\n".join(dossier_lines),
        recent_trades_summary="\n".join(recent_trades_lines) if recent_trades_lines else "近期無交易紀錄",
    )

    ai_resp = await ask_gemini_diagnosis_with_meta(
        prompt,
        tag="taishin_portfolio_diagnosis",
        task_type="THESIS_SYNC",
        stock_id="PORTFOLIO",
        custom_id=f"THESIS_{datetime.now().strftime('%Y%m%d_%H%M%S')}_PORTFOLIO",
        blocking_bq_log=True,
    )
    parsed = parse_portfolio_ai_response(ai_resp.text)
    enriched_rows = apply_ai_diagnosis_to_holdings(holdings_rows, parsed)
    overview = parsed.get("portfolio_overview") or "已完成持股成本安全墊與均線位階診斷。"
    return enriched_rows, overview, ai_resp.model_version


def format_daily_sync_discord_report(
    *,
    as_of: date,
    holdings_rows: list[dict[str, Any]],
    added_statements: int,
    added_settlements: int,
    portfolio_overview: str,
    model_version: str,
    market_regime: Mapping[str, Any],
) -> list[str]:
    """Format personal accounting & AI holdings diagnosis Discord report."""
    lines: list[str] = []
    light = str(market_regime.get("light") or "🟡黃燈")
    lines.append(f"🏦 【台新證券 個人帳務同步與 AI 持股健診】({as_of.isoformat()})")
    if model_version:
        lines.append(f"🤖 分析模型：{model_version} ｜ 🚦 大盤環境：{light}")
    else:
        lines.append(f"🚦 大盤環境：{light}")

    lines.append(
        f"📥 帳務增量同步：對帳單新增 {added_statements} 筆 ｜ 交割/已實現損益新增 {added_settlements} 筆"
    )

    if portfolio_overview:
        lines.append(f"🧭 投組總評：{portfolio_overview}")

    lines.append("─" * 28)
    if not holdings_rows:
        lines.append("📭 目前無庫存持股。")
    else:
        total_cost = sum(_to_int(r.get("total_cost", 0)) for r in holdings_rows)
        total_mv = sum(_to_int(r.get("market_value", 0)) for r in holdings_rows)
        total_pnl = total_mv - total_cost
        total_ret = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0
        lines.append(
            f"📊 總持倉市值：{total_mv:,} 元 ｜ 總成本：{total_cost:,} 元 ｜ "
            f"未實現損益：{total_pnl:+,} 元 ({total_ret:+.2f}%)"
        )
        lines.append("")
        for r in holdings_rows:
            sno = clean_leading_zero_code(r["stock_no"])
            war_link = f" ｜ [戰情室]({r['war_room_url']})" if r.get("war_room_url") else ""
            lines.append(
                f"🔹 {sno} {r['stock_name']}（{r['order_type']}｜權重 {r['weight_pct']:.1f}%）"
                f" ➔ {r['ai_action_tag']}{war_link}"
            )
            lines.append(
                f"   • 庫存：{r['qty']:,} 股（可賣 {r['tradable_qty']:,} 股）｜"
                f"買入均價：{r['avg_cost_price']} ｜ 現價：{r['current_price']} "
                f"({r['return_pct']:+.2f}% / {r['unrealized_pnl']:+,} 元)"
            )
            lines.append(
                f"   • 均線：17MA={r['ma17']} / 45MA={r['ma45']} / 117MA={r['ma117']} / 189MA={r['ma189']} ｜ "
                f"🎯 目標價：{r['ai_target_price']} ｜ 🛡️ 停損/保本價：{r['ai_stop_loss']}"
            )
            lines.append(f"   • 診斷：{r['ai_advice_summary']}")

    return _chunk_discord_messages(lines)


def _chunk_discord_messages(lines: list[str], limit: int = DISCORD_CHUNK_LIMIT) -> list[str]:
    messages: list[str] = []
    current: list[str] = []
    cur_len = 0
    for line in lines:
        add_len = len(line) + 1
        if current and cur_len + add_len > limit:
            messages.append("\n".join(current))
            current = [line]
            cur_len = len(line)
        else:
            current.append(line)
            cur_len += add_len
    if current:
        messages.append("\n".join(current))
    return messages


def post_to_taishin_discord(
    messages: list[str],
    *,
    channel_id: str | None = None,
    bot_token: str | None = None,
) -> int:
    """Post messages to `TAISHIN_ALERT_CHANNEL_ID` (fallback `STOCK_CHANNEL_ID`)."""
    resolved_channel = (
        channel_id
        or (os.getenv("TAISHIN_ALERT_CHANNEL_ID") or "").strip()
        or (os.getenv("STOCK_CHANNEL_ID") or "").strip()
    )
    resolved_token = (
        bot_token
        or (os.getenv("DISCORD_TOKEN") or "").strip()
        or (os.getenv("BOT_TOKEN") or "").strip()
    )
    if not resolved_channel or not resolved_token:
        logger.warning("Discord channel or token not configured; skipping Discord notification.")
        return 0

    url = f"{DISCORD_API_BASE_URL}/channels/{resolved_channel}/messages"
    headers = {
        "Authorization": f"Bot {resolved_token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/nick_chung0725/Discord_Service, 5.1.0)",
    }
    sent = 0
    first_msg_id: str | None = None
    for idx, content in enumerate(messages):
        payload: dict[str, Any] = {"content": content}
        if idx > 0 and first_msg_id:
            payload["message_reference"] = {"message_id": first_msg_id}
        req = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8", errors="replace"))
                if idx == 0 and isinstance(body, dict) and body.get("id"):
                    first_msg_id = str(body["id"])
                sent += 1
        except (HTTPError, URLError) as err:
            logger.error("Failed to post Taishin report to Discord: %s", err)
            break
    return sent


def sync_daily(
    *,
    client: TaishinClient,
    sheet_mgr: TaishinSheetManager | None,
    as_of: date | None = None,
    lookback_days: int = 7,
    run_ai: bool = True,
    post_discord: bool = True,
    dry_run: bool = False,
    reports_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute the daily accounting sync, holdings snapshot, and AI cost-basis diagnosis."""
    today = as_of or _today_in_taipei()
    start_dt = today - timedelta(days=lookback_days)
    now_iso = _now_iso_in_taipei()

    print(f"[sync-daily] 正在登入台新證券 Nova API 並同步帳務 (基準日: {today.isoformat()})...")
    client.ensure_logged_in()
    account_no = client.get_account_no_str()

    # 1. Sync recent 7 days of statements, fills, settlements & realized PnL
    print(f"[sync-daily] 抓取近 {lookback_days} 天對帳單、成交明細與已實現損益紀錄...")
    raw_stmt = client.fetch_account_statement(start_dt, today)
    raw_fills = client.fetch_filled_history(start_dt, today)
    raw_fill_details = client.fetch_filled_history_detail(start_dt, today)
    raw_pnl = client.fetch_realized_profit_and_losses(start_dt, today)
    raw_hist_settle = client.fetch_history_settlement(start_dt, today)
    raw_today_settle = client.fetch_today_settlement()

    stmt_rows = [
        normalize_statement_record(r, source_api="STATEMENT", default_account_no=account_no, synced_at=now_iso)
        for r in raw_stmt
    ]
    fill_rows = [
        normalize_statement_record(
            r, source_api="FILLED_HISTORY", default_account_no=account_no, synced_at=now_iso
        )
        for r in [*raw_fills, *raw_fill_details]
    ]
    recent_statements, _ = merge_statement_and_fills(stmt_rows, fill_rows)

    today_settle_rows = [
        normalize_settlement_or_pnl_record(
            r, record_type="TODAY_SETTLEMENT", synced_at=now_iso
        )
        for r in raw_today_settle
    ]
    hist_settle_rows = [
        normalize_settlement_or_pnl_record(
            r, record_type="HISTORY_SETTLEMENT", synced_at=now_iso
        )
        for r in raw_hist_settle
    ]
    pnl_rows = [
        normalize_settlement_or_pnl_record(
            r, record_type="REALIZED_PNL", synced_at=now_iso
        )
        for r in raw_pnl
    ]
    all_settlement_rows = [*today_settle_rows, *hist_settle_rows, *pnl_rows]

    existing_statements: list[dict[str, Any]] = []
    added_statements = 0
    added_settlements = 0
    if sheet_mgr is not None and not dry_run:
        print("[sync-daily] 寫入 Google Sheet 01_Account_Statement 與 03_Settlement_And_PnL...")
        added_statements = sheet_mgr.append_deduplicated_rows(
            STATEMENT_SHEET_NAME,
            recent_statements,
            sort_key="trade_date",
        )
        added_settlements = sheet_mgr.append_deduplicated_rows(
            SETTLEMENT_SHEET_NAME,
            all_settlement_rows,
            sort_key="record_date",
        )
        existing_statements = sheet_mgr.read_records(STATEMENT_SHEET_NAME)
    elif sheet_mgr is not None:
        try:
            existing_statements = sheet_mgr.read_records(STATEMENT_SHEET_NAME)
        except Exception:  # noqa: BLE001
            existing_statements = []

    combined_statements = [*existing_statements, *recent_statements]

    # 2. Fetch latest holdings (`inventories`) & cross-table technical / fundamental cache
    print("[sync-daily] 抓取即時庫存 (inventories) 與跨表歷史加權成本...")
    raw_inventories = client.fetch_inventories()
    tech_map, market_regime = load_local_technical_context(reports_dir=reports_dir, as_of=today)
    holding_stock_ids = {
        clean_leading_zero_code(_pick_first(inv, "stock_no", "symbol", "stock_id", "code", default=""))
        for inv in raw_inventories
    } - {""}
    public_cache = load_public_watchlist_cache(holding_stock_ids)

    holdings_rows = build_portfolio_holdings_rows(
        raw_inventories,
        statement_records=combined_statements,
        tech_map=tech_map,
        public_cache=public_cache,
        snapshot_date=today,
        updated_at=now_iso,
    )

    # 3. AI Diagnosis (STAGE1_MODEL -> fallback VERTEX_STAGE1_MODEL)
    portfolio_overview = ""
    model_version = ""
    if run_ai and holdings_rows:
        print(f"[sync-daily] 正在調用 AI (Gemini) 進行 18 檔持股成本安全墊與均線健診分析（大約需 10~25 秒）...")
        try:
            holdings_rows, portfolio_overview, model_version = asyncio.run(
                run_portfolio_ai_diagnosis(
                    holdings_rows,
                    public_cache=public_cache,
                    recent_statements=combined_statements,
                    market_regime=market_regime,
                )
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("AI portfolio diagnosis failed; keeping rule-based fallback advice: %s", err)
            portfolio_overview = f"AI 診斷暫時無法連線（已套用量化均線保本防線規則）：{err}"

    # 4. Overwrite `02_Portfolio_Holdings`
    if sheet_mgr is not None and not dry_run:
        print("[sync-daily] 覆寫 Google Sheet 02_Portfolio_Holdings 最新庫存狀態...")
        sheet_mgr.overwrite_tab(HOLDINGS_SHEET_NAME, holdings_rows)

    # 5. Format & push report
    messages = format_daily_sync_discord_report(
        as_of=today,
        holdings_rows=holdings_rows,
        added_statements=added_statements,
        added_settlements=added_settlements,
        portfolio_overview=portfolio_overview,
        model_version=model_version,
        market_regime=market_regime,
    )
    print("\n\n".join(messages))

    sent_discord = 0
    if post_discord and not dry_run:
        print("[sync-daily] 推播持股健診與帳務報告至 Discord...")
        sent_discord = post_to_taishin_discord(messages)

    flush_bq_archives()

    return {
        "date": today.isoformat(),
        "login_count": client.login_count,
        "added_statements": added_statements,
        "added_settlements": added_settlements,
        "holdings_count": len(holdings_rows),
        "holdings": holdings_rows,
        "messages": messages,
        "sent_discord": sent_discord,
    }


# --------------------------------------------------------------------------- #
# Section 4.3: 0050 Smart Weekly Dip-Buying DCA Engine (`eval-dca-0050`)
# --------------------------------------------------------------------------- #
def compute_0050_technical_indicators(price_df: pd.DataFrame) -> dict[str, float]:
    """Compute close, intraday_low, MA17, MA45, RSI(14), KD_K(9,3,3), bias_ma17_pct, and 10d high drop %."""
    if price_df.empty:
        raise TaishinPortfolioError("0050 價格資料為空，無法計算技術指標。")

    df = price_df.copy()
    if "date" in df.columns:
        df = df.sort_values("date").reset_index(drop=True)

    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    if closes.empty:
        raise TaishinPortfolioError("0050 缺少有效收盤價。")

    highs = (
        pd.to_numeric(df["max"], errors="coerce")
        if "max" in df.columns
        else (pd.to_numeric(df["high"], errors="coerce") if "high" in df.columns else closes)
    )
    lows = (
        pd.to_numeric(df["min"], errors="coerce")
        if "min" in df.columns
        else (pd.to_numeric(df["low"], errors="coerce") if "low" in df.columns else closes)
    )

    close_price = float(closes.iloc[-1])
    intraday_low = float(lows.dropna().iloc[-1]) if not lows.dropna().empty else close_price
    ma17 = float(closes.tail(17).mean())
    ma45 = float(closes.tail(45).mean())
    bias_ma17_pct = ((close_price - ma17) / ma17 * 100.0) if ma17 > 0 else 0.0

    recent_10d_high = float(highs.tail(10).max()) if not highs.tail(10).dropna().empty else float(closes.tail(10).max())
    ref_low = min(close_price, intraday_low)
    drop_10d_pct = ((recent_10d_high - ref_low) / recent_10d_high * 100.0) if recent_10d_high > 0 else 0.0

    # RSI(14)
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.rolling(window=14, min_periods=1).mean().iloc[-1]
    avg_loss = loss.rolling(window=14, min_periods=1).mean().iloc[-1]
    if avg_loss == 0:
        rsi_14 = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = float(avg_gain) / float(avg_loss)
        rsi_14 = 100.0 - (100.0 / (1.0 + rs))

    # KD_K (9-day RSV, 1/3 smoothing)
    k_val = 50.0
    for idx in range(len(closes)):
        win_start = max(0, idx - 8)
        w_high = float(highs.iloc[win_start : idx + 1].max())
        w_low = float(lows.iloc[win_start : idx + 1].min())
        c_val = float(closes.iloc[idx])
        rsv = ((c_val - w_low) / (w_high - w_low) * 100.0) if w_high > w_low else 50.0
        k_val = (2.0 / 3.0) * k_val + (1.0 / 3.0) * rsv

    return {
        "close_price": round(close_price, 2),
        "intraday_low": round(intraday_low, 2),
        "ma17": round(ma17, 2),
        "ma45": round(ma45, 2),
        "rsi_14": round(float(rsi_14), 2),
        "kd_k": round(float(k_val), 2),
        "bias_ma17_pct": round(bias_ma17_pct, 2),
        "drop_10d_pct": round(drop_10d_pct, 2),
    }


def evaluate_0050_dip_decision(
    metrics: Mapping[str, float],
    *,
    eval_date: date,
    already_bought_this_week: bool,
    weekly_budget_twd: float = DEFAULT_DCA_0050_WEEKLY_BUDGET_TWD,
    market_margin_chg_1d_yi: float | None = None,
    market_margin_chg_5d_yi: float | None = None,
) -> dict[str, Any]:
    """Calculate `dip_score` (0~100), determine whether to trigger 0050 DCA buy, and compute share quantity."""
    close_price = _to_float(metrics.get("close_price"), 0.0)
    intraday_low = _to_float(metrics.get("intraday_low"), close_price)
    ma17 = _to_float(metrics.get("ma17"), close_price)
    ma45 = _to_float(metrics.get("ma45"), close_price)
    rsi_14 = _to_float(metrics.get("rsi_14"), 50.0)
    kd_k = _to_float(metrics.get("kd_k"), 50.0)
    bias_ma17_pct = _to_float(metrics.get("bias_ma17_pct"), 0.0)
    drop_10d_pct = _to_float(metrics.get("drop_10d_pct"), 0.0)

    # Check the 4 Mon-Thu dip triggers
    triggers: list[str] = []
    score = 20

    if bias_ma17_pct <= DCA_MA17_NEG_BIAS_TRIGGER_PCT:
        triggers.append(f"跌破17MA負乖離({bias_ma17_pct:.2f}%<=-1.5%)")
        score += min(30, int(round(abs(bias_ma17_pct) * 10)))

    ref_low = min(close_price, intraday_low)
    if ma45 > 0 and (ref_low <= ma45 * 1.005 or close_price <= ma45 * 1.005):
        triggers.append(f"回踩45MA季線({ma45:.2f})")
        score += 25

    if rsi_14 <= DCA_RSI14_TRIGGER or kd_k <= DCA_KDK_TRIGGER:
        triggers.append(f"指標超賣(RSI={rsi_14:.1f}/K={kd_k:.1f})")
        score += 25

    if drop_10d_pct >= DCA_10D_DROP_TRIGGER_PCT:
        triggers.append(f"近10日高點回落({drop_10d_pct:.2f}%>=2.5%)")
        score += min(20, int(round(drop_10d_pct * 5)))

    # v5.5.0 大盤恐慌殺融資加碼觸發
    if (
        (market_margin_chg_1d_yi is not None and market_margin_chg_1d_yi <= -40.0)
        or (market_margin_chg_5d_yi is not None and market_margin_chg_5d_yi <= -90.0)
    ):
        if market_margin_chg_1d_yi is not None and market_margin_chg_1d_yi <= -40.0:
            triggers.append(f"大盤融資斷頭清洗(單日{market_margin_chg_1d_yi:+.0f}億)")
        else:
            triggers.append(f"大盤融資斷頭清洗(5日{market_margin_chg_5d_yi:+.0f}億)")
        score += 15

    dip_score = max(0, min(100, score))
    iso_cal = eval_date.isocalendar()
    iso_year_week = f"{iso_cal.year:04d}-W{iso_cal.week:02d}"
    weekday = eval_date.weekday()  # 0=Mon .. 4=Fri

    should_buy = False
    if already_bought_this_week:
        trigger_reason = "本週已完成買進，跳過重複扣款"
    elif weekday < 4 and triggers:
        should_buy = True
        trigger_reason = "🎯 觸發低點訊號：" + "、".join(triggers)
    elif weekday == 4:
        should_buy = True
        if triggers:
            trigger_reason = "🎯 週五低點觸發：" + "、".join(triggers)
        else:
            trigger_reason = "📅 週五保底定投（週一至週四未觸發低點）"
    else:
        trigger_reason = "⏳ 尚未出現低點訊號，持續觀望等待"

    # Scale shares by 0.6x ~ 1.5x based on dip_score (0~100)
    multiplier = DCA_MIN_MULTIPLIER + (dip_score / 100.0) * (DCA_MAX_MULTIPLIER - DCA_MIN_MULTIPLIER)
    multiplier = max(DCA_MIN_MULTIPLIER, min(DCA_MAX_MULTIPLIER, round(multiplier, 2)))
    order_qty_shares = (
        max(1, int(round((weekly_budget_twd * multiplier) / close_price)))
        if (should_buy and close_price > 0)
        else 0
    )

    return {
        "iso_year_week": iso_year_week,
        "eval_date": eval_date.isoformat(),
        "close_price": close_price,
        "intraday_low": intraday_low,
        "ma17": ma17,
        "ma45": ma45,
        "rsi_14": rsi_14,
        "kd_k": kd_k,
        "bias_ma17_pct": bias_ma17_pct,
        "dip_score": dip_score,
        "multiplier": multiplier,
        "should_buy": should_buy,
        "trigger_reason": trigger_reason,
        "already_bought_this_week": bool(already_bought_this_week),
        "order_qty_shares": order_qty_shares,
        "order_price": close_price,
    }


def _check_already_bought_0050_this_week(
    sheet_mgr: TaishinSheetManager | None,
    iso_year_week: str,
    eval_date: date,
) -> bool:
    if sheet_mgr is None:
        return False
    try:
        tracker_rows = sheet_mgr.read_records(DCA_0050_SHEET_NAME)
        for r in tracker_rows:
            if (
                str(r.get("iso_year_week", "")).strip() == iso_year_week
                and str(r.get("eval_date", "")).strip() != eval_date.isoformat()
                and _to_int(r.get("order_qty_shares", 0)) > 0
            ):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _load_0050_price_history(
    reports_dir: Path | None = None,
    as_of: date | None = None,
) -> pd.DataFrame:
    """Load 0050 daily prices from local `finmind_reports/daily/*_prices.csv` or FinMind fallback."""
    target_date = as_of or _today_in_taipei()
    root = reports_dir or Path(os.getenv("FINMIND_REPORTS_DIR") or "finmind_reports")
    daily_dir = root / "daily"
    if daily_dir.is_dir():
        candidates = sorted(daily_dir.glob("*_prices.csv"))
        if candidates:
            try:
                df = pd.read_csv(candidates[-1], dtype={"stock_id": str})
                sub = df.loc[df["stock_id"].astype(str).str.strip() == DCA_0050_SYMBOL].copy()
                if not sub.empty:
                    return sub
            except Exception:  # noqa: BLE001
                pass

    # Fallback to FinMind TaiwanStockPrice for 0050
    from FinMind.data import DataLoader

    loader = DataLoader()
    token = (os.getenv("FINMIND_API_TOKEN") or "").strip()
    if token:
        loader.login_by_token(api_token=token)
    start_str = (target_date - timedelta(days=120)).isoformat()
    df = loader.taiwan_stock_daily(
        stock_id=DCA_0050_SYMBOL,
        start_date=start_str,
        end_date=target_date.isoformat(),
    )
    return df


def eval_dca_0050(
    *,
    client: TaishinClient | None = None,
    sheet_mgr: TaishinSheetManager | None = None,
    price_df: pd.DataFrame | None = None,
    eval_date: date | None = None,
    already_bought_override: bool | None = None,
    enable_trading: bool | None = None,
    confirm_live_order: bool = False,
    weekly_budget_twd: float | None = None,
    post_discord: bool = True,
    dry_run: bool = False,
    reports_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the 0050 Smart Weekly DCA evaluation and optional intraday-odd order placement."""
    today = eval_date or _today_in_taipei()
    iso_cal = today.isocalendar()
    iso_year_week = f"{iso_cal.year:04d}-W{iso_cal.week:02d}"

    budget = (
        weekly_budget_twd
        if weekly_budget_twd is not None
        else _to_float(os.getenv("DCA_0050_WEEKLY_BUDGET_TWD"), DEFAULT_DCA_0050_WEEKLY_BUDGET_TWD)
    )
    trading_enabled = (
        enable_trading
        if enable_trading is not None
        else (os.getenv("TAISHIN_ENABLE_TRADING", "false").strip().lower() == "true")
    )

    df_0050 = price_df if price_df is not None else _load_0050_price_history(reports_dir=reports_dir, as_of=today)
    metrics = compute_0050_technical_indicators(df_0050)

    already_bought = (
        already_bought_override
        if already_bought_override is not None
        else _check_already_bought_0050_this_week(sheet_mgr, iso_year_week, today)
    )

    # Try to load recent market margin changes from history
    m_1d, m_5d = None, None
    m_hist_file = (reports_dir or Path("finmind_reports")) / ".state" / "market_margin_history.json"
    if m_hist_file.is_file():
        try:
            m_hist_data = json.loads(m_hist_file.read_text(encoding="utf-8"))
            if isinstance(m_hist_data, dict) and m_hist_data:
                sorted_d = sorted(m_hist_data.keys())
                today_k = today.isoformat()
                cur_i = sorted_d.index(today_k) if today_k in sorted_d else len(sorted_d) - 1
                cur_tot = float(m_hist_data[sorted_d[cur_i]].get("total_margin_yi") or 0.0)
                if cur_i >= 1:
                    m_1d = round(cur_tot - float(m_hist_data[sorted_d[cur_i - 1]].get("total_margin_yi") or 0.0), 2)
                if cur_i >= 5:
                    m_5d = round(cur_tot - float(m_hist_data[sorted_d[cur_i - 5]].get("total_margin_yi") or 0.0), 2)
                elif cur_i > 0:
                    m_5d = round(cur_tot - float(m_hist_data[sorted_d[0]].get("total_margin_yi") or 0.0), 2)
        except Exception:
            pass

    decision = evaluate_0050_dip_decision(
        metrics,
        eval_date=today,
        already_bought_this_week=already_bought,
        weekly_budget_twd=budget,
        market_margin_chg_1d_yi=m_1d,
        market_margin_chg_5d_yi=m_5d,
    )

    order_mode = "DRY_RUN"
    order_no = "NONE"
    if decision["should_buy"] and decision["order_qty_shares"] > 0:
        if trading_enabled and confirm_live_order and not dry_run:
            active_client = client or TaishinClient()
            order_res = active_client.place_0050_intraday_odd_order(
                qty_shares=decision["order_qty_shares"],
                price=decision["order_price"],
                enable_trading=True,
                confirm_live_order=True,
            )
            order_mode = str(order_res.get("order_mode", "LIVE_ORDER"))
            order_no = str(order_res.get("order_no", "LIVE_SUBMITTED"))
        else:
            order_mode = "DRY_RUN"
            order_no = "DRY_RUN"

    tracker_row = {
        "iso_year_week": decision["iso_year_week"],
        "eval_date": decision["eval_date"],
        "close_price": decision["close_price"],
        "intraday_low": decision["intraday_low"],
        "ma17": decision["ma17"],
        "ma45": decision["ma45"],
        "rsi_14": decision["rsi_14"],
        "kd_k": decision["kd_k"],
        "bias_ma17_pct": decision["bias_ma17_pct"],
        "dip_score": decision["dip_score"],
        "trigger_reason": decision["trigger_reason"],
        "already_bought_this_week": "TRUE" if decision["already_bought_this_week"] else "FALSE",
        "order_mode": order_mode,
        "order_qty_shares": decision["order_qty_shares"],
        "order_price": decision["order_price"] if decision["order_qty_shares"] > 0 else 0.0,
        "order_no": order_no,
        "updated_at": _now_iso_in_taipei(),
    }

    if sheet_mgr is not None and not dry_run:
        sheet_mgr.append_deduplicated_rows(DCA_0050_SHEET_NAME, [tracker_row])

    msg = (
        f"📈 【0050 每週智慧尋底定投評估】({today.isoformat()} / {iso_year_week})\n"
        f"• 現價：{decision['close_price']}（盤中低點 {decision['intraday_low']}）｜"
        f"17MA：{decision['ma17']}（乖離 {decision['bias_ma17_pct']:+.2f}%）｜45MA：{decision['ma45']}\n"
        f"• 指標：RSI(14)={decision['rsi_14']:.1f} ｜ KD_K={decision['kd_k']:.1f} ｜ "
        f"低點評分 (Dip Score)：{decision['dip_score']}/100（權重 {decision['multiplier']:.2f}x）\n"
        f"• 判定結果：{decision['trigger_reason']}\n"
        f"• 執行模式：{order_mode} ｜ 建議買進股數：{decision['order_qty_shares']} 股 "
        f"@ {decision['order_price']} 元（委託單號：{order_no}）"
    )
    print(msg)

    if post_discord and not dry_run:
        post_to_taishin_discord([msg])

    return {
        **decision,
        "order_mode": order_mode,
        "order_no": order_no,
        "tracker_row": tracker_row,
        "message": msg,
    }


# --------------------------------------------------------------------------- #
# Section 4.4: Position Thesis Memory, Style B Gatekeeper, Attribution & Battle Brief Engine
# --------------------------------------------------------------------------- #
def extract_first_price(value: Any, default: float = 0.0) -> float:
    """Extract the primary numeric price from free-form text like '1,020 元 (17MA+12%)' or '收盤-4% (95.2元)'.

    Strips moving-average tokens (e.g. '17MA', 'MA45'), percentage tokens ('+12%', '-4%'),
    date/quarter tokens ('2026Q3', '2026-10'), and day/multiplier counts ('20日', '1.5倍')
    so non-price numbers are never mistaken for prices.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        if math.isnan(value) or math.isinf(value) or value <= 0:
            return default
        return round(float(value), 2)

    text = str(value).strip()
    if not text:
        return default

    # Fast path for pure numeric string
    pure = text.replace(",", "").strip()
    try:
        num = float(pure)
        return round(num, 2) if num > 0 else default
    except ValueError:
        pass

    cleaned = text.replace(",", "")
    # Strip dates / quarters first
    cleaned = re.sub(r"\b20\d{2}\s*Q[1-4]\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b20\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?\b", " ", cleaned)
    # Strip MA tokens like 17MA, 45MA, MA17, MA117, 17/45MA
    cleaned = re.sub(r"\b\d+(?:/\d+)?\s*MA\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bMA\s*\d+\b", " ", cleaned, flags=re.IGNORECASE)
    # Strip percentages like +12%, -4.5%
    cleaned = re.sub(r"[+-]?\d+(?:\.\d+)?\s*%", " ", cleaned)
    # Strip durations / multipliers / lot counts like 5~20日, 60日, 18倍, 1.5x, 500張
    cleaned = re.sub(r"\d+(?:~\d+)?(?:\.\d+)?\s*(?:日|天|週|季|年|倍|張|[xX])\b", " ", cleaned)

    # Prefer numbers explicitly followed by '元' first
    m_yuan = re.search(r"(?<![A-Za-z0-9_-])(\d+(?:\.\d+)?)\s*元", cleaned)
    if m_yuan:
        val = _to_float(m_yuan.group(1), 0.0)
        if val > 0:
            return round(val, 2)

    m_any = re.search(r"(?<![A-Za-z0-9_-])(\d+(?:\.\d+)?)", cleaned)
    if m_any:
        val = _to_float(m_any.group(1), 0.0)
        if val > 0:
            return round(val, 2)
    return default


def compute_holding_waves_from_statements(
    statements: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Replay historical statements chronologically to identify each stock's current holding wave.

    Whenever running share balance drops to 0 and later increases > 0 on a BUY, a new wave begins
    with a fresh `opened_date`, `initial_qty`, and `peak_qty`.
    """
    sorted_rows = sorted(
        statements,
        key=lambda r: (str(r.get("trade_date", "")), str(r.get("order_no", ""))),
    )
    waves: dict[str, dict[str, Any]] = {}

    for r in sorted_rows:
        stock_no = clean_leading_zero_code(r.get("stock_no", ""))
        if not stock_no:
            continue
        action = str(r.get("action", "")).upper()
        qty = _to_int(r.get("filled_qty", 0))
        price = _to_float(r.get("filled_price", 0.0))
        t_date = str(r.get("trade_date", "")).strip()
        if qty <= 0:
            continue

        state = waves.setdefault(
            stock_no,
            {
                "opened_date": "",
                "initial_qty": 0,
                "peak_qty": 0,
                "running_qty": 0,
                "running_cost": 0.0,
                "last_buy_date": "",
                "last_sell_date": "",
            },
        )
        if action == "BUY":
            if state["running_qty"] <= 0:
                state["opened_date"] = t_date
                state["initial_qty"] = qty
                state["peak_qty"] = qty
                state["running_qty"] = qty
                state["running_cost"] = qty * price if price > 0 else 0.0
            else:
                if t_date == state["opened_date"]:
                    state["initial_qty"] += qty
                state["running_qty"] += qty
                if price > 0:
                    state["running_cost"] += qty * price
                state["peak_qty"] = max(state["peak_qty"], state["running_qty"])
            state["last_buy_date"] = t_date
        elif action == "SELL":
            cur_q = state["running_qty"]
            if cur_q > 0:
                avg_p = (state["running_cost"] / cur_q) if cur_q > 0 else 0.0
                rem_q = max(0, cur_q - qty)
                state["running_qty"] = rem_q
                state["running_cost"] = rem_q * avg_p
                if rem_q == 0:
                    state["opened_date"] = ""
                    state["initial_qty"] = 0
                    state["peak_qty"] = 0
            state["last_sell_date"] = t_date

    return waves


def load_public_picks_for_attribution(
    *,
    reports_dir: Path | None = None,
) -> dict[str, Any]:
    """Read `AI_Daily_Picks`, `Rebound_Watchlist`, and `00_Watchlist_Master` read-only for buy attribution."""
    ai_daily_picks: list[dict[str, Any]] = []
    rebound_watchlist: list[dict[str, Any]] = []
    watchlist_master: dict[str, dict[str, str]] = {}

    public_sheet_id = (os.getenv("GSHEET_SPREADSHEET_ID") or "").strip()
    creds_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "certs/sheetbot.json").strip()
    if creds_path and Path(creds_path).is_file():
        pass
    elif Path("certs/sheetbot.json").is_file():
        creds_path = "certs/sheetbot.json"
    elif Path("sheetbot.json").is_file():
        creds_path = "sheetbot.json"

    if public_sheet_id and Path(creds_path).is_file():
        try:
            gc = gspread.service_account(filename=creds_path)
            sp = gc.open_by_key(public_sheet_id)

            def _read_ws_dicts(tab: str) -> list[dict[str, Any]]:
                try:
                    ws = sp.worksheet(tab)
                    rows = ws.get_all_values()
                except Exception:  # noqa: BLE001
                    return []
                if not rows or len(rows) < 2:
                    return []
                hdrs = [str(h).strip() for h in rows[0]]
                out: list[dict[str, Any]] = []
                for raw in rows[1:]:
                    padded = list(raw) + [""] * max(0, len(hdrs) - len(raw))
                    rec = {hdrs[i]: padded[i] for i in range(len(hdrs)) if hdrs[i]}
                    if any(v not in ("", None) for v in rec.values()):
                        out.append(rec)
                return out

            ai_daily_picks = _read_ws_dicts("AI_Daily_Picks")
            rebound_watchlist = _read_ws_dicts("Rebound_Watchlist")
            for rec in _read_ws_dicts("00_Watchlist_Master"):
                sid = clean_leading_zero_code(rec.get("stock_id", ""))
                if sid:
                    watchlist_master[sid] = {
                        "stock_name": str(rec.get("stock_name", "")).strip(),
                        "ai_summary_json": str(rec.get("ai_summary_json", "")).strip(),
                        "war_room_url": str(rec.get("war_room_url", "")).strip(),
                    }
        except Exception as err:  # noqa: BLE001
            logger.warning("Failed to read public GSheet picks for attribution: %s", err)

    # Supplement from local `finmind_reports/daily` JSON/CSV if public sheet was empty or unavailable
    root = reports_dir or Path(os.getenv("FINMIND_REPORTS_DIR") or "finmind_reports")
    daily_dir = root / "daily"
    if daily_dir.is_dir():
        seen_pick_keys = {
            f"{r.get('scan_date')}_{clean_leading_zero_code(r.get('stock_id'))}"
            for r in ai_daily_picks
        }
        for rec_json in sorted(daily_dir.glob("*_ai_recommendations.json")):
            try:
                payload = json.loads(rec_json.read_text(encoding="utf-8"))
                scan_dt = str(payload.get("date") or rec_json.name.split("_")[0]).strip()
                dec = payload.get("decision") or {}
                cands = payload.get("candidates") or {}
                for pick in dec.get("top_picks") or []:
                    sid = clean_leading_zero_code(pick.get("stock_id", ""))
                    pk = f"{scan_dt}_{sid}"
                    if not sid or pk in seen_pick_keys:
                        continue
                    seen_pick_keys.add(pk)
                    ai_daily_picks.append(
                        {
                            "pk_key": pk,
                            "scan_date": scan_dt,
                            "rank": pick.get("rank", ""),
                            "category": "TOP_PICK",
                            "stock_id": sid,
                            "tactical_type": pick.get("tactical_type", ""),
                            "target_price_1": pick.get("target_price_1", ""),
                            "target_price_2": pick.get("target_price_2", ""),
                            "stop_loss_price": pick.get("stop_loss_price", ""),
                            "entry_thesis": pick.get("bull_thesis", ""),
                            "risk_note": pick.get("bear_case_and_invalidation", ""),
                        }
                    )
                for idx, watch in enumerate(dec.get("watchlist") or [], start=1):
                    sid = clean_leading_zero_code(watch.get("stock_id", ""))
                    pk = f"{scan_dt}_{sid}"
                    if not sid or pk in seen_pick_keys:
                        continue
                    seen_pick_keys.add(pk)
                    s1_raw = str((cands.get(sid) or {}).get("summary") or "")
                    s1_plan: dict[str, Any] = {}
                    try:
                        s1_obj = json.loads(s1_raw)
                        if isinstance(s1_obj, dict) and isinstance(s1_obj.get("trade_plan"), dict):
                            s1_plan = s1_obj["trade_plan"]
                    except Exception:  # noqa: BLE001
                        pass
                    ai_daily_picks.append(
                        {
                            "pk_key": pk,
                            "scan_date": scan_dt,
                            "rank": f"W{idx}",
                            "category": "WATCHLIST",
                            "stock_id": sid,
                            "tactical_type": s1_plan.get("tactical_type", ""),
                            "target_price_1": s1_plan.get("target_price_1", ""),
                            "target_price_2": s1_plan.get("target_price_2", ""),
                            "stop_loss_price": s1_plan.get("stop_loss_price", ""),
                            "entry_thesis": watch.get("reason", ""),
                            "risk_note": s1_plan.get("invalidation", ""),
                        }
                    )
            except Exception:  # noqa: BLE001
                continue

        seen_reb_keys = {
            f"{r.get('scan_date')}_{clean_leading_zero_code(r.get('stock_id'))}"
            for r in rebound_watchlist
        }
        for reb_csv in sorted(daily_dir.glob("*_rebound_signals.csv")):
            try:
                scan_dt = reb_csv.name.split("_rebound_signals.csv")[0]
                df_reb = pd.read_csv(reb_csv, dtype={"stock_id": str})
                for r in df_reb.to_dict("records"):
                    sid = clean_leading_zero_code(r.get("stock_id", ""))
                    pk = f"{scan_dt}_{sid}"
                    if not sid or pk in seen_reb_keys:
                        continue
                    seen_reb_keys.add(pk)
                    rebound_watchlist.append(
                        {
                            "pk_key": pk,
                            "scan_date": scan_dt,
                            "stock_id": sid,
                            "stock_name": r.get("stock_name", ""),
                            "close_price": r.get("close", r.get("close_price", "")),
                            "high_60d": r.get("high_60d", ""),
                            "support_ma": r.get("support_ma", ""),
                            "support_price": r.get("support_price", ""),
                            "target_ma17": r.get("target_ma17", ""),
                        }
                    )
            except Exception:  # noqa: BLE001
                continue

    return {
        "ai_daily_picks": ai_daily_picks,
        "rebound_watchlist": rebound_watchlist,
        "watchlist_master": watchlist_master,
    }


def _parse_stage1_trade_plan(ai_summary_raw: Any) -> tuple[dict[str, Any], str]:
    """Return `(trade_plan_dict, verdict_text)` from Stage 1 `ai_summary_json`."""
    raw = str(ai_summary_raw or "").strip()
    if not raw:
        return {}, ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}, ""
    if not isinstance(parsed, dict):
        return {}, ""
    plan = parsed.get("trade_plan")
    verdict = str(parsed.get("verdict") or parsed.get("one_line_verdict") or "").strip()
    return (dict(plan) if isinstance(plan, Mapping) else {}), verdict


def match_entry_to_picks(
    stock_no: str,
    opened_date: date | str,
    *,
    ai_daily_picks: list[dict[str, Any]],
    rebound_watchlist: list[dict[str, Any]],
    public_cache: Mapping[str, Mapping[str, str]] | None = None,
    avg_cost_price: float = 0.0,
    lookback_trading_days: int = DEFAULT_ATTRIBUTION_LOOKBACK_TRADING_DAYS,
    max_calendar_days: int = DEFAULT_ATTRIBUTION_MAX_CALENDAR_DAYS,
) -> dict[str, Any] | None:
    """Match a buy wave (`opened_date`, `stock_no`) against recent 5 trading days of picks.

    Priority:
      1. `AI_Daily_Picks` (`category == "TOP_PICK"`)
      2. `AI_Daily_Picks` (`category == "WATCHLIST"`)
      3. `Rebound_Watchlist` (Pattern A Rebound)

    Returns `None` if the stock was NOT in any actionable list within the window (e.g. `0050`,
    legacy holdings, or discretionary trades).
    """
    sno = clean_leading_zero_code(stock_no)
    opened_dt = parse_flexible_date(opened_date)
    if not sno or opened_dt is None:
        return None

    # Gather all observed scan dates <= opened_dt to determine the N-trading-day window
    observed_dates: set[date] = set()
    for row in [*ai_daily_picks, *rebound_watchlist]:
        dt = parse_flexible_date(row.get("scan_date"))
        if dt is not None and dt <= opened_dt:
            observed_dates.add(dt)

    sorted_scan_dates = sorted(observed_dates, reverse=True)
    allowed_trading_dates: set[date] = set(sorted_scan_dates[: max(1, lookback_trading_days)])

    def _in_window(raw_scan_date: Any) -> tuple[bool, date | None]:
        sdt = parse_flexible_date(raw_scan_date)
        if sdt is None or sdt > opened_dt:
            return False, None
        if (opened_dt - sdt).days > max_calendar_days:
            return False, None
        if allowed_trading_dates and sdt not in allowed_trading_dates:
            return False, None
        return True, sdt

    pub_entry = (public_cache or {}).get(sno, {})
    pub_plan, pub_verdict = _parse_stage1_trade_plan(pub_entry.get("ai_summary_json", ""))
    pub_war_room = str(pub_entry.get("war_room_url", "")).strip()

    # 1 & 2. Search AI_Daily_Picks (TOP_PICK first, then WATCHLIST)
    top_matches: list[tuple[date, dict[str, Any]]] = []
    watch_matches: list[tuple[date, dict[str, Any]]] = []
    for row in ai_daily_picks:
        if clean_leading_zero_code(row.get("stock_id", "")) != sno:
            continue
        ok, sdt = _in_window(row.get("scan_date"))
        if not ok or sdt is None:
            continue
        cat = str(row.get("category", "")).strip().upper()
        if cat == "TOP_PICK":
            top_matches.append((sdt, row))
        elif cat == "WATCHLIST":
            watch_matches.append((sdt, row))

    top_matches.sort(key=lambda t: t[0], reverse=True)
    watch_matches.sort(key=lambda t: t[0], reverse=True)

    if top_matches or watch_matches:
        is_top = bool(top_matches)
        sdt, matched = top_matches[0] if is_top else watch_matches[0]
        ref_close = _to_float(matched.get("close_at_pick"), avg_cost_price)
        ref_ma17 = _to_float(matched.get("ma17_at_pick"), ref_close)

        tp1 = (
            extract_first_price(matched.get("TP1_Target_Or_POC"))
            or extract_first_price(matched.get("target_price_1"))
            or extract_first_price(pub_plan.get("target_price_1"))
        )
        tp2 = extract_first_price(matched.get("target_price_2")) or extract_first_price(pub_plan.get("target_price_2"))
        sl = (
            extract_first_price(matched.get("Hard_Stop_Loss"))
            or extract_first_price(matched.get("stop_loss_price"))
            or extract_first_price(pub_plan.get("stop_loss_price"))
        )

        base_px = avg_cost_price if avg_cost_price > 0 else ref_close
        if tp1 <= 0 and base_px > 0:
            tp1 = round(base_px * 1.10, 2)
        if tp2 <= tp1 and tp1 > 0:
            tp2 = round(tp1 * 1.08, 2)
        if sl <= 0 and base_px > 0:
            sl = round(min(base_px * 0.96, ref_ma17 * 0.98 if ref_ma17 > 0 else base_px * 0.96), 2)

        tactical = (
            str(matched.get("tactical_type") or pub_plan.get("tactical_type") or "🎯 短中期波段持有 (5~20日)").strip()
        )
        thesis = str(matched.get("entry_thesis") or pub_verdict or "依據 AI 嚴選訊號建倉").strip()
        invalidation = str(
            matched.get("Invalidation_Triggers")
            or matched.get("risk_note")
            or pub_plan.get("invalidation")
            or "跌破關鍵防守均線或基本面轉弱退場"
        ).strip()
        war_url = str(matched.get("war_room_url") or pub_war_room).strip()

        return {
            "entry_source": "AI_DAILY_TOP_PICK" if is_top else "AI_DAILY_WATCHLIST",
            "source_scan_date": sdt.isoformat(),
            "source_rank": str(matched.get("rank") or ("1" if is_top else "WATCH")).strip(),
            "stock_name": str(matched.get("stock_name") or pub_entry.get("stock_name") or "").strip(),
            "tactical_type": tactical,
            "initial_tp1": round(tp1, 2),
            "initial_tp2": round(tp2, 2),
            "initial_sl": round(sl, 2),
            "entry_thesis": thesis,
            "invalidation_rule": invalidation,
            "war_room_url": war_url,
            "Audit_Grade": str(matched.get("Audit_Grade") or "").strip(),
            "Execution_Tactic": str(matched.get("Execution_Tactic") or "").strip(),
            "Ideal_Buy_Or_Ref": matched.get("Ideal_Buy_Or_Ref", ""),
            "Chase_Limit_Price": matched.get("Chase_Limit_Price", ""),
            "Hard_Stop_Loss": matched.get("Hard_Stop_Loss", round(sl, 2)),
            "TP1_Target_Or_POC": matched.get("TP1_Target_Or_POC", round(tp1, 2)),
            "Deterministic_RR": matched.get("Deterministic_RR", ""),
            "Bear_Case_Warning": str(matched.get("Bear_Case_Warning") or "").strip(),
            "Invalidation_Triggers": str(matched.get("Invalidation_Triggers") or "").strip(),
        }

    # 3. Search Rebound_Watchlist (Pattern A Rebound)
    reb_matches: list[tuple[date, dict[str, Any]]] = []
    for row in rebound_watchlist:
        if clean_leading_zero_code(row.get("stock_id", "")) != sno:
            continue
        ok, sdt = _in_window(row.get("scan_date"))
        if ok and sdt is not None:
            reb_matches.append((sdt, row))

    reb_matches.sort(key=lambda t: t[0], reverse=True)
    if reb_matches:
        sdt, reb = reb_matches[0]
        reb_plan, reb_verdict = _parse_stage1_trade_plan(reb.get("ai_summary_json") or pub_entry.get("ai_summary_json"))
        close_px = _to_float(reb.get("close_price"), avg_cost_price)
        base_px = avg_cost_price if avg_cost_price > 0 else close_px
        target_ma17 = _to_float(reb.get("target_ma17"), 0.0)
        high_60d = _to_float(reb.get("high_60d"), 0.0)
        support_px = _to_float(reb.get("support_price"), 0.0)
        support_ma = str(reb.get("support_ma") or "45MA/117MA").strip()

        tp1 = target_ma17 if target_ma17 > base_px else extract_first_price(reb_plan.get("target_price_1"))
        if tp1 <= 0 and base_px > 0:
            tp1 = round(base_px * 1.07, 2)

        tp2 = extract_first_price(reb_plan.get("target_price_2"))
        if tp2 <= tp1:
            if high_60d > tp1:
                tp2 = round(min(high_60d, tp1 * 1.12), 2)
            else:
                tp2 = round(tp1 * 1.08, 2)

        sl = extract_first_price(reb_plan.get("stop_loss_price"))
        if sl <= 0:
            sl = round(support_px * 0.97, 2) if support_px > 0 else round(base_px * 0.95, 2)

        thesis = (
            f"【型態A反彈】回踩 {support_ma} ({support_px}) 均值回歸，第一目標反壓 17MA ({tp1})"
            + (f"｜{reb_verdict}" if reb_verdict else "")
        )
        invalidation = str(
            reb_plan.get("invalidation") or f"帶量實質跌破 {support_ma} 支撐 ({sl} 元) 反彈失敗無條件停損"
        ).strip()
        war_url = str(reb.get("war_room_url") or pub_war_room).strip()

        return {
            "entry_source": "REBOUND_WATCHLIST",
            "source_scan_date": sdt.isoformat(),
            "source_rank": "REBOUND",
            "stock_name": str(reb.get("stock_name") or pub_entry.get("stock_name") or "").strip(),
            "tactical_type": f"⚡ 型態A長均線反彈（防守 {support_ma}）",
            "initial_tp1": round(tp1, 2),
            "initial_tp2": round(tp2, 2),
            "initial_sl": round(sl, 2),
            "entry_thesis": thesis[:220],
            "invalidation_rule": invalidation[:220],
            "war_room_url": war_url,
        }

    return None


def compute_style_b_trim_plan(current_qty: int) -> dict[str, Any]:
    """Calculate exact Style B (~50% partial take-profit) share count and order method.

    - `>= 2,000` shares (>= 2 lots): sell whole lots `(current_qty // 2000) * 1000` via `整張現股`.
    - `2 .. 1,999` shares (e.g. 1 lot = 1,000 shares or odd lots): sell `current_qty // 2` via `盤中零股`.
    - `1` share: sell `1` share via `盤中零股`.
    """
    qty = max(0, int(current_qty))
    if qty <= 0:
        return {"trim_qty": 0, "remain_qty": 0, "order_method": "無庫存", "instruction_hint": "無庫存"}
    if qty >= 2000:
        lots_to_sell = (qty // 1000) // 2
        if lots_to_sell < 1:
            lots_to_sell = 1
        trim_qty = lots_to_sell * 1000
        remain_qty = qty - trim_qty
        return {
            "trim_qty": trim_qty,
            "remain_qty": remain_qty,
            "order_method": "整張現股",
            "instruction_hint": f"掛【整張現股】賣出 {trim_qty:,} 股（{lots_to_sell} 張）鎖利，留 {remain_qty:,} 股拼 TP2",
        }
    if qty >= 2:
        trim_qty = qty // 2
        remain_qty = qty - trim_qty
        return {
            "trim_qty": trim_qty,
            "remain_qty": remain_qty,
            "order_method": "盤中零股",
            "instruction_hint": f"掛【盤中零股】賣出 {trim_qty:,} 股鎖利，留 {remain_qty:,} 股拼 TP2",
        }
    return {
        "trim_qty": 1,
        "remain_qty": 0,
        "order_method": "盤中零股",
        "instruction_hint": "掛【盤中零股】賣出 1 股獲利了結",
    }


def compute_ratchet_trailing_stop(
    *,
    prev_trailing_stop: float,
    initial_sl: float,
    avg_cost_price: float,
    current_price: float,
    initial_tp1: float,
    ma17: float,
    status: str,
) -> float:
    """Compute a strictly non-decreasing (ratchet) trailing stop for long positions."""
    floor = max(_to_float(prev_trailing_stop, 0.0), _to_float(initial_sl, 0.0))
    if avg_cost_price <= 0:
        return round(floor, 2)

    breakeven = round(avg_cost_price * BREAKEVEN_BUFFER_MULTIPLIER, 2)
    ret_pct = ((current_price - avg_cost_price) / avg_cost_price * 100.0) if current_price > 0 else 0.0

    # Once profit >= +8% or TP1 partial profit has been taken, lock at least breakeven
    if ret_pct >= RATCHET_PROFIT_TRIGGER_PCT or status == "TP1_PARTIAL_TAKEN":
        if current_price <= 0 or current_price >= breakeven:
            floor = max(floor, breakeven)

    # After TP1 partial take-profit, trail along min(initial_tp1, ma17) * 0.985 when above breakeven
    if status == "TP1_PARTIAL_TAKEN" and initial_tp1 > 0:
        candidates = [v for v in (initial_tp1 * 0.985, ma17 * 0.985 if ma17 > 0 else 0.0) if v > breakeven]
        if candidates:
            tighter = round(min(candidates), 2)
            if current_price <= 0 or tighter < current_price:
                floor = max(floor, tighter)

    return round(floor, 2)


def _aggregate_holdings_by_stock(holdings_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate `02_Portfolio_Holdings` across `現股` and `盤中零股` per `stock_no`."""
    agg: dict[str, dict[str, Any]] = {}
    for r in holdings_rows:
        sno = clean_leading_zero_code(r.get("stock_no", ""))
        if not sno:
            continue
        qty = _to_int(r.get("qty", 0))
        if qty <= 0:
            continue
        cost = _to_float(r.get("total_cost"), qty * _to_float(r.get("avg_cost_price"), 0.0))
        mv = _to_float(r.get("market_value"), qty * _to_float(r.get("current_price"), 0.0))
        if sno not in agg:
            agg[sno] = {
                "stock_no": sno,
                "stock_name": str(r.get("stock_name", sno)).strip(),
                "qty": qty,
                "tradable_qty": _to_int(r.get("tradable_qty", qty)),
                "total_cost": cost,
                "market_value": mv,
                "current_price": _to_float(r.get("current_price"), 0.0),
                "ma17": _to_float(r.get("ma17"), 0.0),
                "ma45": _to_float(r.get("ma45"), 0.0),
                "ma117": _to_float(r.get("ma117"), 0.0),
                "ma189": _to_float(r.get("ma189"), 0.0),
                "weight_pct": _to_float(r.get("weight_pct"), 0.0),
                "war_room_url": str(r.get("war_room_url", "")).strip(),
            }
        else:
            item = agg[sno]
            item["qty"] += qty
            item["tradable_qty"] += _to_int(r.get("tradable_qty", qty))
            item["total_cost"] += cost
            item["market_value"] += mv
            item["weight_pct"] = round(item["weight_pct"] + _to_float(r.get("weight_pct"), 0.0), 2)
            if not item["war_room_url"] and r.get("war_room_url"):
                item["war_room_url"] = str(r["war_room_url"]).strip()

    for sno, item in agg.items():
        q = item["qty"]
        tc = item["total_cost"]
        mv = item["market_value"]
        item["avg_cost_price"] = round(tc / q, 2) if q > 0 else 0.0
        item["unrealized_pnl"] = int(round(mv - tc))
        item["return_pct"] = round(((mv - tc) / tc * 100.0), 2) if tc > 0 else 0.0
    return agg


def _sum_realized_pnl_for_wave(
    settlement_records: list[dict[str, Any]],
    *,
    stock_no: str,
    opened_date: str,
) -> int:
    sno = clean_leading_zero_code(stock_no)
    total = 0
    for rec in settlement_records:
        if str(rec.get("record_type", "")).upper() != "REALIZED_PNL":
            continue
        if clean_leading_zero_code(rec.get("stock_no", "")) != sno:
            continue
        r_date = str(rec.get("record_date", "")).strip()
        if opened_date and r_date and r_date < opened_date:
            continue
        total += _to_int(rec.get("realized_pnl", 0))
    return total


def sync_holdings_with_memory_cards(
    holdings_rows: list[dict[str, Any]],
    memory_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project active `05_Position_Thesis_Memory` fields onto `02_Portfolio_Holdings` rows.

    Holdings WITHOUT an active memory card (e.g. `0050` or legacy holdings) are cleanly marked
    with `📦 帳面損益觀察` and blank target/stop prices per user rules.
    """
    active_by_stock: dict[str, dict[str, Any]] = {}
    for m in memory_rows:
        st = str(m.get("status", "")).strip().upper()
        if st in ("OPEN", "TP1_PARTIAL_TAKEN"):
            sno = clean_leading_zero_code(m.get("stock_no", ""))
            if sno:
                active_by_stock[sno] = m

    projected: list[dict[str, Any]] = []
    for row in holdings_rows:
        r = dict(row)
        sno = clean_leading_zero_code(r.get("stock_no", ""))
        mem = active_by_stock.get(sno)
        if mem is None:
            r["ai_action_tag"] = UNTRACKED_HOLDING_TAG
            r["ai_target_price"] = ""
            r["ai_stop_loss"] = ""
            r["ai_advice_summary"] = "非嚴選波段追蹤標的（僅記錄帳面損益，不設定波段目標價）。"
        else:
            st = str(mem.get("status", "OPEN")).upper()
            tp1 = _to_float(mem.get("initial_tp1"), 0.0)
            tp2 = _to_float(mem.get("initial_tp2"), 0.0)
            active_tp = tp2 if (st == "TP1_PARTIAL_TAKEN" and tp2 > 0) else tp1
            t_stop = _to_float(mem.get("trailing_stop"), _to_float(mem.get("initial_sl"), 0.0))
            last_tag = str(mem.get("last_ai_action", "")).strip()
            last_sum = str(mem.get("last_ai_summary", "")).strip()

            r["ai_action_tag"] = normalize_ai_action_tag(last_tag) if last_tag else "🟢 順勢續抱"
            r["ai_target_price"] = round(active_tp, 2) if active_tp > 0 else ""
            r["ai_stop_loss"] = round(t_stop, 2) if t_stop > 0 else ""
            if last_sum:
                r["ai_advice_summary"] = last_sum
            else:
                src_label = f"{mem.get('source_scan_date')} #{mem.get('source_rank')}"
                r["ai_advice_summary"] = (
                    f"【記憶綁定 {src_label}】原定 TP1={tp1} / TP2={tp2}，目前棘輪防守價={t_stop}。"
                )
            if not r.get("war_room_url") and mem.get("war_room_url"):
                r["war_room_url"] = str(mem["war_room_url"]).strip()
        projected.append(r)
    return projected


def reconcile_position_memory(
    *,
    existing_memory_rows: list[dict[str, Any]],
    holdings_rows: list[dict[str, Any]],
    statement_records: list[dict[str, Any]],
    settlement_records: list[dict[str, Any]],
    picks_bundle: Mapping[str, Any],
    public_cache: Mapping[str, Mapping[str, str]] | None = None,
    as_of: date | None = None,
    updated_at: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconcile `05_Position_Thesis_Memory` with latest Taishin holdings, statements, and recent picks.

    Key guarantees:
    1. Only newly opened waves matching recent 5-trading-day `AI_Daily_Picks` (`TOP_PICK` / `WATCHLIST`)
       or `Rebound_Watchlist` create memory cards. Legacy / ETF (`0050`) / discretionary buys are ignored.
    2. Scale-ins (`current_qty > peak_qty`) update `avg_cost_price` & `peak_qty` WITHOUT overwriting
       `initial_tp1` / `initial_tp2` / `entry_thesis` (zero goalpost drift).
    3. Partial ~50% sell near/above `initial_tp1` transitions `OPEN -> TP1_PARTIAL_TAKEN` and locks breakeven.
    4. Full exit (`current_qty == 0`) transitions `-> CLOSED` and records realized PnL.
    """
    today_iso = (as_of or _today_in_taipei()).isoformat()
    now_iso = updated_at or _now_iso_in_taipei()
    agg_holdings = _aggregate_holdings_by_stock(holdings_rows)
    waves = compute_holding_waves_from_statements(statement_records)

    ai_picks = list(picks_bundle.get("ai_daily_picks") or [])
    reb_picks = list(picks_bundle.get("rebound_watchlist") or [])
    merged_pub_cache: dict[str, dict[str, str]] = {}
    if isinstance(picks_bundle.get("watchlist_master"), Mapping):
        for k, v in picks_bundle["watchlist_master"].items():
            merged_pub_cache[str(k)] = dict(v)
    if public_cache:
        for k, v in public_cache.items():
            merged_pub_cache[str(k)] = {**merged_pub_cache.get(str(k), {}), **dict(v)}

    updated_memory: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    active_handled_stocks: set[str] = set()
    known_position_ids: set[str] = set()

    for raw_mem in existing_memory_rows:
        mem = dict(raw_mem)
        sno = clean_leading_zero_code(mem.get("stock_no", ""))
        mem["stock_no"] = sno
        pid = clean_leading_zero_code(mem.get("position_id", "")) or f"{mem.get('opened_date', '')}_{sno}"
        mem["position_id"] = pid
        known_position_ids.add(pid)

        status = str(mem.get("status", "OPEN")).strip().upper()
        if status == "CLOSED":
            updated_memory.append(mem)
            continue

        active_handled_stocks.add(sno)
        hold = agg_holdings.get(sno)

        if hold is None or _to_int(hold.get("qty", 0)) <= 0:
            # Position was completely sold out!
            realized = _sum_realized_pnl_for_wave(
                settlement_records,
                stock_no=sno,
                opened_date=str(mem.get("opened_date", "")),
            )
            mem["status"] = "CLOSED"
            mem["current_qty"] = 0
            mem["closed_date"] = today_iso
            if realized != 0 or not mem.get("realized_pnl"):
                mem["realized_pnl"] = realized
            mem["updated_at"] = now_iso
            updated_memory.append(mem)
            events.append(
                {
                    "type": "CLOSED",
                    "stock_no": sno,
                    "stock_name": mem.get("stock_name", sno),
                    "position_id": pid,
                    "realized_pnl": mem.get("realized_pnl", 0),
                }
            )
            continue

        # Active holding still open: update qty, avg_cost_price, ratchet stop, and check partial take-profit
        cur_qty = _to_int(hold["qty"])
        prev_peak = max(_to_int(mem.get("peak_qty", 0)), _to_int(mem.get("initial_qty", 0)), 1)
        new_peak = max(prev_peak, cur_qty)
        avg_cost = _to_float(hold["avg_cost_price"], _to_float(mem.get("avg_cost_price"), 0.0))
        cur_price = _to_float(hold["current_price"], avg_cost)
        ma17 = _to_float(hold.get("ma17"), cur_price)
        tp1 = _to_float(mem.get("initial_tp1"), 0.0)
        init_sl = _to_float(mem.get("initial_sl"), 0.0)
        prev_stop = _to_float(mem.get("trailing_stop"), init_sl)

        # Detect Style B partial take-profit execution (~30%+ trimmed near TP1 or after trim advice)
        last_act = str(mem.get("last_ai_action", ""))
        near_tp1 = (tp1 > 0 and cur_price >= tp1 * 0.96) or ("分批停利" in last_act)
        if status == "OPEN" and prev_peak >= 2 and 0 < cur_qty <= int(prev_peak * 0.70) and near_tp1:
            status = "TP1_PARTIAL_TAKEN"
            events.append(
                {
                    "type": "PARTIAL_TAKEN",
                    "stock_no": sno,
                    "stock_name": mem.get("stock_name") or hold["stock_name"],
                    "position_id": pid,
                    "prev_qty": prev_peak,
                    "current_qty": cur_qty,
                    "tp2": _to_float(mem.get("initial_tp2"), 0.0),
                }
            )

        new_stop = compute_ratchet_trailing_stop(
            prev_trailing_stop=prev_stop,
            initial_sl=init_sl,
            avg_cost_price=avg_cost,
            current_price=cur_price,
            initial_tp1=tp1,
            ma17=ma17,
            status=status,
        )

        mem["stock_name"] = str(mem.get("stock_name") or hold["stock_name"]).strip()
        mem["status"] = status
        mem["peak_qty"] = new_peak
        mem["current_qty"] = cur_qty
        mem["avg_cost_price"] = round(avg_cost, 2)
        mem["trailing_stop"] = new_stop
        if hold.get("war_room_url"):
            mem["war_room_url"] = hold["war_room_url"]
        mem["updated_at"] = now_iso
        # Explicitly keep `user_note`, `initial_tp1`, `initial_tp2`, `initial_sl`, `entry_thesis` untouched!
        updated_memory.append(mem)

    # Check holdings that do NOT yet have an active memory card
    for sno, hold in agg_holdings.items():
        if sno in active_handled_stocks:
            continue
        wave = waves.get(sno, {})
        opened_str = str(wave.get("opened_date") or "").strip()
        if not opened_str:
            # If statements have no record at all for this holding, do not fabricate a recent buy date
            # unless there is a statement buy on/near today; legacy positions without buy history stay untracked.
            continue

        pid = f"{opened_str}_{sno}"
        if pid in known_position_ids:
            continue

        matched = match_entry_to_picks(
            sno,
            opened_str,
            ai_daily_picks=ai_picks,
            rebound_watchlist=reb_picks,
            public_cache=merged_pub_cache,
            avg_cost_price=_to_float(hold.get("avg_cost_price"), 0.0),
        )
        if matched is None:
            continue

        cur_qty = _to_int(hold["qty"])
        init_qty = max(_to_int(wave.get("initial_qty", cur_qty)), cur_qty)
        peak_qty = max(_to_int(wave.get("peak_qty", cur_qty)), cur_qty)
        avg_cost = _to_float(hold["avg_cost_price"], 0.0)
        cur_price = _to_float(hold["current_price"], avg_cost)
        ma17 = _to_float(hold.get("ma17"), cur_price)
        init_sl = _to_float(matched["initial_sl"], 0.0)
        tp1 = _to_float(matched["initial_tp1"], 0.0)
        tp2 = _to_float(matched["initial_tp2"], 0.0)

        init_trailing = compute_ratchet_trailing_stop(
            prev_trailing_stop=init_sl,
            initial_sl=init_sl,
            avg_cost_price=avg_cost,
            current_price=cur_price,
            initial_tp1=tp1,
            ma17=ma17,
            status="OPEN",
        )

        new_card = {
            "position_id": pid,
            "stock_no": sno,
            "stock_name": str(hold.get("stock_name") or matched.get("stock_name") or sno).strip(),
            "status": "OPEN",
            "entry_source": matched["entry_source"],
            "source_scan_date": matched["source_scan_date"],
            "source_rank": matched["source_rank"],
            "tactical_type": matched["tactical_type"],
            "opened_date": opened_str,
            "initial_qty": init_qty,
            "peak_qty": peak_qty,
            "current_qty": cur_qty,
            "avg_cost_price": round(avg_cost, 2),
            "initial_tp1": tp1,
            "initial_tp2": tp2,
            "initial_sl": init_sl,
            "trailing_stop": init_trailing,
            "entry_thesis": matched["entry_thesis"],
            "invalidation_rule": matched["invalidation_rule"],
            "war_room_url": str(hold.get("war_room_url") or matched.get("war_room_url") or "").strip(),
            "last_eval_date": "",
            "last_ai_action": "🟢 順勢續抱",
            "last_ai_summary": "",
            "closed_date": "",
            "realized_pnl": "",
            "user_note": "",
            "updated_at": now_iso,
            "Audit_Grade": matched.get("Audit_Grade", ""),
            "Execution_Tactic": matched.get("Execution_Tactic", ""),
            "Ideal_Buy_Or_Ref": matched.get("Ideal_Buy_Or_Ref", ""),
            "Chase_Limit_Price": matched.get("Chase_Limit_Price", ""),
            "Hard_Stop_Loss": matched.get("Hard_Stop_Loss", init_sl),
            "TP1_Target_Or_POC": matched.get("TP1_Target_Or_POC", tp1),
            "Deterministic_RR": matched.get("Deterministic_RR", ""),
            "Bear_Case_Warning": matched.get("Bear_Case_Warning", ""),
            "Invalidation_Triggers": matched.get("Invalidation_Triggers", ""),
        }
        updated_memory.append(new_card)
        known_position_ids.add(pid)
        events.append(
            {
                "type": "NEW_BOUND",
                "stock_no": sno,
                "stock_name": new_card["stock_name"],
                "position_id": pid,
                "entry_source": new_card["entry_source"],
                "source_scan_date": new_card["source_scan_date"],
                "source_rank": new_card["source_rank"],
                "avg_cost_price": new_card["avg_cost_price"],
                "qty": cur_qty,
                "initial_tp1": tp1,
                "initial_tp2": tp2,
                "initial_sl": init_sl,
            }
        )

    # Sort OPEN / TP1_PARTIAL_TAKEN first, then CLOSED by opened_date descending
    status_order = {"OPEN": 0, "TP1_PARTIAL_TAKEN": 1, "CLOSED": 2}
    updated_memory.sort(
        key=lambda m: (
            status_order.get(str(m.get("status", "OPEN")).upper(), 9),
            str(m.get("opened_date", "")),
            clean_leading_zero_code(m.get("stock_no", "")),
        )
    )

    projected_holdings = sync_holdings_with_memory_cards(holdings_rows, updated_memory)
    return updated_memory, projected_holdings, events


def format_memory_sync_discord_report(
    *,
    as_of: date,
    events: list[dict[str, Any]],
) -> list[str]:
    """Format concise notification when new memory cards are bound or positions change stage."""
    if not events:
        return []
    lines: list[str] = [f"🌙 【台新 盤後持股記憶歸因快訊】({as_of.isoformat()})"]
    for ev in events:
        etype = ev.get("type")
        sno = ev.get("stock_no")
        sname = ev.get("stock_name")
        if etype == "NEW_BOUND":
            lines.append(
                f"📌 **新建倉綁定**：`{sno} {sname}`（{ev['qty']:,} 股｜買入均價 {ev['avg_cost_price']} 元）\n"
                f"   • 命中來源：`{ev['entry_source']}` ({ev['source_scan_date']} #{ev['source_rank']})\n"
                f"   • 鎖定錨點：🎯 TP1 `{ev['initial_tp1']}` ｜ 🏆 TP2 `{ev['initial_tp2']}` ｜ 🛡️ 初始停損 `{ev['initial_sl']}`"
            )
        elif etype == "PARTIAL_TAKEN":
            lines.append(
                f"💰 **已完成分批停利**：`{sno} {sname}` 持股由 {ev['prev_qty']:,} 股減碼至 {ev['current_qty']:,} 股，"
                f"狀態推進至 `TP1_PARTIAL_TAKEN`，後續主目標鎖定 TP2 `{ev['tp2']}` 元！"
            )
        elif etype == "CLOSED":
            pnl = _to_int(ev.get("realized_pnl", 0))
            pnl_str = f"（波段已實現損益 {pnl:+,} 元）" if pnl != 0 else ""
            lines.append(f"🏁 **波段結案平倉**：`{sno} {sname}` 庫存已歸零結案{pnl_str}。")
    return _chunk_discord_messages(lines)


def parse_morning_battle_ai_response(raw_text: str) -> dict[str, Any]:
    """Extract and normalize JSON output from `TAISHIN_MORNING_BATTLE_PROMPT`."""
    text = (raw_text or "").strip()
    if not text:
        return {"morning_battle_headline": "", "positions": []}

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = m.group(1) if m else text
    if not m:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([\]\}])", r"\1", candidate)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return {"morning_battle_headline": "", "positions": []}

    if not isinstance(parsed, dict):
        return {"morning_battle_headline": "", "positions": []}

    pos_list = parsed.get("positions")
    if not isinstance(pos_list, list):
        pos_list = []

    norm_positions: list[dict[str, Any]] = []
    for item in pos_list:
        if not isinstance(item, dict):
            continue
        sno = clean_leading_zero_code(item.get("stock_no") or item.get("stock_id") or "")
        pid = clean_leading_zero_code(item.get("position_id") or "")
        if not sno and not pid:
            continue
        norm_positions.append(
            {
                "position_id": pid,
                "stock_no": sno,
                "ai_action_tag": normalize_ai_action_tag(item.get("ai_action_tag")),
                "recommended_trailing_stop": round(_to_float(item.get("recommended_trailing_stop"), 0.0), 2),
                "thesis_intact": bool(item.get("thesis_intact", True)),
                "order_instruction": str(item.get("order_instruction") or "").strip(),
                "pm_rationale": str(item.get("pm_rationale") or "").strip(),
            }
        )

    return {
        "morning_battle_headline": str(parsed.get("morning_battle_headline") or "").strip(),
        "positions": norm_positions,
    }


def build_tracked_position_gatekeeper_signals(
    mem: Mapping[str, Any],
    hold_agg: Mapping[str, Any],
    tech_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run deterministic Python checks (TP1/TP2 reach, stop breach, Style B share math, ratchet & left-side floor)."""
    qty = _to_int(hold_agg.get("qty", mem.get("current_qty", 0)))
    avg_cost = _to_float(hold_agg.get("avg_cost_price"), _to_float(mem.get("avg_cost_price"), 0.0))
    cur_price = _to_float(hold_agg.get("current_price"), avg_cost)
    ma17 = _to_float(hold_agg.get("ma17"), cur_price)
    ma45 = _to_float(hold_agg.get("ma45"), cur_price)
    ma117 = _to_float(hold_agg.get("ma117"), cur_price)
    ma189 = _to_float(hold_agg.get("ma189"), cur_price)
    tp1 = _to_float(mem.get("initial_tp1"), 0.0)
    tp2 = _to_float(mem.get("initial_tp2"), 0.0)
    init_sl = _to_float(mem.get("initial_sl"), 0.0)
    prev_stop = _to_float(mem.get("trailing_stop"), init_sl)
    status = str(mem.get("status", "OPEN")).strip().upper()

    t_info = tech_info or {}
    bias_ma17_pct = _to_float(
        t_info.get("bias_ma17_pct"),
        round(((cur_price - ma17) / ma17 * 100.0), 2) if ma17 > 0 else 0.0,
    )
    bias_ma189_pct = _to_float(
        t_info.get("bias_ma189_pct"),
        round(((cur_price - ma189) / ma189 * 100.0), 2) if ma189 > 0 else 0.0,
    )
    prev_high = _to_float(t_info.get("prev_high"), cur_price)
    prev_low = _to_float(t_info.get("prev_low"), cur_price)
    atr14 = _to_float(t_info.get("atr14"), round(cur_price * 0.025, 2))
    vol_poc_60d = _to_float(t_info.get("vol_poc_60d"), cur_price)
    rsi_14 = _to_float(t_info.get("rsi_14"), 50.0)
    daily_return_pr = _to_float(t_info.get("daily_return_pr"), 50.0)

    ratchet_floor = compute_ratchet_trailing_stop(
        prev_trailing_stop=prev_stop,
        initial_sl=init_sl,
        avg_cost_price=avg_cost,
        current_price=cur_price,
        initial_tp1=tp1,
        ma17=ma17,
        status=status,
    )
    trim_plan = compute_style_b_trim_plan(qty)

    # Calculate left-side structural support floor for deeply oversold / breakdown positions
    base_low = min(prev_low, cur_price) if prev_low > 0 else cur_price
    atr_buffer = atr14 * 0.8 if atr14 > 0 else cur_price * 0.02
    poc_candidate = vol_poc_60d if (0 < vol_poc_60d <= cur_price) else base_low
    left_side_structural_floor = round(
        max(cur_price * 0.90, min(base_low * 0.985, poc_candidate, base_low - atr_buffer * 0.5)),
        2,
    )
    if left_side_structural_floor <= 0 and cur_price > 0:
        left_side_structural_floor = round(cur_price * 0.96, 2)

    tp2_reached = tp2 > 0 and cur_price >= tp2 * 0.995
    tp1_reached = tp1 > 0 and cur_price >= tp1 * 0.99
    stop_breached = ratchet_floor > 0 and cur_price > 0 and cur_price <= ratchet_floor

    flags: list[str] = []
    if tp2_reached:
        flags.append(f"🏆【已達建倉第二目標價 TP2 ({tp2})】")
    elif tp1_reached and status == "OPEN":
        flags.append(f"🔔【已達或逼近建倉第一目標價 TP1 ({tp1}) — 待執行風格B賣半鎖利】")
    elif status == "TP1_PARTIAL_TAKEN":
        flags.append(f"✅【已完成 TP1 賣半鎖利，目前剩餘部位主攻 TP2 ({tp2})】")
    if stop_breached:
        flags.append(
            f"⚡【收盤價 {cur_price} 低於右側防守參考價 {ratchet_floor} "
            f"(17MA乖離 {bias_ma17_pct:+.1f}% / 189MA乖離 {bias_ma189_pct:+.1f}%) "
            f"➔ 請啟動瑤姐四維度審判：裁決屬『錯殺低接/價值防守』或『邏輯毀滅停損』】"
        )

    dist_tp1_pct = round(((tp1 - cur_price) / cur_price * 100.0), 2) if (tp1 > 0 and cur_price > 0) else 0.0
    dist_tp2_pct = round(((tp2 - cur_price) / cur_price * 100.0), 2) if (tp2 > 0 and cur_price > 0) else 0.0

    return {
        "qty": qty,
        "avg_cost_price": avg_cost,
        "current_price": cur_price,
        "return_pct": _to_float(hold_agg.get("return_pct"), 0.0),
        "unrealized_pnl": _to_int(hold_agg.get("unrealized_pnl"), 0),
        "weight_pct": _to_float(hold_agg.get("weight_pct"), 0.0),
        "ma17": ma17,
        "ma45": ma45,
        "ma117": ma117,
        "ma189": ma189,
        "bias_ma17_pct": bias_ma17_pct,
        "bias_ma189_pct": bias_ma189_pct,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "atr14": atr14,
        "vol_poc_60d": vol_poc_60d,
        "rsi_14": rsi_14,
        "daily_return_pr": daily_return_pr,
        "ratchet_stop_floor": ratchet_floor,
        "left_side_structural_floor": left_side_structural_floor,
        "trim_plan": trim_plan,
        "tp1_reached": tp1_reached,
        "tp2_reached": tp2_reached,
        "stop_breached": stop_breached,
        "dist_tp1_pct": dist_tp1_pct,
        "dist_tp2_pct": dist_tp2_pct,
        "flags": flags,
    }


async def run_morning_battle_ai_review(
    active_memories: list[dict[str, Any]],
    *,
    agg_holdings: Mapping[str, Mapping[str, Any]],
    public_cache: Mapping[str, Mapping[str, str]],
    market_regime: Mapping[str, Any],
    macro_context: Mapping[str, Any],
    eval_date: date,
    tech_map: Mapping[str, Mapping[str, Any]] | None = None,
    deep_fund_map: Mapping[str, Mapping[str, Any]] | None = None,
    fresh_top_picks_block: str = "",
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str, str]:
    """Call `VERTEX_STAGE2_MODEL` once across all active tracked memory cards with deep fundamental & chip dossier."""
    if not active_memories:
        return active_memories, {}, "", ""

    dossier_blocks: list[str] = []
    gatekeeper_by_pid: dict[str, dict[str, Any]] = {}

    for idx, mem in enumerate(active_memories, start=1):
        sno = clean_leading_zero_code(mem.get("stock_no", ""))
        pid = str(mem.get("position_id", f"{mem.get('opened_date')}_{sno}"))
        hold = agg_holdings.get(sno, {})
        t_info = (tech_map or {}).get(sno, {})
        gk = build_tracked_position_gatekeeper_signals(mem, hold, tech_info=t_info)
        gatekeeper_by_pid[pid] = gk

        pub = public_cache.get(sno, {})
        _, fund_verdict = _parse_stage1_trade_plan(pub.get("ai_summary_json", ""))
        fund_line = fund_verdict or "維持建倉時財報結論"
        flag_str = " ".join(gk["flags"]) if gk["flags"] else "🟢【劇本區間行進中】"
        user_note = str(mem.get("user_note") or "").strip()
        note_line = f"\n   - 投資人自訂備註：{user_note}" if user_note else ""

        # Assemble deep fundamental, valuation, inventory/DIO, and margin maintenance lines
        deep = (deep_fund_map or {}).get(sno, {})
        tax = deep.get("taxonomy") or {}
        inc = deep.get("income") or {}
        bal = deep.get("balance") or {}
        cf = deep.get("cashflow") or {}
        rev = deep.get("revenue") or {}
        val = deep.get("valuation") or {}
        chip = deep.get("chip") or {}

        tax_str = ""
        if tax.get("sector_group") or tax.get("sub_industry"):
            tax_str = (
                f"\n   - 產業定位與估值屬性：`{tax.get('sector_group', '')} > {tax.get('sub_industry', '')}`"
                f"（估值屬性：`{tax.get('valuation_regime', '標準估值')}`｜當日強弱同業PR=`PR{gk['daily_return_pr']:.0f}`）"
            )

        deep_lines: list[str] = []
        if inc:
            deep_lines.append(
                f"📊 最新季報 ({inc.get('period')})：單季 EPS=`{inc.get('eps')}` 元｜"
                f"毛利率=`{inc.get('gross_margin_pct')}%` (QoQ `{inc.get('gm_qoq_pp'):+.2f}`pp)｜"
                f"營益率=`{inc.get('operating_margin_pct')}%`"
            )
        if bal:
            inv_v = bal.get("inventory_yi")
            inv_str = f"`{inv_v}` 億元" if inv_v not in ("", None, "N/A") else "`N/A`"
            cl_v = bal.get("contract_liabilities_yi")
            cl_str = f"`{cl_v}` 億元" if cl_v not in ("", None, "N/A") else "`N/A`"
            dio_txt = f"｜DIO=`{bal.get('dio_days')}` 天" if bal.get("dio_days") not in ("", None, "N/A") else ""
            dso_txt = f"｜DSO=`{bal.get('dso_days')}` 天" if bal.get("dso_days") not in ("", None, "N/A") else ""
            deep_lines.append(
                f"📦 營運資金實測（核對失效紅線）：最新存貨={inv_str}｜"
                f"合約負債(在手訂單)={cl_str}{dio_txt}{dso_txt}"
            )
        if cf:
            ocf_v = cf.get("ocf_yi")
            ocf_str = f"`{ocf_v}` 億元" if ocf_v not in ("", None, "N/A") else "`N/A`"
            fcf_v = cf.get("fcf_yi")
            fcf_str = f"`{fcf_v}` 億元" if fcf_v not in ("", None, "N/A") else "`N/A`"
            deep_lines.append(
                f"💵 現金流含金量：最新單季 OCF={ocf_str}｜單季 FCF={fcf_str}"
            )
        if rev and rev.get("rev_ym"):
            mom_v = rev.get("mom_pct")
            yoy_v = rev.get("yoy_pct")
            cum_v = rev.get("cum_yoy_pct")
            mom_str = f"`{mom_v:+.2f}%`" if isinstance(mom_v, (int, float)) else "`N/A`"
            yoy_str = f"`{yoy_v:+.2f}%`" if isinstance(yoy_v, (int, float)) else "`N/A`"
            cum_txt = f"｜累計 YoY=`{cum_v:+.2f}%`" if isinstance(cum_v, (int, float)) else ""
            deep_lines.append(
                f"📈 最新月營收 ({rev.get('rev_ym')})：MoM={mom_str}｜YoY={yoy_str}{cum_txt}"
            )
        if val and any(val.get(k) not in ("", None) for k in ("pe", "pb", "dividend_yield")):
            dy_v = val.get("dividend_yield")
            dy_str = f"{dy_v}%" if dy_v not in ("", None, "N/A") else "N/A"
            deep_lines.append(
                f"⚖️ 估值與殖利率護城河：PE=`{val.get('pe') or 'N/A'}`｜PB=`{val.get('pb') or 'N/A'}`｜"
                f"近一年現金殖利率=`{dy_str}`"
            )
        if chip:
            maint_str = (
                f"`{chip.get('est_margin_maint_pct')}%`"
                if chip.get("est_margin_maint_pct") not in ("", None, "N/A")
                else "`N/A`"
            )
            quad_str = chip.get("chip_quadrant_tag") or "未分類"
            f5_v = chip.get("foreign_5d_lots")
            f5_str = f"`{f5_v}` 張" if f5_v not in ("", None, "N/A") else "`N/A`"
            it5_v = chip.get("trust_5d_lots")
            it5_str = f"`{it5_v}` 張" if it5_v not in ("", None, "N/A") else "`N/A`"
            m5_v = chip.get("margin_chg_5d_lots")
            m5_str = f"`{m5_v}` 張" if m5_v not in ("", None, "N/A") else "`N/A`"
            deep_lines.append(
                f"籌碼與融資維持率：外資近5日={f5_str}｜"
                f"投信近5日={it5_str}｜融資近5日增減={m5_str}｜"
                f"估算融資維持率={maint_str}｜資法人對作標籤=`{quad_str}`"
            )

        deep_block_str = ""
        if deep_lines:
            deep_block_str = "\n   - 最新深度財報、估值護城河與籌碼微結構實測（供穿透審判與核對失效紅線）：\n     * " + "\n     * ".join(deep_lines)

        weight_hint = "（輕倉具加碼彈性）" if gk["weight_pct"] < 10.0 else ("（重倉需控管風險）" if gk["weight_pct"] >= 25.0 else "")

        dossier_blocks.append(
            f"### {idx}. `{sno} {mem.get('stock_name', '')}`（position_id: `{pid}`｜狀態：`{mem.get('status')}`）\n"
            f"   - 系統狀態觀測旗標：{flag_str}\n"
            f"   - 持倉成本與部位：現有 `{gk['qty']:,}` 股（峰值 `{mem.get('peak_qty')}` 股）｜"
            f"買入均價 `{gk['avg_cost_price']}` 元｜昨收 `{gk['current_price']}` 元｜"
            f"未實現 `{gk['unrealized_pnl']:+,}` 元 (`{gk['return_pct']:+.2f}%`)｜**佔總持倉權重 `{gk['weight_pct']:.1f}%`{weight_hint}**"
            f"{tax_str}\n"
            f"   - 釘死建倉錨點（來源 `{mem.get('entry_source')}` {mem.get('source_scan_date')} #{mem.get('source_rank')}｜{mem.get('tactical_type')}）：\n"
            f"     * 初始 TP1：`{mem.get('initial_tp1')}` 元（距離 `{gk['dist_tp1_pct']:+.2f}%`）｜"
            f"初始 TP2：`{mem.get('initial_tp2')}` 元（距離 `{gk['dist_tp2_pct']:+.2f}%`）\n"
            f"     * 初始停損 SL：`{mem.get('initial_sl')}` 元 ｜ **目前右側棘輪防守參考 (`ratchet_stop_floor`)：`{gk['ratchet_stop_floor']}` 元** ｜ "
            f"**左側結構支撐參考 (`left_side_structural_floor`)：`{gk['left_side_structural_floor']}` 元**\n"
            f"     * 風格 B 預設減碼算盤：{gk['trim_plan']['instruction_hint']}\n"
            f"   - 當時買進劇本：{mem.get('entry_thesis')}\n"
            f"   - 當時設定失效紅線：{mem.get('invalidation_rule')}\n"
            f"   - 最新技術與微結構快取：17MA=`{gk['ma17']}` (乖離 `{gk['bias_ma17_pct']:+.1f}%`) / 45MA=`{gk['ma45']}` / "
            f"117MA=`{gk['ma117']}` / 189MA=`{gk['ma189']}` (乖離 `{gk['bias_ma189_pct']:+.1f}%`) ｜ "
            f"RSI(14)=`{gk['rsi_14']}` ｜ 昨高=`{gk['prev_high']}` / 昨低=`{gk['prev_low']}` ｜ "
            f"ATR(14)=`{gk['atr14']}` ｜ 60日大量POC=`{gk['vol_poc_60d']}` ｜ 財報結論：{fund_line}"
            f"{deep_block_str}"
            f"{note_line}"
        )

    resolved_picks_block = (
        fresh_top_picks_block.strip()
        if fresh_top_picks_block and fresh_top_picks_block.strip()
        else "- 暫無昨晚最新 AI 嚴選清單快取（以既有部位控管為主）。"
    )

    prompt = TAISHIN_MORNING_BATTLE_PROMPT.format(
        market_regime_summary=str(market_regime.get("summary") or market_regime.get("light") or "🟡黃燈"),
        macro_context_block=str(macro_context.get("prompt_block") or "無國際盤快取"),
        fresh_top_picks_block=resolved_picks_block,
        tracked_holdings_dossier="\n\n".join(dossier_blocks),
    )

    stage2_model = get_vertex_stage2_model()
    ai_resp = await ask_vertex_ai_with_meta(
        prompt,
        model=stage2_model,
        tag="taishin_morning_brief",
        task_type="MORNING_BRIEF",
        stock_id="PORTFOLIO",
        custom_id=f"MORNING_BRIEF_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        blocking_bq_log=True,
    )
    parsed = parse_morning_battle_ai_response(ai_resp.text)

    by_pid: dict[str, dict[str, Any]] = {}
    by_sno: dict[str, dict[str, Any]] = {}
    for item in parsed.get("positions") or []:
        if item.get("position_id"):
            by_pid[item["position_id"]] = item
        if item.get("stock_no"):
            by_sno[item["stock_no"]] = item

    decisions_by_sno: dict[str, dict[str, Any]] = {}
    enriched_memories: list[dict[str, Any]] = []
    now_iso = _now_iso_in_taipei()

    left_side_tags = {"🎯 逢低加碼", "🛡️ 破線轉價值防守"}

    for mem in active_memories:
        m = dict(mem)
        sno = clean_leading_zero_code(m.get("stock_no", ""))
        pid = str(m.get("position_id", f"{m.get('opened_date')}_{sno}"))
        gk = gatekeeper_by_pid[pid]
        floor = gk["ratchet_stop_floor"]
        left_floor = gk["left_side_structural_floor"]
        cur_px = gk["current_price"]
        matched_ai = by_pid.get(pid) or by_sno.get(sno)

        if matched_ai:
            tag = normalize_ai_action_tag(matched_ai.get("ai_action_tag"))
            thesis_intact = bool(matched_ai.get("thesis_intact", True))
            # Guardrail: if already took TP1 partial profit, do not repeat "分批停利" unless TP2 reached
            if m.get("status") == "TP1_PARTIAL_TAKEN" and tag == "💰 分批停利":
                tag = "🏆 達標收割" if gk["tp2_reached"] else "🟢 順勢續抱"

            rec_stop = _to_float(matched_ai.get("recommended_trailing_stop"), 0.0)

            # Dual-Track Stop Anchoring:
            # Track 1 (Left-Side Value / Dip-Buying on breakdown when thesis is intact):
            # Allow re-anchoring stop to left_side_structural_floor or AI's reasonable left-side support below current_price
            if gk["stop_breached"] and thesis_intact and tag in left_side_tags:
                if cur_px > 0 and (cur_px * 0.85) <= rec_stop <= (cur_px * 0.998):
                    final_stop = round(rec_stop, 2)
                else:
                    final_stop = left_floor if left_floor > 0 else floor
            else:
                # Track 2 (Standard Right-Side Ratchet Clamp: never allow below ratchet_stop_floor)
                if rec_stop < floor:
                    final_stop = floor
                elif cur_px > 0 and rec_stop >= cur_px and not gk["stop_breached"]:
                    final_stop = floor
                else:
                    final_stop = round(rec_stop, 2)

            order_inst = matched_ai.get("order_instruction") or ""
            rationale = matched_ai.get("pm_rationale") or ""
            combined_summary = f"{order_inst}｜{rationale}".strip("｜")
        else:
            # Deterministic fallback if AI omitted this position
            if gk["stop_breached"]:
                tag = "🚨 觸及停損"
                order_inst = f"現價跌破防守線 {floor} 元，建議嚴格執行停損出場。"
            elif gk["tp2_reached"]:
                tag = "🏆 達標收割"
                order_inst = f"達成原定第二目標價 TP2 ({m.get('initial_tp2')} 元)，建議獲利收割。"
            elif gk["tp1_reached"] and m.get("status") == "OPEN":
                tag = "💰 分批停利"
                order_inst = gk["trim_plan"]["instruction_hint"]
            else:
                tag = "🟢 順勢續抱"
                order_inst = f"劇本完好續抱，嚴守棘輪防守價 {floor} 元。"
            final_stop = floor
            rationale = "依建倉原定目標與棘輪防線規則執行。"
            combined_summary = f"{order_inst}｜{rationale}"

        m["trailing_stop"] = final_stop
        m["last_eval_date"] = eval_date.isoformat()
        m["last_ai_action"] = tag
        m["last_ai_summary"] = combined_summary[:220]
        m["updated_at"] = now_iso
        enriched_memories.append(m)

        decisions_by_sno[sno] = {
            "ai_action_tag": tag,
            "trailing_stop": final_stop,
            "order_instruction": order_inst,
            "pm_rationale": rationale,
            "gatekeeper": gk,
        }

    headline = parsed.get("morning_battle_headline") or "已完成建倉劇本、棘輪防線與隔夜國際盤交叉檢核。"
    return enriched_memories, decisions_by_sno, headline, ai_resp.model_version


def format_morning_brief_discord_report(
    *,
    as_of: date,
    holdings_rows: list[dict[str, Any]],
    memory_rows: list[dict[str, Any]],
    decisions_by_sno: Mapping[str, Mapping[str, Any]],
    market_regime: Mapping[str, Any],
    macro_context: Mapping[str, Any],
    battle_headline: str,
    model_version: str,
) -> list[str]:
    """Format Pre-Market Battle Plan report for Discord."""
    lines: list[str] = []
    tw_light = str(market_regime.get("light") or "🟡黃燈")
    macro_tone = str(macro_context.get("risk_tone") or "🟡 國際中性震盪")
    lines.append(f"☀️ 【台新證券 盤前開盤作戰晨報】({as_of.isoformat()})")
    if model_version:
        lines.append(f"🤖 決策模型：{model_version} ｜ 🚦 台股：{tw_light} ｜ 🌍 國際：{macro_tone}")
    else:
        lines.append(f"🚦 台股環境：{tw_light} ｜ 🌍 國際環境：{macro_tone}（無波段追蹤股，0 Token 快報模式）")

    # 1. Global macro compact snapshot
    compact_macro = str(macro_context.get("compact_line") or "").strip()
    if compact_macro:
        lines.append(f"🌎 隔夜美股與匯率：{compact_macro}")
    headlines = macro_context.get("headlines") or []
    if headlines:
        top_news = "；".join(h.get("title", "") for h in headlines[:2] if h.get("title"))
        if top_news:
            lines.append(f"📰 國際要聞：{top_news}")
    if battle_headline:
        lines.append(f"🧭 晨會總評：{battle_headline}")

    lines.append("─" * 28)

    agg = _aggregate_holdings_by_stock(holdings_rows)
    active_memories = [
        m for m in memory_rows if str(m.get("status", "")).upper() in ("OPEN", "TP1_PARTIAL_TAKEN")
    ]
    tracked_snos = {clean_leading_zero_code(m.get("stock_no", "")) for m in active_memories}

    total_cost = sum(_to_int(r.get("total_cost", 0)) for r in holdings_rows)
    total_mv = sum(_to_int(r.get("market_value", 0)) for r in holdings_rows)
    total_pnl = total_mv - total_cost
    total_ret = (total_pnl / total_cost * 100.0) if total_cost > 0 else 0.0
    
    # lines.append(
    #     f"📊 帳戶總市值：{total_mv:,} 元 ｜ 總成本：{total_cost:,} 元 ｜ "
    #     f"帳面總損益：{total_pnl:+,} 元 ({total_ret:+.2f}%)"
    # )

    # 3. Tracked Position Memory Battle Cards
    if active_memories:
        lines.append("")
        lines.append(f"🎯 **【嚴選波段記憶追蹤 — 09:00 開盤作戰指令】（共 {len(active_memories)} 檔）**")
        for mem in active_memories:
            sno = clean_leading_zero_code(mem.get("stock_no", ""))
            hold = agg.get(sno, {})
            dec = decisions_by_sno.get(sno, {})
            gk = dec.get("gatekeeper") or build_tracked_position_gatekeeper_signals(mem, hold)
            tag = dec.get("ai_action_tag") or mem.get("last_ai_action") or "🟢 順勢續抱"
            war_link = f" ｜ [戰情室]({mem['war_room_url']})" if mem.get("war_room_url") else ""
            stage_badge = "已賣半鎖利" if mem.get("status") == "TP1_PARTIAL_TAKEN" else "建倉持有中"

            lines.append(
                f"🔹 **{sno} {mem.get('stock_name', '')}**（{stage_badge}｜來源 {mem.get('source_scan_date')} #{mem.get('source_rank')}）"
                f" ➔ **{tag}**{war_link}"
            )
            lines.append(
                f"   • 部位：{gk['qty']:,} 股｜買入均價：`{gk['avg_cost_price']}` ｜ "
                f"昨收：`{gk['current_price']}` (`{gk['return_pct']:+.2f}%` / `{gk['unrealized_pnl']:+,}` 元)"
            )
            lines.append(
                f"   • 釘死目標：TP1 `{mem.get('initial_tp1')}` ({gk['dist_tp1_pct']:+.1f}%) / "
                f"TP2 `{mem.get('initial_tp2')}` ({gk['dist_tp2_pct']:+.1f}%) ｜ "
                f"🛡️ 棘輪防守價：`{mem.get('trailing_stop')}`（初始停損 {mem.get('initial_sl')}）"
            )
            order_inst = dec.get("order_instruction") or mem.get("last_ai_summary") or ""
            rationale = dec.get("pm_rationale") or ""
            if order_inst:
                lines.append(f"   • ⚡ **09:00 掛單指令**：{order_inst}")
            if rationale:
                lines.append(f"   • 📝 劇本檢核：{rationale}")
    else:
        lines.append("")
        lines.append("📭 目前無命中嚴選記憶卡之波段追蹤部位。")

    # 4. Untracked Holdings (0050 / legacy stocks -> pure PnL display, zero target nagging)
    untracked_rows = [
        r for r in holdings_rows if clean_leading_zero_code(r.get("stock_no", "")) not in tracked_snos
    ]
    if untracked_rows:
        lines.append("")
        lines.append(f"📦 **【長投與其他庫存帳面快照（不設定波段目標價）】（共 {len(untracked_rows)} 筆）**")
        for r in untracked_rows:
            sno = clean_leading_zero_code(r.get("stock_no", ""))
            lines.append(
                f"   • `{sno} {r['stock_name']}`（{r['order_type']}）：{r['qty']:,} 股 ｜ "
                f"均價 {r['avg_cost_price']} ➔ 現價 {r['current_price']} "
                f"({r['return_pct']:+.2f}% / {r['unrealized_pnl']:+,} 元｜權重 {r['weight_pct']:.1f}%)"
            )

    return _chunk_discord_messages(lines)


def _sync_taishin_core_accounting(
    *,
    client: TaishinClient,
    sheet_mgr: TaishinSheetManager | None,
    today: date,
    lookback_days: int,
    dry_run: bool,
    reports_dir: Path | None,
) -> dict[str, Any]:
    """Shared helper to fetch statements, fills, settlements, inventories & technicals."""
    start_dt = today - timedelta(days=lookback_days)
    now_iso = _now_iso_in_taipei()

    client.ensure_logged_in()
    account_no = client.get_account_no_str()

    raw_stmt = client.fetch_account_statement(start_dt, today)
    raw_fills = client.fetch_filled_history(start_dt, today)
    raw_fill_details = client.fetch_filled_history_detail(start_dt, today)
    raw_pnl = client.fetch_realized_profit_and_losses(start_dt, today)
    raw_hist_settle = client.fetch_history_settlement(start_dt, today)
    raw_today_settle = client.fetch_today_settlement()
    raw_inventories = client.fetch_inventories()

    stmt_rows = [
        normalize_statement_record(r, source_api="STATEMENT", default_account_no=account_no, synced_at=now_iso)
        for r in raw_stmt
    ]
    fill_rows = [
        normalize_statement_record(r, source_api="FILLED_HISTORY", default_account_no=account_no, synced_at=now_iso)
        for r in [*raw_fills, *raw_fill_details]
    ]
    recent_statements, _ = merge_statement_and_fills(stmt_rows, fill_rows)

    today_settle_rows = [
        normalize_settlement_or_pnl_record(
            r, record_type="TODAY_SETTLEMENT", synced_at=now_iso
        )
        for r in raw_today_settle
    ]
    hist_settle_rows = [
        normalize_settlement_or_pnl_record(
            r, record_type="HISTORY_SETTLEMENT", synced_at=now_iso
        )
        for r in raw_hist_settle
    ]
    pnl_rows = [
        normalize_settlement_or_pnl_record(
            r, record_type="REALIZED_PNL", synced_at=now_iso
        )
        for r in raw_pnl
    ]
    all_settlement_rows = [*today_settle_rows, *hist_settle_rows, *pnl_rows]

    existing_statements: list[dict[str, Any]] = []
    existing_settlements: list[dict[str, Any]] = []
    existing_memories: list[dict[str, Any]] = []
    added_statements = 0
    added_settlements = 0

    if sheet_mgr is not None and not dry_run:
        added_statements = sheet_mgr.append_deduplicated_rows(
            STATEMENT_SHEET_NAME,
            recent_statements,
            sort_key="trade_date",
        )
        added_settlements = sheet_mgr.append_deduplicated_rows(
            SETTLEMENT_SHEET_NAME,
            all_settlement_rows,
            sort_key="record_date",
        )
        existing_statements = sheet_mgr.read_records(STATEMENT_SHEET_NAME)
        existing_settlements = sheet_mgr.read_records(SETTLEMENT_SHEET_NAME)
        existing_memories = sheet_mgr.read_records(MEMORY_SHEET_NAME)
    elif sheet_mgr is not None:
        try:
            existing_statements = sheet_mgr.read_records(STATEMENT_SHEET_NAME)
            existing_settlements = sheet_mgr.read_records(SETTLEMENT_SHEET_NAME)
            existing_memories = sheet_mgr.read_records(MEMORY_SHEET_NAME)
        except Exception:  # noqa: BLE001
            pass

    # Deduplicate combined statements in memory
    seen_stmt_keys: set[str] = set()
    combined_statements: list[dict[str, Any]] = []
    for r in [*existing_statements, *recent_statements]:
        uk = get_unique_key_for_tab(STATEMENT_SHEET_NAME, r)
        if uk and uk not in seen_stmt_keys:
            seen_stmt_keys.add(uk)
            combined_statements.append(r)

    combined_settlements = [*existing_settlements, *all_settlement_rows]
    tech_map, market_regime = load_local_technical_context(reports_dir=reports_dir, as_of=today)
    holding_stock_ids = {
        clean_leading_zero_code(_pick_first(inv, "stock_no", "symbol", "stock_id", "code", default=""))
        for inv in raw_inventories
    } - {""}
    picks_bundle = load_public_picks_for_attribution(reports_dir=reports_dir)
    public_cache = dict(picks_bundle.get("watchlist_master") or {})
    missing_pub_ids = holding_stock_ids - set(public_cache.keys())
    if missing_pub_ids:
        public_cache.update(load_public_watchlist_cache(missing_pub_ids))

    holdings_rows = build_portfolio_holdings_rows(
        raw_inventories,
        statement_records=combined_statements,
        tech_map=tech_map,
        public_cache=public_cache,
        snapshot_date=today,
        updated_at=now_iso,
    )

    return {
        "now_iso": now_iso,
        "added_statements": added_statements,
        "added_settlements": added_settlements,
        "combined_statements": combined_statements,
        "combined_settlements": combined_settlements,
        "existing_memories": existing_memories,
        "holdings_rows": holdings_rows,
        "tech_map": tech_map,
        "market_regime": market_regime,
        "picks_bundle": picks_bundle,
        "public_cache": public_cache,
    }


def sync_memory(
    *,
    client: TaishinClient,
    sheet_mgr: TaishinSheetManager | None,
    as_of: date | None = None,
    lookback_days: int = 7,
    post_discord: bool = True,
    force_discord: bool = False,
    dry_run: bool = False,
    reports_dir: Path | None = None,
    picks_bundle_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute evening sync: match today's buys to recent AI picks & lock `05_Position_Thesis_Memory`."""
    today = as_of or _today_in_taipei()
    print(f"[sync-memory] 正在執行盤後成交對帳單同步與嚴選記憶歸因 (基準日: {today.isoformat()})...")

    core = _sync_taishin_core_accounting(
        client=client,
        sheet_mgr=sheet_mgr,
        today=today,
        lookback_days=lookback_days,
        dry_run=dry_run,
        reports_dir=reports_dir,
    )
    picks_bundle = picks_bundle_override if picks_bundle_override is not None else core["picks_bundle"]

    updated_memory, projected_holdings, events = reconcile_position_memory(
        existing_memory_rows=core["existing_memories"],
        holdings_rows=core["holdings_rows"],
        statement_records=core["combined_statements"],
        settlement_records=core["combined_settlements"],
        picks_bundle=picks_bundle,
        public_cache=core["public_cache"],
        as_of=today,
        updated_at=core["now_iso"],
    )

    if sheet_mgr is not None and not dry_run:
        sheet_mgr.overwrite_tab(MEMORY_SHEET_NAME, updated_memory)
        sheet_mgr.overwrite_tab(HOLDINGS_SHEET_NAME, projected_holdings)

    messages = format_memory_sync_discord_report(as_of=today, events=events)
    if force_discord and not messages:
        active_cnt = sum(
            1 for m in updated_memory if str(m.get("status", "")).upper() in ("OPEN", "TP1_PARTIAL_TAKEN")
        )
        messages = [
            f"🌙 【台新 盤後持股記憶歸因同步完成】({today.isoformat()})\n"
            f"• 今日無新綁定或平倉異動 ｜ 目前追蹤中波段記憶卡：{active_cnt} 檔。"
        ]

    if messages:
        print("\n\n".join(messages))
    else:
        print(f"[sync-memory] 同步完成（今日無新綁定或平倉事件，保持靜默不發推播）。")

    sent_discord = 0
    if post_discord and not dry_run and messages:
        sent_discord = post_to_taishin_discord(messages)

    return {
        "date": today.isoformat(),
        "login_count": client.login_count,
        "added_statements": core["added_statements"],
        "added_settlements": core["added_settlements"],
        "memory_rows": updated_memory,
        "holdings": projected_holdings,
        "events": events,
        "messages": messages,
        "sent_discord": sent_discord,
    }


def run_morning_brief(
    *,
    client: TaishinClient,
    sheet_mgr: TaishinSheetManager | None,
    as_of: date | None = None,
    lookback_days: int = 7,
    run_ai: bool = True,
    post_discord: bool = True,
    dry_run: bool = False,
    reports_dir: Path | None = None,
    macro_context_override: Mapping[str, Any] | None = None,
    picks_bundle_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute Pre-Market Battle Plan: US/Macro overlay + Style B AI review."""
    today = as_of or _today_in_taipei()
    print(f"[morning-brief] 正在執行盤前隔夜國際盤與持股記憶作戰晨報 (基準日: {today.isoformat()})...")

    core = _sync_taishin_core_accounting(
        client=client,
        sheet_mgr=sheet_mgr,
        today=today,
        lookback_days=lookback_days,
        dry_run=dry_run,
        reports_dir=reports_dir,
    )
    picks_bundle = picks_bundle_override if picks_bundle_override is not None else core["picks_bundle"]

    updated_memory, holdings_rows, _ = reconcile_position_memory(
        existing_memory_rows=core["existing_memories"],
        holdings_rows=core["holdings_rows"],
        statement_records=core["combined_statements"],
        settlement_records=core["combined_settlements"],
        picks_bundle=picks_bundle,
        public_cache=core["public_cache"],
        as_of=today,
        updated_at=core["now_iso"],
    )

    macro_context = (
        dict(macro_context_override)
        if macro_context_override is not None
        else fetch_global_macro_context()
    )

    active_memories = [
        m for m in updated_memory if str(m.get("status", "")).upper() in ("OPEN", "TP1_PARTIAL_TAKEN")
    ]
    closed_memories = [
        m for m in updated_memory if str(m.get("status", "")).upper() not in ("OPEN", "TP1_PARTIAL_TAKEN")
    ]
    agg_holdings = _aggregate_holdings_by_stock(holdings_rows)

    decisions_by_sno: dict[str, dict[str, Any]] = {}
    battle_headline = ""
    model_version = ""
    ai_called = False

    if run_ai and active_memories:
        print(f"[morning-brief] 偵測到 {len(active_memories)} 檔嚴選記憶追蹤股，載入深度財報/籌碼微結構並呼叫 VERTEX_STAGE2_MODEL...")
        try:
            tracked_sids = {clean_leading_zero_code(m.get("stock_no", "")) for m in active_memories} - {""}
            prices_by_sid = {
                sno: _to_float(hold.get("current_price"), 0.0)
                for sno, hold in agg_holdings.items()
                if _to_float(hold.get("current_price"), 0.0) > 0
            }
            deep_fund_map = load_public_deep_fundamental_context(tracked_sids, prices_by_sid=prices_by_sid)
            fresh_top_picks_block = build_fresh_top_picks_block(picks_bundle)
            reviewed_active, decisions_by_sno, battle_headline, model_version = asyncio.run(
                run_morning_battle_ai_review(
                    active_memories,
                    agg_holdings=agg_holdings,
                    public_cache=core["public_cache"],
                    market_regime=core["market_regime"],
                    macro_context=macro_context,
                    eval_date=today,
                    tech_map=core["tech_map"],
                    deep_fund_map=deep_fund_map,
                    fresh_top_picks_block=fresh_top_picks_block,
                )
            )
            ai_called = True
            updated_memory = [*reviewed_active, *closed_memories]
        except Exception as err:  # noqa: BLE001
            logger.warning("Pre-market battle Vertex Stage2 AI review failed; applying deterministic gatekeeper fallback: %s", err)
            battle_headline = f"AI 暫無法連線，已自動套用建倉目標價與棘輪防守硬規則（{err}）。"
    elif not active_memories:
        print("[morning-brief] 目前無命中嚴選記憶卡之未平倉波段股，啟動 0-Token 純 Python 快報模式。")

    final_holdings = sync_holdings_with_memory_cards(holdings_rows, updated_memory)

    if sheet_mgr is not None and not dry_run:
        sheet_mgr.overwrite_tab(MEMORY_SHEET_NAME, updated_memory)
        sheet_mgr.overwrite_tab(HOLDINGS_SHEET_NAME, final_holdings)

    messages = format_morning_brief_discord_report(
        as_of=today,
        holdings_rows=final_holdings,
        memory_rows=updated_memory,
        decisions_by_sno=decisions_by_sno,
        market_regime=core["market_regime"],
        macro_context=macro_context,
        battle_headline=battle_headline,
        model_version=model_version,
    )
    print("\n\n".join(messages))

    sent_discord = 0
    if post_discord and not dry_run:
        sent_discord = post_to_taishin_discord(messages)

    flush_bq_archives()

    return {
        "date": today.isoformat(),
        "ai_called": ai_called,
        "model_version": model_version,
        "active_tracked_count": len(active_memories),
        "memory_rows": updated_memory,
        "holdings": final_holdings,
        "macro_context": macro_context,
        "messages": messages,
        "sent_discord": sent_discord,
    }


def dump_portfolio_context(
    *,
    sheet_mgr: TaishinSheetManager | None = None,
    client: TaishinClient | None = None,
    refresh_live: bool = False,
    include_macro: bool = True,
    as_of: date | None = None,
    reports_dir: Path | None = None,
) -> dict[str, Any]:
    """Assemble read-only portfolio + thesis memory + technical + macro dossier for Antigravity Skill."""
    today = as_of or _today_in_taipei()
    mgr = sheet_mgr or TaishinSheetManager()

    holdings_rows = mgr.read_records(HOLDINGS_SHEET_NAME)
    memory_rows = mgr.read_records(MEMORY_SHEET_NAME)
    statement_rows = mgr.read_records(STATEMENT_SHEET_NAME)

    tech_map, market_regime = load_local_technical_context(reports_dir=reports_dir, as_of=today)

    if refresh_live:
        active_client = client or TaishinClient()
        active_client.ensure_logged_in()
        raw_inv = active_client.fetch_inventories()
        sids = {
            clean_leading_zero_code(_pick_first(inv, "stock_no", "symbol", "stock_id", "code", default=""))
            for inv in raw_inv
        } - {""}
        pub_cache = load_public_watchlist_cache(sids)
        holdings_rows = build_portfolio_holdings_rows(
            raw_inv,
            statement_records=statement_rows,
            tech_map=tech_map,
            public_cache=pub_cache,
            snapshot_date=today,
        )
        holdings_rows = sync_holdings_with_memory_cards(holdings_rows, memory_rows)

    holding_sids = {clean_leading_zero_code(r.get("stock_no", "")) for r in holdings_rows} - {""}
    public_cache = load_public_watchlist_cache(holding_sids)
    agg = _aggregate_holdings_by_stock(holdings_rows)

    active_cards: list[dict[str, Any]] = []
    for mem in memory_rows:
        if str(mem.get("status", "")).upper() not in ("OPEN", "TP1_PARTIAL_TAKEN"):
            continue
        sno = clean_leading_zero_code(mem.get("stock_no", ""))
        hold = agg.get(sno, {})
        gk = build_tracked_position_gatekeeper_signals(mem, hold)
        pub_plan, pub_verdict = _parse_stage1_trade_plan((public_cache.get(sno) or {}).get("ai_summary_json", ""))
        active_cards.append(
            {
                **mem,
                "live_metrics": gk,
                "latest_fundamental_verdict": pub_verdict,
                "latest_stage1_trade_plan": pub_plan,
            }
        )

    tracked_snos = {clean_leading_zero_code(c.get("stock_no", "")) for c in active_cards}
    untracked_holdings = [
        r for r in holdings_rows if clean_leading_zero_code(r.get("stock_no", "")) not in tracked_snos
    ]
    closed_cards = [
        m for m in memory_rows if str(m.get("status", "")).upper() == "CLOSED"
    ][-10:]

    macro_ctx = fetch_global_macro_context() if include_macro else {}

    total_cost = sum(_to_int(r.get("total_cost", 0)) for r in holdings_rows)
    total_mv = sum(_to_int(r.get("market_value", 0)) for r in holdings_rows)

    return {
        "as_of": today.isoformat(),
        "refresh_live": refresh_live,
        "account_summary": {
            "total_cost": total_cost,
            "total_market_value": total_mv,
            "total_unrealized_pnl": total_mv - total_cost,
            "total_return_pct": round(((total_mv - total_cost) / total_cost * 100.0), 2) if total_cost > 0 else 0.0,
            "tracked_positions_count": len(active_cards),
            "untracked_holdings_count": len(untracked_holdings),
        },
        "market_regime": market_regime,
        "macro_context": macro_ctx,
        "tracked_positions": active_cards,
        "untracked_holdings": untracked_holdings,
        "recent_closed_positions": closed_cards,
        "recent_trades": statement_rows[-15:],
    }


# --------------------------------------------------------------------------- #
# Section 5: CLI Entrypoint (`taishin_portfolio_service.py`)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# SASD v5.4.0 Phase 1：候選股 × 庫存曝險與跨產業相關係數耦合
# --------------------------------------------------------------------------- #
EXPOSURE_OPEN = "OPEN"
EXPOSURE_HIGH_CORR = "HIGH_CORR_WARNING"
EXPOSURE_SWAP_ONLY = "SECTOR_OR_CORR_FULL_SWAP_ONLY"
EXPOSURE_SECTOR_LIMIT = 2
EXPOSURE_SECTOR_WEIGHT_LIMIT_PCT = 35.0
EXPOSURE_CORR_WARN = 0.70
EXPOSURE_CORR_BLOCK = 0.80
EXPOSURE_MIN_OVERLAP_DAYS = 30
EXPOSURE_CORR_WINDOW = 60


@dataclass
class PortfolioExposureCheckResult:
    stock_id: str
    candidate_sector: str
    same_sector_holdings: list[str]
    max_corr_holding_id: str | None
    max_corr_value: float
    exposure_status: str  # OPEN / HIGH_CORR_WARNING / SECTOR_OR_CORR_FULL_SWAP_ONLY
    weakest_holding_to_swap: str | None


def _close_series(prices: Any) -> pd.Series:
    """將 Series 或含 date/close 的 DataFrame 統一成以日期為索引之收盤價序列。"""
    if isinstance(prices, pd.Series):
        series = prices.copy()
        series.index = pd.to_datetime(series.index, errors="coerce")
    else:
        frame = pd.DataFrame(prices)
        series = pd.Series(
            pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
            index=pd.to_datetime(frame["date"], errors="coerce"),
        )
    series = pd.to_numeric(series, errors="coerce")
    series = series[~series.index.isna()].dropna()
    return series[~series.index.duplicated(keep="last")].sort_index()


def compute_return_correlation(
    candidate_prices: Any,
    holding_prices: Any,
    *,
    window: int = EXPOSURE_CORR_WINDOW,
    min_overlap: int = EXPOSURE_MIN_OVERLAP_DAYS,
) -> float | None:
    """近 window 個交易日日報酬皮爾森相關係數；重疊交易日 < min_overlap 回傳 None。"""
    cand = _close_series(candidate_prices).tail(window + 1).pct_change().dropna()
    hold = _close_series(holding_prices).tail(window + 1).pct_change().dropna()
    overlap = cand.index.intersection(hold.index)
    if len(overlap) < min_overlap:
        return None
    value = cand.loc[overlap].corr(hold.loc[overlap], method="pearson")
    return None if pd.isna(value) else float(value)


def evaluate_candidate_exposure(
    stock_id: str,
    candidate_sector: str,
    holdings: list[Mapping[str, Any]],
    *,
    candidate_prices: Any,
    holding_prices: Mapping[str, Any],
    sector_limit: int = EXPOSURE_SECTOR_LIMIT,
) -> PortfolioExposureCheckResult:
    """比對候選股與台新庫存：同細分產業檔數 / 市值占比，以及 60 日報酬相關係數。

    holdings 每筆需含 stock_id（或 stock_no）、sector；選填 return_pct、weight_pct。
    - 同產業 >= sector_limit 檔（或同產業權重 > 35%）或 max_corr >= 0.80 → SECTOR_OR_CORR_FULL_SWAP_ONLY
    - 0.70 <= max_corr < 0.80 → HIGH_CORR_WARNING；其餘 OPEN
    """
    sid = str(stock_id).strip()
    sector = str(candidate_sector or "").strip()
    normalized: list[dict[str, Any]] = []
    for row in holdings:
        hid = str(row.get("stock_id") or row.get("stock_no") or "").strip()
        if not hid or hid == sid:
            continue
        normalized.append({**row, "stock_id": hid, "sector": str(row.get("sector") or "").strip()})

    same_sector = [h for h in normalized if sector and h["sector"] == sector]
    same_sector_ids = list(dict.fromkeys(h["stock_id"] for h in same_sector))
    sector_weight = sum(_to_float(h.get("weight_pct")) for h in same_sector)

    max_id: str | None = None
    max_corr = 0.0
    corr_by_id: dict[str, float] = {}
    for holding in normalized:
        hid = holding["stock_id"]
        if hid not in holding_prices:
            continue
        corr = compute_return_correlation(candidate_prices, holding_prices[hid])
        if corr is None:
            continue
        corr_by_id[hid] = corr
        if max_id is None or corr > max_corr:
            max_id, max_corr = hid, corr

    sector_full = len(same_sector_ids) >= sector_limit or sector_weight > EXPOSURE_SECTOR_WEIGHT_LIMIT_PCT
    if sector_full or (max_id is not None and max_corr >= EXPOSURE_CORR_BLOCK):
        status = EXPOSURE_SWAP_ONLY
    elif max_id is not None and max_corr >= EXPOSURE_CORR_WARN:
        status = EXPOSURE_HIGH_CORR
    else:
        status = EXPOSURE_OPEN

    weakest: str | None = None
    if status == EXPOSURE_SWAP_ONLY:
        pool = same_sector if same_sector else [h for h in normalized if h["stock_id"] == max_id]
        if pool:
            weakest = min(pool, key=lambda h: _to_float(h.get("return_pct"), 0.0))["stock_id"]

    return PortfolioExposureCheckResult(
        stock_id=sid,
        candidate_sector=sector,
        same_sector_holdings=same_sector_ids,
        max_corr_holding_id=max_id,
        max_corr_value=round(max_corr, 4) if max_id is not None else 0.0,
        exposure_status=status,
        weakest_holding_to_swap=weakest,
    )


def load_portfolio_exposure_inputs(
    daily_dir: Path,
    sector_resolver: Callable[[str], str] | None = None,
    *,
    sheet_mgr: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, pd.DataFrame]]:
    """讀取最新台新庫存快照與其本地日 K（prices.csv），供 evaluate_candidate_exposure 使用。

    任何環節失敗皆回傳空庫存（不阻斷選股流程）。回傳 (holdings, {stock_id: prices_df})。
    """
    try:
        mgr = sheet_mgr or TaishinSheetManager()
        records = mgr.read_records(HOLDINGS_SHEET_NAME)
    except Exception as error:  # noqa: BLE001
        logging.getLogger(__name__).warning("無法讀取台新庫存，略過庫存曝險檢查：%s", error)
        return [], {}
    if not records:
        return [], {}
    latest_snapshot = max(str(r.get("snapshot_date") or "") for r in records)
    holdings: list[dict[str, Any]] = []
    for rec in records:
        if str(rec.get("snapshot_date") or "") != latest_snapshot:
            continue
        hid = str(rec.get("stock_no") or "").strip()
        if not hid or _to_float(rec.get("qty")) <= 0:
            continue
        holdings.append(
            {
                "stock_id": hid,
                "sector": sector_resolver(hid) if sector_resolver else "",
                "return_pct": _to_float(rec.get("return_pct")),
                "weight_pct": _to_float(rec.get("weight_pct")),
            }
        )
    prices_by_id: dict[str, pd.DataFrame] = {}
    price_files = sorted(Path(daily_dir).glob("*_prices.csv"))
    if holdings and price_files:
        try:
            frame = pd.read_csv(price_files[-1], dtype={"stock_id": str})
            frame["stock_id"] = frame["stock_id"].astype(str).str.strip()
            for holding in holdings:
                sub = frame.loc[frame["stock_id"] == holding["stock_id"], ["date", "close"]]
                if not sub.empty:
                    prices_by_id[holding["stock_id"]] = sub
        except Exception as error:  # noqa: BLE001
            logging.getLogger(__name__).warning("讀取庫存日 K 失敗：%s", error)
    return holdings, prices_by_id


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="台新證券 Nova API 歷史對帳單回補、持倉建倉記憶追蹤、盤前作戰晨報與 0050 智慧定投服務 (SASD v5.2.0)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. inspect-sdk
    p_inspect = subparsers.add_parser("inspect-sdk", help="探測已安裝之 taishin_sdk 所有方法簽章與回傳結構")
    p_inspect.add_argument("--live", action="store_true", help="實際登入並印出各 API 原始回傳欄位結構")

    # 2. auth-init
    subparsers.add_parser(
        "auth-init",
        help="首次開通台新 API 認證 (執行 login + register_api_auth，收到開通通知 15 日內執行一次)",
    )

    # 3. backfill-history
    p_backfill = subparsers.add_parser(
        "backfill-history",
        help="分段撈取歷史所有對帳單、歷史成交與已實現損益並寫入個人 Google Sheet",
    )
    p_backfill.add_argument(
        "--from-date",
        default=DEFAULT_BACKFILL_FROM_DATE,
        help=f"起始日期 YYYY-MM-DD（預設 {DEFAULT_BACKFILL_FROM_DATE}）",
    )
    p_backfill.add_argument("--to-date", default=None, help="結束日期 YYYY-MM-DD（預設今日）")
    p_backfill.add_argument(
        "--chunk-days",
        type=int,
        default=DEFAULT_BACKFILL_CHUNK_DAYS,
        help=f"單次查詢區間天數（預設 {DEFAULT_BACKFILL_CHUNK_DAYS} 天）",
    )
    p_backfill.add_argument("--dry-run", action="store_true", help="僅預覽抓取筆數，不寫入 Google Sheet")

    # 4. sync-daily (legacy compatible)
    p_sync = subparsers.add_parser(
        "sync-daily",
        help="[相容舊版] 同步近期對帳單 + 最新庫存 + 單次持股健診",
    )
    p_sync.add_argument("--date", default=None, help="指定基準日期 YYYY-MM-DD（預設今日）")
    p_sync.add_argument("--dry-run", action="store_true", help="僅在終端機預覽，不寫入 Sheet 亦不發送 Discord")
    p_sync.add_argument("--no-ai", action="store_true", help="僅同步帳務與庫存至 Sheet，不跑 AI 診斷")
    p_sync.add_argument("--no-discord", action="store_true", help="僅寫入 Sheet，不發送 Discord 推播")

    # 5. sync-memory (Evening Attribution Sync)
    p_mem = subparsers.add_parser(
        "sync-memory",
        help="同步當日成交與庫存，自動回查近 5 交易日嚴選榜單建立/更新 05_Position_Thesis_Memory",
    )
    p_mem.add_argument("--date", default=None, help="指定基準日期 YYYY-MM-DD（預設今日）")
    p_mem.add_argument("--dry-run", action="store_true", help="僅預覽歸因結果，不寫入 Sheet 亦不發送 Discord")
    p_mem.add_argument("--no-discord", action="store_true", help="不發送 Discord 推播")
    p_mem.add_argument("--force-discord", action="store_true", help="即使今日無新綁定/平倉事件亦強制發送確認訊息")

    # 6. morning-brief (Pre-Market Battle Plan)
    p_morn = subparsers.add_parser(
        "morning-brief",
        help="整合隔夜美股國際盤 + VERTEX_STAGE2_MODEL 嚴選記憶持股開盤作戰建議",
    )
    p_morn.add_argument("--date", default=None, help="指定基準日期 YYYY-MM-DD（預設今日）")
    p_morn.add_argument("--dry-run", action="store_true", help="僅在終端機預覽，不寫入 Sheet 亦不發送 Discord")
    p_morn.add_argument("--no-ai", action="store_true", help="強制跳過 AI 呼叫，僅輸出量化規則與帳務美股快報")
    p_morn.add_argument("--no-discord", action="store_true", help="僅寫入 Sheet，不發送 Discord 推播")

    # 7. dump-context (Read-only context exporter for Antigravity portfolio-advisor Skill)
    p_dump = subparsers.add_parser(
        "dump-context",
        help="唯讀匯出完整持股均價、05_Position_Thesis_Memory 記憶卡、均線財報與國際盤 JSON（供 Antigravity Skill 使用）",
    )
    p_dump.add_argument("--date", default=None, help="指定基準日期 YYYY-MM-DD（預設今日）")
    p_dump.add_argument("--refresh-live", action="store_true", help="即時登入台新 API 拉取盤中最新庫存")
    p_dump.add_argument("--no-macro", action="store_true", help="略過抓取外部 Yahoo 國際指數與新聞")

    # 8. eval-dca-0050
    p_dca = subparsers.add_parser(
        "eval-dca-0050",
        help="0050 每週智慧尋底定投評估",
    )
    p_dca.add_argument("--date", default=None, help="指定評估日期 YYYY-MM-DD（預設今日）")
    p_dca.add_argument("--dry-run", action="store_true", help="僅預覽評估結果，不寫入 Sheet 亦不下單")
    p_dca.add_argument(
        "--confirm-live-order",
        action="store_true",
        help="確認執行真實盤中零股下單（需同時於 .env 開啟 TAISHIN_ENABLE_TRADING=true）",
    )
    p_dca.add_argument("--no-discord", action="store_true", help="不發送 Discord 推播")

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "inspect-sdk":
        info = inspect_sdk_capabilities(live=bool(args.live))
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "auth-init":
        client = TaishinClient()
        res = client.register_api_auth()
        print("[auth-init] 台新 API 首次開通認證完成：", json.dumps(res, ensure_ascii=False, default=str))
        return 0

    if args.command == "backfill-history":
        client = TaishinClient()
        sheet_mgr = None if args.dry_run else TaishinSheetManager()
        backfill_history(
            client=client,
            sheet_mgr=sheet_mgr,
            from_date=args.from_date,
            to_date=args.to_date,
            chunk_days=args.chunk_days,
            dry_run=bool(args.dry_run),
        )
        return 0

    if args.command == "sync-daily":
        client = TaishinClient()
        sheet_mgr = None if args.dry_run else TaishinSheetManager()
        target_dt = parse_flexible_date(args.date) if args.date else None
        sync_daily(
            client=client,
            sheet_mgr=sheet_mgr,
            as_of=target_dt,
            run_ai=not bool(args.no_ai),
            post_discord=not bool(args.no_discord),
            dry_run=bool(args.dry_run),
        )
        return 0

    def _make_sheet_mgr(dry: bool) -> TaishinSheetManager | None:
        if not dry:
            return TaishinSheetManager()
        try:
            return TaishinSheetManager()
        except TaishinPortfolioError:
            return None

    if args.command == "sync-memory":
        client = TaishinClient()
        sheet_mgr = _make_sheet_mgr(bool(args.dry_run))
        target_dt = parse_flexible_date(args.date) if args.date else None
        sync_memory(
            client=client,
            sheet_mgr=sheet_mgr,
            as_of=target_dt,
            post_discord=not bool(args.no_discord),
            force_discord=bool(args.force_discord),
            dry_run=bool(args.dry_run),
        )
        return 0

    if args.command == "morning-brief":
        client = TaishinClient()
        sheet_mgr = _make_sheet_mgr(bool(args.dry_run))
        target_dt = parse_flexible_date(args.date) if args.date else None
        run_morning_brief(
            client=client,
            sheet_mgr=sheet_mgr,
            as_of=target_dt,
            run_ai=not bool(args.no_ai),
            post_discord=not bool(args.no_discord),
            dry_run=bool(args.dry_run),
        )
        return 0

    if args.command == "dump-context":
        target_dt = parse_flexible_date(args.date) if args.date else None
        ctx = dump_portfolio_context(
            refresh_live=bool(args.refresh_live),
            include_macro=not bool(args.no_macro),
            as_of=target_dt,
        )
        print(json.dumps(ctx, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "eval-dca-0050":
        target_dt = parse_flexible_date(args.date) if args.date else None
        sheet_mgr = None if args.dry_run else TaishinSheetManager()
        eval_dca_0050(
            sheet_mgr=sheet_mgr,
            eval_date=target_dt,
            confirm_live_order=bool(args.confirm_live_order),
            post_discord=not bool(args.no_discord),
            dry_run=bool(args.dry_run),
        )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
