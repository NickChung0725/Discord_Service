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
MOVING_AVERAGE_PERIODS = (3, 17, 45, 117, 189)
ALIGNMENT_MIN_GAP_PERCENT = -1.5
ALIGNMENT_MAX_GAP_PERCENT = 2.5
NEAR_CROSS_PERCENT = ALIGNMENT_MAX_GAP_PERCENT
REQUEST_PAUSE_SECONDS = 6.2
REQUEST_TIMEOUT_SECONDS = 30
INDICATOR_HISTORY_MONTHS = 12
REPORT_HISTORY_MONTHS = 6
DISCORD_API_BASE_URL = "https://discord.com/api/v10"
TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
DISCORD_POST_PAUSE_SECONDS = 1.0
SIGNAL_TRACKER_SHEET_NAME = "MA_Signal_Tracker"
SIGNAL_TRACKER_HEADERS = list(SHEET_SCHEMAS[SIGNAL_TRACKER_SHEET_NAME])
REBOUND_WATCHLIST_SHEET_NAME = "Rebound_Watchlist"
REBOUND_WATCHLIST_HEADERS = list(SHEET_SCHEMAS[REBOUND_WATCHLIST_SHEET_NAME])
REBOUND_SUPPORT_MIN_PCT = -1.5
REBOUND_SUPPORT_MAX_PCT = 2.5
REBOUND_MIN_RETURN_TO_MA17_PCT = 5.0
REBOUND_MIN_CLOSE_STRENGTH = 0.55
REBOUND_LOOKBACK_DAYS = 60
REBOUND_CSV_COLUMNS = [
    "market",
    "industry_category",
    "stock_id",
    "stock_name",
    "date",
    "close",
    "high_60d",
    "drop_amount",
    "drop_pct",
    "support_ma",
    "support_price",
    "support_gap_pct",
    "target_ma17",
    "potential_return_pct",
    "close_strength",
    "MA3",
    "MA17",
    "MA45",
    "MA117",
    "MA189",
    "vol_lots",
    "vol_ma5_lots",
    "vol_ma20_lots",
    "vol_status",
]
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
def _rate_limit_finmind_http_requests(
    limiter: FinMindRequestLimiter,
    only_hosts: tuple[str, ...] | None = None,
):
    """Throttle HTTP requests. If `only_hosts` is given, only matching URLs consume the budget."""
    original_send = requests.adapters.HTTPAdapter.send

    def rate_limited_send(
        adapter: requests.adapters.HTTPAdapter,
        request: requests.PreparedRequest,
        *args: object,
        **kwargs: object,
    ) -> requests.Response:
        if only_hosts is None or any(host in (request.url or "") for host in only_hosts):
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

    if "Trading_Volume" in result.columns:
        vol_shares = pd.to_numeric(result["Trading_Volume"], errors="coerce")
        result["vol_lots"] = (vol_shares / 1000.0).round(0)
        result["vol_ma5_lots"] = (
            result["vol_lots"].rolling(window=5, min_periods=1).mean().round(0)
        )
        result["vol_ma20_lots"] = (
            result["vol_lots"].rolling(window=20, min_periods=1).mean().round(0)
        )
    return result


def describe_volume_status(
    vol_lots: float | None,
    vol_ma5_lots: float | None,
    vol_ma20_lots: float | None,
) -> str:
    """依操盤手實戰角度評估量比 (今日量/20日均量)、短中均量結構與低流動性警示。"""
    if vol_lots is None or pd.isna(vol_lots):
        return "-"
    vol = float(vol_lots)
    ma5 = float(vol_ma5_lots) if (vol_ma5_lots is not None and not pd.isna(vol_ma5_lots)) else vol
    ma20 = float(vol_ma20_lots) if (vol_ma20_lots is not None and not pd.isna(vol_ma20_lots) and float(vol_ma20_lots) > 0) else 0.0

    ratio = (vol / ma20) if ma20 > 0 else 1.0
    trend_tag = "｜5MA>20MA" if (ma20 > 0 and ma5 > ma20) else ""

    if vol < 500 and ma20 < 500:
        return f"⚠️ 低量冷門 ({ratio:.2f}x｜<500張)"
    if ratio > 4.5:
        return f"⚠️ 天量過熱 ({ratio:.2f}x{trend_tag})"
    if ratio >= 2.0:
        return f"🔥 放量點火 ({ratio:.2f}x{trend_tag})"
    if ratio >= 1.2:
        return f"📈 溫和放量 ({ratio:.2f}x{trend_tag})"
    if ratio >= 0.8:
        return f"➡️ 量能平穩 ({ratio:.2f}x{trend_tag})"
    return f"💧 量縮整理 ({ratio:.2f}x)"


def describe_ma17_trend(ma17_series: pd.Series) -> str:
    """綜合比對今日 MA17 與前 1 日、前 5 日變化，判斷 MA17 近期走勢。"""
    valid = pd.to_numeric(ma17_series, errors="coerce").dropna()
    if valid.empty:
        return "-"
    latest = float(valid.iloc[-1])
    if latest <= 0:
        return "-"
    prev_1d = float(valid.iloc[-2]) if len(valid) >= 2 else latest
    prev_5d = float(valid.iloc[-6]) if len(valid) >= 6 else float(valid.iloc[0])

    chg_1d_pct = ((latest - prev_1d) / prev_1d * 100.0) if prev_1d > 0 else 0.0
    chg_5d_pct = ((latest - prev_5d) / prev_5d * 100.0) if prev_5d > 0 else 0.0

    if abs(chg_1d_pct) < 0.01 and abs(chg_5d_pct) < 0.05:
        return f"➡️ 走平整理 (5日 {chg_5d_pct:+.2f}%)"
    if chg_1d_pct >= 0 and chg_5d_pct >= 0:
        return f"📈 連漲上揚 (5日 {chg_5d_pct:+.2f}%)"
    if chg_1d_pct > 0 and chg_5d_pct < 0:
        return f"↗️ 轉折向上 (日 {chg_1d_pct:+.2f}%｜5日 {chg_5d_pct:+.2f}%)"
    if chg_1d_pct < 0 and chg_5d_pct > 0:
        return f"↘️ 漲勢收斂 (日 {chg_1d_pct:+.2f}%｜5日 {chg_5d_pct:+.2f}%)"
    return f"📉 持續下彎 (5日 {chg_5d_pct:+.2f}%)"


def _extract_latest_volume_metrics(usable: pd.DataFrame) -> dict[str, object]:
    """從已計算均線的日線 DataFrame 提取最新一筆的張數與量能狀態。"""
    latest = usable.iloc[-1]
    vol_lots: float | None = None
    vol_ma5: float | None = None
    vol_ma20: float | None = None

    if "vol_lots" in usable.columns and pd.notna(latest.get("vol_lots")):
        vol_lots = round(float(latest["vol_lots"]))
        vol_ma5 = (
            round(float(latest["vol_ma5_lots"]))
            if "vol_ma5_lots" in usable.columns and pd.notna(latest.get("vol_ma5_lots"))
            else vol_lots
        )
        vol_ma20 = (
            round(float(latest["vol_ma20_lots"]))
            if "vol_ma20_lots" in usable.columns and pd.notna(latest.get("vol_ma20_lots"))
            else vol_lots
        )
    elif "Trading_Volume" in usable.columns:
        vol_series = pd.to_numeric(usable["Trading_Volume"], errors="coerce") / 1000.0
        if pd.notna(vol_series.iloc[-1]):
            vol_lots = round(float(vol_series.iloc[-1]))
            vol_ma5 = round(float(vol_series.tail(5).mean()))
            vol_ma20 = round(float(vol_series.tail(20).mean()))

    vol_status = describe_volume_status(vol_lots, vol_ma5, vol_ma20)
    return {
        "vol_lots": vol_lots if vol_lots is not None else "",
        "vol_ma5_lots": vol_ma5 if vol_ma5 is not None else "",
        "vol_ma20_lots": vol_ma20 if vol_ma20 is not None else "",
        "vol_status": vol_status,
    }


