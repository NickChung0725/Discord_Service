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
from ai_service import ask_gemini_diagnosis_with_meta
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
    DCA_0050_SHEET_NAME,
    DCA_0050_SYMBOL,
    DCA_10D_DROP_TRIGGER_PCT,
    DCA_KDK_TRIGGER,
    DCA_MA17_NEG_BIAS_TRIGGER_PCT,
    DCA_MAX_MULTIPLIER,
    DCA_MIN_MULTIPLIER,
    DCA_RSI14_TRIGGER,
    DEFAULT_BACKFILL_CHUNK_DAYS,
    DEFAULT_BACKFILL_FROM_DATE,
    DEFAULT_DCA_0050_WEEKLY_BUDGET_TWD,
    GSHEET_APPEND_BATCH_SIZE,
    GSHEET_RETRY_DELAYS_SECONDS,
    HOLDINGS_SHEET_NAME,
    LEADING_ZERO_PROTECTED_FIELDS,
    SETTLEMENT_SHEET_NAME,
    STANDARD_ETF_TAX_RATE,
    STANDARD_FEE_RATE,
    STANDARD_STOCK_TAX_RATE,
    STATEMENT_SHEET_NAME,
    TAISHIN_PORTFOLIO_AI_PROMPT,
    TAISHIN_SHEET_SCHEMAS,
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
            first_row = self._gspread_call(ws.row_values, 1)
            if not first_row:
                self._gspread_call(
                    ws.update,
                    range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(headers))}",
                    values=[headers],
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
    bank_balance: float | None = None,
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

    bal_val = bank_balance if bank_balance is not None else (
        _to_float(raw["bank_balance"]) if raw.get("bank_balance") not in (None, "") else None
    )
    shortfall_alert = "✅正常"
    if net_amount < 0 and bal_val is not None and (bal_val + net_amount) < 0:
        shortfall_alert = "⚠️餘額不足"

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
        "bank_balance": int(round(bal_val)) if bal_val is not None else "",
        "shortfall_alert": shortfall_alert,
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
# Section 4.2: Daily 11:00 Accounting Sync & AI Holdings Diagnosis (`sync-daily`)
# --------------------------------------------------------------------------- #
def load_local_technical_context(
    reports_dir: Path | None = None,
    as_of: date | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load per-stock MA3/17/45/117/189 and market regime from `finmind_reports/daily`."""
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
                for sid, group in df.groupby("stock_id"):
                    g = group.sort_values("date").dropna(subset=["close"])
                    if g.empty:
                        continue
                    closes = g["close"]
                    latest_close = float(closes.iloc[-1])
                    tech_map[str(sid)] = {
                        "close": round(latest_close, 2),
                        "ma3": round(float(closes.tail(3).mean()), 2),
                        "ma17": round(float(closes.tail(17).mean()), 2),
                        "ma45": round(float(closes.tail(45).mean()), 2),
                        "ma117": round(float(closes.tail(117).mean()), 2),
                        "ma189": round(float(closes.tail(189).mean()), 2),
                    }
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
            f"均線：17MA={h['ma17']} / 45MA={h['ma45']} / 117MA={h['ma117']}｜"
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

    ai_resp = await ask_gemini_diagnosis_with_meta(prompt, tag="taishin_portfolio_diagnosis")
    parsed = parse_portfolio_ai_response(ai_resp.text)
    enriched_rows = apply_ai_diagnosis_to_holdings(holdings_rows, parsed)
    overview = parsed.get("portfolio_overview") or "已完成持股成本安全墊與均線位階診斷。"
    return enriched_rows, overview, ai_resp.model_version


def format_daily_sync_discord_report(
    *,
    as_of: date,
    holdings_rows: list[dict[str, Any]],
    today_settlements: list[dict[str, Any]],
    bank_balance: float | None,
    added_statements: int,
    added_settlements: int,
    portfolio_overview: str,
    model_version: str,
    market_regime: Mapping[str, Any],
) -> list[str]:
    """Format the 11:00 personal accounting & AI holdings diagnosis Discord report."""
    lines: list[str] = []
    light = str(market_regime.get("light") or "🟡黃燈")
    lines.append(f"🏦 【台新證券 11:00 個人帳務同步與 AI 持股健診】({as_of.isoformat()})")
    if model_version:
        lines.append(f"🤖 分析模型：{model_version} ｜ 🚦 大盤環境：{light}")
    else:
        lines.append(f"🚦 大盤環境：{light}")

    bal_str = f"{int(round(bank_balance)):,} 元" if bank_balance is not None else "未提供"
    today_net = sum(_to_int(s.get("net_amount", 0)) for s in today_settlements)
    has_shortfall = any(s.get("shortfall_alert") == "⚠️餘額不足" for s in today_settlements)
    alert_badge = "⚠️ 交割款餘額不足預警！" if has_shortfall else "✅ 交割資金正常"
    lines.append(
        f"💰 分戶帳餘額：{bal_str} ｜ 今日應收付交割款：{today_net:+,} 元（{alert_badge}）"
    )
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
                f"   • 均線：17MA={r['ma17']} / 45MA={r['ma45']} / 117MA={r['ma117']} ｜ "
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
    """Execute the daily 11:00 accounting sync, holdings snapshot, and AI cost-basis diagnosis."""
    today = as_of or _today_in_taipei()
    start_dt = today - timedelta(days=lookback_days)
    now_iso = _now_iso_in_taipei()

    print(f"[sync-daily] 正在登入台新證券 Nova API 並同步帳務 (基準日: {today.isoformat()})...")
    client.ensure_logged_in()
    account_no = client.get_account_no_str()

    # 1. Sync recent 7 days of statements, fills, settlements, bank balance & realized PnL
    print(f"[sync-daily] 抓取近 {lookback_days} 天對帳單、成交明細與交割紀錄...")
    raw_stmt = client.fetch_account_statement(start_dt, today)
    raw_fills = client.fetch_filled_history(start_dt, today)
    raw_fill_details = client.fetch_filled_history_detail(start_dt, today)
    raw_pnl = client.fetch_realized_profit_and_losses(start_dt, today)
    raw_hist_settle = client.fetch_history_settlement(start_dt, today)
    raw_today_settle = client.fetch_today_settlement()
    bank_balance = client.fetch_bank_balance()

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
            r, record_type="TODAY_SETTLEMENT", bank_balance=bank_balance, synced_at=now_iso
        )
        for r in raw_today_settle
    ]
    hist_settle_rows = [
        normalize_settlement_or_pnl_record(
            r, record_type="HISTORY_SETTLEMENT", bank_balance=bank_balance, synced_at=now_iso
        )
        for r in raw_hist_settle
    ]
    pnl_rows = [
        normalize_settlement_or_pnl_record(
            r, record_type="REALIZED_PNL", bank_balance=bank_balance, synced_at=now_iso
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
        today_settlements=today_settle_rows,
        bank_balance=bank_balance,
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

    return {
        "date": today.isoformat(),
        "login_count": client.login_count,
        "added_statements": added_statements,
        "added_settlements": added_settlements,
        "holdings_count": len(holdings_rows),
        "holdings": holdings_rows,
        "bank_balance": bank_balance,
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
            trigger_reason = "📅 週五 13:15 保底定投（週一至週四未觸發低點）"
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
    """Run the 13:15 0050 Smart Weekly DCA evaluation and optional intraday-odd order placement."""
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

    decision = evaluate_0050_dip_decision(
        metrics,
        eval_date=today,
        already_bought_this_week=already_bought,
        weekly_budget_twd=budget,
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
# Section 5: CLI Entrypoint (`taishin_portfolio_service.py`)
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="台新證券 Nova API 歷史對帳單回補、帳務同步與 AI 持股健診服務 (SASD v5.1.0)"
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

    # 4. sync-daily
    p_sync = subparsers.add_parser(
        "sync-daily",
        help="每日 11:00 同步近期對帳單 + 最新庫存 + 交割款預警 + AI 持股均價與操作建議",
    )
    p_sync.add_argument("--date", default=None, help="指定基準日期 YYYY-MM-DD（預設今日）")
    p_sync.add_argument("--dry-run", action="store_true", help="僅在終端機預覽，不寫入 Sheet 亦不發送 Discord")
    p_sync.add_argument("--no-ai", action="store_true", help="僅同步帳務與庫存至 Sheet，不跑 AI 診斷")
    p_sync.add_argument("--no-discord", action="store_true", help="僅寫入 Sheet，不發送 Discord 推播")

    # 5. eval-dca-0050
    p_dca = subparsers.add_parser(
        "eval-dca-0050",
        help="週一至週五 13:15 0050 每週不定期定額低點評估",
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
