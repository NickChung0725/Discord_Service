"""Google Sheets incremental cache manager for Taiwan fundamentals.

Implements the core rules from SASD:
- Double-track watermark checks (monthly revenue vs quarterly statements)
- Incremental fetch using start_date = max_raw_date + 1 day
- In-memory stitching before computing derived metrics
- Validation before writing deltas back to Google Sheets

This module does not talk to Discord or AI directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import os
from typing import Any

import gspread
import pandas as pd
from FinMind.data import DataLoader

from fundamental_config import (
    ACCOUNT_ANCHOR_MAP,
    BALANCE_TOLERANCE_ABS,
    BALANCE_TOLERANCE_RATIO,
    SHEET_SCHEMAS,
    get_expected_watermarks,
)


TAIPEI_TIMEZONE = timezone(timedelta(hours=8))


@dataclass
class SyncResult:
    stock_id: str
    rev_updated: bool
    fin_updated: bool
    max_rev_raw_date: str | None
    last_rev_ym: str | None
    max_fin_raw_date: str | None
    last_fin_qtr: str | None
    warnings: list[str]


class FundamentalCacheError(RuntimeError):
    pass


class BalanceMismatchError(FundamentalCacheError):
    pass


def _today_in_taipei() -> date:
    return datetime.now(TAIPEI_TIMEZONE).date()


def _now_in_taipei() -> datetime:
    return datetime.now(TAIPEI_TIMEZONE)


def _safe_iso_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _rev_ym_from_raw_date(raw_date: str) -> str:
    # FinMind month revenue date is the announcement month 1st.
    # Example: 2026-10-01 corresponds to 2026-09 revenue.
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
    """Expected raw_date for the latest disclosed monthly revenue.

    Note: FinMind revenue "date" is the announcement month 1st, which is the
    month AFTER the revenue period. Therefore, if expected_rev_ym is YYYY-12,
    raw_date should be (YYYY+1)-01-01.
    """

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


def _anchor_pick(
    pivot_type: pd.DataFrame,
     aliases: list[str],
    pivot_origin: pd.DataFrame | None = None,
) -> pd.Series:
    """依優先序從英文 type 樞紐表與中文 origin_name 樞紐表提取數值，支援跨期互補。"""
    result = pd.Series(float("nan"), index=pivot_type.index, dtype="float64")

    for alias in aliases:
        if alias in pivot_type.columns:
            col_series = pd.to_numeric(pivot_type[alias], errors="coerce")
            result = result.combine_first(col_series)
        elif pivot_origin is not None and alias in pivot_origin.columns:
            col_series = pd.to_numeric(pivot_origin[alias], errors="coerce")
            result = result.combine_first(col_series)

    # 若仍為全空，針對中文 origin_name 進行去括號模糊比對保底
    if result.isna().all() and pivot_origin is not None:
        for alias in aliases:
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
    """Validate assets == liabilities + equity within tolerance.

    Fails when BOTH:
    - abs_diff > BALANCE_TOLERANCE_ABS
    - ratio_diff > BALANCE_TOLERANCE_RATIO
    """

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
    """Validate free_cf == operating_cf - abs(capex) within tolerance."""

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

        # 新增：台股基本資料記憶體快取 {stock_id: (stock_name, industry_category)}
        self._stock_info_cache: dict[str, tuple[str, str]] = {}

    def ensure_workbook_schema(self) -> None:
        existing_tabs = {sheet.title for sheet in self._spreadsheet.worksheets()}
        for tab_name, headers in SHEET_SCHEMAS.items():
            if tab_name not in existing_tabs:
                worksheet = self._spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(headers))
                worksheet.append_row(headers)
                continue

            worksheet = self._spreadsheet.worksheet(tab_name)
            first_row = worksheet.row_values(1)
            if not first_row:
                worksheet.append_row(headers)
                continue

            # Append missing headers (never delete/reorder).
            missing = [header for header in headers if header not in first_row]
            if missing:
                worksheet.update(
                    range_name=f"A1:{gspread.utils.rowcol_to_a1(1, len(first_row) + len(missing))}",
                    values=[first_row + missing],
                )

    def check_and_sync_stock(self, stock_id: str, force_refresh: bool = False) -> SyncResult:
        normalized_id = stock_id.strip()
        if not normalized_id.isdigit() or not (4 <= len(normalized_id) <= 6):
            raise FundamentalCacheError("股票代號必須是 4 至 6 位數字。")

        self.ensure_workbook_schema()

        now = _now_in_taipei()
        today = now.date()
        expected_rev_date = _expected_rev_raw_date(today)
        expected_fin_date = _expected_fin_raw_date(today)

        watchlist = self._spreadsheet.worksheet("00_Watchlist_Master")
        watch_row = self._get_or_create_watch_row(watchlist, normalized_id)

        # 新增：若 Watchlist_Master 內的 stock_name 或 industry_category 為空，自動查詢補齊
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

        rev_sheet = self._spreadsheet.worksheet("01_Monthly_Revenue")
        df_rev_sheet = self._read_stock_rows(rev_sheet, normalized_id)
        max_rev_raw = _parse_iso_date(
            str(df_rev_sheet["raw_date"].max()) if not df_rev_sheet.empty else None
        )

        fin_is_sheet = self._spreadsheet.worksheet("02_Income_Statement")
        fin_bs_sheet = self._spreadsheet.worksheet("03_Balance_Sheet")
        fin_cf_sheet = self._spreadsheet.worksheet("04_Cash_Flow")
        fin_wide_sheet = self._spreadsheet.worksheet("05_AI_Metrics_Wide")

        df_is_sheet = self._read_stock_rows(fin_is_sheet, normalized_id)
        df_bs_sheet = self._read_stock_rows(fin_bs_sheet, normalized_id)
        df_cf_sheet = self._read_stock_rows(fin_cf_sheet, normalized_id)
        df_wide_sheet = self._read_stock_rows(fin_wide_sheet, normalized_id)

        max_fin_raw = _parse_iso_date(
            str(df_wide_sheet["raw_date"].max()) if not df_wide_sheet.empty else None
        )

        need_rev = force_refresh or (max_rev_raw is None) or (max_rev_raw < expected_rev_date)
        need_fin = force_refresh or (max_fin_raw is None) or (max_fin_raw < expected_fin_date)

        # If we recently probed and are still behind expected watermarks, skip another probe.
        if cooldown_active and (need_rev or need_fin):
            self._update_watchlist_master(
                watchlist,
                normalized_id,
                {
                    "stock_name": stock_name,
                    "industry_category": industry_category,
                    "last_accessed_at": now.isoformat(timespec="seconds"),
                },
            )
            max_rev_raw_date_str = df_rev_sheet["raw_date"].max() if not df_rev_sheet.empty else None
            max_fin_raw_date_str = df_wide_sheet["raw_date"].max() if not df_wide_sheet.empty else None
            last_rev_ym = _rev_ym_from_raw_date(max_rev_raw_date_str) if max_rev_raw_date_str else None
            last_fin_qtr = _qtr_from_raw_date(max_fin_raw_date_str) if max_fin_raw_date_str else None
            return SyncResult(
                stock_id=normalized_id,
                rev_updated=False,
                fin_updated=False,
                max_rev_raw_date=max_rev_raw_date_str,
                last_rev_ym=last_rev_ym,
                max_fin_raw_date=max_fin_raw_date_str,
                last_fin_qtr=last_fin_qtr,
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
        df_is_full, df_bs_full, df_cf_full, df_wide_full = df_is_sheet, df_bs_sheet, df_cf_sheet, df_wide_sheet
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

            df_is_delta_new = self._delta_by_key(df_is_sheet, df_is_full, "pk_key")
            df_bs_delta_new = self._delta_by_key(df_bs_sheet, df_bs_full, "pk_key")
            df_cf_delta_new = self._delta_by_key(df_cf_sheet, df_cf_full, "pk_key")

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

        max_rev_raw_date_str = df_rev_full["raw_date"].max() if not df_rev_full.empty else None
        max_fin_raw_date_str = df_wide_full["raw_date"].max() if not df_wide_full.empty else None
        last_rev_ym = _rev_ym_from_raw_date(max_rev_raw_date_str) if max_rev_raw_date_str else None
        last_fin_qtr = _qtr_from_raw_date(max_fin_raw_date_str) if max_fin_raw_date_str else None

        rev_updated = (not df_rev_delta.empty)
        fin_updated = any(
            not df.empty for df in (df_is_delta, df_bs_delta, df_cf_delta, df_wide_delta)
        )

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
                "last_probe_at": now.isoformat(timespec="seconds"),
                "last_accessed_at": now.isoformat(timespec="seconds"),
            },
        )

        return SyncResult(
            stock_id=normalized_id,
            rev_updated=rev_updated,
            fin_updated=fin_updated,
            max_rev_raw_date=max_rev_raw_date_str,
            last_rev_ym=last_rev_ym,
            max_fin_raw_date=max_fin_raw_date_str,
            last_fin_qtr=last_fin_qtr,
            warnings=warnings,
        )

    def _get_or_create_watch_row(self, watchlist: gspread.Worksheet, stock_id: str) -> dict[str, str]:
        records = watchlist.get_all_records()
        for record in records:
            if str(record.get("stock_id", "")).strip() == stock_id:
                return {k: str(v) if v is not None else "" for k, v in record.items()}

        # Create an empty row.
        row = [""] * len(SHEET_SCHEMAS["00_Watchlist_Master"])
        row[0] = stock_id
        watchlist.append_row(row)
        return {"stock_id": stock_id}

    def _get_stock_basic_info(self, stock_id: str) -> tuple[str, str]:
        """從 FinMind TaiwanStockInfo 取得 (stock_name, industry_category) 並快取於記憶體。"""
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
    
    def _update_watchlist_master(
        self,
        watchlist: gspread.Worksheet,
        stock_id: str,
        updates: dict[str, str],
    ) -> None:
        headers = watchlist.row_values(1)
        if not headers:
            raise FundamentalCacheError("Watchlist_Master missing headers.")

        stock_col = headers.index("stock_id") + 1
        found_cell = None
        for cell in watchlist.findall(stock_id):
            if cell.col == stock_col and cell.row != 1:
                found_cell = cell
                break
        if found_cell is None:
            raise FundamentalCacheError("Failed to locate watchlist row after creation.")

        row_values = watchlist.row_values(found_cell.row)
        if len(row_values) < len(headers):
            row_values += [""] * (len(headers) - len(row_values))

        for key, value in updates.items():
            if key not in headers:
                continue
            row_values[headers.index(key)] = value

        watchlist.update(
            range_name=f"A{found_cell.row}:{gspread.utils.rowcol_to_a1(found_cell.row, len(headers))}",
            values=[row_values],
        )

    def _read_stock_rows(self, worksheet: gspread.Worksheet, stock_id: str) -> pd.DataFrame:
        records = worksheet.get_all_records()
        if not records:
            return pd.DataFrame(columns=SHEET_SCHEMAS.get(worksheet.title, []))

        df = pd.DataFrame.from_records(records)
        if "stock_id" in df.columns:
            df = df[df["stock_id"].astype(str).str.strip() == stock_id].copy()
        return df

    def _fetch_monthly_revenue(self, stock_id: str, df_sheet: pd.DataFrame) -> pd.DataFrame:
        today = _today_in_taipei()
        if df_sheet.empty:
            start_date = (today - timedelta(days=365 * 4)).isoformat()
        else:
            max_raw = _parse_iso_date(str(df_sheet["raw_date"].max()))
            if max_raw is None:
                start_date = (today - timedelta(days=365 * 4)).isoformat()
            else:
                start_date = (max_raw + timedelta(days=1)).isoformat()

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
        combined["mom_pct"] = combined["revenue"].pct_change(periods=1) * 100.0
        combined["yoy_pct"] = combined["revenue"].pct_change(periods=12) * 100.0
        combined["updated_at"] = _now_in_taipei().isoformat(timespec="seconds")

        df_delta = self._delta_by_key(df_sheet, combined, key_col="pk_key")
        return combined.reset_index(drop=True), df_delta

    def _fetch_quarterly_statements(
        self,
        stock_id: str,
        df_is_sheet: pd.DataFrame,
        df_bs_sheet: pd.DataFrame,
        df_cf_sheet: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        today = _today_in_taipei()
        start_date = f"{today.year - 3}-01-01"
        if not df_is_sheet.empty:
            max_raw = _parse_iso_date(str(df_is_sheet["raw_date"].max()))
            if max_raw is not None:
                start_date = (max_raw + timedelta(days=1)).isoformat()

        # 【關鍵修正】現金流量表 (CF) 在 FinMind 為「年初至今累計值 (YTD)」
        # 為了將 Q2/Q3/Q4 精準減去前一季還原為「單季值」，CF 的抓取起點必須對齊該年 1 月 1 日！
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

        # 過濾掉 CF 因為對齊年初而多抓的已存在舊季度（若非 force_refresh）
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

        # 同時建立英文 type 樞紐表與中文 origin_name 樞紐表
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
                "selling_expenses",
                "admin_expenses",
                "rd_expenses",
                "operating_expenses",
                "operating_income",
                "interest_income",
                "other_income",
                "other_gains_losses",
                "finance_costs",
                "equity_method_income",
                "non_op_income_total",
                "pretax_income",
                "income_tax_expense",
                "net_income",
                "net_income_parent",
                "net_income_nci",
                "eps",
            ):
                out[col] = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP[col], pivot_origin)

            # 若母公司淨利缺值，以合併總淨利保底
            out["net_income_parent"] = out["net_income_parent"].combine_first(out["net_income"])
            out["updated_at"] = updated_at
            out["pk_key"] = out["stock_id"] + "_" + out["year_qtr"]
            return out[SHEET_SCHEMAS["02_Income_Statement"]]

        if statement == "BS":
            for col in (
                "total_assets",
                "current_assets",
                "cash_and_equiv",
                "fvpl_assets",
                "fvoci_assets",
                "ac_assets",
                "accounts_receivable",
                "inventory",
                "prepayments",
                "total_liabilities",
                "current_liabilities",
                "accounts_payable",
                "contract_liabilities",
                "short_term_debt",
                "short_term_bills",
                "current_portion_lt_debt",
                "long_term_debt",
                "bonds_payable",
                "parent_equity",
                "total_equity",
            ):
                out[col] = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP[col], pivot_origin)

            out["inventory"] = out["inventory"].fillna(0.0)
            out["prepayments"] = out["prepayments"].fillna(0.0)
            out["parent_equity"] = out["parent_equity"].combine_first(out["total_equity"])
            out["updated_at"] = updated_at
            out["pk_key"] = out["stock_id"] + "_" + out["year_qtr"]
            return out[SHEET_SCHEMAS["03_Balance_Sheet"]]

        # ================= 現金流量表 (CF)：擷取並將「YTD 累季值」拆解還原為「真實單季值」 =================
        cf_cols = [
            "operating_cf",
            "investing_cf",
            "financing_cf",
            "capex",
            "depreciation",
            "amortization",
            "cf_change_ar",
            "cf_change_inv",
            "cf_change_ap",
            "cash_dividends_paid",
        ]
        for col in cf_cols:
            out[col] = _anchor_pick(pivot_type, ACCOUNT_ANCHOR_MAP[col], pivot_origin)

        # 按日期排序後，針對同年度 (year) 執行差額拆算：Q1 維持原值，Q2=Q2-Q1, Q3=Q3-Q2, Q4=Q4-Q3
        out = out.sort_values("raw_date").reset_index(drop=True)
        out["_year"] = out["year_qtr"].str[:4]
        out["_qtr_num"] = out["year_qtr"].str[-1].astype(int)

        for col in cf_cols:
            ytd_series = pd.to_numeric(out[col], errors="coerce")
            prev_ytd = out.groupby("_year")[col].shift(1)
            prev_qtr_num = out.groupby("_year")["_qtr_num"].shift(1)
            # 僅當上一筆確實是同年度緊鄰的前一季 (qtr_num - prev_qtr_num == 1) 時才相減，確保不因缺季算錯
            is_consecutive = (out["_qtr_num"] - prev_qtr_num) == 1
            out[col] = ytd_series.where(~is_consecutive, ytd_series - prev_ytd)

        out.drop(columns=["_year", "_qtr_num"], inplace=True)
        out["capex"] = out["capex"].fillna(0.0)
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
            merged = pd.concat([df_sheet.dropna(axis=1, how="all"), df_new.dropna(axis=1, how="all")], ignore_index=True)
            # 補齊標準欄位順序 (若某欄全空被 drop 掉，自動補回 float('nan'))
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

        # 將所有數值欄位轉為 float
        id_cols = {"pk_key", "stock_id", "raw_date", "year_qtr"}
        for col in merged.columns:
            if col not in id_cols:
                merged[col] = pd.to_numeric(merged[col], errors="coerce")

        rev_nonzero = merged["revenue"].replace({0: float("nan")})
        cogs_nonzero = merged["cost_of_goods_sold"].abs().replace({0: float("nan")})
        pretax_nonzero = merged["pretax_income"].replace({0: float("nan")})
        assets_nonzero = merged["total_assets"].replace({0: float("nan")})
        equity_nonzero = merged["total_equity"].replace({0: float("nan")})
        curr_liab_nonzero = merged["current_liabilities"].replace({0: float("nan")})

        # 1. 三率與三項費用率、業外佔比
        merged["gross_margin_pct"] = (merged["gross_profit"] / rev_nonzero) * 100.0
        merged["selling_exp_pct"] = (merged["selling_expenses"] / rev_nonzero) * 100.0
        merged["admin_exp_pct"] = (merged["admin_expenses"] / rev_nonzero) * 100.0
        merged["rd_exp_pct"] = (merged["rd_expenses"] / rev_nonzero) * 100.0
        merged["op_margin_pct"] = (merged["operating_income"] / rev_nonzero) * 100.0
        merged["non_op_share_pct"] = (merged["non_op_income_total"] / pretax_nonzero) * 100.0
        merged["net_margin_pct"] = (merged["net_income"] / rev_nonzero) * 100.0
        merged["parent_net_margin_pct"] = (merged["net_income_parent"] / rev_nonzero) * 100.0

        # 2. 財務槓桿、償債能力與淨負債比
        merged["debt_ratio_pct"] = (merged["total_liabilities"] / assets_nonzero) * 100.0
        total_interest_debt = (
            merged["short_term_debt"].fillna(0.0)
            + merged["short_term_bills"].fillna(0.0)
            + merged["current_portion_lt_debt"].fillna(0.0)
            + merged["long_term_debt"].fillna(0.0)
            + merged["bonds_payable"].fillna(0.0)
        )
        merged["net_debt_ratio_pct"] = (
            (total_interest_debt - merged["cash_and_equiv"].fillna(0.0)) / equity_nonzero
        ) * 100.0
        merged["current_ratio_pct"] = (merged["current_assets"] / curr_liab_nonzero) * 100.0
        merged["quick_ratio_pct"] = (
            (merged["current_assets"] - merged["inventory"].fillna(0.0) - merged["prepayments"].fillna(0.0))
            / curr_liab_nonzero
        ) * 100.0
        fin_cost_abs = merged["finance_costs"].abs().replace({0: float("nan")})
        merged["interest_coverage"] = (merged["pretax_income"] + fin_cost_abs) / fin_cost_abs

        # 3. 營運週轉天數 (DIO / DSO / DPO / CCC，以單季 91.25 天計)
        avg_inv = (merged["inventory"] + merged["inventory"].shift(1)) / 2.0
        avg_ar = (merged["accounts_receivable"] + merged["accounts_receivable"].shift(1)) / 2.0
        avg_ap = (merged["accounts_payable"] + merged["accounts_payable"].shift(1)) / 2.0
        merged["dio_days"] = (avg_inv / cogs_nonzero) * 91.25
        merged["dso_days"] = (avg_ar / rev_nonzero) * 91.25
        merged["dpo_days"] = (avg_ap / cogs_nonzero) * 91.25
        merged["ccc_days"] = merged["dio_days"] + merged["dso_days"] - merged["dpo_days"]

        # 4. TTM EPS 與真實母公司 ROE / ROA
        merged["ttm_eps"] = merged["eps"].rolling(4).sum()
        merged["qtr_rev_yoy_pct"] = merged["revenue"].pct_change(4) * 100.0

        parent_ni_ttm = merged["net_income_parent"].rolling(4).sum()
        parent_eq = merged["parent_equity"].combine_first(merged["total_equity"])
        avg_parent_equity = (parent_eq + parent_eq.shift(1)) / 2.0
        avg_assets = (merged["total_assets"] + merged["total_assets"].shift(1)) / 2.0
        merged["roe_pct"] = (parent_ni_ttm / avg_parent_equity.replace({0: float("nan")})) * 100.0
        merged["roa_pct"] = (parent_ni_ttm / avg_assets.replace({0: float("nan")})) * 100.0

        # 5. 現金流、EBITDA 與獲利含金量 (OCF / 母公司淨利)
        ebitda = (
            merged["operating_income"]
            + merged["depreciation"].fillna(0.0)
            + merged["amortization"].fillna(0.0)
        )
        merged["revenue_亿元"] = merged["revenue"] / 1e8
        merged["ocf_亿元"] = merged["operating_cf"] / 1e8
        merged["capex_亿元"] = merged["capex"] / 1e8
        merged["fcf_亿元"] = merged["free_cf"] / 1e8
        merged["ebitda_亿元"] = ebitda / 1e8
        merged["ocf_to_ni_pct"] = (
            merged["operating_cf"] / merged["net_income_parent"].replace({0: float("nan")})
        ) * 100.0
        merged["updated_at"] = _now_in_taipei().isoformat(timespec="seconds")

        wide = merged[[c for c in SHEET_SCHEMAS["05_AI_Metrics_Wide"] if c in merged.columns]].copy()
        if wide.empty:
            wide = pd.DataFrame(columns=SHEET_SCHEMAS["05_AI_Metrics_Wide"])

        full = self._stitch_simple(df_wide_sheet, wide, key_col="pk_key")
        delta = self._delta_by_key(df_wide_sheet, full, key_col="pk_key")
        return full, delta

    def _delta_by_key(self, df_old: pd.DataFrame, df_new: pd.DataFrame, key_col: str) -> pd.DataFrame:
        if df_new.empty:
            return df_new
        if df_old.empty:
            return df_new
        old_keys = set(df_old[key_col].astype(str).tolist()) if key_col in df_old.columns else set()
        return df_new[~df_new[key_col].astype(str).isin(old_keys)].copy()

    def _upsert_delta(self, worksheet: gspread.Worksheet, df_delta: pd.DataFrame, key_col: str) -> None:
        if df_delta.empty:
            return

        headers = worksheet.row_values(1)
        if not headers:
            raise FundamentalCacheError(f"Worksheet {worksheet.title} missing headers.")
        if key_col not in headers:
            raise FundamentalCacheError(f"Worksheet {worksheet.title} missing key column {key_col}.")

        key_col_idx = headers.index(key_col) + 1
        existing_keys = worksheet.col_values(key_col_idx)
        key_to_row: dict[str, int] = {}
        for offset, key in enumerate(existing_keys[1:], start=2):
            k = str(key).strip()
            if k and k not in key_to_row:
                key_to_row[k] = offset

        batch_updates: list[dict[str, Any]] = []
        append_rows: list[list[Any]] = []
        num_cols = len(headers)

        for _, row in df_delta.iterrows():
            row_key = str(row.get(key_col, "")).strip()
            row_values = [_sheet_cell_value(row.get(col, "")) for col in headers]
            if row_key and row_key in key_to_row:
                row_index = key_to_row[row_key]
                # 若 df_delta 內剛好有重複的 key，直接覆蓋最新一筆，避免同一個 range 重複送出
                batch_updates.append({
                    "range": f"A{row_index}:{gspread.utils.rowcol_to_a1(row_index, num_cols)}",
                    "values": [row_values],
                })
            else:
                append_rows.append(row_values)

        if batch_updates:
            worksheet.batch_update(batch_updates, value_input_option="USER_ENTERED")

        if append_rows:
            worksheet.append_rows(append_rows, value_input_option="USER_ENTERED")
