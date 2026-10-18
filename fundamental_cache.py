"""Google Sheets incremental cache manager for Taiwan fundamentals.

Implements the core rules from SASD v3.1.0:
- Double-track watermark checks (monthly revenue vs quarterly statements)
- Incremental fetch using start_date = max_raw_date + 1 day
- In-memory stitching before computing derived metrics
- In-memory worksheet caching + 429 exponential backoff to eliminate Read Quota storms
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import logging
import os
import re
import time
from typing import Any, Mapping

import gspread
from gspread.exceptions import APIError
import pandas as pd
from FinMind.data import DataLoader

from fundamental_config import (
    ACCOUNT_ANCHOR_MAP,
    BALANCE_TOLERANCE_ABS,
    BALANCE_TOLERANCE_RATIO,
    SHEET_SCHEMAS,
    get_expected_watermarks,
)

logger = logging.getLogger(__name__)

TAIPEI_TIMEZONE = timezone(timedelta(hours=8))


@dataclass
class SyncResult:
    stock_id: str
    rev_updated: bool
    fin_updated: bool
    div_updated: bool
    chip_updated: bool
    max_rev_raw_date: str | None
    last_rev_ym: str | None
    max_fin_raw_date: str | None
    last_fin_qtr: str | None
    max_div_raw_date: str | None
    max_chip_raw_date: str | None
    warnings: list[str]


class FundamentalCacheError(RuntimeError):
    pass


class BalanceMismatchError(FundamentalCacheError):
    pass


def _today_in_taipei() -> date:
    return datetime.now(TAIPEI_TIMEZONE).date()


def _now_in_taipei() -> datetime:
    return datetime.now(TAIPEI_TIMEZONE)


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _rev_ym_from_raw_date(raw_date: str) -> str:
    parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
    year = parsed.year
    month = parsed.month - 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


def _qtr_from_raw_date(raw_date: str) -> str:
    parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
    if (parsed.month, parsed.day) == (3, 31):
        q = 1
    elif (parsed.month, parsed.day) == (6, 30):
        q = 2
    elif (parsed.month, parsed.day) == (9, 30):
        q = 3
    else:
        q = 4
    return f"{parsed.year:04d}Q{q}"


def _expected_rev_raw_date(today: date) -> date:
    expected_rev_ym, _ = get_expected_watermarks(today)
    year_str, month_str = expected_rev_ym.split("-")
    year = int(year_str)
    month = int(month_str)
    if month == 12:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def _expected_fin_raw_date(today: date) -> date:
    _, expected_fin_qtr = get_expected_watermarks(today)
    year = int(expected_fin_qtr[:4])
    q = int(expected_fin_qtr[-1])
    if q == 1:
        return date(year, 3, 31)
    if q == 2:
        return date(year, 6, 30)
    if q == 3:
        return date(year, 9, 30)
    return date(year, 12, 31)


def _normalize_div_year(raw_year: Any, raw_date: Any) -> str:
    """將 FinMind 股利表的民國年（如 '114年'、'113'）統一轉為西元年字串（如 '2025'）。"""
    text = str(raw_year or "").strip()
    m = re.search(r"(\d+)", text)
    if m:
        num = int(m.group(1))
        if num < 1911:
            return str(num + 1911)
        return str(num)
    date_str = str(raw_date or "").strip()
    if len(date_str) >= 4 and date_str[:4].isdigit():
        return str(int(date_str[:4]) - 1)
    return text


def _anchor_pick(
    pivot_type: pd.DataFrame,
    anchor_spec: list[str] | Mapping[str, list[str]],
    pivot_origin: pd.DataFrame | None = None,
) -> pd.Series:
    if isinstance(anchor_spec, Mapping):
        type_aliases = list(anchor_spec.get("types", []))
        origin_aliases = list(anchor_spec.get("origins", []))
    else:
        type_aliases = list(anchor_spec)
        origin_aliases = list(anchor_spec)

    result = pd.Series(float("nan"), index=pivot_type.index, dtype="float64")

    for alias in type_aliases:
        if alias in pivot_type.columns:
            col_series = pd.to_numeric(pivot_type[alias], errors="coerce")
            result = result.combine_first(col_series)

    if pivot_origin is not None:
        for alias in origin_aliases:
            if alias in pivot_origin.columns:
                col_series = pd.to_numeric(pivot_origin[alias], errors="coerce")
                result = result.combine_first(col_series)

    if result.isna().all() and pivot_origin is not None:
        for alias in origin_aliases:
            clean_alias = alias.replace("（", "(").replace("）", ")")
            for col in pivot_origin.columns:
                clean_col = str(col).replace("（", "(").replace("）", ")").strip()
                if clean_alias == clean_col or (len(clean_alias) >= 4 and clean_alias in clean_col):
                    col_series = pd.to_numeric(pivot_origin[col], errors="coerce")
                    result = result.combine_first(col_series)
                    break

    return result


def _sheet_cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if pd.isna(value):
        return ""
    return value


def validate_balance_sheet_rows(df_delta: pd.DataFrame) -> None:
    if df_delta.empty:
        return
    required = {"total_assets", "total_liabilities", "total_equity"}
    if not required.issubset(df_delta.columns):
        return

    assets = pd.to_numeric(df_delta["total_assets"], errors="coerce")
    liabilities = pd.to_numeric(df_delta["total_liabilities"], errors="coerce")
    equity = pd.to_numeric(df_delta["total_equity"], errors="coerce")

    diff_abs = (assets - (liabilities + equity)).abs()
    diff_ratio = diff_abs / assets.replace({0: pd.NA})

    failed = (diff_abs > BALANCE_TOLERANCE_ABS) & (diff_ratio > BALANCE_TOLERANCE_RATIO)
    if failed.fillna(False).any():
        raise BalanceMismatchError("資產負債不平衡，拒絕寫入 Google Sheets。")


def validate_cash_flow_rows(df_delta: pd.DataFrame, *, tolerance_abs: float = 1e-6) -> None:
    if df_delta.empty:
        return
    required = {"operating_cf", "capex", "free_cf"}
    if not required.issubset(df_delta.columns):
        return

    operating_cf = pd.to_numeric(df_delta["operating_cf"], errors="coerce")
    capex = pd.to_numeric(df_delta["capex"], errors="coerce")
    free_cf = pd.to_numeric(df_delta["free_cf"], errors="coerce")

    expected = operating_cf - capex.abs()
    diff_abs = (free_cf - expected).abs()
    failed = diff_abs > tolerance_abs
    if failed.fillna(False).any():
        raise FundamentalCacheError("自由現金流計算不一致，拒絕寫入 Google Sheets。")


class FundamentalCacheManager:
    def __init__(self) -> None:
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        spreadsheet_id = os.getenv("GSHEET_SPREADSHEET_ID", "").strip()
        if not credentials_path:
            raise FundamentalCacheError("GOOGLE_APPLICATION_CREDENTIALS is not configured.")
        if not spreadsheet_id:
            raise FundamentalCacheError("GSHEET_SPREADSHEET_ID is not configured.")

        self.credentials_path = credentials_path
        self.spreadsheet_id = spreadsheet_id

        cooldown_hours = os.getenv("FINMIND_PROBE_COOLDOWN_HOURS", "6").strip() or "6"
        try:
            self.cooldown_hours = int(cooldown_hours)
        except ValueError as error:
            raise FundamentalCacheError("FINMIND_PROBE_COOLDOWN_HOURS must be an integer") from error

        self._gc = gspread.service_account(filename=self.credentials_path)
        self._spreadsheet = self._gc.open_by_key(self.spreadsheet_id)

        self._finmind = DataLoader()
        finmind_token = os.getenv("FINMIND_API_TOKEN", "").strip()
        if finmind_token:
            self._finmind.login_by_token(api_token=finmind_token)

        self._stock_info_cache: dict[str, tuple[str, str]] = {}

        # Google Sheets 記憶體快取（避免單次指令重複觸發 60 次 Read Quota）
        self._schema_ensured: bool = False
        self._worksheet_objs: dict[str, gspread.Worksheet] = {}
        self._records_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._headers_cache: dict[str, list[str]] = {}
        self._cache_ttl: float = 90.0
        self.batch_mode: bool = False

    def _gspread_call(self, func, *args, **kwargs):
        """包裝所有 gspread API 呼叫，遇到 429 / 5xx 自動指數退避重試。"""
        max_retries = 4
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except APIError as error:
                status = getattr(getattr(error, "response", None), "status_code", None)
                if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    wait_sec = (2 ** attempt) * 2.0 + 1.0
                    logger.warning(
                        "Google Sheets API %s rate-limited, retrying in %.1fs (%d/%d)...",
                        status,
                        wait_sec,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(wait_sec)
                else:
                    raise

    def _get_worksheet(self, tab_name: str) -> gspread.Worksheet:
        if tab_name not in self._worksheet_objs:
            self._worksheet_objs[tab_name] = self._gspread_call(self._spreadsheet.worksheet, tab_name)
        return self._worksheet_objs[tab_name]

    def _get_records_cached(self, worksheet: gspread.Worksheet, *, force: bool = False) -> list[dict[str, Any]]:
        title = worksheet.title
        now_ts = time.time()
        if not force and title in self._records_cache:
            ts, records = self._records_cache[title]
            if now_ts - ts < self._cache_ttl:
                return records

        # 改用 get_all_values() 取得所有矩陣資料，避免 gspread get_all_records() 因尾隨空欄位崩潰。
        # 同時使用 UNFORMATTED_VALUE 讀取，避免像 revenue 這類數字被千分位逗號格式化後
        # 讓 pandas.to_numeric(..., errors="coerce") 轉成 NaN，進而在 upsert 時把原值覆蓋成空白。
        rows = self._gspread_call(
            worksheet.get_all_values,
            value_render_option=gspread.utils.ValueRenderOption.unformatted,
            date_time_render_option=gspread.utils.DateTimeOption.formatted_string,
        )
        if not rows or len(rows) < 1:
            return []

        raw_headers = rows[0]
        headers = [str(h).strip() for h in raw_headers]
        
        records = []
        for row in rows[1:]:
            # 補齊欄位長度以防部分列較短
            padded = row + [""] * (len(headers) - len(row))
            record = {headers[i]: padded[i] for i in range(len(headers)) if headers[i]}
            if any(record.values()):  # 過濾完全空白的行
                records.append(record)

        self._records_cache[title] = (now_ts, records)
        return records

    def _invalidate_cache(self, tab_name: str | None = None) -> None:
        if tab_name:
            self._records_cache.pop(tab_name, None)
        else:
            self._records_cache.clear()

    def ensure_workbook_schema(self) -> None:
        if self._schema_ensured:
            return

        worksheets = self._gspread_call(self._spreadsheet.worksheets)
        existing_tabs = {ws.title: ws for ws in worksheets}
        self._worksheet_objs.update(existing_tabs)

        for tab_name, headers in SHEET_SCHEMAS.items():
            if tab_name not in existing_tabs:
                ws = self._gspread_call(
                    self._spreadsheet.add_worksheet,
                    title=tab_name,
                    rows=1000,
                    cols=len(headers),
                )
                self._gspread_call(ws.append_row, headers)
                self._worksheet_objs[tab_name] = ws
                self._headers_cache[tab_name] = list(headers)
                continue

            ws = existing_tabs[tab_name]
            first_row = self._gspread_call(ws.row_values, 1)
            if not first_row:
                if ws.col_count < len(headers):
                    self._gspread_call(ws.resize, cols=len(headers))
                self._gspread_call(
                    ws.update,
                    range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(headers))}",
                    values=[headers],
                )
                self._headers_cache[tab_name] = list(headers)
                continue

            if len(first_row) < len(headers) or ws.col_count < len(headers):
                self._gspread_call(ws.resize, cols=max(len(headers), len(first_row)))

            missing = [h for h in headers if h not in first_row]
            if missing:
                merged_headers = first_row + missing
                if ws.col_count < len(merged_headers):
                    self._gspread_call(ws.resize, cols=len(merged_headers))
                self._gspread_call(
                    ws.update,
                    range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(merged_headers))}",
                    values=[merged_headers],
                )
                self._headers_cache[tab_name] = merged_headers
            else:
                self._headers_cache[tab_name] = first_row

        self._schema_ensured = True

    def check_and_sync_stock(self, stock_id: str, force_refresh: bool = False) -> SyncResult:
        normalized_id = stock_id.strip()
        if not normalized_id.isdigit() or not (4 <= len(normalized_id) <= 6):
            raise FundamentalCacheError("股票代號必須是 4 至 6 位數字。")

        if force_refresh:
            self._schema_ensured = False
            self._invalidate_cache()

        self.ensure_workbook_schema()

        now = _now_in_taipei()
        today = now.date()
        expected_rev_date = _expected_rev_raw_date(today)
        expected_fin_date = _expected_fin_raw_date(today)

        watchlist = self._get_worksheet("00_Watchlist_Master")
        watch_row = self._get_or_create_watch_row(watchlist, normalized_id)

        stock_name = str(watch_row.get("stock_name", "")).strip()
        industry_category = str(watch_row.get("industry_category", "")).strip()
        if not stock_name or not industry_category:
            fetched_name, fetched_industry = self._get_stock_basic_info(normalized_id)
            stock_name = stock_name or fetched_name
            industry_category = industry_category or fetched_industry

        cooldown_active = False
        last_probe_at = watch_row.get("last_probe_at")
        if not force_refresh and last_probe_at:
            try:
                last_probe_dt = datetime.fromisoformat(last_probe_at)
            except ValueError:
                last_probe_dt = None
            if last_probe_dt is not None and now - last_probe_dt < timedelta(hours=self.cooldown_hours):
                cooldown_active = True

        warnings: list[str] = []

        rev_sheet = self._get_worksheet("01_Monthly_Revenue")
        df_rev_sheet = self._read_stock_rows(rev_sheet, normalized_id)
        max_rev_raw = _parse_iso_date(
            str(df_rev_sheet["raw_date"].max()) if not df_rev_sheet.empty else None
        )

        fin_is_sheet = self._get_worksheet("02_Income_Statement")
        fin_bs_sheet = self._get_worksheet("03_Balance_Sheet")
        fin_cf_sheet = self._get_worksheet("04_Cash_Flow")
        fin_wide_sheet = self._get_worksheet("05_AI_Metrics_Wide")

        df_is_sheet = self._read_stock_rows(fin_is_sheet, normalized_id)
        df_bs_sheet = self._read_stock_rows(fin_bs_sheet, normalized_id)
        df_cf_sheet = self._read_stock_rows(fin_cf_sheet, normalized_id)
        df_wide_sheet = self._read_stock_rows(fin_wide_sheet, normalized_id)

        max_fin_raw = _parse_iso_date(
            str(df_wide_sheet["raw_date"].max()) if not df_wide_sheet.empty else None
        )

        div_sheet = self._get_worksheet("06_Dividend_Policy")
        chip_sheet = self._get_worksheet("07_Chip_Summary")
        df_div_sheet = self._read_stock_rows(div_sheet, normalized_id)
        df_chip_sheet = self._read_stock_rows(chip_sheet, normalized_id)

        max_div_raw = _parse_iso_date(
            str(df_div_sheet["raw_date"].max()) if not df_div_sheet.empty else None
        )
        max_chip_raw = _parse_iso_date(
            str(df_chip_sheet["raw_date"].max()) if not df_chip_sheet.empty else None
        )

        need_rev = force_refresh or (max_rev_raw is None) or (max_rev_raw < expected_rev_date)
        need_fin = force_refresh or (max_fin_raw is None) or (max_fin_raw < expected_fin_date)
        need_div = force_refresh or (max_div_raw is None) or (today - max_div_raw >= timedelta(days=30))
        need_chip = force_refresh or (max_chip_raw is None) or (today - max_chip_raw >= timedelta(days=1))

        if cooldown_active and (need_rev or need_fin or need_div or need_chip):
            self._update_watchlist_master(
                watchlist,
                normalized_id,
                {
                    "stock_name": stock_name,
                    "industry_category": industry_category,
                    "last_accessed_at": now.isoformat(timespec="seconds"),
                },
            )
            max_rev_raw_date_str = str(df_rev_sheet["raw_date"].max()) if not df_rev_sheet.empty else None
            max_fin_raw_date_str = str(df_wide_sheet["raw_date"].max()) if not df_wide_sheet.empty else None
            last_rev_ym = _rev_ym_from_raw_date(max_rev_raw_date_str) if max_rev_raw_date_str else None
            last_fin_qtr = _qtr_from_raw_date(max_fin_raw_date_str) if max_fin_raw_date_str else None
            max_div_raw_date_str = str(df_div_sheet["raw_date"].max()) if not df_div_sheet.empty else None
            max_chip_raw_date_str = str(df_chip_sheet["raw_date"].max()) if not df_chip_sheet.empty else None
            return SyncResult(
                stock_id=normalized_id,
                rev_updated=False,
                fin_updated=False,
                div_updated=False,
                chip_updated=False,
                max_rev_raw_date=max_rev_raw_date_str,
                last_rev_ym=last_rev_ym,
                max_fin_raw_date=max_fin_raw_date_str,
                last_fin_qtr=last_fin_qtr,
                max_div_raw_date=max_div_raw_date_str,
                max_chip_raw_date=max_chip_raw_date_str,
                warnings=["probe_cooldown_active"],
            )

        # Revenue track.
        df_rev_full = df_rev_sheet
        df_rev_delta = pd.DataFrame()
        if need_rev:
            df_rev_new = self._fetch_monthly_revenue(
                normalized_id,
                pd.DataFrame() if force_refresh else df_rev_sheet,
            )
            df_rev_full, df_rev_delta_new = self._stitch_monthly_revenue(df_rev_sheet, df_rev_new)
            df_rev_delta = df_rev_full if force_refresh else df_rev_delta_new
            if not df_rev_delta.empty:
                self._upsert_delta(rev_sheet, df_rev_delta, key_col="pk_key")
            else:
                warnings.append("no_new_monthly_revenue")

        # Finance track.
        df_is_full, df_bs_full, df_cf_full, df_wide_full = (
            df_is_sheet,
            df_bs_sheet,
            df_cf_sheet,
            df_wide_sheet,
        )
        df_is_delta = pd.DataFrame()
        df_bs_delta = pd.DataFrame()
        df_cf_delta = pd.DataFrame()
        df_wide_delta = pd.DataFrame()

        if need_fin:
            df_is_new, df_bs_new, df_cf_new = self._fetch_quarterly_statements(
                normalized_id,
                pd.DataFrame() if force_refresh else df_is_sheet,
                pd.DataFrame() if force_refresh else df_bs_sheet,
                pd.DataFrame() if force_refresh else df_cf_sheet,
            )

            df_is_full = self._stitch_simple(df_is_sheet, df_is_new, key_col="pk_key")
            df_bs_full = self._stitch_simple(df_bs_sheet, df_bs_new, key_col="pk_key")
            df_cf_full = self._stitch_simple(df_cf_sheet, df_cf_new, key_col="pk_key")

            df_wide_full, df_wide_delta_new = self._stitch_and_compute_metrics(
                df_is_full,
                df_bs_full,
                df_cf_full,
                df_wide_sheet,
            )

            # 財報回頭抓 1 期，因此 delta 需比對內容差異，才能覆蓋更新同 pk_key 的上一季。
            df_is_delta_new = self._delta_by_content(df_is_sheet, df_is_full, key_col="pk_key")
            df_bs_delta_new = self._delta_by_content(df_bs_sheet, df_bs_full, key_col="pk_key")
            df_cf_delta_new = self._delta_by_content(df_cf_sheet, df_cf_full, key_col="pk_key")

            df_is_delta = df_is_full if force_refresh else df_is_delta_new
            df_bs_delta = df_bs_full if force_refresh else df_bs_delta_new
            df_cf_delta = df_cf_full if force_refresh else df_cf_delta_new
            df_wide_delta = df_wide_full if force_refresh else df_wide_delta_new

            validate_balance_sheet_rows(df_bs_delta)
            validate_cash_flow_rows(df_cf_delta)

            if not df_is_delta.empty:
                self._upsert_delta(fin_is_sheet, df_is_delta, "pk_key")
            if not df_bs_delta.empty:
                self._upsert_delta(fin_bs_sheet, df_bs_delta, "pk_key")
            if not df_cf_delta.empty:
                self._upsert_delta(fin_cf_sheet, df_cf_delta, "pk_key")
            if not df_wide_delta.empty:
                self._upsert_delta(fin_wide_sheet, df_wide_delta, "pk_key")

        # Dividend track.
        df_div_full = df_div_sheet
        df_div_delta = pd.DataFrame()
        if need_div:
            df_div_full, df_div_delta_new = self._sync_dividend_policy(
                normalized_id,
                pd.DataFrame() if force_refresh else df_div_sheet,
                df_is_full,
                df_bs_full,
            )
            df_div_delta = df_div_full if force_refresh else df_div_delta_new
            if not df_div_delta.empty:
                self._upsert_delta(div_sheet, df_div_delta, key_col="pk_key")
            else:
                warnings.append("no_new_dividend_policy")

        # Chip track.
        df_chip_full = df_chip_sheet
        df_chip_delta = pd.DataFrame()
        if need_chip:
            df_chip_full, df_chip_delta_new, chip_warnings = self._sync_chip_summary(
                normalized_id,
                pd.DataFrame() if force_refresh else df_chip_sheet,
            )
            warnings.extend(chip_warnings)
            df_chip_delta = df_chip_full if force_refresh else df_chip_delta_new
            if not df_chip_delta.empty:
                self._upsert_delta(chip_sheet, df_chip_delta, key_col="pk_key")
            else:
                warnings.append("no_new_chip_summary")

        max_rev_raw_date_str = str(df_rev_full["raw_date"].max()) if not df_rev_full.empty else None
        max_fin_raw_date_str = str(df_wide_full["raw_date"].max()) if not df_wide_full.empty else None
        max_div_raw_date_str = str(df_div_full["raw_date"].max()) if not df_div_full.empty else None
        max_chip_raw_date_str = str(df_chip_full["raw_date"].max()) if not df_chip_full.empty else None

        last_rev_ym = _rev_ym_from_raw_date(max_rev_raw_date_str) if max_rev_raw_date_str else None
        last_fin_qtr = _qtr_from_raw_date(max_fin_raw_date_str) if max_fin_raw_date_str else None

        rev_updated = not df_rev_delta.empty
        fin_updated = any(not df.empty for df in (df_is_delta, df_bs_delta, df_cf_delta, df_wide_delta))
        div_updated = not df_div_delta.empty
        chip_updated = not df_chip_delta.empty

        self._update_watchlist_master(
            watchlist,
            normalized_id,
            {
                "stock_name": stock_name,
                "industry_category": industry_category,
                "max_rev_raw_date": max_rev_raw_date_str or "",
                "last_rev_ym": last_rev_ym or "",
                "max_fin_raw_date": max_fin_raw_date_str or "",
                "last_fin_qtr": last_fin_qtr or "",
                "max_div_raw_date": max_div_raw_date_str or "",
                "max_chip_raw_date": max_chip_raw_date_str or "",
                "last_probe_at": now.isoformat(timespec="seconds"),
                "last_accessed_at": now.isoformat(timespec="seconds"),
            },
        )

        return SyncResult(
            stock_id=normalized_id,
            rev_updated=rev_updated,
            fin_updated=fin_updated,
            div_updated=div_updated,
            chip_updated=chip_updated,
            max_rev_raw_date=max_rev_raw_date_str,
            last_rev_ym=last_rev_ym,
            max_fin_raw_date=max_fin_raw_date_str,
            last_fin_qtr=last_fin_qtr,
            max_div_raw_date=max_div_raw_date_str,
            max_chip_raw_date=max_chip_raw_date_str,
            warnings=warnings,
        )

    def _get_or_create_watch_row(self, watchlist: gspread.Worksheet, stock_id: str) -> dict[str, str]:
        records = self._get_records_cached(watchlist)
        for record in records:
            if str(record.get("stock_id", "")).strip() == stock_id:
                return {k: str(v) if v is not None else "" for k, v in record.items()}

        headers = self._headers_cache.get(watchlist.title) or SHEET_SCHEMAS["00_Watchlist_Master"]
        row = [""] * len(headers)
        row[0] = stock_id
        self._gspread_call(watchlist.append_row, row)
        if self.batch_mode and watchlist.title in self._records_cache:
            self._patch_records_cache(watchlist.title, list(headers), {}, [row])
        else:
            self._invalidate_cache(watchlist.title)
        return {"stock_id": stock_id}

    def _get_stock_basic_info(self, stock_id: str) -> tuple[str, str]:
        if stock_id in self._stock_info_cache:
            return self._stock_info_cache[stock_id]

        try:
            df_info = self._finmind.taiwan_stock_info()
            if df_info is not None and not df_info.empty and "stock_id" in df_info.columns:
                for _, row in df_info.iterrows():
                    sid = str(row.get("stock_id", "")).strip()
                    sname = str(row.get("stock_name", "")).strip()
                    ind = str(row.get("industry_category", "")).strip()
                    if sid and sname:
                        self._stock_info_cache[sid] = (sname, ind)
        except Exception:
            pass

        return self._stock_info_cache.get(stock_id, ("", ""))

    def _to_iso_trade_week(self, raw_date: str | date) -> str:
        if isinstance(raw_date, str):
            parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
        else:
            parsed = raw_date
        iso_year, iso_week, _ = parsed.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"

    def _sync_dividend_policy(
        self,
        stock_id: str,
        df_sheet: pd.DataFrame,
        df_is_full: pd.DataFrame,
        df_bs_full: pd.DataFrame | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        today = _today_in_taipei()
        start_date = f"{today.year - 10}-01-01"

        try:
            frame = self._finmind.taiwan_stock_dividend(
                stock_id=stock_id,
                start_date=start_date,
                end_date=today.isoformat(),
            )
        except Exception as error:
            logger.warning("Dividend fetch failed for %s: %s", stock_id, error)
            return df_sheet, pd.DataFrame()

        if frame is None or frame.empty:
            return df_sheet, pd.DataFrame()

        frame = frame.copy()
        frame.rename(columns={"date": "raw_date"}, inplace=True)
        frame["raw_date"] = frame["raw_date"].astype(str)
        frame["div_year"] = [
            _normalize_div_year(y, d)
            for y, d in zip(frame.get("year", []), frame["raw_date"])
        ]

        cash_earn = pd.to_numeric(frame.get("CashEarningsDistribution"), errors="coerce").fillna(0.0)
        cash_surplus = pd.to_numeric(frame.get("CashStatutorySurplus"), errors="coerce").fillna(0.0)
        cash_dividend = cash_earn + cash_surplus
        stock_dividend = (
            pd.to_numeric(frame.get("StockEarningsDistribution"), errors="coerce").fillna(0.0)
            + pd.to_numeric(frame.get("StockStatutorySurplus"), errors="coerce").fillna(0.0)
        )

        # 以普通股股本 (面額 10 元 => 股數 = capital_stock / 10) 推算年度現金股利發放總額(億元)
        latest_shares = float("nan")
        if df_bs_full is not None and not df_bs_full.empty and "capital_stock" in df_bs_full.columns:
            cap_series = pd.to_numeric(df_bs_full["capital_stock"], errors="coerce").dropna()
            if not cap_series.empty:
                latest_shares = float(cap_series.iloc[-1]) / 10.0

        total_cash_div_yi = (
            (cash_dividend * latest_shares) / 1e8
            if pd.notna(latest_shares)
            else pd.Series(float("nan"), index=frame.index)
        )

        out = pd.DataFrame(
            {
                "stock_id": frame["stock_id"].astype(str).str.strip(),
                "raw_date": frame["raw_date"].astype(str),
                "div_year": frame["div_year"].astype(str),
                "cash_dividend": cash_dividend.round(4),
                "stock_dividend": stock_dividend.round(4),
                "total_dividend": (cash_dividend + stock_dividend).round(4),
                "total_cash_dividend_亿元": pd.to_numeric(total_cash_div_yi, errors="coerce").round(2),
                "ex_dividend_date": frame.get("CashExDividendTradingDate", ""),
                "payment_date": frame.get("CashDividendPaymentDate", ""),
            }
        )

        # 若同一年有多筆配息（如季配/半年配），按年度匯總股利
        out = (
            out.sort_values("raw_date")
            .groupby(["stock_id", "div_year"], as_index=False)
            .agg(
                {
                    "raw_date": "last",
                    "cash_dividend": "sum",
                    "stock_dividend": "sum",
                    "total_dividend": "sum",
                    "total_cash_dividend_亿元": "sum",
                    "ex_dividend_date": "last",
                    "payment_date": "last",
                }
            )
        )

        annual_eps_map: dict[str, float] = {}
        if df_is_full is not None and not df_is_full.empty and {"year_qtr", "eps"}.issubset(df_is_full.columns):
            tmp = df_is_full.copy()
            tmp["_year"] = tmp["year_qtr"].astype(str).str[:4]
            tmp["eps"] = pd.to_numeric(tmp["eps"], errors="coerce")
            annual = tmp.groupby(["_year"], dropna=False)["eps"].sum(min_count=1)
            annual_eps_map = {str(k): round(float(v), 2) for k, v in annual.items() if pd.notna(v)}

        out["annual_eps"] = out["div_year"].map(annual_eps_map)
        out["payout_ratio_pct"] = (
            (
                pd.to_numeric(out["cash_dividend"], errors="coerce")
                / pd.to_numeric(out["annual_eps"], errors="coerce").replace({0: float("nan")})
            )
            * 100.0
        ).round(2)

        out["pk_key"] = out["stock_id"] + "_" + out["div_year"].astype(str)
        out["updated_at"] = _now_in_taipei().isoformat(timespec="seconds")

        out = out[[c for c in SHEET_SCHEMAS["06_Dividend_Policy"] if c in out.columns]].copy()
        full = self._stitch_simple(df_sheet, out, key_col="pk_key")
        delta = self._delta_by_content(df_sheet, full, key_col="pk_key")
        return full, delta

    def _sync_chip_summary(
        self,
        stock_id: str,
        df_sheet: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
        today = _today_in_taipei()
        start_date = (today - timedelta(days=60)).isoformat()
        warnings: list[str] = []

        # 1) 外資持股比例 (TaiwanStockShareholding)
        try:
            df_hold = self._finmind.get_data(
                dataset="TaiwanStockShareholding",
                data_id=stock_id,
                start_date=start_date,
                end_date=today.isoformat(),
            )
        except Exception as error:
            warnings.append("chip_shareholding_fetch_failed")
            logger.warning("Shareholding fetch failed for %s: %s", stock_id, error)
            return df_sheet, pd.DataFrame(), warnings

        if df_hold is None or df_hold.empty or "date" not in df_hold.columns:
            warnings.append("chip_shareholding_empty")
            return df_sheet, pd.DataFrame(), warnings

        df_hold = df_hold.copy().sort_values("date").reset_index(drop=True)
        df_hold.rename(columns={"date": "raw_date"}, inplace=True)
        df_hold["raw_date"] = df_hold["raw_date"].astype(str)
        df_hold["foreign_holding_pct"] = pd.to_numeric(
            df_hold.get("ForeignInvestmentSharesRatio"), errors="coerce"
        )
        df_hold["foreign_holding_chg_20d"] = (
            df_hold["foreign_holding_pct"] - df_hold["foreign_holding_pct"].shift(20)
        )

        latest_date = str(df_hold["raw_date"].iloc[-1])
        foreign_holding_pct = (
            round(float(df_hold["foreign_holding_pct"].iloc[-1]), 2)
            if pd.notna(df_hold["foreign_holding_pct"].iloc[-1])
            else float("nan")
        )
        if pd.notna(df_hold["foreign_holding_chg_20d"].iloc[-1]):
            foreign_holding_chg_20d = round(float(df_hold["foreign_holding_chg_20d"].iloc[-1]), 2)
        elif len(df_hold) >= 2 and pd.notna(df_hold["foreign_holding_pct"].iloc[0]):
            foreign_holding_chg_20d = round(
                foreign_holding_pct - float(df_hold["foreign_holding_pct"].iloc[0]), 2
            )
        else:
            foreign_holding_chg_20d = float("nan")

        # 2) 三大法人買賣超 (TaiwanStockInstitutionalInvestorsBuySell) - 統一使用 get_data()
        foreign_5 = foreign_20 = trust_5 = trust_20 = dealer_5 = dealer_20 = 0.0
        try:
            df_inst = self._finmind.get_data(
                dataset="TaiwanStockInstitutionalInvestorsBuySell",
                data_id=stock_id,
                start_date=start_date,
                end_date=today.isoformat(),
            )
        except Exception as error:
            warnings.append("chip_investors_fetch_failed")
            logger.warning("Institutional buy/sell fetch failed for %s: %s", stock_id, error)
            df_inst = None

        if df_inst is not None and not df_inst.empty and {"date", "name", "buy", "sell"}.issubset(df_inst.columns):
            df_inst = df_inst.copy()
            df_inst["date"] = df_inst["date"].astype(str)
            df_inst["net_lots"] = (
                pd.to_numeric(df_inst["buy"], errors="coerce").fillna(0.0)
                - pd.to_numeric(df_inst["sell"], errors="coerce").fillna(0.0)
            ) / 1000.0

            unique_dates = sorted(df_inst["date"].unique())
            if unique_dates:
                latest_date = max(latest_date, unique_dates[-1])
            dates_5d = set(unique_dates[-5:])
            dates_20d = set(unique_dates[-20:])

            def _sum_net_lots(names: tuple[str, ...], date_filter: set[str]) -> float:
                sub = df_inst[df_inst["name"].isin(names) & df_inst["date"].isin(date_filter)]
                return round(float(sub["net_lots"].sum()), 1)

            foreign_names = ("Foreign_Investor", "Foreign_Dealer_Self")
            trust_names = ("Investment_Trust",)
            dealer_names = ("Dealer_self", "Dealer_Hedging", "Dealer")

            foreign_5 = _sum_net_lots(foreign_names, dates_5d)
            foreign_20 = _sum_net_lots(foreign_names, dates_20d)
            trust_5 = _sum_net_lots(trust_names, dates_5d)
            trust_20 = _sum_net_lots(trust_names, dates_20d)
            dealer_5 = _sum_net_lots(dealer_names, dates_5d)
            dealer_20 = _sum_net_lots(dealer_names, dates_20d)

        trade_week = self._to_iso_trade_week(latest_date)

        row = {
            "pk_key": f"{stock_id}_{trade_week}",
            "stock_id": stock_id,
            "raw_date": latest_date,
            "trade_week": trade_week,
            "foreign_holding_pct": foreign_holding_pct,
            "foreign_holding_chg_20d": foreign_holding_chg_20d,
            "foreign_net_buy_5d_lots": foreign_5,
            "foreign_net_buy_20d_lots": foreign_20,
            "trust_net_buy_5d_lots": trust_5,
            "trust_net_buy_20d_lots": trust_20,
            "dealer_net_buy_5d_lots": dealer_5,
            "dealer_net_buy_20d_lots": dealer_20,
            "three_inst_net_5d_lots": round(foreign_5 + trust_5 + dealer_5, 1),
            "three_inst_net_20d_lots": round(foreign_20 + trust_20 + dealer_20, 1),
            "updated_at": _now_in_taipei().isoformat(timespec="seconds"),
        }

        out = pd.DataFrame([row])
        out = out[[c for c in SHEET_SCHEMAS["07_Chip_Summary"] if c in out.columns]].copy()
        full = self._stitch_simple(df_sheet, out, key_col="pk_key")
        delta = self._delta_by_content(df_sheet, full, key_col="pk_key")
        return full, delta, warnings

    def _update_watchlist_master(
        self,
        watchlist: gspread.Worksheet,
        stock_id: str,
        updates: dict[str, str],
    ) -> None:
        headers = self._headers_cache.get(watchlist.title) or self._gspread_call(watchlist.row_values, 1)
        if not headers:
            raise FundamentalCacheError("Watchlist_Master missing headers.")
        self._headers_cache[watchlist.title] = headers

        records = self._get_records_cached(watchlist)
        found_row = None
        existing_record: dict[str, Any] = {}
        for idx, rec in enumerate(records, start=2):
            if str(rec.get("stock_id", "")).strip() == stock_id:
                found_row = idx
                existing_record = rec
                break

        if found_row is None:
            stock_col = headers.index("stock_id") + 1
            stock_ids = self._gspread_call(watchlist.col_values, stock_col)
            for row_idx, value in enumerate(stock_ids[1:], start=2):
                if str(value).strip() == stock_id:
                    found_row = row_idx
                    break

        if found_row is None:
            raise FundamentalCacheError("Failed to locate watchlist row after creation.")

        row_values = [str(existing_record.get(col, "") or "") for col in headers]
        for key, value in updates.items():
            if key in headers:
                row_values[headers.index(key)] = str(value if value is not None else "")

        row_range = f"A{found_row}:{gspread.utils.rowcol_to_a1(found_row, len(headers))}"
        self._gspread_call(
            watchlist.update,
            range_name=row_range,
            values=[row_values],
        )
        if self.batch_mode and watchlist.title in self._records_cache:
            self._patch_records_cache(watchlist.title, headers, {found_row: row_values}, [])
        else:
            self._invalidate_cache(watchlist.title)

    def _read_stock_rows(self, worksheet: gspread.Worksheet, stock_id: str) -> pd.DataFrame:
        records = self._get_records_cached(worksheet)
        if not records:
            return pd.DataFrame(columns=SHEET_SCHEMAS.get(worksheet.title, []))

        df = pd.DataFrame.from_records(records)
        if "stock_id" in df.columns:
            df = df[df["stock_id"].astype(str).str.strip() == stock_id].copy()
        return df

    def _fetch_monthly_revenue(self, stock_id: str, df_sheet: pd.DataFrame) -> pd.DataFrame:
        today = _today_in_taipei()

        # 月營收資料有可能在發布後被回補/更正（例如更正公告、補列、FinMind 後處理修正）。
        # 因此在增量抓取時，我們會「回頭多抓 1 期」，確保上一個月份的資料能被覆蓋更新。
        if df_sheet.empty:
            start_date = (today - timedelta(days=365 * 4)).isoformat()
        else:
            max_raw = _parse_iso_date(str(df_sheet["raw_date"].max()))
            if max_raw is None:
                start_date = (today - timedelta(days=365 * 4)).isoformat()
            else:
                # raw_date 是「次月 1 號」（例如 2022-10 的營收 raw_date=2022-11-01）。
                # 回頭抓 1 期：往回推 40 天，保證涵蓋至少上一個月份。
                backfill_start = max_raw - timedelta(days=40)
                start_date = backfill_start.isoformat()

        frame = self._finmind.taiwan_stock_month_revenue(
            stock_id=stock_id,
            start_date=start_date,
            end_date=today.isoformat(),
        )
        if frame.empty:
            return frame

        frame = frame.copy()
        frame.rename(columns={"date": "raw_date"}, inplace=True)
        frame["raw_date"] = frame["raw_date"].astype(str)
        frame["rev_ym"] = frame["raw_date"].map(_rev_ym_from_raw_date)
        frame["pk_key"] = frame["stock_id"].astype(str).str.strip() + "_" + frame["rev_ym"]
        frame["updated_at"] = _now_in_taipei().isoformat(timespec="seconds")
        for column in ("revenue", "revenue_year", "revenue_month"):
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")

        return frame[[c for c in SHEET_SCHEMAS["01_Monthly_Revenue"] if c in frame.columns]].copy()

    def _stitch_monthly_revenue(
        self, df_sheet: pd.DataFrame, df_new: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        if df_new.empty and df_sheet.empty:
            return df_sheet, pd.DataFrame()
        combined = pd.concat([df_sheet, df_new], ignore_index=True)
        combined["raw_date"] = combined["raw_date"].astype(str)
        combined = combined.sort_values("raw_date").drop_duplicates(subset=["pk_key"], keep="last")
        combined["revenue"] = pd.to_numeric(combined["revenue"], errors="coerce")
        combined["mom_pct"] = (
            combined["revenue"].pct_change(periods=1, fill_method=None) * 100.0
        ).round(2)
        combined["yoy_pct"] = (
            combined["revenue"].pct_change(periods=12, fill_method=None) * 100.0
        ).round(2)
        combined["updated_at"] = _now_in_taipei().isoformat(timespec="seconds")

        # 月營收會回頭抓 1 期，因此 delta 不能只用 "new key"，必須比對內容差異，
        # 才能在同 pk_key 的情況下覆蓋更新上一期資料。
        df_delta = self._delta_by_content(df_sheet, combined, key_col="pk_key")
        return combined.reset_index(drop=True), df_delta

    def _fetch_quarterly_statements(
        self,
        stock_id: str,
        df_is_sheet: pd.DataFrame,
        df_bs_sheet: pd.DataFrame,
        df_cf_sheet: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        today = _today_in_taipei()

        # 財報資料可能在發布後因更正公告、補列或資料源修正而回補。
        # 因此增量抓取時「回頭抓 1 期」（上一季），確保上一季資料可被覆蓋更新。
        start_date = f"{today.year - 3}-01-01"
        if not df_is_sheet.empty:
            max_raw = _parse_iso_date(str(df_is_sheet["raw_date"].max()))
            if max_raw is not None:
                # raw_date 是季底日 (3/31, 6/30, 9/30, 12/31)
                backfill_start = max_raw - timedelta(days=120)
                start_date = backfill_start.isoformat()

        cf_start_year = int(start_date[:4])
        cf_start_date = f"{cf_start_year:04d}-01-01"

        df_is_raw = self._finmind.taiwan_stock_financial_statement(
            stock_id=stock_id,
            start_date=start_date,
            end_date=today.isoformat(),
        )
        df_bs_raw = self._finmind.taiwan_stock_balance_sheet(
            stock_id=stock_id,
            start_date=start_date,
            end_date=today.isoformat(),
        )
        df_cf_raw = self._finmind.taiwan_stock_cash_flows_statement(
            stock_id=stock_id,
            start_date=cf_start_date,
            end_date=today.isoformat(),
        )

        df_is = self._normalize_quarterly_statement(df_is_raw, stock_id, statement="IS")
        df_bs = self._normalize_quarterly_statement(df_bs_raw, stock_id, statement="BS")
        df_cf = self._normalize_quarterly_statement(df_cf_raw, stock_id, statement="CF")

        if not df_cf.empty and not df_cf_sheet.empty:
            df_cf = df_cf[df_cf["raw_date"] >= start_date].reset_index(drop=True)

        return df_is, df_bs, df_cf

    def _normalize_quarterly_statement(
        self, df_raw: pd.DataFrame, stock_id: str, *, statement: str
    ) -> pd.DataFrame:
        schema_key = (
            "02_Income_Statement"
            if statement == "IS"
            else "03_Balance_Sheet"
            if statement == "BS"
            else "04_Cash_Flow"
        )
        if df_raw.empty:
            return pd.DataFrame(columns=SHEET_SCHEMAS[schema_key])

        df = df_raw.copy()
        df.rename(columns={"date": "raw_date"}, inplace=True)
        df["raw_date"] = df["raw_date"].astype(str)
        df["year_qtr"] = df["raw_date"].map(_qtr_from_raw_date)

        if "type" not in df.columns or "value" not in df.columns:
            raise FundamentalCacheError("FinMind statement payload missing type/value columns.")

        # 過濾資產負債表中的 _per 百分比列
        df = df[~df["type"].astype(str).str.endswith("_per")].copy()

        pivot_type = df.pivot_table(
            index=["stock_id", "raw_date", "year_qtr"],
            columns="type",
            values="value",
            aggfunc="first",
        ).reset_index()

        pivot_origin = None
        if "origin_name" in df.columns:
            pivot_origin = df.pivot_table(
                index=["stock_id", "raw_date", "year_qtr"],
                columns="origin_name",
                values="value",
                aggfunc="first",
            ).reset_index()

        out = pd.DataFrame(
            {
                "stock_id": pivot_type["stock_id"].astype(str).str.strip(),
                "raw_date": pivot_type["raw_date"].astype(str),
                "year_qtr": pivot_type["year_qtr"].astype(str),
            }
        )

        updated_at = _now_in_taipei().isoformat(timespec="seconds")

        if statement == "IS":
            for col in (
                "revenue",
                "cost_of_goods_sold",
                "gross_profit",
                "operating_expenses",
                "operating_income",
                "non_op_income_total",
                "pretax_income",
                "income_tax_expense",
                "net_income_total",
                "net_income_parent",
                "net_income_nci",
                "eps",
            ):
                out[col] = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP[col], pivot_origin)

            out["net_income_parent"] = out["net_income_parent"].combine_first(out["net_income_total"])
            out["net_income_nci"] = out["net_income_nci"].fillna(0.0)
            out["updated_at"] = updated_at
            out["pk_key"] = out["stock_id"] + "_" + out["year_qtr"]
            return out[SHEET_SCHEMAS["02_Income_Statement"]]

        if statement == "BS":
            for col in (
                "cash_and_equiv",
                "inventory",
                "prepayments",
                "current_assets",
                "ppe_assets",
                "intangible_assets",
                "total_assets",
                "short_term_debt",
                "accounts_payable",
                "other_payables",
                "current_liabilities",
                "bonds_payable",
                "long_term_debt",
                "total_liabilities",
                "capital_stock",
                "retained_earnings",
                "parent_equity",
                "total_equity",
            ):
                out[col] = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP[col], pivot_origin)

            fvpl_c = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["fvpl_financial_assets_curr"], pivot_origin).fillna(0.0)
            fvpl_nc = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["fvpl_financial_assets_noncurr"], pivot_origin).fillna(0.0)
            out["fvpl_financial_assets"] = fvpl_c + fvpl_nc

            fvoci_c = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["fvoci_financial_assets_curr"], pivot_origin).fillna(0.0)
            fvoci_nc = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["fvoci_financial_assets_noncurr"], pivot_origin).fillna(0.0)
            out["fvoci_financial_assets"] = fvoci_c + fvoci_nc

            ac_c = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["ac_financial_assets_curr"], pivot_origin).fillna(0.0)
            ac_nc = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["ac_financial_assets_noncurr"], pivot_origin).fillna(0.0)
            out["ac_financial_assets"] = ac_c + ac_nc

            ar_only = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["accounts_receivable_only"], pivot_origin).fillna(0.0)
            notes_r = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["notes_receivable"], pivot_origin).fillna(0.0)
            out["accounts_receivable"] = ar_only + notes_r

            contract_c = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["contract_liab_curr"], pivot_origin).fillna(0.0)
            contract_nc = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["contract_liab_noncurr"], pivot_origin).fillna(0.0)
            out["contract_liab_current"] = contract_c + contract_nc

            for zero_col in (
                "inventory",
                "prepayments",
                "ppe_assets",
                "intangible_assets",
                "short_term_debt",
                "other_payables",
                "bonds_payable",
                "long_term_debt",
            ):
                out[zero_col] = out[zero_col].fillna(0.0)

            out["parent_equity"] = out["parent_equity"].combine_first(out["total_equity"])
            out["updated_at"] = updated_at
            out["pk_key"] = out["stock_id"] + "_" + out["year_qtr"]
            return out[SHEET_SCHEMAS["03_Balance_Sheet"]]

        # ================= 04_現金流量表 (CF)：YTD 累季還原單季 =================
        cf_cols = [
            "depreciation",
            "amortization",
            "interest_income",
            "interest_expense",
            "cf_change_ar",
            "cf_change_inv",
            "cf_change_ap",
            "operating_cf",
            "capex",
            "investing_cf",
            "cf_debt_net_change",
            "financing_cf",
        ]
        for col in cf_cols:
            if col == "cf_debt_net_change":
                continue
            out[col] = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP[col], pivot_origin)

        st_inc = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["cf_debt_st_increase"], pivot_origin).fillna(0.0)
        lt_proceeds = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["cf_debt_lt_proceeds"], pivot_origin).fillna(0.0)
        lt_repay = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP["cf_debt_lt_repayments"], pivot_origin).fillna(0.0)
        out["cf_debt_net_change"] = st_inc + lt_proceeds + lt_repay

        out = out.sort_values("raw_date").reset_index(drop=True)
        out["_year"] = out["year_qtr"].str[:4]
        out["_qtr_num"] = out["year_qtr"].str[-1].astype(int)

        for col in cf_cols:
            ytd_series = pd.to_numeric(out[col], errors="coerce")
            prev_ytd = out.groupby("_year")[col].shift(1)
            prev_qtr_num = out.groupby("_year")["_qtr_num"].shift(1)
            is_consecutive = (out["_qtr_num"] - prev_qtr_num) == 1
            out[col] = ytd_series.where(~is_consecutive, ytd_series - prev_ytd)

        out.drop(columns=["_year", "_qtr_num"], inplace=True)
        for fill_col in ("depreciation", "amortization", "interest_income", "interest_expense", "capex"):
            out[fill_col] = out[fill_col].fillna(0.0)
        out["free_cf"] = out["operating_cf"] - out["capex"].abs()
        out["updated_at"] = updated_at
        out["pk_key"] = out["stock_id"] + "_" + out["year_qtr"]
        return out[SHEET_SCHEMAS["04_Cash_Flow"]]

    def _stitch_simple(self, df_sheet: pd.DataFrame, df_new: pd.DataFrame, *, key_col: str) -> pd.DataFrame:
        if df_new.empty and df_sheet.empty:
            return df_sheet
        if df_sheet is None or df_sheet.empty:
            merged = df_new.copy()
        elif df_new is None or df_new.empty:
            merged = df_sheet.copy()
        else:
            s_clean = df_sheet.dropna(axis=1, how="all")
            n_clean = df_new.dropna(axis=1, how="all")
            if s_clean.empty:
                merged = df_new.copy()
            elif n_clean.empty:
                merged = df_sheet.copy()
            else:
                merged = pd.concat([s_clean, n_clean], ignore_index=True)
            for col in df_new.columns:
                if col not in merged.columns:
                    merged[col] = float("nan")
            merged = merged[df_new.columns]
        merged = merged.drop_duplicates(subset=[key_col], keep="last")
        if "raw_date" in merged.columns:
            merged = merged.sort_values("raw_date")
        return merged.reset_index(drop=True)

    def _stitch_and_compute_metrics(
        self,
        df_is: pd.DataFrame,
        df_bs: pd.DataFrame,
        df_cf: pd.DataFrame,
        df_wide_sheet: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        df_is_ = df_is.copy().drop(columns=["updated_at"], errors="ignore")
        df_bs_ = df_bs.copy().drop(columns=["updated_at"], errors="ignore")
        df_cf_ = df_cf.copy().drop(columns=["updated_at"], errors="ignore")

        merged = df_is_.merge(df_bs_, on=["pk_key", "stock_id", "raw_date", "year_qtr"], how="outer")
        merged = merged.merge(df_cf_, on=["pk_key", "stock_id", "raw_date", "year_qtr"], how="outer")
        merged = merged.sort_values("raw_date").reset_index(drop=True)

        id_cols = {"pk_key", "stock_id", "raw_date", "year_qtr"}
        for col in merged.columns:
            if col not in id_cols:
                merged[col] = pd.to_numeric(merged[col], errors="coerce")

        rev_nonzero = merged["revenue"].replace({0: float("nan")})
        cogs_nonzero = merged["cost_of_goods_sold"].abs().replace({0: float("nan")})
        pretax_nonzero = merged["pretax_income"].replace({0: float("nan")})
        equity_nonzero = merged["total_equity"].replace({0: float("nan")})
        curr_liab_nonzero = merged["current_liabilities"].replace({0: float("nan")})

        # 1) 利潤率、營業費用率與業外佔比
        merged["gross_margin_pct"] = ((merged["gross_profit"] / rev_nonzero) * 100.0).round(2)
        merged["op_expense_pct"] = ((merged["operating_expenses"] / rev_nonzero) * 100.0).round(2)
        merged["op_margin_pct"] = ((merged["operating_income"] / rev_nonzero) * 100.0).round(2)
        merged["non_op_share_pct"] = ((merged["non_op_income_total"] / pretax_nonzero) * 100.0).round(2)
        merged["parent_net_margin_pct"] = ((merged["net_income_parent"] / rev_nonzero) * 100.0).round(2)

        # 2) 槓桿、償債與淨負債比（跨表從 04_Cash_Flow 取 interest_expense 計算利息保障倍數）
        total_debt = (
            merged["short_term_debt"].fillna(0.0)
            + merged["long_term_debt"].fillna(0.0)
            + merged["bonds_payable"].fillna(0.0)
        )
        merged["total_debt_亿元"] = (total_debt / 1e8).round(2)
        merged["net_debt_ratio_pct"] = (
            ((total_debt - merged["cash_and_equiv"].fillna(0.0)) / equity_nonzero) * 100.0
        ).round(2)
        merged["current_ratio_pct"] = ((merged["current_assets"] / curr_liab_nonzero) * 100.0).round(2)
        merged["quick_ratio_pct"] = (
            (
                (merged["current_assets"] - merged["inventory"].fillna(0.0) - merged["prepayments"].fillna(0.0))
                / curr_liab_nonzero
            )
            * 100.0
        ).round(2)

        int_exp_abs = merged["interest_expense"].abs().replace({0: float("nan")})
        merged["interest_coverage"] = ((merged["pretax_income"] + int_exp_abs) / int_exp_abs).round(2)

        # 3) 營運週轉天數 (DIO / DSO / DPO / CCC) - 若為首季無前季值，以單季期末值兜底避免首季空白
        avg_inv = ((merged["inventory"] + merged["inventory"].shift(1)) / 2.0).combine_first(merged["inventory"])
        avg_ar = ((merged["accounts_receivable"] + merged["accounts_receivable"].shift(1)) / 2.0).combine_first(
            merged["accounts_receivable"]
        )
        avg_ap = ((merged["accounts_payable"] + merged["accounts_payable"].shift(1)) / 2.0).combine_first(
            merged["accounts_payable"]
        )
        merged["dio_days"] = ((avg_inv / cogs_nonzero) * 91.25).round(2)
        merged["dso_days"] = ((avg_ar / rev_nonzero) * 91.25).round(2)
        merged["dpo_days"] = ((avg_ap / cogs_nonzero) * 91.25).round(2)
        merged["ccc_days"] = (merged["dio_days"] + merged["dso_days"] - merged["dpo_days"]).round(2)

        # 4) TTM EPS 與真實母公司 ROE
        merged["ttm_eps"] = merged["eps"].rolling(4, min_periods=1).sum().round(2)
        merged["qtr_rev_yoy_pct"] = (
            merged["revenue"].pct_change(4, fill_method=None) * 100.0
        ).round(2)

        parent_ni_ttm = merged["net_income_parent"].rolling(4, min_periods=1).sum()
        parent_eq = merged["parent_equity"].combine_first(merged["total_equity"])
        avg_parent_equity = ((parent_eq + parent_eq.shift(1)) / 2.0).combine_first(parent_eq)
        merged["parent_roe_pct"] = (
            (parent_ni_ttm / avg_parent_equity.replace({0: float("nan")})) * 100.0
        ).round(2)

        # 5) 現金流、EBITDA 與獲利含金量
        ebitda = (
            merged["operating_income"]
            + merged["depreciation"].fillna(0.0)
            + merged["amortization"].fillna(0.0)
        )

        merged["rev_亿元"] = (merged["revenue"] / 1e8).round(2)
        merged["contract_liab_亿元"] = (merged["contract_liab_current"].fillna(0.0) / 1e8).round(2)
        merged["ocf_亿元"] = (merged["operating_cf"] / 1e8).round(2)
        merged["capex_亿元"] = (merged["capex"] / 1e8).round(2)
        merged["fcf_亿元"] = (merged["free_cf"] / 1e8).round(2)
        merged["ebitda_亿元"] = (ebitda / 1e8).round(2)
        merged["ocf_to_ni_pct"] = (
            (merged["operating_cf"] / merged["net_income_parent"].replace({0: float("nan")})) * 100.0
        ).round(2)

        merged["updated_at"] = _now_in_taipei().isoformat(timespec="seconds")

        wide = merged[[c for c in SHEET_SCHEMAS["05_AI_Metrics_Wide"] if c in merged.columns]].copy()
        if wide.empty:
            wide = pd.DataFrame(columns=SHEET_SCHEMAS["05_AI_Metrics_Wide"])

        full = self._stitch_simple(df_wide_sheet, wide, key_col="pk_key")
        # 財報回頭抓 1 期時，wide 指標也需要可覆蓋更新同 pk_key 的上一季。
        delta = self._delta_by_content(df_wide_sheet, full, key_col="pk_key")
        return full, delta

    def _delta_by_key(self, df_old: pd.DataFrame, df_new: pd.DataFrame, key_col: str) -> pd.DataFrame:
        if df_new.empty:
            return df_new
        if df_old.empty:
            return df_new
        old_keys = set(df_old[key_col].astype(str).tolist()) if key_col in df_old.columns else set()
        return df_new[~df_new[key_col].astype(str).isin(old_keys)].copy()

    def _delta_by_content(self, df_old: pd.DataFrame, df_new: pd.DataFrame, *, key_col: str) -> pd.DataFrame:
        if df_new.empty:
            return df_new
        if df_old.empty:
            return df_new
        if key_col not in df_old.columns or key_col not in df_new.columns:
            return df_new

        old_indexed = df_old.set_index(key_col, drop=False)
        changed_rows: list[pd.Series] = []
        for _, row in df_new.iterrows():
            key = str(row.get(key_col, "")).strip()
            if not key or key not in old_indexed.index:
                changed_rows.append(row)
                continue

            old_row = old_indexed.loc[key]
            if isinstance(old_row, pd.DataFrame):
                old_row = old_row.iloc[-1]

            shared = [c for c in df_new.columns if c in df_old.columns and c != "updated_at"]
            differs = False
            for col in shared:
                a = old_row.get(col)
                b = row.get(col)
                if pd.isna(a) and pd.isna(b):
                    continue
                if str(a) != str(b):
                    differs = True
                    break
            if differs:
                changed_rows.append(row)

        if not changed_rows:
            return pd.DataFrame(columns=df_new.columns)
        return pd.DataFrame(changed_rows).reset_index(drop=True)

    def _upsert_delta(self, worksheet: gspread.Worksheet, df_delta: pd.DataFrame, key_col: str) -> None:
        """零額外 Read API 的 Upsert：直接復用記憶體中的表頭與紀錄快取定位列號。"""
        if df_delta.empty:
            return

        title = worksheet.title
        headers = self._headers_cache.get(title) or self._gspread_call(worksheet.row_values, 1)
        if not headers:
            raise FundamentalCacheError(f"Worksheet {title} missing headers.")
        self._headers_cache[title] = headers
        if key_col not in headers:
            raise FundamentalCacheError(f"Worksheet {title} missing key column {key_col}.")

        records = self._get_records_cached(worksheet)
        key_to_row: dict[str, int] = {}
        existing_rows_by_index: dict[int, list[Any]] = {}
        num_cols = len(headers)

        for offset, rec in enumerate(records, start=2):
            k = str(rec.get(key_col, "")).strip()
            if k and k not in key_to_row:
                key_to_row[k] = offset
            existing_rows_by_index[offset] = [_sheet_cell_value(rec.get(col, "")) for col in headers]

        batch_updates_by_row: dict[int, list[Any]] = {}
        append_rows: list[list[Any]] = []

        for _, row in df_delta.iterrows():
            row_key = str(row.get(key_col, "")).strip()
            row_values = [_sheet_cell_value(row.get(col, "")) for col in headers]
            if row_key and row_key in key_to_row:
                row_index = key_to_row[row_key]
                existing_row_values = list(existing_rows_by_index.get(row_index, [""] * num_cols))
                for idx, col in enumerate(headers):
                    if col in df_delta.columns:
                        existing_row_values[idx] = row_values[idx]
                batch_updates_by_row[row_index] = existing_row_values
            else:
                append_rows.append(row_values)

        if batch_updates_by_row:
            batch_updates = [
                {
                    "range": f"A{row_index}:{gspread.utils.rowcol_to_a1(row_index, num_cols)}",
                    "values": [values],
                }
                for row_index, values in sorted(batch_updates_by_row.items())
            ]
            self._gspread_call(worksheet.batch_update, batch_updates, value_input_option="USER_ENTERED")

        if append_rows:
            self._gspread_call(worksheet.append_rows, append_rows, value_input_option="USER_ENTERED")

        if self.batch_mode:
            self._patch_records_cache(title, headers, batch_updates_by_row, append_rows)
        else:
            self._invalidate_cache(title)

    def enable_batch_mode(self) -> None:
        """批次模式：寫入後就地更新記憶體快取，避免每檔股票重讀整張工作表（Sheets Read Quota）。"""
        self.batch_mode = True
        self._cache_ttl = max(self._cache_ttl, 3600.0)

    def _patch_records_cache(
        self,
        title: str,
        headers: list[str],
        updated_rows: dict[int, list[Any]],
        appended_rows: list[list[Any]],
    ) -> None:
        cached = self._records_cache.get(title)
        if cached is None:
            return
        _, records = cached
        for row_index, values in updated_rows.items():
            record = {headers[i]: values[i] for i in range(min(len(headers), len(values))) if headers[i]}
            position = row_index - 2
            if 0 <= position < len(records):
                records[position] = record
            else:
                records.append(record)
        for values in appended_rows:
            records.append(
                {headers[i]: values[i] for i in range(min(len(headers), len(values))) if headers[i]}
            )
        self._records_cache[title] = (time.time(), records)