def classify_latest_signal(
    analyzed_prices: pd.DataFrame,
) -> dict[str, object] | None:
    required_columns = ("MA3", "MA17", "MA45", "MA117", "MA189")
    if not set(required_columns).issubset(analyzed_prices.columns):
        return None
    usable = analyzed_prices.dropna(subset=list(required_columns))
    if usable.empty:
        return None

    latest = usable.iloc[-1]
    ma3, ma17, ma45, ma117, ma189 = (
        float(latest[column]) for column in required_columns
    )
    close = float(latest["close"])
    if min(ma3, ma17, ma45, ma117, ma189) <= 0:
        return None

    # 1. 三日線 (MA3) 必須 >= 所有均線 (MA17, MA45, MA117, MA189)
    if ma3 < max(ma17, ma45, ma117, ma189):
        return None

    # 2. 117MA 與 189MA 皆需在 45MA 底下，且 189MA 必須在 117MA 底下 (45MA > 117MA > 189MA)
    if not (ma45 > ma117 and ma117 > ma189):
        return None

    # 3. 以 17MA 為基準計算差距百分比：(17MA - 45MA) / 17MA * 100
    # 採雙區間設計：-1.5% ~ 0% 即將金叉，0% ~ +2.5% 剛金叉起漲
    gap_percent = (ma17 - ma45) / ma17 * 100
    rounded_gap = round(gap_percent, 2)
    if rounded_gap < 0:
        if rounded_gap >= ALIGNMENT_MIN_GAP_PERCENT:
            signal = "即將金叉"
        else:
            return None
    elif rounded_gap <= ALIGNMENT_MAX_GAP_PERCENT:
        signal = "剛金叉起漲"
    else:
        return None

    ma17_trend = describe_ma17_trend(usable["MA17"])
    vol_metrics = _extract_latest_volume_metrics(usable)

    return {
        "date": latest["date"].strftime("%Y-%m-%d"),
        "close": close,
        "MA3": ma3,
        "MA17": ma17,
        "MA45": ma45,
        "MA117": ma117,
        "MA189": ma189,
        "gap_percent": gap_percent,
        "signal": signal,
        "ma17_trend": ma17_trend,
        **vol_metrics,
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
        "MA3": signal_data["MA3"],
        "MA17": signal_data["MA17"],
        "MA45": signal_data["MA45"],
        "MA117": signal_data["MA117"],
        "MA189": signal_data["MA189"],
        "MA17_MA45_gap_percent": signal_data["gap_percent"],
        "signal": signal_data["signal"],
        "ma17_trend": signal_data["ma17_trend"],
        "vol_lots": signal_data.get("vol_lots", ""),
        "vol_ma5_lots": signal_data.get("vol_ma5_lots", ""),
        "vol_ma20_lots": signal_data.get("vol_ma20_lots", ""),
        "vol_status": signal_data.get("vol_status", "-"),
    }


def _rebound_min_drop_amount(price: float) -> float | None:
    """依股價位階回傳型態 A 要求的最低高點回跌金額（元）；低於 30 元不納入。"""
    if price < 30.0:
        return None
    if price < 150.0:
        return 10.0
    if price < 500.0:
        return 30.0
    if price < 1000.0:
        return 100.0
    return 300.0


def classify_rebound_signal(
    analyzed_prices: pd.DataFrame,
    *,
    lookback_days: int = REBOUND_LOOKBACK_DAYS,
) -> dict[str, object] | None:
    """型態 A：高點回跌 10/30/100/300 元（且跌幅 >= 10%）回踩長均線（45MA/117MA）反彈篩選。

    條件：
    1. 長線護體：MA45 >= MA117 > MA189
    2. 高點回跌門檻：近 60 個交易日最高價 (high_60d) 回跌金額達位階門檻，且跌幅 >= 10%
    3. 下有長均線支撐：今日收盤價落在 45MA 或 117MA 的 -1.5% ~ +2.5% 區間內
    4. 上有反彈空間：收盤價 < 17MA，且反彈至 17MA 的空間 ((MA17 - Close) / Close) >= 5%
    5. K 線收盤強度濾網：收盤強度 (Close - Min) / (Max - Min) >= 0.55（排除長上影線假反彈）
    """
    required_columns = ("MA3", "MA17", "MA45", "MA117", "MA189")
    if not set(required_columns).issubset(analyzed_prices.columns):
        return None
    usable = analyzed_prices.dropna(subset=list(required_columns))
    if usable.empty:
        return None

    latest = usable.iloc[-1]
    ma3, ma17, ma45, ma117, ma189 = (
        float(latest[column]) for column in required_columns
    )
    close = float(latest["close"])
    if min(close, ma3, ma17, ma45, ma117, ma189) <= 0:
        return None

    # 1. 長線護體：MA45 >= MA117 > MA189
    if not (ma45 >= ma117 and ma117 > ma189):
        return None

    # 2. 近 60 個交易日高點與回跌門檻（位階可用 close 或 high_60d 判定，此處若 close 達 30 元以上即依位階檢核）
    min_drop_req = _rebound_min_drop_amount(close)
    if min_drop_req is None:
        return None

    window = analyzed_prices.tail(lookback_days)
    if "max" in window.columns and window["max"].notna().any():
        high_series = pd.to_numeric(window["max"], errors="coerce").dropna()
        high_60d = float(high_series.max()) if not high_series.empty else float(window["close"].max())
    else:
        high_60d = float(pd.to_numeric(window["close"], errors="coerce").max())

    if high_60d <= close or high_60d <= 0:
        return None

    drop_amount = high_60d - close
    drop_pct = (drop_amount / high_60d) * 100.0
    if round(drop_amount, 4) < min_drop_req or round(drop_pct, 4) < 10.0:
        return None

    # 3. 下有長均線支撐：今日收盤價落在 45MA 或 117MA 的 -1.5% ~ +2.5% 區間內
    gap_45 = (close - ma45) / ma45 * 100.0
    gap_117 = (close - ma117) / ma117 * 100.0
    in_45 = REBOUND_SUPPORT_MIN_PCT <= round(gap_45, 2) <= REBOUND_SUPPORT_MAX_PCT
    in_117 = REBOUND_SUPPORT_MIN_PCT <= round(gap_117, 2) <= REBOUND_SUPPORT_MAX_PCT
    if not (in_45 or in_117):
        return None

    if in_45 and in_117:
        if abs(gap_45) <= abs(gap_117):
            support_ma, support_price, support_gap_pct = "45MA", ma45, gap_45
        else:
            support_ma, support_price, support_gap_pct = "117MA", ma117, gap_117
    elif in_45:
        support_ma, support_price, support_gap_pct = "45MA", ma45, gap_45
    else:
        support_ma, support_price, support_gap_pct = "117MA", ma117, gap_117

    # 4. 上有反彈空間：收盤價低於 17MA，且反彈至 17MA 的空間 >= 5%
    if close >= ma17:
        return None
    potential_return_pct = (ma17 - close) / close * 100.0
    if round(potential_return_pct, 4) < REBOUND_MIN_RETURN_TO_MA17_PCT:
        return None

    # 5. K 線收盤強度濾網：(Close - Min) / (Max - Min) >= 0.55
    day_max = _parse_optional_float(latest.get("max"))
    day_min = _parse_optional_float(latest.get("min"))
    if day_max is not None and day_min is not None:
        if day_max > day_min:
            close_strength = (close - day_min) / (day_max - day_min)
        elif day_max == day_min and close >= day_max:
            close_strength = 1.0
        else:
            close_strength = 0.0
        if round(close_strength, 4) < REBOUND_MIN_CLOSE_STRENGTH:
            return None
    else:
        close_strength = 1.0

    vol_metrics = _extract_latest_volume_metrics(usable)
    date_val = (
        latest["date"].strftime("%Y-%m-%d")
        if hasattr(latest["date"], "strftime")
        else str(latest["date"])[:10]
    )

    return {
        "date": date_val,
        "close": round(close, 2),
        "high_60d": round(high_60d, 2),
        "drop_amount": round(drop_amount, 2),
        "drop_pct": round(drop_pct, 2),
        "support_ma": support_ma,
        "support_price": round(support_price, 2),
        "support_gap_pct": round(support_gap_pct, 2),
        "target_ma17": round(ma17, 2),
        "potential_return_pct": round(potential_return_pct, 2),
        "close_strength": round(close_strength, 2),
        "MA3": round(ma3, 2),
        "MA17": round(ma17, 2),
        "MA45": round(ma45, 2),
        "MA117": round(ma117, 2),
        "MA189": round(ma189, 2),
        "vol_lots": vol_metrics.get("vol_lots", ""),
        "vol_ma5_lots": vol_metrics.get("vol_ma5_lots", ""),
        "vol_ma20_lots": vol_metrics.get("vol_ma20_lots", ""),
        "vol_status": vol_metrics.get("vol_status", "-"),
    }


