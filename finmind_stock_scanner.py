"""List Taiwan stock markets and scan FinMind prices for MA45/MA17 signals."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import gspread
from gspread.exceptions import APIError
import pandas as pd
import requests
from dotenv import load_dotenv
from FinMind.data import DataLoader

from fundamental_config import SHEET_SCHEMAS

logger = logging.getLogger(__name__)

MARKET_TYPES = {
    "上市": {"twse"},
    "上櫃": {"tpex"},
    "興櫃": {"emerging", "rotc", "esb"},
}
MARKET_CODES = {"上市": "twse", "上櫃": "tpex", "興櫃": "emerging"}
STOCK_INFO_COLUMNS = {"stock_id", "stock_name", "industry_category", "type"}
PRICE_COLUMNS = {"date", "close"}
MOVING_AVERAGE_PERIODS = (17, 45, 117, 189)
NEAR_CROSS_PERCENT = 1.0
ALIGNMENT_MAX_GAP_PERCENT = 1.0
REQUEST_PAUSE_SECONDS = 6.2
REQUEST_TIMEOUT_SECONDS = 30
INDICATOR_HISTORY_MONTHS = 12
REPORT_HISTORY_MONTHS = 6
DISCORD_API_BASE_URL = "https://discord.com/api/v10"
TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
DISCORD_POST_PAUSE_SECONDS = 1.0
SIGNAL_TRACKER_SHEET_NAME = "MA_Signal_Tracker"
SIGNAL_TRACKER_HEADERS = list(SHEET_SCHEMAS[SIGNAL_TRACKER_SHEET_NAME])
EXCLUDED_INDUSTRY_CATEGORIES = {
    "ETF",
    "ETN",
    "Index",
    "大盤",
    "所有證券",
    "受益證券",
    "上櫃ETF",
    "上櫃指數股票型基金(ETF)",
    "指數投資證券(ETN)",
}


def _today_in_taipei() -> date:
    return datetime.now(TAIPEI_TIMEZONE).date()


class FinMindRequestLimiter:
    """Persist a rolling-window FinMind request budget across process restarts."""

    def __init__(
        self,
        database_path: Path,
        max_requests_per_hour: int,
        min_interval_seconds: float,
    ) -> None:
        if not 1 <= max_requests_per_hour <= 540:
            raise ValueError(
                "max_requests_per_hour must be between 1 and 540 to reserve "
                "at least 60 requests of the 600/hour account limit."
            )
        if min_interval_seconds < 3600 / max_requests_per_hour:
            raise ValueError(
                "min_interval_seconds must be at least "
                "3600 / max_requests_per_hour."
            )

        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_requests_per_hour = max_requests_per_hour
        self.min_interval_seconds = min_interval_seconds
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS request_times ("
                "requested_at REAL PRIMARY KEY)"
            )
            connection.commit()

    def wait_for_slot(self) -> None:
        while True:
            now = time.time()
            with closing(
                sqlite3.connect(self.database_path, timeout=30)
            ) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM request_times WHERE requested_at <= ?",
                    (now - 3600,),
                )
                row = connection.execute(
                    "SELECT COUNT(*), MIN(requested_at), MAX(requested_at) "
                    "FROM request_times"
                ).fetchone()
                request_count, oldest_request, latest_request = row
                wait_seconds = 0.0
                if request_count >= self.max_requests_per_hour:
                    wait_seconds = oldest_request + 3600 - now
                if latest_request is not None:
                    wait_seconds = max(
                        wait_seconds,
                        self.min_interval_seconds - (now - latest_request),
                    )
                if wait_seconds <= 0:
                    connection.execute(
                        "INSERT INTO request_times (requested_at) VALUES (?)",
                        (now,),
                    )
                    connection.commit()
                    return
                connection.rollback()
            time.sleep(wait_seconds + 0.05)


@contextmanager
def _rate_limit_finmind_http_requests(limiter: FinMindRequestLimiter):
    original_send = requests.adapters.HTTPAdapter.send

    def rate_limited_send(
        adapter: requests.adapters.HTTPAdapter,
        request: requests.PreparedRequest,
        *args: object,
        **kwargs: object,
    ) -> requests.Response:
        limiter.wait_for_slot()
        return original_send(adapter, request, *args, **kwargs)

    requests.adapters.HTTPAdapter.send = rate_limited_send
    try:
        yield
    finally:
        requests.adapters.HTTPAdapter.send = original_send


def _atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        frame.to_csv(temporary_path, index=False, encoding="utf-8-sig")
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _validate_stock_info(stock_info: pd.DataFrame) -> None:
    missing = sorted(STOCK_INFO_COLUMNS.difference(stock_info.columns))
    if missing:
        raise RuntimeError(
            "FinMind 股票清單缺少必要欄位：" + ", ".join(missing)
        )


def _get_market_types(market: str) -> set[str]:
    try:
        return MARKET_TYPES[market]
    except KeyError as error:
        raise ValueError(
            f"不支援的市場 {market!r}；請選擇："
            + "、".join(MARKET_TYPES)
        ) from error


def _filter_market(stock_info: pd.DataFrame, market: str) -> pd.DataFrame:
    _validate_stock_info(stock_info)
    accepted_types = _get_market_types(market)
    market_mask = stock_info["type"].astype(str).str.strip().str.lower().isin(
        accepted_types
    )
    matched = stock_info.loc[market_mask].copy()
    if matched.empty:
        available = sorted(stock_info["type"].dropna().astype(str).unique())
        raise RuntimeError(
            f"FinMind 未回傳{market}股票（預期市場代碼："
            f"{', '.join(sorted(accepted_types))}；實際代碼："
            f"{', '.join(available)}）。"
        )
    return matched


def list_markets(stock_info: pd.DataFrame) -> pd.DataFrame:
    _validate_stock_info(stock_info)
    counts = stock_info["type"].astype(str).str.strip().str.lower().value_counts()
    rows = [
        {
            "market": market,
            "market_code": code,
            "stock_count": int(sum(counts.get(item, 0) for item in aliases)),
        }
        for market, aliases in MARKET_TYPES.items()
        for code in [MARKET_CODES[market]]
    ]
    known_codes = set().union(*MARKET_TYPES.values())
    rows.extend(
        {
            "market": f"其他（{market_type}）",
            "market_code": market_type,
            "stock_count": int(count),
        }
        for market_type, count in counts.items()
        if market_type not in known_codes
    )
    return pd.DataFrame(rows)


def list_industries(
    stock_info: pd.DataFrame, market: str | None = None
) -> pd.DataFrame:
    _validate_stock_info(stock_info)
    selected = (
        _filter_market(stock_info, market)
        if market is not None
        else stock_info.copy()
    )
    return (
        selected.dropna(subset=["industry_category"])
        .groupby("industry_category", as_index=False)
        .agg(stock_count=("stock_id", "nunique"))
        .sort_values("industry_category")
        .reset_index(drop=True)
    )


def select_stocks(
    stock_info: pd.DataFrame, market: str, industry: str
) -> pd.DataFrame:
    market_stocks = _filter_market(stock_info, market)
    selected = market_stocks.loc[
        market_stocks["industry_category"].astype(str).str.strip() == industry
    ].copy()
    if selected.empty:
        industries = sorted(
            market_stocks["industry_category"].dropna().astype(str).unique()
        )
        raise ValueError(
            f"{market}找不到產業「{industry}」。可用產業："
            + "、".join(industries)
        )
    return selected.drop_duplicates(subset=["stock_id"]).reset_index(drop=True)


def add_moving_averages(prices: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(PRICE_COLUMNS.difference(prices.columns))
    if missing:
        raise RuntimeError("FinMind 日線資料缺少必要欄位：" + ", ".join(missing))

    result = prices.copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result = (
        result.dropna(subset=["date", "close"])
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )
    for period in MOVING_AVERAGE_PERIODS:
        result[f"MA{period}"] = result["close"].rolling(
            window=period, min_periods=period
        ).mean()
    return result


def classify_latest_signal(
    analyzed_prices: pd.DataFrame,
) -> dict[str, object] | None:
    required_columns = ("MA17", "MA45", "MA117", "MA189")
    usable = analyzed_prices.dropna(subset=list(required_columns))
    if usable.empty:
        return None

    latest = usable.iloc[-1]
    ma17, ma45, ma117, ma189 = (
        float(latest[column]) for column in required_columns
    )
    close = float(latest["close"])
    if min(ma17, ma45, ma117, ma189) <= 0:
        return None

    # 117MA 與 189MA 皆需在 45MA 底下，且 189MA 必須在 117MA 底下 (45MA > 117MA > 189MA)
    if not (ma45 > ma117 and ma117 > ma189):
        return None

    # 以 17MA 為基準計算差距百分比
    gap_percent = (ma17 - ma45) / ma17 * 100
    if ma45 >= ma17:
        signal = "已超過"
    elif gap_percent <= NEAR_CROSS_PERCENT:
        signal = "接近（差距 <= 1%）"
    else:
        return None

    return {
        "date": latest["date"].strftime("%Y-%m-%d"),
        "close": close,
        "MA17": ma17,
        "MA45": ma45,
        "MA117": ma117,
        "MA189": ma189,
        "gap_percent": gap_percent,
        "signal": signal,
    }


def classify_latest_alignment_signal(
    analyzed_prices: pd.DataFrame,
) -> dict[str, object] | None:
    signal_data = classify_latest_signal(analyzed_prices)
    if signal_data is None:
        return None

    return {
        "date": signal_data["date"],
        "close": signal_data["close"],
        "MA17": signal_data["MA17"],
        "MA45": signal_data["MA45"],
        "MA117": signal_data["MA117"],
        "MA189": signal_data["MA189"],
        "MA17_MA45_gap_percent": signal_data["gap_percent"],
        "signal": signal_data["signal"],
    }


def format_alignment_report(
    alignment_signals: pd.DataFrame,
    as_of: date | None = None,
    failed_count: int = 0,
) -> list[str]:
    report_date = (as_of or _today_in_taipei()).isoformat()
    heading = (
        f"📊 {report_date} MA 均線排列訊號"
        "\n條件：以 MA17 為基準，MA45 已超過或即將超過 MA17（差距 <= 1%）；"
        "且 MA45 > MA117 > MA189"
    )
    if failed_count:
        heading += f"\n⚠️ 有 {failed_count} 檔股票資料抓取失敗，以下僅列成功分析的股票。"
    if alignment_signals.empty:
        return [heading + "\n今日沒有符合條件的股票。"]

    chunks: list[str] = []
    current = heading
    for row in alignment_signals.itertuples(index=False):
        gap_val = getattr(
            row,
            "MA17_MA45_gap_percent",
            getattr(row, "gap_percent", 0.0),
        )
        signal_label = getattr(row, "signal", None)
        signal_part = f" | {signal_label}" if signal_label else ""
        line = (
            f"\n{row.stock_id} {row.stock_name}{signal_part} | 收盤 {float(row.close):,.2f}"
            f" | MA17 {float(row.MA17):,.2f}"
            f" | MA45 {float(row.MA45):,.2f}"
            f" | MA117 {float(row.MA117):,.2f}"
            f" | MA189 {float(row.MA189):,.2f}"
            f" | 17/45 差 {float(gap_val):.2f}%"
        )
        if len(current) + len(line) > 1800:
            chunks.append(current)
            current = heading + "\n（續）"
        current += line
    chunks.append(current)
    return chunks


def post_alignment_report_to_discord(
    alignment_signals: pd.DataFrame,
    channel_id: str,
    bot_token: str,
    as_of: date | None = None,
    failed_count: int = 0,
) -> int:
    if not channel_id.isdigit():
        raise ValueError("STOCK_CHANNEL_ID 必須是 Discord 頻道 ID 數字。")
    if not bot_token.strip():
        raise ValueError("DISCORD_TOKEN (or BOT_TOKEN) is required to post the report.")

    messages = format_alignment_report(
        alignment_signals, as_of, failed_count=failed_count
    )
    sent_count = 0
    for index, content in enumerate(messages):
        request = Request(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages",
            data=json.dumps({"content": content}, ensure_ascii=False).encode(
                "utf-8"
            ),
            headers={
                "Authorization": f"Bot {bot_token.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordService (FinMind stock scanner)",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status not in (200, 201):
                    raise RuntimeError(
                        f"Discord returned unexpected HTTP status {response.status}."
                    )
        except HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Discord rejected the report (HTTP {error.code}): {response_body}"
            ) from error
        except URLError as error:
            raise RuntimeError(f"Failed to reach Discord: {error.reason}") from error
        sent_count += 1
        if index + 1 < len(messages):
            time.sleep(DISCORD_POST_PAUSE_SECONDS)
    return sent_count


def _gspread_call_with_retry(func, *args, **kwargs):
    """包裝 gspread API 呼叫，遇到 429 / 5xx 自動指數退避重試。"""
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


def _parse_optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)
    text = str(value).replace("%", "").replace(",", "").strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_existing_tracker_record(raw: Mapping[str, object]) -> dict[str, object]:
    """相容中英文欄位名稱，統一正規化為 SIGNAL_TRACKER_HEADERS 結構。"""
    scan_date = str(raw.get("執行日期") or raw.get("scan_date") or raw.get("date") or "").strip()
    data_date = str(raw.get("K線日期") or raw.get("date") or scan_date).strip()
    market = str(raw.get("市場別") or raw.get("market") or "").strip()
    industry = str(raw.get("產業類別") or raw.get("industry_category") or "").strip()
    stock_id = str(raw.get("股票代號") or raw.get("stock_id") or "").strip()
    stock_name = str(raw.get("股票名稱") or raw.get("stock_name") or "").strip()
    is_new = str(raw.get("當日新進") or raw.get("is_new_stock") or "").strip()
    streak_raw = raw.get("連續天數") if "連續天數" in raw else raw.get("streak_days", 0)
    try:
        streak_days = int(float(str(streak_raw).strip() or "0"))
    except ValueError:
        streak_days = 0
    daily_status = str(raw.get("每日執行狀況") or raw.get("daily_status") or "").strip()
    signal = str(raw.get("訊號類型") or raw.get("signal") or "").strip()

    close_val = _parse_optional_float(raw.get("收盤價") if "收盤價" in raw else raw.get("close"))
    ma17_val = _parse_optional_float(raw.get("17MA") if "17MA" in raw else raw.get("MA17"))
    ma45_val = _parse_optional_float(raw.get("45MA") if "45MA" in raw else raw.get("MA45"))
    ma117_val = _parse_optional_float(raw.get("117MA") if "117MA" in raw else raw.get("MA117"))
    ma189_val = _parse_optional_float(raw.get("189MA") if "189MA" in raw else raw.get("MA189"))
    gap_val = _parse_optional_float(
        raw.get("17與45MA差距(%)")
        if "17與45MA差距(%)" in raw
        else raw.get("gap_percent")
    )
    alignment = str(
        raw.get("均線排列檢核")
        or raw.get("ma_alignment")
        or ("45MA > 117MA > 189MA ✓" if stock_id and stock_id != "-" else "-")
    ).strip()
    updated_at = str(raw.get("更新時間") or raw.get("updated_at") or "").strip()

    return {
        "執行日期": scan_date,
        "K線日期": data_date,
        "市場別": market,
        "產業類別": industry,
        "股票代號": stock_id,
        "股票名稱": stock_name,
        "當日新進": is_new,
        "連續天數": streak_days,
        "每日執行狀況": daily_status,
        "訊號類型": signal,
        "收盤價": "" if close_val is None else round(close_val, 2),
        "17MA": "" if ma17_val is None else round(ma17_val, 2),
        "45MA": "" if ma45_val is None else round(ma45_val, 2),
        "117MA": "" if ma117_val is None else round(ma117_val, 2),
        "189MA": "" if ma189_val is None else round(ma189_val, 2),
        "17與45MA差距(%)": "" if gap_val is None else round(gap_val, 2),
        "均線排列檢核": alignment,
        "更新時間": updated_at,
    }


def build_signal_tracker_rows(
    signals_df: pd.DataFrame,
    existing_records: list[Mapping[str, object]] | None = None,
    as_of: date | None = None,
    updated_at: str | None = None,
    replace_date: bool = True,
) -> list[dict[str, object]]:
    """將當日 signals.csv 資料與歷史紀錄比對，標註『當日新進』、『連續天數』與『每日執行狀況』。"""
    scan_date_str = (as_of or _today_in_taipei()).isoformat()
    now_str = updated_at or datetime.now(TAIPEI_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    normalized_existing = [
        _normalize_existing_tracker_record(rec)
        for rec in (existing_records or [])
        if str(rec.get("執行日期") or rec.get("scan_date") or rec.get("date") or "").strip()
    ]

    prior_records = [
        rec for rec in normalized_existing if rec["執行日期"] != scan_date_str
    ]
    same_day_records = [
        rec
        for rec in normalized_existing
        if rec["執行日期"] == scan_date_str and rec["股票代號"] not in ("", "-")
    ]

    # 建立先前的日期 -> 股票代號索引（僅納入早於 scan_date_str 的有效股票紀錄）
    history_by_date: dict[str, dict[str, dict[str, object]]] = {}
    for rec in prior_records:
        rec_date = str(rec["執行日期"])
        rec_sid = str(rec["股票代號"])
        if rec_date < scan_date_str and rec_sid and rec_sid != "-":
            history_by_date.setdefault(rec_date, {})[rec_sid] = rec

    prev_dates_desc = sorted(history_by_date.keys(), reverse=True)
    latest_prev_map = history_by_date[prev_dates_desc[0]] if prev_dates_desc else {}

    # 若為單一市場/產業掃描 (replace_date=False)，保留同日其他市場/產業已寫入的股票並以新結果覆寫同股
    working_df = signals_df.copy() if signals_df is not None else pd.DataFrame()
    if not replace_date and same_day_records:
        new_ids = (
            set(working_df["stock_id"].astype(str).str.strip())
            if not working_df.empty and "stock_id" in working_df.columns
            else set()
        )
        retained_rows = []
        for rec in same_day_records:
            if str(rec["股票代號"]) not in new_ids:
                retained_rows.append(
                    {
                        "market": rec["市場別"],
                        "industry_category": rec["產業類別"],
                        "stock_id": rec["股票代號"],
                        "stock_name": rec["股票名稱"],
                        "date": rec["K線日期"],
                        "close": rec["收盤價"],
                        "MA17": rec["17MA"],
                        "MA45": rec["45MA"],
                        "MA117": rec["117MA"],
                        "MA189": rec["189MA"],
                        "gap_percent": rec["17與45MA差距(%)"],
                        "signal": rec["訊號類型"],
                    }
                )
        if retained_rows:
            retained_df = pd.DataFrame(retained_rows)
            working_df = (
                pd.concat([retained_df, working_df], ignore_index=True)
                if not working_df.empty
                else retained_df
            )

    today_rows: list[dict[str, object]] = []
    if not working_df.empty:
        working_df = working_df.drop_duplicates(subset=["stock_id"], keep="last")
        for _, row in working_df.iterrows():
            stock_id = str(row.get("stock_id", "")).strip()
            if not stock_id or stock_id == "-":
                continue
            stock_name = str(row.get("stock_name", "")).strip()
            market = str(row.get("market", "")).strip()
            industry = str(row.get("industry_category", "")).strip()
            data_date = str(row.get("date", scan_date_str)).strip()
            signal = str(row.get("signal", "")).strip()

            close_val = round(float(row["close"]), 2)
            ma17_val = round(float(row["MA17"]), 2)
            ma45_val = round(float(row["MA45"]), 2)
            ma117_val = round(float(row["MA117"]), 2)
            ma189_val = round(float(row["MA189"]), 2)
            gap_val = round(float(row["gap_percent"]), 2)

            streak = 1
            for prev_d in prev_dates_desc:
                if stock_id in history_by_date[prev_d]:
                    streak += 1
                else:
                    break

            is_new = stock_id not in latest_prev_map
            if is_new:
                is_new_label = "🆕 是（新進榜）"
                daily_status = f"🆕 當日新產生｜{signal}（差距 {gap_val:.2f}%）"
            else:
                is_new_label = "🔄 否（持續符合）"
                prev_rec = latest_prev_map[stock_id]
                prev_signal = str(prev_rec.get("訊號類型", "")).strip()
                prev_gap = _parse_optional_float(prev_rec.get("17與45MA差距(%)"))
                if prev_signal and prev_signal != signal:
                    if signal == "已超過":
                        daily_status = (
                            f"🔥 轉強突破（接近 ➔ 已超過｜連 {streak} 日｜差距 {gap_val:.2f}%）"
                        )
                    else:
                        daily_status = (
                            f"⚠️ 訊號收斂（已超過 ➔ 接近｜連 {streak} 日｜差距 {gap_val:.2f}%）"
                        )
                elif prev_gap is not None:
                    daily_status = (
                        f"🔄 持續符合（連 {streak} 日｜前次 {prev_gap:.2f}% ➔ 本次 {gap_val:.2f}%）"
                    )
                else:
                    daily_status = f"🔄 持續符合（連 {streak} 日｜本次差距 {gap_val:.2f}%）"

            today_rows.append(
                {
                    "執行日期": scan_date_str,
                    "K線日期": data_date,
                    "市場別": market,
                    "產業類別": industry,
                    "股票代號": stock_id,
                    "股票名稱": stock_name,
                    "當日新進": is_new_label,
                    "連續天數": streak,
                    "每日執行狀況": daily_status,
                    "訊號類型": signal,
                    "收盤價": close_val,
                    "17MA": ma17_val,
                    "45MA": ma45_val,
                    "117MA": ma117_val,
                    "189MA": ma189_val,
                    "17與45MA差距(%)": gap_val,
                    "均線排列檢核": "45MA > 117MA > 189MA ✓",
                    "更新時間": now_str,
                }
            )
    else:
        today_rows.append(
            {
                "執行日期": scan_date_str,
                "K線日期": scan_date_str,
                "市場別": "-",
                "產業類別": "-",
                "股票代號": "-",
                "股票名稱": "（今日無符合條件股票）",
                "當日新進": "-",
                "連續天數": 0,
                "每日執行狀況": "✅ 每日掃描執行完成，今日無符合條件之個股",
                "訊號類型": "-",
                "收盤價": "",
                "17MA": "",
                "45MA": "",
                "117MA": "",
                "189MA": "",
                "17與45MA差距(%)": "",
                "均線排列檢核": "-",
                "更新時間": now_str,
            }
        )

    all_records = today_rows + prior_records

    def _sort_key(rec: dict[str, object]) -> tuple[object, ...]:
        # 日期由新到舊排序（負整數轉換 YYYYMMDD）
        date_str = str(rec.get("執行日期", ""))
        try:
            date_num = -int(date_str.replace("-", ""))
        except ValueError:
            date_num = 0
        is_new_rank = (
            0
            if "🆕" in str(rec.get("當日新進", ""))
            else (2 if str(rec.get("股票代號", "")) == "-" else 1)
        )
        signal_rank = 0 if str(rec.get("訊號類型", "")) == "已超過" else 1
        gap_num = _parse_optional_float(rec.get("17與45MA差距(%)"))
        return (
            date_num,
            is_new_rank,
            signal_rank,
            999.0 if gap_num is None else gap_num,
            str(rec.get("股票代號", "")),
        )

    all_records.sort(key=_sort_key)
    return all_records


def _load_local_prior_signals(
    daily_directory: Path | None, before_date: date
) -> list[dict[str, object]]:
    """若工作表尚無歷史資料，從本地 daily/*_signals.csv 載入符合新均線邏輯的歷史訊號以銜接連續天數。"""
    if daily_directory is None or not daily_directory.is_dir():
        return []

    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_signals\.csv$")
    matched_files: list[tuple[date, Path]] = []
    for path in daily_directory.iterdir():
        m = pattern.fullmatch(path.name)
        if not m or not path.is_file():
            continue
        file_date = date.fromisoformat(m.group(1))
        if file_date < before_date:
            matched_files.append((file_date, path))

    matched_files.sort(key=lambda item: item[0])
    seeded_records: list[dict[str, object]] = []
    for file_date, csv_path in matched_files:
        try:
            df = pd.read_csv(csv_path, dtype={"stock_id": str})
        except Exception:
            continue
        if not df.empty:
            # 確保歷史 CSV 亦符合 45MA > 117MA > 189MA
            for col in ("MA17", "MA45", "MA117", "MA189"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["MA17", "MA45", "MA117", "MA189"])
            df = df.loc[(df["MA45"] > df["MA117"]) & (df["MA117"] > df["MA189"])].copy()
        seeded_records = build_signal_tracker_rows(
            df,
            existing_records=seeded_records,
            as_of=file_date,
            updated_at=f"{file_date.isoformat()} 00:00:00",
            replace_date=True,
        )
    return seeded_records


def _format_signal_tracker_worksheet(
    spreadsheet: gspread.Spreadsheet,
    worksheet: gspread.Worksheet,
    records: list[dict[str, object]],
) -> None:
    """為 MA_Signal_Tracker 套用凍結表頭、自動篩選、欄寬、數值格式與當日新進醒目底色。"""
    sheet_id = worksheet.id
    num_cols = len(SIGNAL_TRACKER_HEADERS)
    total_rows = max(len(records) + 1, 2)

    column_widths = [
        105,  # 執行日期
        105,  # K線日期
        75,   # 市場別
        130,  # 產業類別
        85,   # 股票代號
        105,  # 股票名稱
        135,  # 當日新進
        85,   # 連續天數
        320,  # 每日執行狀況
        145,  # 訊號類型
        95,   # 收盤價
        95,   # 17MA
        95,   # 45MA
        95,   # 117MA
        95,   # 189MA
        130,  # 17與45MA差距(%)
        165,  # 均線排列檢核
        155,  # 更新時間
    ]

    requests_payload: list[dict[str, object]] = [
        # 1. 凍結第一列 (表頭)
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # 2. 啟用表頭篩選器
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": total_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols,
                    }
                }
            }
        },
        # 3. 表頭深海藍底白字粗體置中
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            "bold": True,
                            "fontSize": 10,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
            }
        },
        # 4. 清除資料列預設背景色並設定垂直置中與字體大小
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": total_rows,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {
                            "foregroundColor": {"red": 0.15, "green": 0.15, "blue": 0.15},
                            "bold": False,
                            "fontSize": 10,
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,verticalAlignment,textFormat)",
            }
        },
        # 5. 收盤價與各均線欄位 (index 10..15) 數值格式 #,##0.00
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": total_rows,
                    "startColumnIndex": 10,
                    "endColumnIndex": 15,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"},
                        "horizontalAlignment": "RIGHT",
                    }
                },
                "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
            }
        },
        # 6. 差距百分比欄位 (index 15..16) 數值格式 0.00
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": total_rows,
                    "startColumnIndex": 15,
                    "endColumnIndex": 16,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "0.00"},
                        "horizontalAlignment": "RIGHT",
                    }
                },
                "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
            }
        },
    ]

    # 設定各欄寬度
    for col_idx, px in enumerate(column_widths[:num_cols]):
        requests_payload.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": col_idx,
                        "endIndex": col_idx + 1,
                    },
                    "properties": {"pixelSize": px},
                    "fields": "pixelSize",
                }
            }
        )

    # 依每列狀態套用醒目視覺標記（當日新進整列淺綠底＋綠字粗體；已超過暖橘底；接近淺藍底）
    for idx, rec in enumerate(records, start=1):
        is_new_str = str(rec.get("當日新進", ""))
        signal_str = str(rec.get("訊號類型", ""))
        if "🆕" in is_new_str:
            # 整列柔和薄荷綠底色，一眼看出是當日新產生的股票
            requests_payload.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx,
                            "endRowIndex": idx + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": num_cols,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.91, "green": 0.97, "blue": 0.92},
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )
            # 當日新進欄位加粗深綠字
            requests_payload.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx,
                            "endRowIndex": idx + 1,
                            "startColumnIndex": 6,
                            "endColumnIndex": 7,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.80, "green": 0.94, "blue": 0.83},
                                "horizontalAlignment": "CENTER",
                                "textFormat": {
                                    "foregroundColor": {"red": 0.08, "green": 0.45, "blue": 0.20},
                                    "bold": True,
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
                    }
                }
            )
        if signal_str == "已超過":
            requests_payload.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx,
                            "endRowIndex": idx + 1,
                            "startColumnIndex": 9,
                            "endColumnIndex": 10,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 1.0, "green": 0.91, "blue": 0.84},
                                "horizontalAlignment": "CENTER",
                                "textFormat": {
                                    "foregroundColor": {"red": 0.72, "green": 0.25, "blue": 0.05},
                                    "bold": True,
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
                    }
                }
            )
        elif "接近" in signal_str:
            requests_payload.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx,
                            "endRowIndex": idx + 1,
                            "startColumnIndex": 9,
                            "endColumnIndex": 10,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.88, "green": 0.94, "blue": 1.0},
                                "horizontalAlignment": "CENTER",
                                "textFormat": {
                                    "foregroundColor": {"red": 0.10, "green": 0.36, "blue": 0.68},
                                    "bold": True,
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
                    }
                }
            )

    _gspread_call_with_retry(spreadsheet.batch_update, {"requests": requests_payload})


def sync_signals_to_gsheet(
    signals_df: pd.DataFrame,
    *,
    as_of: date | None = None,
    spreadsheet_id: str | None = None,
    credentials_path: str | None = None,
    tab_name: str = SIGNAL_TRACKER_SHEET_NAME,
    daily_directory: Path | None = None,
    replace_date: bool = True,
) -> int:
    """使用現有 Service Account JSON 將 signals.csv 報表同步至 Google Sheets MA_Signal_Tracker。"""
    resolved_credentials = (
        credentials_path
        if credentials_path is not None
        else os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    )
    resolved_spreadsheet_id = (
        spreadsheet_id
        if spreadsheet_id is not None
        else os.getenv("GSHEET_SPREADSHEET_ID", "").strip()
    )
    if not resolved_credentials:
        raise ValueError("GOOGLE_APPLICATION_CREDENTIALS 未設定，無法寫入 Google Sheets。")
    if not resolved_spreadsheet_id:
        raise ValueError("GSHEET_SPREADSHEET_ID 未設定，無法寫入 Google Sheets。")

    today = as_of or _today_in_taipei()
    gc = gspread.service_account(filename=resolved_credentials)
    spreadsheet = _gspread_call_with_retry(gc.open_by_key, resolved_spreadsheet_id)

    worksheets = _gspread_call_with_retry(spreadsheet.worksheets)
    existing_tabs = {ws.title: ws for ws in worksheets}

    if tab_name not in existing_tabs:
        worksheet = _gspread_call_with_retry(
            spreadsheet.add_worksheet,
            title=tab_name,
            rows=1000,
            cols=len(SIGNAL_TRACKER_HEADERS),
        )
        _gspread_call_with_retry(worksheet.append_row, SIGNAL_TRACKER_HEADERS)
        raw_rows = [SIGNAL_TRACKER_HEADERS]
    else:
        worksheet = existing_tabs[tab_name]
        raw_rows = _gspread_call_with_retry(
            worksheet.get_all_values,
            value_render_option=gspread.utils.ValueRenderOption.unformatted,
            date_time_render_option=gspread.utils.DateTimeOption.formatted_string,
        )

    existing_records: list[dict[str, object]] = []
    if raw_rows and len(raw_rows) > 1:
        headers = [str(h).strip() for h in raw_rows[0]]
        for row in raw_rows[1:]:
            padded = list(row) + [""] * (len(headers) - len(row))
            record = {headers[i]: padded[i] for i in range(len(headers)) if headers[i]}
            if any(str(v).strip() for v in record.values()):
                existing_records.append(record)

    # 若工作表內尚無早於今日的歷史紀錄，自動從本地 daily/*_signals.csv 補齊歷史基準
    has_prior_history = any(
        str(rec.get("執行日期") or rec.get("scan_date") or "").strip() < today.isoformat()
        for rec in existing_records
        if str(rec.get("執行日期") or rec.get("scan_date") or "").strip()
    )
    if not has_prior_history and daily_directory is not None:
        seeded = _load_local_prior_signals(daily_directory, before_date=today)
        if seeded:
            existing_records = seeded + existing_records

    merged_records = build_signal_tracker_rows(
        signals_df,
        existing_records=existing_records,
        as_of=today,
        replace_date=replace_date,
    )

    matrix: list[list[object]] = [SIGNAL_TRACKER_HEADERS]
    for rec in merged_records:
        matrix.append([rec.get(col, "") for col in SIGNAL_TRACKER_HEADERS])

    required_rows = max(len(matrix) + 50, 200)
    required_cols = len(SIGNAL_TRACKER_HEADERS)
    if worksheet.row_count < required_rows or worksheet.col_count < required_cols:
        _gspread_call_with_retry(
            worksheet.resize,
            rows=max(worksheet.row_count, required_rows),
            cols=max(worksheet.col_count, required_cols),
        )

    _gspread_call_with_retry(worksheet.clear)
    end_cell = gspread.utils.rowcol_to_a1(len(matrix), required_cols)
    _gspread_call_with_retry(
        worksheet.update,
        range_name=f"A1:{end_cell}",
        values=matrix,
        value_input_option="USER_ENTERED",
    )

    try:
        _format_signal_tracker_worksheet(spreadsheet, worksheet, merged_records)
    except Exception:
        logger.warning("Failed to apply visual formatting to %s", tab_name, exc_info=True)

    return len(merged_records)


def _fetch_stock_prices(
    loader: DataLoader, stock_id: str, start_date: str, end_date: str
) -> pd.DataFrame:
    return loader.taiwan_stock_daily(
        stock_id=stock_id,
        start_date=start_date,
        end_date=end_date,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _output_stem(market: str, industry: str) -> str:
    safe_industry = re.sub(r"[^\w.-]+", "_", industry, flags=re.UNICODE).strip("_")
    timestamp = datetime.now(TAIPEI_TIMEZONE).strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{MARKET_CODES[market]}_{safe_industry or 'industry'}"


def scan_industry(
    loader: DataLoader,
    stocks: pd.DataFrame,
    market: str,
    industry: str,
    output_dir: Path,
    as_of: date | None = None,
    request_pause_seconds: float | None = REQUEST_PAUSE_SECONDS,
) -> tuple[Path, Path, Path, Path | None, int, int]:
    today = as_of or _today_in_taipei()
    report_start_date = (
        pd.Timestamp(today) - pd.DateOffset(months=REPORT_HISTORY_MONTHS)
    ).strftime("%Y-%m-%d")
    fetch_start_date = (
        pd.Timestamp(today) - pd.DateOffset(months=INDICATOR_HISTORY_MONTHS)
    ).strftime("%Y-%m-%d")
    end_date = today.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _output_stem(market, industry)

    all_prices: list[pd.DataFrame] = []
    signals: list[dict[str, object]] = []
    alignment_signals: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    stock_count = len(stocks)

    for index, stock in enumerate(stocks.itertuples(index=False), start=1):
        stock_id = str(stock.stock_id)
        try:
            prices = _fetch_stock_prices(
                loader, stock_id, fetch_start_date, end_date
            )
            if prices.empty:
                raise ValueError("FinMind 未回傳日線資料")
            analyzed = add_moving_averages(prices)
            analyzed = analyzed.loc[
                analyzed["date"] >= pd.Timestamp(report_start_date)
            ].copy()
            signal = classify_latest_signal(analyzed)
            alignment_signal = classify_latest_alignment_signal(analyzed)
            if analyzed["MA189"].notna().sum() == 0:
                raise ValueError("近一年有效日線不足 189 個交易日，無法計算 MA189")

            analyzed["stock_id"] = stock_id
            analyzed["stock_name"] = str(stock.stock_name)
            analyzed["industry_category"] = industry
            analyzed["market"] = market
            metadata_columns = [
                "stock_id",
                "stock_name",
                "industry_category",
                "market",
            ]
            analyzed = analyzed.loc[
                :, metadata_columns + [
                    column for column in analyzed.columns
                    if column not in metadata_columns
                ]
            ]
            all_prices.append(analyzed)
            if signal is not None:
                signals.append(
                    {
                        "market": market,
                        "industry_category": industry,
                        "stock_id": stock_id,
                        "stock_name": str(stock.stock_name),
                        **signal,
                    }
                )
            if alignment_signal is not None:
                alignment_signals.append(
                    {
                        "market": market,
                        "industry_category": industry,
                        "stock_id": stock_id,
                        "stock_name": str(stock.stock_name),
                        **alignment_signal,
                    }
                )
            print(f"[{index}/{stock_count}] {stock_id} {stock.stock_name} 完成")
        except Exception as error:
            logger.exception("FinMind scan failed for stock %s", stock_id)
            failures.append(
                {
                    "stock_id": stock_id,
                    "stock_name": str(stock.stock_name),
                    "error": str(error),
                }
            )
            print(
                f"[{index}/{stock_count}] {stock_id} 失敗：{error}",
                file=sys.stderr,
            )
        if index < stock_count and request_pause_seconds is not None:
            time.sleep(request_pause_seconds)

    price_path = output_dir / f"prices_{stem}.csv"
    signal_path = output_dir / f"signals_{stem}.csv"
    alignment_path = output_dir / f"alignment_signals_{stem}.csv"
    failure_path = output_dir / f"failures_{stem}.csv" if failures else None

    price_frame = (
        pd.concat(all_prices, ignore_index=True)
        if all_prices
        else pd.DataFrame(
            columns=[
                "stock_id",
                "stock_name",
                "industry_category",
                "market",
                "date",
                "close",
                *(f"MA{period}" for period in MOVING_AVERAGE_PERIODS),
            ]
        )
    )
    signal_frame = pd.DataFrame(
        signals,
        columns=[
            "market",
            "industry_category",
            "stock_id",
            "stock_name",
            "date",
            "close",
            "MA17",
            "MA45",
            "MA117",
            "MA189",
            "gap_percent",
            "signal",
        ],
    )
    alignment_frame = pd.DataFrame(
        alignment_signals,
        columns=[
            "market",
            "industry_category",
            "stock_id",
            "stock_name",
            "date",
            "close",
            "MA17",
            "MA45",
            "MA117",
            "MA189",
            "MA17_MA45_gap_percent",
            "signal",
        ],
    )
    _atomic_write_csv(price_frame, price_path)
    _atomic_write_csv(signal_frame, signal_path)
    _atomic_write_csv(alignment_frame, alignment_path)
    if failure_path is not None:
        _atomic_write_csv(pd.DataFrame(failures), failure_path)

    return (
        price_path,
        signal_path,
        alignment_path,
        failure_path,
        len(signals),
        len(alignment_signals),
    )


def load_scan_config(config_path: Path) -> dict[str, object]:
    try:
        with config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"無法讀取掃描設定檔 {config_path}: {error}") from error
    if not isinstance(config, dict):
        raise ValueError("掃描設定檔最上層必須是 JSON object。")

    markets = config.get("markets")
    industries = config.get("industries")
    excluded_stocks = config.get("excluded_stocks")
    excluded_categories = config.get("excluded_industry_categories")
    request_limit = config.get("max_requests_per_hour")
    request_interval = config.get("min_request_interval_seconds")
    retention_days = config.get("local_retention_days")
    if not isinstance(markets, list) or not markets:
        raise ValueError("設定 markets 必須是非空市場清單。")
    unknown_markets = sorted(set(markets).difference(MARKET_TYPES))
    if unknown_markets:
        raise ValueError(f"設定包含不支援的市場：{', '.join(unknown_markets)}")
    if not isinstance(industries, list) or not industries or not all(
        isinstance(item, str) and item.strip() for item in industries
    ):
        raise ValueError("設定 industries 必須是非空產業名稱清單。")
    if not isinstance(excluded_stocks, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("stock_id"), str)
        and item["stock_id"].isascii()
        and item["stock_id"].isalnum()
        and isinstance(item.get("stock_name"), str)
        and isinstance(item.get("reason"), str)
        for item in excluded_stocks
    ):
        raise ValueError(
            "設定 excluded_stocks 必須是包含 stock_id、stock_name、reason 的清單。"
        )
    if not isinstance(excluded_categories, list) or not all(
        isinstance(item, str) for item in excluded_categories
    ):
        raise ValueError(
            "設定 excluded_industry_categories 必須是產業類別字串清單。"
        )
    if not isinstance(request_limit, int) or isinstance(request_limit, bool):
        raise ValueError("設定 max_requests_per_hour 必須是整數。")
    if not isinstance(request_interval, (int, float)) or isinstance(
        request_interval, bool
    ):
        raise ValueError("設定 min_request_interval_seconds 必須是數字。")
    if not isinstance(retention_days, int) or retention_days < 1:
        raise ValueError("設定 local_retention_days 必須是正整數。")
    if not isinstance(config.get("daily_start_time"), str):
        raise ValueError("設定 daily_start_time 必須是 HH:MM 格式字串。")
    if config.get("timezone") != "Asia/Taipei":
        raise ValueError("目前排程設定 timezone 必須是 Asia/Taipei。")
    if not isinstance(config.get("post_alignment_to_discord"), bool):
        raise ValueError("設定 post_alignment_to_discord 必須是布林值。")
    if not isinstance(config.get("stock_channel_id"), str):
        raise ValueError("設定 stock_channel_id 必須是字串。")
    if config["post_alignment_to_discord"] and not config["stock_channel_id"].isdigit():
        raise ValueError(
            "post_alignment_to_discord 為 true 時，stock_channel_id 必須是數字 ID。"
        )
    if "sync_signals_to_gsheet" in config and not isinstance(
        config.get("sync_signals_to_gsheet"), bool
    ):
        raise ValueError("設定 sync_signals_to_gsheet 必須是布林值。")
    if "gsheet_tab_name" in config and (
        not isinstance(config.get("gsheet_tab_name"), str)
        or not str(config.get("gsheet_tab_name")).strip()
    ):
        raise ValueError("設定 gsheet_tab_name 必須是非空字串。")
    if not isinstance(config.get("output_directory"), str):
        raise ValueError("設定 output_directory 必須是路徑字串。")
    if not 1 <= request_limit <= 540:
        raise ValueError("max_requests_per_hour 必須介於 1 到 540。")
    if float(request_interval) < 3600 / request_limit:
        raise ValueError(
            "min_request_interval_seconds 不可小於 "
            "3600 / max_requests_per_hour。"
        )
    try:
        datetime.strptime(config["daily_start_time"], "%H:%M")
    except ValueError as error:
        raise ValueError("daily_start_time 必須符合 HH:MM 格式。") from error
    if config["daily_start_time"] != "00:00":
        raise ValueError(
            "daily_start_time 目前必須是 00:00，且需與 deploy/finmind-scan.timer 相同。"
        )
    return config


def _configured_universe(
    stock_info: pd.DataFrame, config: Mapping[str, object]
) -> dict[tuple[str, str], pd.DataFrame]:
    _validate_stock_info(stock_info)
    excluded_ids = {
        item["stock_id"] for item in config["excluded_stocks"]
    }
    excluded_categories = EXCLUDED_INDUSTRY_CATEGORIES.union(
        config["excluded_industry_categories"]
    )
    configured_industries = config["industries"]
    configured_markets = config["markets"]
    available_industries = set(
        stock_info["industry_category"].dropna().astype(str)
    )
    missing_industries = sorted(set(configured_industries).difference(available_industries))
    if missing_industries:
        raise ValueError(
            "FinMind 股票清單不存在設定的產業："
            + "、".join(missing_industries)
        )

    universe: dict[tuple[str, str], pd.DataFrame] = {}
    for market in configured_markets:
        market_stocks = _filter_market(stock_info, market)
        market_stocks = market_stocks.loc[
            ~market_stocks["industry_category"].isin(excluded_categories)
            & ~market_stocks["stock_id"].astype(str).isin(excluded_ids)
        ]
        for industry in configured_industries:
            selected = market_stocks.loc[
                market_stocks["industry_category"].astype(str).str.strip()
                == industry
            ].copy()
            universe[(market, industry)] = (
                selected.drop_duplicates(subset=["stock_id"])
                .reset_index(drop=True)
            )
    return universe


def _combine_csvs(paths: list[Path], columns: list[str]) -> pd.DataFrame:
    frames = [pd.read_csv(path, dtype={"stock_id": str}) for path in paths]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)


def _cleanup_old_daily_reports(
    daily_directory: Path, as_of: date, retention_days: int
) -> None:
    cutoff = as_of - timedelta(days=retention_days)
    filename_pattern = re.compile(
        r"^\d{4}-\d{2}-\d{2}_(prices|signals|alignment_signals|failures)\.csv$"
    )
    for path in daily_directory.iterdir():
        match = filename_pattern.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        file_date = date.fromisoformat(path.name[:10])
        if file_date < cutoff:
            path.unlink()


def scan_configured_universe(
    loader: DataLoader,
    stock_info: pd.DataFrame,
    config: Mapping[str, object],
    output_dir: Path | None = None,
    as_of: date | None = None,
    request_limiter: FinMindRequestLimiter | None = None,
) -> tuple[dict[str, Path], int, int, int]:
    today = as_of or _today_in_taipei()
    output_root = output_dir or Path(config["output_directory"])
    daily_directory = output_root / "daily"
    daily_directory.mkdir(parents=True, exist_ok=True)
    limiter = request_limiter or FinMindRequestLimiter(
        output_root / ".state" / "finmind_request_history.sqlite3",
        int(config["max_requests_per_hour"]),
        float(config["min_request_interval_seconds"]),
    )
    universe = _configured_universe(stock_info, config)

    all_prices: list[Path] = []
    all_signals: list[Path] = []
    all_alignments: list[Path] = []
    all_failures: list[Path] = []
    failed_groups: list[str] = []

    with tempfile.TemporaryDirectory(prefix="finmind-daily-") as temp_path:
        temporary_output = Path(temp_path)
        with _rate_limit_finmind_http_requests(limiter):
            for (market, industry), stocks in universe.items():
                if stocks.empty:
                    logger.warning("No eligible stocks for %s / %s", market, industry)
                    continue
                try:
                    paths = scan_industry(
                        loader=loader,
                        stocks=stocks,
                        market=market,
                        industry=industry,
                        output_dir=temporary_output,
                        as_of=today,
                        request_pause_seconds=None,
                    )
                except Exception:
                    logger.exception("Failed to scan %s / %s", market, industry)
                    failed_groups.append(f"{market}/{industry}")
                    continue
                all_prices.append(paths[0])
                all_signals.append(paths[1])
                all_alignments.append(paths[2])
                if paths[3] is not None:
                    all_failures.append(paths[3])

        report_files = {
            "prices": _combine_csvs(
                all_prices,
                [
                    "stock_id",
                    "stock_name",
                    "industry_category",
                    "market",
                    "date",
                    "close",
                    *(f"MA{period}" for period in MOVING_AVERAGE_PERIODS),
                ],
            ),
            "signals": _combine_csvs(
                all_signals,
                [
                    "market",
                    "industry_category",
                    "stock_id",
                    "stock_name",
                    "date",
                    "close",
                    "MA17",
                    "MA45",
                    "MA117",
                    "MA189",
                    "gap_percent",
                    "signal",
                ],
            ),
            "alignment_signals": _combine_csvs(
                all_alignments,
                [
                    "market",
                    "industry_category",
                    "stock_id",
                    "stock_name",
                    "date",
                    "close",
                    "MA17",
                    "MA45",
                    "MA117",
                    "MA189",
                    "MA17_MA45_gap_percent",
                    "signal",
                ],
            ),
            "failures": _combine_csvs(
                all_failures, ["stock_id", "stock_name", "error"]
            ),
        }
        report_paths = {
            name: daily_directory / f"{today.isoformat()}_{name}.csv"
            for name in report_files
        }
        for name, frame in report_files.items():
            _atomic_write_csv(frame, report_paths[name])

    failures = report_files["failures"]
    signals = report_files["signals"]
    alignment_signals = report_files["alignment_signals"]

    if config.get("sync_signals_to_gsheet"):
        tab_name = str(config.get("gsheet_tab_name") or SIGNAL_TRACKER_SHEET_NAME)
        sync_signals_to_gsheet(
            signals,
            as_of=today,
            tab_name=tab_name,
            daily_directory=daily_directory,
            replace_date=True,
        )

    if config["post_alignment_to_discord"]:
        bot_token = (
            os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or ""
        ).strip()
        post_alignment_report_to_discord(
            alignment_signals,
            channel_id=str(config["stock_channel_id"]),
            bot_token=bot_token,
            as_of=today,
            failed_count=len(failures) + len(failed_groups),
        )

    _cleanup_old_daily_reports(
        daily_directory, today, int(config["local_retention_days"])
    )
    return report_paths, len(failures), len(alignment_signals), len(failed_groups)


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 FinMind 列出台股市場／產業，並掃描 MA45 與 MA17。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-markets", help="列出上市、上櫃、興櫃與其他類別")
    all_parser = subparsers.add_parser(
        "scan-all", help="依 JSON 設定掃描多個市場及產業並產生日報"
    )
    all_parser.add_argument(
        "--config",
        type=Path,
        default=Path("finmind_scanner.json"),
        help="JSON 設定檔路徑（預設：finmind_scanner.json）",
    )
    all_parser.add_argument(
        "--output-dir",
        type=Path,
        help="覆寫設定檔的 output_directory",
    )

    industries_parser = subparsers.add_parser(
        "list-industries", help="列出台灣產業清單與各產業股票數"
    )
    industries_parser.add_argument(
        "--market",
        choices=[*MARKET_TYPES, "全部"],
        default="全部",
        help="只列出指定市場的產業，預設列出所有市場",
    )

    scan_parser = subparsers.add_parser("scan", help="掃描指定市場與產業")
    scan_parser.add_argument("--market", choices=MARKET_TYPES, required=True)
    scan_parser.add_argument("--industry", required=True, help="完整產業名稱，例如半導體業")
    scan_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("finmind_reports"),
        help="CSV 輸出資料夾（預設：finmind_reports）",
    )
    scan_parser.add_argument(
        "--post-to-discord",
        action="store_true",
        help="將 MA17/MA45 對 MA117/MA189 特別清單傳至 STOCK_CHANNEL_ID",
    )
    scan_parser.add_argument(
        "--channel-id",
        default=os.getenv("STOCK_CHANNEL_ID", "").strip(),
        help="Discord 頻道 ID（預設讀取 STOCK_CHANNEL_ID 環境變數）",
    )
    scan_parser.add_argument(
        "--no-sync-gsheet",
        action="store_true",
        help="停用將 signals.csv 同步寫入 Google Sheets MA_Signal_Tracker",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
    load_dotenv()
    args = _make_parser().parse_args(argv)
    api_token = os.getenv("FINMIND_API_TOKEN", "").strip()

    try:
        if args.command == "scan-all":
            config = load_scan_config(args.config)
            output_dir = args.output_dir or Path(config["output_directory"])
            request_limiter = FinMindRequestLimiter(
                output_dir / ".state" / "finmind_request_history.sqlite3",
                int(config["max_requests_per_hour"]),
                float(config["min_request_interval_seconds"]),
            )
            with _rate_limit_finmind_http_requests(request_limiter):
                loader = DataLoader(token=api_token)
                stock_info = loader.taiwan_stock_info(
                    timeout=REQUEST_TIMEOUT_SECONDS
                )
            if stock_info.empty:
                raise RuntimeError("FinMind 股票總覽沒有回傳任何資料。")
            report_paths, failure_count, alignment_count, failed_groups = (
                scan_configured_universe(
                    loader=loader,
                    stock_info=stock_info,
                    config=config,
                    output_dir=output_dir,
                    request_limiter=request_limiter,
                )
            )
            for report_name, path in report_paths.items():
                print(f"{report_name}: {path}")
            print(
                f"均線排列特別訊號 {alignment_count} 檔；"
                f"股票資料失敗 {failure_count} 檔；"
                f"市場／產業批次失敗 {failed_groups} 組。"
            )
            return 1 if failure_count or failed_groups else 0

        loader = DataLoader(token=api_token)
        stock_info = loader.taiwan_stock_info(timeout=REQUEST_TIMEOUT_SECONDS)
        if stock_info.empty:
            raise RuntimeError("FinMind 股票總覽沒有回傳任何資料。")

        if args.command == "list-markets":
            print(list_markets(stock_info).to_string(index=False))
            return 0
        if args.command == "list-industries":
            market = None if args.market == "全部" else args.market
            print(list_industries(stock_info, market).to_string(index=False))
            return 0

        stocks = select_stocks(stock_info, args.market, args.industry)
        print(
            f"開始掃描：{args.market} / {args.industry}，"
            f"{len(stocks)} 檔；下載近 {INDICATOR_HISTORY_MONTHS} 個月以計算均線，"
            f"輸出近 {REPORT_HISTORY_MONTHS} 個月，"
            f"每檔間隔 {REQUEST_PAUSE_SECONDS:g} 秒"
        )
        (
            price_path,
            signal_path,
            alignment_path,
            failure_path,
            signal_count,
            alignment_count,
        ) = scan_industry(
            loader=loader,
            stocks=stocks,
            market=args.market,
            industry=args.industry,
            output_dir=args.output_dir,
        )
        print(
            f"\n符合 MA45 已超過或距 MA17 <= 1% 且 MA45 > MA117 > MA189：{signal_count} 檔"
        )
        signals_df = pd.read_csv(signal_path, dtype={"stock_id": str})
        if signal_count:
            print(signals_df.to_string(index=False))
        print(
            f"\n符合均線排列訊號（MA45 > MA117 > MA189）："
            f"{alignment_count} 檔"
        )
        if alignment_count:
            print(pd.read_csv(alignment_path).to_string(index=False))
        print(f"\n近六個月交易資料（均線使用近一年計算）：{price_path}")
        print(f"訊號清單：{signal_path}")
        print(f"均線排列特別清單：{alignment_path}")
        if (
            not args.no_sync_gsheet
            and os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            and os.getenv("GSHEET_SPREADSHEET_ID", "").strip()
        ):
            synced_rows = sync_signals_to_gsheet(
                signals_df,
                daily_directory=args.output_dir / "daily",
                replace_date=False,
            )
            print(
                f"已將 signals 報表同步寫入 Google Sheets [{SIGNAL_TRACKER_SHEET_NAME}]（共 {synced_rows} 筆紀錄）。"
            )
        failures = pd.read_csv(failure_path) if failure_path is not None else None
        if args.post_to_discord:
            bot_token = (
                os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or ""
            ).strip()
            sent_count = post_alignment_report_to_discord(
                pd.read_csv(alignment_path, dtype={"stock_id": str}),
                channel_id=args.channel_id,
                bot_token=bot_token,
                failed_count=0 if failures is None else len(failures),
            )
            print(f"已將均線排列特別清單傳送至 Discord（{sent_count} 則訊息）。")
        if failure_path is not None:
            print(f"失敗明細（{len(failures)} 檔）：{failure_path}")
            return 1
        return 0
    except Exception:
        logger.exception("FinMind stock scan failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