def evaluate_market_regime(
    benchmark_prices: pd.DataFrame,
    benchmark_label: str = "0050",
) -> dict[str, object]:
    """依據大盤基準（0050 或 TAIEX）日線均線位階計算大盤環境紅綠燈。

    - 🟢綠燈：大盤 >= 17MA 且 17MA 向上（偏多環境，順勢主推 17/45MA 金叉突破）
    - 🟡黃燈：大盤跌破 17MA 但守住 45MA/117MA，或處於震盪整理（提高型態 A 反彈清單權重）
    - 🔴紅燈：大盤跌破 45MA 且 45MA 下彎（空頭警戒，強制防禦警示）
    """
    default_regime = {
        "light": "🟡黃燈",
        "status": "震盪整理（無基準日線，預設中性風控）",
        "benchmark": benchmark_label,
        "close": None,
        "MA17": None,
        "MA45": None,
        "MA117": None,
        "summary": f"🟡黃燈（{benchmark_label} 預設震盪中性）",
    }
    if benchmark_prices is None or benchmark_prices.empty or "close" not in benchmark_prices.columns:
        return default_regime

    try:
        analyzed = add_moving_averages(benchmark_prices)
    except Exception:
        return default_regime

    usable = analyzed.dropna(subset=["close", "MA17", "MA45"])
    if usable.empty:
        return default_regime

    latest = usable.iloc[-1]
    close = float(latest["close"])
    ma17 = float(latest["MA17"])
    ma45 = float(latest["MA45"])
    ma117 = float(latest["MA117"]) if pd.notna(latest.get("MA117")) else ma45

    prev_ma17 = float(usable.iloc[-2]["MA17"]) if len(usable) >= 2 else ma17
    prev_ma45 = float(usable.iloc[-2]["MA45"]) if len(usable) >= 2 else ma45
    ma17_up = ma17 >= prev_ma17
    ma45_down = ma45 < prev_ma45

    if close < ma45 and ma45_down:
        light = "🔴紅燈"
        status = "空頭防禦警戒（跌破 45MA 且季線下彎，嚴格過濾基本面與現金流）"
    elif close >= ma17 and close >= ma45 and ma17_up:
        light = "🟢綠燈"
        status = "偏多順勢環境（站上 17MA 與 45MA 且短均向上，主推 17/45MA 金叉突破）"
    else:
        light = "🟡黃燈"
        status = "震盪盤整環境（跌破或測試 17MA 但守住長均線，提高型態 A 反彈權重）"

    summary = (
        f"{light}｜{benchmark_label} 收盤 {close:,.2f}（17MA {ma17:,.2f}／45MA {ma45:,.2f}／117MA {ma117:,.2f}）— {status}"
    )
    return {
        "light": light,
        "status": status,
        "benchmark": benchmark_label,
        "close": round(close, 2),
        "MA17": round(ma17, 2),
        "MA45": round(ma45, 2),
        "MA117": round(ma117, 2),
        "summary": summary,
    }


def fetch_or_load_market_regime(
    *,
    daily_directory: Path | None = None,
    as_of: date | None = None,
    loader: DataLoader | None = None,
    benchmark_id: str = "0050",
) -> dict[str, object]:
    """優先從當日緩存或本地 prices.csv 讀取大盤紅綠燈，若無則透過 FinMind 抓取 0050 日線並快取。"""
    today = as_of or _today_in_taipei()
    cache_path = (
        daily_directory / f"{today.isoformat()}_market_regime.json"
        if daily_directory is not None
        else None
    )
    if cache_path is not None and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("light"):
                return cached
        except Exception:
            pass

    if daily_directory is not None:
        prices_path = daily_directory / f"{today.isoformat()}_prices.csv"
        if prices_path.is_file():
            try:
                prices_df = pd.read_csv(prices_path, dtype={"stock_id": str})
                sub = prices_df.loc[prices_df["stock_id"].astype(str).str.strip() == benchmark_id].copy()
                if not sub.empty:
                    regime = evaluate_market_regime(sub, benchmark_label=benchmark_id)
                    if cache_path is not None:
                        cache_path.write_text(
                            json.dumps(regime, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    return regime
            except Exception:
                pass

    if loader is not None:
        try:
            start_date = (pd.Timestamp(today) - pd.DateOffset(months=INDICATOR_HISTORY_MONTHS)).strftime("%Y-%m-%d")
            df = _fetch_stock_prices(loader, benchmark_id, start_date, today.isoformat())
            if not df.empty:
                regime = evaluate_market_regime(df, benchmark_label=benchmark_id)
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps(regime, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                return regime
        except Exception:
            logger.warning("Failed to fetch market regime benchmark %s", benchmark_id, exc_info=True)

    return evaluate_market_regime(pd.DataFrame(), benchmark_label=benchmark_id)


def format_alignment_report(
    alignment_signals: pd.DataFrame,
    as_of: date | None = None,
    failed_count: int = 0,
    market_regime: Mapping[str, object] | None = None,
) -> list[str]:
    report_date = (as_of or _today_in_taipei()).isoformat()
    heading = (
        f"📊 {report_date} MA 均線排列訊號"
        "\n條件：MA3 >= 所有均線；17/45MA 處於即將金叉（-1.5%~0%）或剛金叉起漲（0%~+2.5%）；"
        "且 MA45 > MA117 > MA189"
    )
    if market_regime and market_regime.get("summary"):
        heading += f"\n🚦 大盤紅綠燈：{market_regime['summary']}"
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
        ma3_val = getattr(row, "MA3", None)
        ma3_part = (
            f" | MA3 {float(ma3_val):,.2f}"
            if ma3_val is not None and not pd.isna(ma3_val)
            else ""
        )
        trend_label = getattr(row, "ma17_trend", None)
        trend_part = f" | 17MA走勢 {trend_label}" if trend_label else ""

        vol_lots = _parse_optional_float(getattr(row, "vol_lots", None))
        vol_ma20 = _parse_optional_float(getattr(row, "vol_ma20_lots", None))
        vol_status = getattr(row, "vol_status", None)
        vol_part = ""
        if vol_lots is not None:
            ma20_str = f"｜20日均 {int(round(vol_ma20)):,}張" if vol_ma20 is not None else ""
            status_str = (
                f" ({vol_status}{ma20_str})"
                if vol_status and str(vol_status) not in ("", "-", "nan")
                else ""
            )
            vol_part = f" | 量 {int(round(vol_lots)):,}張{status_str}"

        line = (
            f"\n{row.stock_id} {row.stock_name}{signal_part} | 收盤 {float(row.close):,.2f}"
            f"{vol_part}"
            f"{trend_part}"
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
    market_regime: Mapping[str, object] | None = None,
) -> int:
    if not channel_id.isdigit():
        raise ValueError("STOCK_CHANNEL_ID 必須是 Discord 頻道 ID 數字。")
    if not bot_token.strip():
        raise ValueError("DISCORD_TOKEN (or BOT_TOKEN) is required to post the report.")

    messages = format_alignment_report(
        alignment_signals,
        as_of,
        failed_count=failed_count,
        market_regime=market_regime,
    )
    sent_count = 0
    first_message_id: str | None = None
    for index, content in enumerate(messages):
        payload_dict: dict[str, Any] = {
            "content": content,
            "flags": 4,
        }
        if index > 0 and first_message_id:
            payload_dict["message_reference"] = {
                "message_id": str(first_message_id),
                "fail_if_not_exists": False,
            }
            payload_dict["allowed_mentions"] = {"replied_user": False}

        request = Request(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages",
            data=json.dumps(payload_dict, ensure_ascii=False).encode("utf-8"),
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
                resp_body = response.read()
                if isinstance(resp_body, bytes):
                    resp_text = resp_body.decode("utf-8", errors="replace")
                elif isinstance(resp_body, str):
                    resp_text = resp_body
                else:
                    resp_text = ""
                try:
                    resp_data = json.loads(resp_text) if resp_text else {}
                    if index == 0 and isinstance(resp_data, dict) and resp_data.get("id"):
                        first_message_id = str(resp_data["id"])
                except Exception:
                    pass
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


def format_rebound_report(
    rebound_signals: pd.DataFrame,
    as_of: date | None = None,
    failed_count: int = 0,
    market_regime: Mapping[str, object] | None = None,
    ai_executed: bool = False,
) -> list[str]:
    """格式化 22:30 型態 A（高點急跌回踩長均線反彈）Discord 快報。"""
    report_date = (as_of or _today_in_taipei()).isoformat()
    count = len(rebound_signals)
    heading_lines = [
        f"🪃 【22:30 型態 A：高點回跌回踩長均線反彈清單】{report_date}",
        f"🎯 今日符合反彈條件標的總數：**{count} 檔**",
        "條件：MA45 >= MA117 > MA189｜近 60 日高點回跌達位階門檻（30~150跌>=10元、150~500跌>=30元、500~1000跌>=100元、>=1000跌>=300元）且跌幅 >=10%｜回踩 45MA/117MA（-1.5%~+2.5%）｜反彈至 17MA 空間 >=5%｜K線收盤強度 >=0.55",
    ]
    if market_regime and market_regime.get("summary"):
        heading_lines.append(f"🚦 大盤紅綠燈：{market_regime['summary']}")
        if "🔴" in str(market_regime.get("light", "")):
            heading_lines.append("🛡️ **空頭防禦警示**：目前大盤跌破 45MA 且下彎，搶反彈請嚴格過濾基本面與自由現金流，嚴守停損！")
        elif "🟡" in str(market_regime.get("light", "")):
            heading_lines.append("💡 **震盪盤整環境**：大盤處於黃燈區間，系統提高「型態 A 反彈清單」關注權重。")
    if failed_count:
        heading_lines.append(f"⚠️ 有 {failed_count} 檔股票分析過程發生異常。")
    if not ai_executed and count > 0:
        heading_lines.append(
            "📌 **第一階段觀測模式**：已寫入 Google Sheets `Rebound_Watchlist`。若欲啟動 AI 深度診斷與嚴選，請執行 `analyze-rebound --run-ai`。"
        )
    heading = "\n".join(heading_lines)

    if rebound_signals.empty:
        return [heading + "\n今日沒有符合型態 A 反彈條件的股票。"]

    chunks: list[str] = []
    current = heading
    for row in rebound_signals.itertuples(index=False):
        vol_lots = _parse_optional_float(getattr(row, "vol_lots", None))
        vol_status = getattr(row, "vol_status", "-")
        vol_str = f" | 量 {int(round(vol_lots)):,}張 ({vol_status})" if vol_lots is not None else ""
        line = (
            f"\n• **{row.stock_id} {row.stock_name}**（{getattr(row, 'industry_category', '')}）"
            f"｜收盤 {float(row.close):,.2f}｜60日高 {float(row.high_60d):,.2f}"
            f"（跌 {float(row.drop_amount):,.2f}元 / -{float(row.drop_pct):.1f}%）"
            f"｜回踩 {row.support_ma} {float(row.support_price):,.2f}（{float(row.support_gap_pct):+.2f}%）"
            f"｜17MA目標 {float(row.target_ma17):,.2f}（空間 +{float(row.potential_return_pct):.1f}%）"
            f"｜收盤強度 {float(row.close_strength):.2f}{vol_str}"
        )
        if len(current) + len(line) > 1800:
            chunks.append(current)
            current = heading_lines[0] + "\n（續）"
        current += line
    chunks.append(current)
    return chunks


def post_rebound_report_to_discord(
    rebound_signals: pd.DataFrame,
    channel_id: str,
    bot_token: str,
    as_of: date | None = None,
    failed_count: int = 0,
    market_regime: Mapping[str, object] | None = None,
    ai_executed: bool = False,
) -> int:
    if not channel_id.isdigit():
        raise ValueError("STOCK_CHANNEL_ID 必須是 Discord 頻道 ID 數字。")
    if not bot_token.strip():
        raise ValueError("DISCORD_TOKEN (or BOT_TOKEN) is required to post the report.")

    messages = format_rebound_report(
        rebound_signals,
        as_of=as_of,
        failed_count=failed_count,
        market_regime=market_regime,
        ai_executed=ai_executed,
    )
    sent_count = 0
    first_message_id: str | None = None
    for index, content in enumerate(messages):
        payload_dict: dict[str, Any] = {
            "content": content,
            "flags": 4,
        }
        if index > 0 and first_message_id:
            payload_dict["message_reference"] = {
                "message_id": str(first_message_id),
                "fail_if_not_exists": False,
            }
            payload_dict["allowed_mentions"] = {"replied_user": False}

        request = Request(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages",
            data=json.dumps(payload_dict, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bot {bot_token.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordService (FinMind rebound scanner)",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status not in (200, 201):
                    raise RuntimeError(f"Discord returned unexpected HTTP status {response.status}.")
                resp_body = response.read()
                if isinstance(resp_body, bytes):
                    resp_text = resp_body.decode("utf-8", errors="replace")
                elif isinstance(resp_body, str):
                    resp_text = resp_body
                else:
                    resp_text = ""
                try:
                    resp_data = json.loads(resp_text) if resp_text else {}
                    if index == 0 and isinstance(resp_data, dict) and resp_data.get("id"):
                        first_message_id = str(resp_data["id"])
                except Exception:
                    pass
        except HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Discord rejected the report (HTTP {error.code}): {response_body}") from error
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
    ma17_trend = str(raw.get("17MA近期走勢") or raw.get("ma17_trend") or "-").strip()

    vol_lots = _parse_optional_float(
        raw.get("成交量(張)") if "成交量(張)" in raw else raw.get("vol_lots")
    )
    vol_ma5 = _parse_optional_float(
        raw.get("5日均量(張)") if "5日均量(張)" in raw else raw.get("vol_ma5_lots")
    )
    vol_ma20 = _parse_optional_float(
        raw.get("20日均量(張)") if "20日均量(張)" in raw else raw.get("vol_ma20_lots")
    )
    vol_status = str(
        raw.get("量能狀態(量比)")
        or raw.get("vol_status")
        or (describe_volume_status(vol_lots, vol_ma5, vol_ma20) if vol_lots is not None else "-")
    ).strip()

    close_val = _parse_optional_float(raw.get("收盤價") if "收盤價" in raw else raw.get("close"))
    ma3_val = _parse_optional_float(raw.get("3MA") if "3MA" in raw else raw.get("MA3"))
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
        or ("3MA >= 各線 & 45MA > 117MA > 189MA ✓" if stock_id and stock_id != "-" else "-")
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
        "17MA近期走勢": ma17_trend,
        "成交量(張)": "" if vol_lots is None else int(round(vol_lots)),
        "5日均量(張)": "" if vol_ma5 is None else int(round(vol_ma5)),
        "20日均量(張)": "" if vol_ma20 is None else int(round(vol_ma20)),
        "量能狀態(量比)": vol_status,
        "收盤價": "" if close_val is None else round(close_val, 2),
        "3MA": "" if ma3_val is None else round(ma3_val, 2),
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
    """將當日均線排列清單資料與歷史紀錄比對，標註『當日新進』、『連續天數』與『每日執行狀況』。"""
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
                        "MA3": rec["3MA"],
                        "MA17": rec["17MA"],
                        "MA45": rec["45MA"],
                        "MA117": rec["117MA"],
                        "MA189": rec["189MA"],
                        "gap_percent": rec["17與45MA差距(%)"],
                        "signal": rec["訊號類型"],
                        "ma17_trend": rec["17MA近期走勢"],
                        "vol_lots": rec.get("成交量(張)", ""),
                        "vol_ma5_lots": rec.get("5日均量(張)", ""),
                        "vol_ma20_lots": rec.get("20日均量(張)", ""),
                        "vol_status": rec.get("量能狀態(量比)", "-"),
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
            ma17_trend = str(row.get("ma17_trend", "") or "-").strip()

            vol_lots_raw = _parse_optional_float(
                row.get("vol_lots") if "vol_lots" in row else row.get("成交量(張)")
            )
            vol_ma5_raw = _parse_optional_float(
                row.get("vol_ma5_lots") if "vol_ma5_lots" in row else row.get("5日均量(張)")
            )
            vol_ma20_raw = _parse_optional_float(
                row.get("vol_ma20_lots") if "vol_ma20_lots" in row else row.get("20日均量(張)")
            )
            vol_lots_val: object = "" if vol_lots_raw is None else int(round(vol_lots_raw))
            vol_ma5_val: object = "" if vol_ma5_raw is None else int(round(vol_ma5_raw))
            vol_ma20_val: object = "" if vol_ma20_raw is None else int(round(vol_ma20_raw))
            vol_status_raw = str(row.get("vol_status") or row.get("量能狀態(量比)") or "").strip()
            if not vol_status_raw or vol_status_raw in ("-", "nan"):
                vol_status_val = describe_volume_status(vol_lots_raw, vol_ma5_raw, vol_ma20_raw)
            else:
                vol_status_val = vol_status_raw

            close_val = round(float(row["close"]), 2)
            ma3_raw = _parse_optional_float(row.get("MA3"))
            ma3_val = "" if ma3_raw is None else round(ma3_raw, 2)
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
                    if "剛金叉" in signal or signal == "已超過":
                        daily_status = (
                            f"🔥 轉強突破（{prev_signal} ➔ {signal}｜連 {streak} 日｜差距 {gap_val:.2f}%）"
                        )
                    else:
                        daily_status = (
                            f"⚠️ 均線拉回（{prev_signal} ➔ {signal}｜連 {streak} 日｜差距 {gap_val:.2f}%）"
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
                    "17MA近期走勢": ma17_trend,
                    "成交量(張)": vol_lots_val,
                    "5日均量(張)": vol_ma5_val,
                    "20日均量(張)": vol_ma20_val,
                    "量能狀態(量比)": vol_status_val,
                    "收盤價": close_val,
                    "3MA": ma3_val,
                    "17MA": ma17_val,
                    "45MA": ma45_val,
                    "117MA": ma117_val,
                    "189MA": ma189_val,
                    "17與45MA差距(%)": gap_val,
                    "均線排列檢核": "3MA >= 各線 & 45MA > 117MA > 189MA ✓",
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
                "17MA近期走勢": "-",
                "成交量(張)": "",
                "5日均量(張)": "",
                "20日均量(張)": "",
                "量能狀態(量比)": "-",
                "收盤價": "",
                "3MA": "",
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
        is_golden = "剛金叉" in str(rec.get("訊號類型", "")) or str(rec.get("訊號類型", "")) == "已超過"
        signal_rank = 0 if is_golden else 1
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


def _ensure_tracker_signal_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Ensure tracker-required columns exist when feeding alignment_signals into tracker logic."""
    if frame is None:
        return pd.DataFrame()
    df = frame.copy()
    if "stock_id" in df.columns:
        df["stock_id"] = df["stock_id"].astype(str).str.strip()
    if "gap_percent" not in df.columns and "MA17_MA45_gap_percent" in df.columns:
        df["gap_percent"] = df["MA17_MA45_gap_percent"]
    for col, default in (
        ("MA3", ""),
        ("ma17_trend", "-"),
        ("vol_lots", ""),
        ("vol_ma5_lots", ""),
        ("vol_ma20_lots", ""),
        ("vol_status", "-"),
    ):
        if col not in df.columns:
            df[col] = default
    return df


def _load_local_prior_signals(
    daily_directory: Path | None, before_date: date
) -> list[dict[str, object]]:
    """若工作表尚無歷史資料，從本地 daily/*_alignment_signals.csv 載入符合新均線邏輯的歷史訊號以銜接連續天數。"""
    if daily_directory is None or not daily_directory.is_dir():
        return []

    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_alignment_signals\.csv$")
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
        df = _ensure_tracker_signal_columns(df)
        if not df.empty:
            for col in ("MA3", "MA17", "MA45", "MA117", "MA189"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["MA17", "MA45", "MA117", "MA189"])
            df = df.loc[(df["MA45"] > df["MA117"]) & (df["MA117"] > df["MA189"])].copy()
            if "MA3" in df.columns and df["MA3"].notna().any():
                df = df.loc[
                    df["MA3"] >= df[["MA17", "MA45", "MA117", "MA189"]].max(axis=1)
                ].copy()
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
        105,  # 0: 執行日期
        105,  # 1: K線日期
        75,   # 2: 市場別
        130,  # 3: 產業類別
        85,   # 4: 股票代號
        105,  # 5: 股票名稱
        135,  # 6: 當日新進
        85,   # 7: 連續天數
        320,  # 8: 每日執行狀況
        145,  # 9: 訊號類型
        215,  # 10: 17MA近期走勢
        105,  # 11: 成交量(張)
        105,  # 12: 5日均量(張)
        105,  # 13: 20日均量(張)
        210,  # 14: 量能狀態(量比)
        95,   # 15: 收盤價
        95,   # 16: 3MA
        95,   # 17: 17MA
        95,   # 18: 45MA
        95,   # 19: 117MA
        95,   # 20: 189MA
        130,  # 21: 17與45MA差距(%)
        225,  # 22: 均線排列檢核
        155,  # 23: 更新時間
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
        # 5. 成交量與均量欄位 (index 11..14) 整數千分位 #,##0
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": total_rows,
                    "startColumnIndex": 11,
                    "endColumnIndex": 14,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "#,##0"},
                        "horizontalAlignment": "RIGHT",
                    }
                },
                "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
            }
        },
        # 6. 收盤價與各均線欄位 (index 15..21) 數值格式 #,##0.00
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": total_rows,
                    "startColumnIndex": 15,
                    "endColumnIndex": 21,
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
        # 7. 差距百分比欄位 (index 21..22) 數值格式 0.00
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": total_rows,
                    "startColumnIndex": 21,
                    "endColumnIndex": 22,
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

    # 依每列狀態套用醒目視覺標記（當日新進整列淺綠底＋綠字粗體；已超過暖橘底；接近淺藍底；放量點火亮橘紅字）
    for idx, rec in enumerate(records, start=1):
        is_new_str = str(rec.get("當日新進", ""))
        signal_str = str(rec.get("訊號類型", ""))
        vol_status_str = str(rec.get("量能狀態(量比)", ""))
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
        if "剛金叉" in signal_str or signal_str == "已超過":
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
        elif "即將金叉" in signal_str or "接近" in signal_str:
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
        if "🔥" in vol_status_str or "📈" in vol_status_str:
            requests_payload.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx,
                            "endRowIndex": idx + 1,
                            "startColumnIndex": 14,
                            "endColumnIndex": 15,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {
                                    "foregroundColor": {"red": 0.80, "green": 0.22, "blue": 0.08},
                                    "bold": True,
                                },
                            }
                        },
                        "fields": "userEnteredFormat.textFormat",
                    }
                }
            )
        elif "低量冷門" in vol_status_str:
            requests_payload.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": idx,
                            "endRowIndex": idx + 1,
                            "startColumnIndex": 14,
                            "endColumnIndex": 15,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {
                                    "foregroundColor": {"red": 0.55, "green": 0.55, "blue": 0.55},
                                    "bold": False,
                                },
                            }
                        },
                        "fields": "userEnteredFormat.textFormat",
                    }
                }
            )

    _gspread_call_with_retry(spreadsheet.batch_update, {"requests": requests_payload})


def sync_alignment_to_gsheet(
    alignment_signals_df: pd.DataFrame,
    *,
    as_of: date | None = None,
    spreadsheet_id: str | None = None,
    credentials_path: str | None = None,
    tab_name: str = SIGNAL_TRACKER_SHEET_NAME,
    daily_directory: Path | None = None,
    replace_date: bool = True,
) -> int:
    """將均線排列清單（alignment_signals）同步至 Google Sheets MA_Signal_Tracker。"""
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

    # 若工作表內尚無早於今日的歷史紀錄，自動從本地 daily/*_alignment_signals.csv 補齊歷史基準
    has_prior_history = any(
        str(rec.get("執行日期") or rec.get("scan_date") or "").strip() < today.isoformat()
        for rec in existing_records
        if str(rec.get("執行日期") or rec.get("scan_date") or "").strip()
    )
    if not has_prior_history and daily_directory is not None:
        seeded = _load_local_prior_signals(daily_directory, before_date=today)
        if seeded:
            existing_records = seeded + existing_records

    tracker_df = _ensure_tracker_signal_columns(alignment_signals_df)

    merged_records = build_signal_tracker_rows(
        tracker_df,
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
) -> tuple[Path, Path, Path | None, int]:
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
    alignment_frame = pd.DataFrame(
        alignment_signals,
        columns=[
            "market",
            "industry_category",
            "stock_id",
            "stock_name",
            "date",
            "close",
            "MA3",
            "MA17",
            "MA45",
            "MA117",
            "MA189",
            "MA17_MA45_gap_percent",
            "signal",
            "ma17_trend",
            "vol_lots",
            "vol_ma5_lots",
            "vol_ma20_lots",
            "vol_status",
        ],
    )
    _atomic_write_csv(price_frame, price_path)
    _atomic_write_csv(alignment_frame, alignment_path)
    if failure_path is not None:
        _atomic_write_csv(pd.DataFrame(failures), failure_path)

    return (
        price_path,
        alignment_path,
        failure_path,
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
    if "sync_alignment_to_gsheet" in config and not isinstance(
        config.get("sync_alignment_to_gsheet"), bool
    ):
        raise ValueError("設定 sync_alignment_to_gsheet 必須是布林值。")
    if "sync_signals_to_gsheet" in config and not isinstance(
        config.get("sync_signals_to_gsheet"), bool
    ):
        raise ValueError(
            "設定 sync_signals_to_gsheet 必須是布林值（舊鍵，建議改用 sync_alignment_to_gsheet）。"
        )
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
        r"^\d{4}-\d{2}-\d{2}_("
        r"(prices|alignment_signals|failures|rebound_signals|rebound_failures)\.csv|"
        r"(ai_recommendations|rebound_ai_recommendations)\.(json|md)|"
        r"market_regime\.json"
        r")$"
    )
    for path in daily_directory.iterdir():
        match = filename_pattern.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        file_date = date.fromisoformat(path.name[:10])
        if file_date < cutoff:
            path.unlink()


def scan_rebound_from_prices(
    prices: pd.DataFrame,
    *,
    lookback_days: int = REBOUND_LOOKBACK_DAYS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """從 daily/{date}_prices.csv 讀取近 60 個交易日數據，篩選「型態 A：高點回跌回踩長均線反彈」清單。

    回傳 (rebound_signals_df, rebound_failures_df)。
    """
    rebounds: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    if prices.empty:
        return (
            pd.DataFrame(columns=REBOUND_CSV_COLUMNS),
            pd.DataFrame(columns=["stock_id", "stock_name", "error"]),
        )

    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values(["stock_id", "date"])
    for stock_id, group in frame.groupby("stock_id", sort=False):
        first = group.iloc[0]
        sid = str(stock_id).strip()
        sname = str(first.get("stock_name", "")).strip()
        meta = {
            "market": first.get("market", ""),
            "industry_category": first.get("industry_category", ""),
            "stock_id": sid,
            "stock_name": sname,
        }
        try:
            working = group
            if not set(("MA3", "MA17", "MA45", "MA117", "MA189")).issubset(working.columns):
                working = add_moving_averages(working)
            signal = classify_rebound_signal(working, lookback_days=lookback_days)
            if signal is not None:
                rebounds.append({**meta, **signal})
        except Exception as error:  # noqa: BLE001
            failures.append({"stock_id": sid, "stock_name": sname, "error": str(error)})

    rebound_df = pd.DataFrame(rebounds, columns=REBOUND_CSV_COLUMNS)
    if not rebound_df.empty:
        rebound_df = rebound_df.sort_values(
            ["potential_return_pct", "drop_pct", "stock_id"],
            ascending=[False, False, True],
            kind="stable",
        ).reset_index(drop=True)
    failure_df = pd.DataFrame(failures, columns=["stock_id", "stock_name", "error"])
    return rebound_df, failure_df


def build_rebound_watchlist_rows(
    rebound_df: pd.DataFrame,
    *,
    as_of: date | None = None,
    market_light: str = "🟡黃燈",
    ai_results: Mapping[str, Mapping[str, object]] | None = None,
    existing_master_map: Mapping[str, Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """建立寫入 Google Sheets Rebound_Watchlist 的結構化列。"""
    scan_date_str = (as_of or _today_in_taipei()).isoformat()
    if rebound_df is None or rebound_df.empty:
        return []

    rows: list[dict[str, object]] = []
    for _, row in rebound_df.drop_duplicates(subset=["stock_id"], keep="last").iterrows():
        sid = str(row.get("stock_id", "")).strip()
        if not sid:
            continue
        ai_summary = ""
        war_room_url = ""
        if ai_results and sid in ai_results:
            item = ai_results[sid]
            ai_summary = str(item.get("summary", "") or "")
            report = item.get("report")
            war_room_url = str(getattr(report, "war_room_url", "") or item.get("war_room_url", "") or "")
        elif existing_master_map and sid in existing_master_map:
            m_row = existing_master_map[sid]
            ai_summary = str(m_row.get("ai_summary_json", "") or "")
            war_room_url = str(m_row.get("war_room_url", "") or "")

        rows.append(
            {
                "pk_key": f"{scan_date_str}_{sid}",
                "stock_id": sid,
                "stock_name": str(row.get("stock_name", "")).strip(),
                "industry_category": str(row.get("industry_category", "")).strip(),
                "scan_date": scan_date_str,
                "close_price": round(float(row["close"]), 2) if pd.notna(row.get("close")) else "",
                "high_60d": round(float(row["high_60d"]), 2) if pd.notna(row.get("high_60d")) else "",
                "drop_amount": round(float(row["drop_amount"]), 2) if pd.notna(row.get("drop_amount")) else "",
                "drop_pct": round(float(row["drop_pct"]), 2) if pd.notna(row.get("drop_pct")) else "",
                "support_ma": str(row.get("support_ma", "")).strip(),
                "support_price": round(float(row["support_price"]), 2) if pd.notna(row.get("support_price")) else "",
                "target_ma17": round(float(row["target_ma17"]), 2) if pd.notna(row.get("target_ma17")) else "",
                "potential_return_pct": (
                    round(float(row["potential_return_pct"]), 2)
                    if pd.notna(row.get("potential_return_pct"))
                    else ""
                ),
                "volume_status": str(row.get("vol_status") or row.get("volume_status") or "-").strip(),
                "market_light": market_light,
                "ai_summary_json": ai_summary,
                "war_room_url": war_room_url,
            }
        )
    return rows


def sync_rebound_watchlist_to_gsheet(
    rebound_df: pd.DataFrame,
    *,
    as_of: date | None = None,
    market_light: str = "🟡黃燈",
    ai_results: Mapping[str, Mapping[str, object]] | None = None,
    spreadsheet_id: str | None = None,
    credentials_path: str | None = None,
    tab_name: str = REBOUND_WATCHLIST_SHEET_NAME,
    replace_date: bool = True,
) -> int:
    """將 22:30 型態 A 反彈清單同步寫入 Google Sheets Rebound_Watchlist。"""
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
    today_iso = today.isoformat()
    gc = gspread.service_account(filename=resolved_credentials)
    spreadsheet = _gspread_call_with_retry(gc.open_by_key, resolved_spreadsheet_id)

    worksheets = _gspread_call_with_retry(spreadsheet.worksheets)
    existing_tabs = {ws.title: ws for ws in worksheets}

    # 讀取 00_Watchlist_Master 既有快取（若未啟動 AI 診斷時可自動帶入既有 ai_summary_json 與 war_room_url）
    master_map: dict[str, dict[str, object]] = {}
    if "00_Watchlist_Master" in existing_tabs:
        try:
            master_raw = _gspread_call_with_retry(
                existing_tabs["00_Watchlist_Master"].get_all_values,
                value_render_option=gspread.utils.ValueRenderOption.unformatted,
                date_time_render_option=gspread.utils.DateTimeOption.formatted_string,
            )
            if master_raw and len(master_raw) > 1:
                m_headers = [str(h).strip() for h in master_raw[0]]
                for r in master_raw[1:]:
                    padded = list(r) + [""] * (len(m_headers) - len(r))
                    rec = {m_headers[i]: padded[i] for i in range(len(m_headers)) if m_headers[i]}
                    sid = str(rec.get("stock_id", "")).strip()
                    if sid:
                        master_map[sid] = rec
        except Exception:
            logger.warning("Failed to read 00_Watchlist_Master for rebound enrichment", exc_info=True)

    headers = list(REBOUND_WATCHLIST_HEADERS)
    if tab_name not in existing_tabs:
        worksheet = _gspread_call_with_retry(
            spreadsheet.add_worksheet,
            title=tab_name,
            rows=1000,
            cols=len(headers),
        )
        _gspread_call_with_retry(worksheet.append_row, headers)
        raw_rows = [headers]
    else:
        worksheet = existing_tabs[tab_name]
        raw_rows = _gspread_call_with_retry(
            worksheet.get_all_values,
            value_render_option=gspread.utils.ValueRenderOption.unformatted,
            date_time_render_option=gspread.utils.DateTimeOption.formatted_string,
        )

    existing_records: list[dict[str, object]] = []
    if raw_rows and len(raw_rows) > 1:
        sheet_headers = [str(h).strip() for h in raw_rows[0]]
        for row in raw_rows[1:]:
            padded = list(row) + [""] * (len(sheet_headers) - len(row))
            record = {sheet_headers[i]: padded[i] for i in range(len(sheet_headers)) if sheet_headers[i]}
            if any(str(v).strip() for v in record.values()):
                existing_records.append(record)

    prior_records = (
        [rec for rec in existing_records if str(rec.get("scan_date", "")).strip() != today_iso]
        if replace_date
        else existing_records
    )

    today_rows = build_rebound_watchlist_rows(
        rebound_df,
        as_of=today,
        market_light=market_light,
        ai_results=ai_results,
        existing_master_map=master_map,
    )
    merged_records = today_rows + prior_records

    matrix: list[list[object]] = [headers]
    for rec in merged_records:
        matrix.append([rec.get(col, "") for col in headers])

    required_rows = max(len(matrix) + 50, 200)
    required_cols = len(headers)
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
    return len(today_rows)


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
                all_alignments.append(paths[1])
                if paths[2] is not None:
                    all_failures.append(paths[2])

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
            "alignment_signals": _combine_csvs(
                all_alignments,
                [
                    "market",
                    "industry_category",
                    "stock_id",
                    "stock_name",
                    "date",
                    "close",
                    "MA3",
                    "MA17",
                    "MA45",
                    "MA117",
                    "MA189",
                    "MA17_MA45_gap_percent",
                    "signal",
                    "ma17_trend",
                    "vol_lots",
                    "vol_ma5_lots",
                    "vol_ma20_lots",
                    "vol_status",
                ],
            ),
            "failures": _combine_csvs(
                all_failures, ["stock_id", "stock_name", "error"]
            ),
        }
        # 若設定了大盤基準（例如 TAIEX），在掃描完成後一併抓取並快取大盤日線，同時追加進 prices.csv
        market_regime: dict[str, object] | None = None
        if config.get("enable_market_regime"):
            benchmark_id = str(config.get("market_benchmark_id") or "TAIEX")
            with _rate_limit_finmind_http_requests(limiter):
                market_regime = fetch_or_load_market_regime(
                    daily_directory=daily_directory,
                    as_of=today,
                    loader=loader,
                    benchmark_id=benchmark_id,
                )
            # 若 prices.csv 尚未包含該大盤代號，將大盤基準日線追加至 prices.csv，方便 22:00/22:30 秒級重算
            prices_frame = report_files["prices"]
            if not prices_frame.empty and benchmark_id not in prices_frame["stock_id"].astype(str).values:
                try:
                    start_date = (pd.Timestamp(today) - pd.DateOffset(months=REPORT_HISTORY_MONTHS)).strftime("%Y-%m-%d")
                    fetch_start_date = (pd.Timestamp(today) - pd.DateOffset(months=INDICATOR_HISTORY_MONTHS)).strftime("%Y-%m-%d")
                    bench_raw = _fetch_stock_prices(loader, benchmark_id, fetch_start_date, today.isoformat())
                    if not bench_raw.empty:
                        bench_analyzed = add_moving_averages(bench_raw)
                        bench_analyzed = bench_analyzed.loc[bench_analyzed["date"] >= pd.Timestamp(start_date)].copy()
                        bench_analyzed["stock_id"] = benchmark_id
                        bench_analyzed["stock_name"] = "加權指數" if benchmark_id == "TAIEX" else benchmark_id
                        bench_analyzed["industry_category"] = "大盤"
                        bench_analyzed["market"] = "上市"
                        report_files["prices"] = pd.concat([prices_frame, bench_analyzed], ignore_index=True)
                except Exception:
                    logger.warning("Failed to append benchmark %s to prices.csv", benchmark_id, exc_info=True)

        report_paths = {
            name: daily_directory / f"{today.isoformat()}_{name}.csv"
            for name in report_files
        }
        for name, frame in report_files.items():
            _atomic_write_csv(frame, report_paths[name])

    failures = report_files["failures"]
    alignment_signals = report_files["alignment_signals"]

    if config.get("sync_alignment_to_gsheet") or config.get("sync_signals_to_gsheet"):
        tab_name = str(config.get("gsheet_tab_name") or SIGNAL_TRACKER_SHEET_NAME)
        sync_alignment_to_gsheet(
            alignment_signals,
            as_of=today,
            tab_name=tab_name,
            daily_directory=daily_directory,
            replace_date=True,
        )

    market_regime: dict[str, object] | None = None
    if config.get("enable_market_regime"):
        benchmark_id = str(config.get("market_benchmark_id") or "TAIEX")
        with _rate_limit_finmind_http_requests(limiter):
            market_regime = fetch_or_load_market_regime(
                daily_directory=daily_directory,
                as_of=today,
                loader=loader,
                benchmark_id=benchmark_id,
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
            market_regime=market_regime,
        )

    _cleanup_old_daily_reports(
        daily_directory, today, int(config["local_retention_days"])
    )
    return report_paths, len(failures), len(alignment_signals), len(failed_groups)


SIGNAL_CSV_COLUMNS = [
    "market",
    "industry_category",
    "stock_id",
    "stock_name",
    "date",
    "close",
    "MA3",
    "MA17",
    "MA45",
    "MA117",
    "MA189",
    "gap_percent",
    "signal",
    "ma17_trend",
    "vol_lots",
    "vol_ma5_lots",
    "vol_ma20_lots",
    "vol_status",
]
ALIGNMENT_CSV_COLUMNS = [
    "MA17_MA45_gap_percent" if column == "gap_percent" else column
    for column in SIGNAL_CSV_COLUMNS
]


def recalc_signals_from_prices(
    prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """純本地運算：以已存檔的 prices.csv（內含完整均線與成交量）重新套用訊號規則。

    不呼叫 FinMind API；prices.csv 內的 MA3~MA189 為 19:00 以近一年資料算好的值，
    因此直接沿用，不在截短後的半年視窗內重算（否則 MA189 會變空值）。
    """
    signals: list[dict[str, object]] = []
    alignments: list[dict[str, object]] = []
    if prices.empty:
        return (
            pd.DataFrame(columns=SIGNAL_CSV_COLUMNS),
            pd.DataFrame(columns=ALIGNMENT_CSV_COLUMNS),
        )

    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values(["stock_id", "date"])
    for stock_id, group in frame.groupby("stock_id", sort=False):
        first = group.iloc[0]
        meta = {
            "market": first.get("market", ""),
            "industry_category": first.get("industry_category", ""),
            "stock_id": str(stock_id),
            "stock_name": first.get("stock_name", ""),
        }
        signal = classify_latest_signal(group)
        if signal is not None:
            signals.append({**meta, **signal})
        alignment = classify_latest_alignment_signal(group)
        if alignment is not None:
            alignments.append({**meta, **alignment})

    return (
        pd.DataFrame(signals, columns=SIGNAL_CSV_COLUMNS),
        pd.DataFrame(alignments, columns=ALIGNMENT_CSV_COLUMNS),
    )


def recalc_from_cache(
    config: Mapping[str, object],
    *,
    as_of: date | None = None,
    output_dir: Path | None = None,
    dry_run: bool = False,
    skip_gsheet: bool = False,
    skip_discord: bool = False,
) -> pd.DataFrame:
    """讀取 {date}_prices.csv 秒級重算訊號，並（非 dry-run 時）覆寫 CSV／Sheets／Discord。"""
    today = as_of or _today_in_taipei()
    output_root = output_dir or Path(str(config["output_directory"]))
    daily_directory = output_root / "daily"
    prices_path = daily_directory / f"{today.isoformat()}_prices.csv"
    if not prices_path.is_file():
        raise FileNotFoundError(
            f"找不到 {prices_path}；請先執行 scan-all 抓取當日日線資料。"
        )

    prices = pd.read_csv(prices_path, dtype={"stock_id": str})
    signals, alignments = recalc_signals_from_prices(prices)
    print(
        f"[recalc] 讀取 {prices_path.name}（{prices['stock_id'].nunique()} 檔）；"
        f"均線排列清單 {len(alignments)} 檔。"
    )
    if not alignments.empty:
        display_columns = [
            "stock_id",
            "stock_name",
            "signal",
            "MA17_MA45_gap_percent",
            "vol_status",
        ]
        print(alignments[display_columns].to_string(index=False))

    if dry_run:
        print("[recalc] --dry-run：未寫入任何檔案、Google Sheets 與 Discord。")
        return alignments

    _atomic_write_csv(
        alignments, daily_directory / f"{today.isoformat()}_alignment_signals.csv"
    )
    print(f"[recalc] 已覆寫 {today.isoformat()}_alignment_signals.csv。")

    if (config.get("sync_alignment_to_gsheet") or config.get("sync_signals_to_gsheet")) and not skip_gsheet:
        tab_name = str(config.get("gsheet_tab_name") or SIGNAL_TRACKER_SHEET_NAME)
        tracker_source = _ensure_tracker_signal_columns(alignments)
        rows = sync_alignment_to_gsheet(
            tracker_source,
            as_of=today,
            tab_name=tab_name,
            daily_directory=daily_directory,
            replace_date=True,
        )
        print(f"[recalc] 已覆寫 Google Sheets [{tab_name}] 今日資料（共 {rows} 筆紀錄）。")

    if config.get("post_alignment_to_discord") and not skip_discord:
        failures_path = daily_directory / f"{today.isoformat()}_failures.csv"
        failed_count = 0
        if failures_path.is_file():
            try:
                failed_count = len(pd.read_csv(failures_path))
            except pd.errors.EmptyDataError:
                failed_count = 0
        bot_token = (
            os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or ""
        ).strip()
        post_alignment_report_to_discord(
            alignments,
            channel_id=str(config["stock_channel_id"]),
            bot_token=bot_token,
            as_of=today,
            failed_count=failed_count,
        )
        print("[recalc] 已重發 Discord 均線快報。")
    return alignments


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
        help="停用將均線排列清單同步寫入 Google Sheets MA_Signal_Tracker",
    )

    recalc_parser = subparsers.add_parser(
        "recalc-signals",
        help="秒級重算：不呼叫 FinMind，直接讀當日 prices.csv 重新套用訊號規則（調參／回測用）",
    )
    recalc_parser.add_argument(
        "--config",
        type=Path,
        default=Path("finmind_scanner.json"),
        help="JSON 設定檔路徑（預設：finmind_scanner.json）",
    )
    recalc_parser.add_argument("--date", help="指定日期 YYYY-MM-DD（預設今日）")
    recalc_parser.add_argument("--output-dir", type=Path, help="覆寫設定檔的 output_directory")
    recalc_parser.add_argument(
        "--dry-run", action="store_true", help="只在終端機印出結果，不寫 CSV／Sheets／Discord"
    )
    recalc_parser.add_argument("--no-gsheet", action="store_true", help="不更新 Google Sheets")
    recalc_parser.add_argument("--no-discord", action="store_true", help="不重發 Discord 快報")

    analyze_parser = subparsers.add_parser(
        "analyze-alignment",
        help="22:00 流程：對當日 alignment_signals 做財報/法說分析並由 AI 嚴選進場標的",
    )
    analyze_parser.add_argument(
        "--config",
        type=Path,
        default=Path("finmind_scanner.json"),
        help="JSON 設定檔路徑（預設：finmind_scanner.json）",
    )
    analyze_parser.add_argument("--date", help="指定分析日期 YYYY-MM-DD（預設今日）")
    analyze_parser.add_argument("--force-ai", action="store_true", help="忽略單股 AI 快取，強制重新診斷")
    analyze_parser.add_argument("--dry-run", action="store_true", help="不寫入 Sheets、不推播 Discord，只輸出結果")

    rebound_parser = subparsers.add_parser(
        "analyze-rebound",
        help="22:30 流程：執行型態 A（高點回跌回踩長均線反彈）掃描、寫入 Rebound_Watchlist 並彈性決定是否啟動 AI 嚴選",
    )
    rebound_parser.add_argument(
        "--config",
        type=Path,
        default=Path("finmind_scanner.json"),
        help="JSON 設定檔路徑（預設：finmind_scanner.json）",
    )
    rebound_parser.add_argument("--date", help="指定分析日期 YYYY-MM-DD（預設今日）")
    rebound_parser.add_argument("--output-dir", type=Path, help="覆寫設定檔的 output_directory")
    rebound_parser.add_argument(
        "--run-ai",
        action="store_true",
        help="強制啟動 AI 深度財報診斷與嚴選（預設依設定檔 rebound_auto_run_ai 或第一階段僅輸出清單）",
    )
    rebound_parser.add_argument(
        "--no-ai",
        action="store_true",
        help="強制僅執行量化掃描與推播觀看標的數量，不呼叫 AI 深度診斷",
    )
    rebound_parser.add_argument("--force-ai", action="store_true", help="忽略單股 AI 快取，強制重新診斷")
    rebound_parser.add_argument("--dry-run", action="store_true", help="只在終端機印出結果，不寫入 Sheets 與 Discord")
    rebound_parser.add_argument("--no-gsheet", action="store_true", help="不更新 Google Sheets Rebound_Watchlist")
    rebound_parser.add_argument("--no-discord", action="store_true", help="不推播 Discord 訊息")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
    load_dotenv()
    args = _make_parser().parse_args(argv)
    api_token = os.getenv("FINMIND_API_TOKEN", "").strip()

    try:
        if args.command == "analyze-alignment":
            from alignment_ai_picker import run_from_cli

            return run_from_cli(
                config_path=args.config,
                target_date=args.date,
                force_ai=args.force_ai,
                dry_run=args.dry_run,
            )

        if args.command == "analyze-rebound":
            from alignment_ai_picker import run_rebound_from_cli

            return run_rebound_from_cli(
                config_path=args.config,
                target_date=args.date,
                output_dir=args.output_dir,
                run_ai=True if args.run_ai else (False if args.no_ai else None),
                force_ai=args.force_ai,
                dry_run=args.dry_run,
                skip_gsheet=args.no_gsheet,
                skip_discord=args.no_discord,
            )

        if args.command == "recalc-signals":
            config = load_scan_config(args.config)
            recalc_from_cache(
                config,
                as_of=date.fromisoformat(args.date) if args.date else None,
                output_dir=args.output_dir,
                dry_run=args.dry_run,
                skip_gsheet=args.no_gsheet,
                skip_discord=args.no_discord,
            )
            return 0

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
        (price_path, alignment_path, failure_path, alignment_count) = scan_industry(
            loader=loader,
            stocks=stocks,
            market=args.market,
            industry=args.industry,
            output_dir=args.output_dir,
        )
        print(
            f"\n符合均線排列訊號（MA45 > MA117 > MA189）："
            f"{alignment_count} 檔"
        )
        alignment_df = pd.read_csv(alignment_path, dtype={"stock_id": str})
        if alignment_count:
            print(alignment_df.to_string(index=False))
        print(f"\n近六個月交易資料（均線使用近一年計算）：{price_path}")
        print(f"均線排列特別清單：{alignment_path}")
        if (
            not args.no_sync_gsheet
            and os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            and os.getenv("GSHEET_SPREADSHEET_ID", "").strip()
        ):
            tracker_source = _ensure_tracker_signal_columns(alignment_df)
            synced_rows = sync_alignment_to_gsheet(
                tracker_source,
                daily_directory=args.output_dir / "daily",
                replace_date=False,
            )
            print(
                f"已將均線排列清單同步寫入 Google Sheets [{SIGNAL_TRACKER_SHEET_NAME}]（共 {synced_rows} 筆紀錄）。"
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
