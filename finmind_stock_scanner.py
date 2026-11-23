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
from dataclasses import dataclass
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

import quant  # noqa: F401 - registers services/, quant/, taishin/ on sys.path
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
    "rebound_subtype",
    "support_ma",
    "support_price",
    "support_gap_pct",
    "target_ma17",
    "potential_return_pct",
    "close_strength",
    "K9",
    "D9",
    "MACD_OSC",
    "MA3",
    "MA17",
    "MA45",
    "MA117",
    "MA189",
    "ideal_buy_zone_low",
    "ideal_buy_zone_high",
    "chase_limit_price",
    "hard_stop_loss",
    "tp1_price",
    "deterministic_rr_ratio",
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


def add_momentum_indicators(prices: pd.DataFrame) -> pd.DataFrame:
    """純向量化補齊 9 日 KD (K9, D9)、MACD(12,26,9) 柱狀體 (MACD_OSC) 與成交量均線，不覆蓋既有 MA3~MA189。"""
    result = prices.copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result = (
        result.dropna(subset=["date", "close"])
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )
    if "Trading_Volume" in result.columns and "vol_lots" not in result.columns:
        vol_shares = pd.to_numeric(result["Trading_Volume"], errors="coerce")
        result["vol_lots"] = (vol_shares / 1000.0).round(0)
        result["vol_ma5_lots"] = (
            result["vol_lots"].rolling(window=5, min_periods=1).mean().round(0)
        )
        result["vol_ma20_lots"] = (
            result["vol_lots"].rolling(window=20, min_periods=1).mean().round(0)
        )

    ema12 = result["close"].ewm(span=12, adjust=False).mean()
    ema26 = result["close"].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    result["MACD_DIF"] = dif.round(4)
    result["MACD_DEA"] = dea.round(4)
    result["MACD_OSC"] = (dif - dea).round(4)

    delta = result["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 14.0, min_periods=1, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14.0, min_periods=1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    result["RSI14"] = rsi.fillna(50.0).clip(0.0, 100.0).round(2)

    bb_mid = result["close"].rolling(window=20, min_periods=5).mean()
    bb_std = result["close"].rolling(window=20, min_periods=5).std(ddof=0).fillna(0.0)
    result["BB_MID"] = bb_mid.round(2)
    result["BB_UPPER"] = (bb_mid + 2.0 * bb_std).round(2)
    result["BB_LOWER"] = (bb_mid - 2.0 * bb_std).round(2)

    high_col = "high" if "high" in result.columns else ("max" if "max" in result.columns else None)
    low_col = "low" if "low" in result.columns else ("min" if "min" in result.columns else None)
    if high_col and low_col:
        high_s = pd.to_numeric(result[high_col], errors="coerce")
        low_s = pd.to_numeric(result[low_col], errors="coerce")
        low_9 = low_s.rolling(window=9, min_periods=1).min()
        high_9 = high_s.rolling(window=9, min_periods=1).max()
        denom = (high_9 - low_9).replace(0.0, float("nan"))
        rsv = ((result["close"] - low_9) / denom * 100.0).fillna(50.0).clip(0.0, 100.0)
        result["K9"] = rsv.ewm(com=2, adjust=False).mean().round(2)
        result["D9"] = result["K9"].ewm(com=2, adjust=False).mean().round(2)
    else:
        result["K9"] = float("nan")
        result["D9"] = float("nan")
    return result


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

    return add_momentum_indicators(result)


# --------------------------------------------------------------------------- #
# SASD v5.4.0 Phase 1：確定性結構點位（算術歸 Python）與新聞量價反應對齊
# --------------------------------------------------------------------------- #
SIGNAL_RIGHT_BREAKOUT = "RIGHT_BREAKOUT"
SIGNAL_STYLE_B_PULLBACK = "STYLE_B_PULLBACK"
RR_WARNING_THRESHOLD = 1.8
NEWS_TAG_DISTRIBUTION = "【⚠️ 爆量收黑/長上影線：疑似利多出貨】"
NEWS_TAG_STRONG_BUY = "【🔥 帶量長紅實質表態：市場正面買單】"
NEWS_TAG_NEUTRAL = "【➖ 量價反應中性】"


def _normalize_ohlcv(prices: pd.DataFrame) -> pd.DataFrame:
    """統一 OHLCV 欄位（支援 FinMind 的 max/min/Trading_Volume 與一般 high/low/volume），依日期升冪。"""
    frame = prices.copy()
    rename: dict[str, str] = {}
    if "high" not in frame.columns and "max" in frame.columns:
        rename["max"] = "high"
    if "low" not in frame.columns and "min" in frame.columns:
        rename["min"] = "low"
    if "volume" not in frame.columns:
        if "Trading_Volume" in frame.columns:
            rename["Trading_Volume"] = "volume"
        elif "vol_lots" in frame.columns:
            rename["vol_lots"] = "volume"
    frame = frame.rename(columns=rename)
    for column in ("open", "high", "low", "close", "volume"):
        if column not in frame.columns:
            frame[column] = float("nan") if column != "open" else frame.get("close")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    return (
        frame.dropna(subset=["date", "close", "high", "low"])
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )


def compute_atr(frame: pd.DataFrame, period: int = 14) -> float | None:
    """近 period 日真實波動幅度均值（輸入需為已正規化、升冪之 OHLC）。"""
    if len(frame) < period + 1:
        return None
    prev_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = true_range.tail(period).mean()
    return float(value) if pd.notna(value) else None


def compute_volume_poc(frame: pd.DataFrame, window: int = 60, top_n: int = 3) -> float | None:
    """近 window 日成交量前 top_n 大交易日之典型價格 (H+L+C)/3 成交量加權均價。"""
    recent = frame.tail(window).dropna(subset=["volume"])
    recent = recent.loc[recent["volume"] > 0]
    if recent.empty:
        return None
    top = recent.nlargest(top_n, "volume")
    typical = (top["high"] + top["low"] + top["close"]) / 3.0
    total_volume = float(top["volume"].sum())
    if total_volume <= 0:
        return None
    return float((typical * top["volume"]).sum() / total_volume)


def infer_signal_type(prices: pd.DataFrame) -> str:
    """收盤仍貼近 17MA（≤ MA17 + 0.4 ATR）視為回測型，否則視為右側突破型。"""
    frame = _normalize_ohlcv(prices)
    atr = compute_atr(frame)
    if atr is None or len(frame) < 17:
        return SIGNAL_RIGHT_BREAKOUT
    ma17 = float(frame["close"].tail(17).mean())
    close_t = float(frame["close"].iloc[-1])
    return SIGNAL_STYLE_B_PULLBACK if close_t <= ma17 + 0.4 * atr else SIGNAL_RIGHT_BREAKOUT


def compute_deterministic_trade_setup(
    prices: pd.DataFrame,
    signal_type: str | None = None,
) -> dict[str, object] | None:
    """計算確定性交易點位（ATR、60 日量 POC、追價上限、硬停損、TP1、風報比）。

    資料不足（< 15 根 K 線）回傳 None。價格一律由 Python 產生，禁止 AI 自行捏造。
    """
    frame = _normalize_ohlcv(prices)
    atr = compute_atr(frame)
    if atr is None or atr <= 0 or len(frame) < 15:
        return None
    close_t = float(frame["close"].iloc[-1])
    low_t = float(frame["low"].iloc[-1])
    ma17 = float(frame["close"].tail(17).mean())
    resolved = signal_type or infer_signal_type(frame)

    poc = compute_volume_poc(frame)
    if poc is not None and poc > close_t and (poc - close_t) / close_t > 0.015:
        tp1_price = round(poc, 2)
        tp1_source = "VOLUME_POC_RESISTANCE"
    else:
        tp1_price = round(close_t + 3.0 * atr, 2)
        tp1_source = "ATR_3X_TARGET"

    if resolved == SIGNAL_STYLE_B_PULLBACK:
        buy_low = round(ma17, 2)
        buy_high = round(min(close_t, ma17 + 0.4 * atr), 2)
        buy_high = max(buy_high, buy_low)
        entry_ref = buy_high
        chase_limit = buy_high
        hard_stop = round(buy_low - 1.0 * atr, 2)
    else:
        resolved = SIGNAL_RIGHT_BREAKOUT
        entry_ref = round(close_t, 2)
        buy_low = buy_high = entry_ref
        chase_limit = round(min(close_t + 0.8 * atr, close_t * 1.035), 2)
        hard_stop = round(min(low_t, ma17) - 0.2 * atr, 2)

    risk = entry_ref - hard_stop
    rr_ratio: float | None = None
    rr_warning = ""
    if risk <= 0:
        rr_warning = "POOR_RR_RATIO"
    else:
        rr_ratio = round((tp1_price - entry_ref) / risk, 2)
        if rr_ratio < RR_WARNING_THRESHOLD:
            rr_warning = "POOR_RR_RATIO"

    return {
        "signal_type": resolved,
        "close_t": round(close_t, 2),
        "ma17_t": round(ma17, 2),
        "atr_14": round(atr, 2),
        "volume_poc_60d": round(poc, 2) if poc is not None else None,
        "entry_reference_price": entry_ref,
        "ideal_buy_zone_low": buy_low,
        "ideal_buy_zone_high": buy_high,
        "chase_limit_price": chase_limit,
        "hard_stop_loss": hard_stop,
        "tp1_price": tp1_price,
        "tp1_source": tp1_source,
        "deterministic_rr_ratio": rr_ratio,
        "rr_warning": rr_warning,
    }


def round_tw_tick(price: float) -> float:
    """依台股升降單位規則將價格對齊至合法 Tick。"""
    if price < 10.0:
        step = 0.01
    elif price < 50.0:
        step = 0.05
    elif price < 100.0:
        step = 0.1
    elif price < 500.0:
        step = 0.5
    elif price < 1000.0:
        step = 1.0
    else:
        step = 5.0
    return round(round(float(price) / step) * step, 2)


def _snap_to_psychological_support(raw_price: float) -> float:
    """台股整數心理關卡磁吸函數：若落在 50 元倍數（<100 則 10 元倍數）下方 1.0% 以內，向上吸附至關卡價。"""
    if raw_price <= 0:
        return raw_price
    unit = 50.0 if raw_price >= 95.0 else 10.0
    next_level = ((int(raw_price // unit)) + 1) * unit
    gap_ratio = (next_level - raw_price) / next_level
    if 0.0 < gap_ratio <= 0.01:
        return round_tw_tick(next_level)
    return round_tw_tick(raw_price)


def compute_rebound_trade_setup(
    prices: pd.DataFrame,
    *,
    support_ma: str | None = None,
    support_price: float | None = None,
    rebound_subtype: str | None = None,
) -> dict[str, object] | None:
    """計算型態 A 反彈專屬確定性點位與 3 層結構錨點選單 (SASD v6.0)。"""
    frame = _normalize_ohlcv(prices)
    atr = compute_atr(frame)
    if atr is None or atr <= 0 or len(frame) < 15:
        return None

    close_t = float(frame["close"].iloc[-1])
    last_row = frame.iloc[-1]
    ma17 = _parse_optional_float(last_row.get("MA17")) or float(frame["close"].tail(17).mean())
    ma45 = _parse_optional_float(last_row.get("MA45")) or (
        float(frame["close"].tail(45).mean()) if len(frame) >= 45 else ma17
    )
    ma117 = _parse_optional_float(last_row.get("MA117")) or (
        float(frame["close"].tail(117).mean()) if len(frame) >= 117 else ma45
    )
    ma189 = _parse_optional_float(last_row.get("MA189")) or (
        float(frame["close"].tail(189).mean()) if len(frame) >= 189 else ma117
    )

    ma_lookup = {"45MA": ma45, "117MA": ma117, "189MA": ma189}
    resolved_ma_name = support_ma if support_ma in ma_lookup else None
    p_ma = float(support_price) if (support_price is not None and support_price > 0) else 0.0
    if p_ma <= 0:
        if resolved_ma_name:
            p_ma = ma_lookup[resolved_ma_name]
        else:
            # 自動挑選最貼近 close_t 且 <= close_t * 1.02 的長均線
            candidates = [(name, val) for name, val in ma_lookup.items() if val > 0]
            resolved_ma_name, p_ma = min(candidates, key=lambda item: abs(close_t - item[1]))

    recent_12 = frame.tail(12)
    l12_idx = recent_12["low"].idxmin()
    l12 = float(recent_12.loc[l12_idx, "low"])
    after_l12 = frame.loc[l12_idx:]
    h_rebound = float(after_l12["high"].max()) if not after_l12.empty else float(recent_12["high"].max())
    h_rebound = max(h_rebound, close_t, l12)

    p_fib_30 = l12 + 0.30 * (h_rebound - l12)
    p_fib_786 = l12 + 0.214 * (h_rebound - l12)
    magnet_ma = _snap_to_psychological_support(p_ma)

    buy_low = round_tw_tick(p_ma * 1.005)
    buy_high = round_tw_tick(max(p_fib_30, magnet_ma, buy_low))
    if rebound_subtype == "REBOUND_TOUCH_HOLD" and close_t < buy_low:
        buy_low = round_tw_tick(min(close_t, p_ma * 1.005))
        buy_high = round_tw_tick(max(close_t, magnet_ma))

    entry_ref = round_tw_tick((buy_low + buy_high) / 2.0)
    chase_limit = buy_high
    hard_stop = round_tw_tick(min(l12 * 0.994, buy_low - 1.2 * atr))

    poc = compute_volume_poc(frame)
    overhead_candidates: list[tuple[float, str]] = []
    if ma17 > buy_high * 1.02:
        overhead_candidates.append((ma17, "MA17_RESISTANCE"))
    if ma117 > buy_high * 1.02 and ma117 != p_ma:
        overhead_candidates.append((ma117, "MA117_RESISTANCE"))
    if poc is not None and poc > buy_high * 1.02:
        overhead_candidates.append((poc, "VOLUME_POC_RESISTANCE"))

    if overhead_candidates:
        raw_tp1, tp1_source = min(overhead_candidates, key=lambda item: item[0])
        tp1_price = round_tw_tick(raw_tp1)
    else:
        tp1_price = round_tw_tick(buy_high + 2.5 * atr)
        tp1_source = "ATR_2_5X_REBOUND_TARGET"

    risk = buy_high - hard_stop
    rr_ratio: float | None = None
    rr_warning = ""
    if risk <= 0:
        rr_warning = "POOR_RR_RATIO"
    else:
        rr_ratio = round((tp1_price - buy_high) / risk, 2)
        if rr_ratio < RR_WARNING_THRESHOLD:
            rr_warning = "POOR_RR_RATIO"

    tier2_low = round_tw_tick(min(p_fib_786, buy_low * 0.985))
    tier2_high = round_tw_tick(max(p_fib_786, buy_low * 0.992))
    tier3_low = round_tw_tick(l12)
    tier3_high = round_tw_tick(max(l12 * 1.015, hard_stop + 0.5 * atr))

    return {
        "signal_type": SIGNAL_STYLE_B_PULLBACK,
        "rebound_subtype": rebound_subtype or "REBOUND_TOUCH_HOLD",
        "support_ma": resolved_ma_name or "189MA",
        "support_price": round(p_ma, 2),
        "close_t": round(close_t, 2),
        "ma17_t": round(ma17, 2),
        "atr_14": round(atr, 2),
        "left_foot_low_12d": round(l12, 2),
        "rebound_high_12d": round(h_rebound, 2),
        "volume_poc_60d": round(poc, 2) if poc is not None else None,
        "entry_reference_price": entry_ref,
        "ideal_buy_zone_low": buy_low,
        "ideal_buy_zone_high": buy_high,
        "chase_limit_price": chase_limit,
        "hard_stop_loss": hard_stop,
        "tp1_price": tp1_price,
        "tp1_source": tp1_source,
        "deterministic_rr_ratio": rr_ratio,
        "rr_warning": rr_warning,
        "tier1_price": buy_high,
        "tier2_price": tier2_high,
        "tier3_price": tier3_high,
        "anchor_tiers": {
            "tier1_aggressive_support": f"{buy_low:.2f}~{buy_high:.2f}（{resolved_ma_name or '長均線'}與整數關卡）",
            "tier2_steady_washout": f"{tier2_low:.2f}~{tier2_high:.2f}（0.786深度回檔/破線洗盤區）",
            "tier3_extreme_left_foot": f"{tier3_low:.2f}~{tier3_high:.2f}（前波左腳低點防守區）",
        },
    }


def _classify_candle_shadow(open_p: float, high_p: float, low_p: float, close_p: float) -> str:
    rng = high_p - low_p
    if rng <= 0:
        return "一字/平盤"
    body = abs(close_p - open_p)
    upper = high_p - max(open_p, close_p)
    lower = min(open_p, close_p) - low_p
    if lower >= rng * 0.42 and lower > upper * 1.3:
        return "🛡️長下影承接"
    if upper >= rng * 0.42 and upper > lower * 1.3:
        return "⚠️長上影壓力"
    if close_p > open_p and body >= rng * 0.65:
        return "🔥實體長紅"
    if close_p < open_p and body >= rng * 0.65:
        return "🌧️實體長黑"
    return "一般震盪"


def compute_relative_strength_and_beta(
    stock_prices: pd.DataFrame,
    benchmark_prices: pd.DataFrame | None,
) -> dict[str, Any] | None:
    """純本地計算個股相對大盤之近 5 日 / 20 日超額報酬 (RS) 與 60 日 Beta 值（零外部 API 配額）。"""
    if (
        stock_prices is None
        or stock_prices.empty
        or benchmark_prices is None
        or benchmark_prices.empty
    ):
        return None
    s_norm = _normalize_ohlcv(stock_prices)
    b_norm = _normalize_ohlcv(benchmark_prices)
    if s_norm.empty or b_norm.empty:
        return None

    s_df = s_norm[["date", "close"]].rename(columns={"close": "s_close"}).drop_duplicates(subset=["date"], keep="last")
    b_df = b_norm[["date", "close"]].rename(columns={"close": "b_close"}).drop_duplicates(subset=["date"], keep="last")
    merged = pd.merge(s_df, b_df, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if len(merged) < 6:
        return None

    s_close = merged["s_close"].astype(float)
    b_close = merged["b_close"].astype(float)

    def _window_ret(series: pd.Series, window: int) -> float | None:
        if len(series) <= window:
            return None
        base = float(series.iloc[-(window + 1)])
        cur = float(series.iloc[-1])
        if base <= 0:
            return None
        return round((cur / base - 1.0) * 100.0, 2)

    s_ret_5d = _window_ret(s_close, 5)
    b_ret_5d = _window_ret(b_close, 5)
    rs_5d = round(s_ret_5d - b_ret_5d, 2) if (s_ret_5d is not None and b_ret_5d is not None) else None

    s_ret_20d = _window_ret(s_close, 20)
    b_ret_20d = _window_ret(b_close, 20)
    rs_20d = round(s_ret_20d - b_ret_20d, 2) if (s_ret_20d is not None and b_ret_20d is not None) else None

    beta_60d: float | None = None
    ret_df = pd.DataFrame({
        "s_ret": s_close.pct_change(),
        "b_ret": b_close.pct_change(),
    }).dropna().tail(60)
    if len(ret_df) >= 20:
        var_b = float(ret_df["b_ret"].var())
        if var_b > 1e-12:
            cov_sb = float(ret_df["s_ret"].cov(ret_df["b_ret"]))
            beta_60d = round(cov_sb / var_b, 2)

    if rs_5d is None and rs_20d is None and beta_60d is None:
        return None

    # 抗跌／強弱風格標籤
    tag = "➖ 隨大盤同步"
    if beta_60d is not None and beta_60d <= 0.85 and (rs_5d or 0.0) >= 1.0:
        tag = "🛡️ 低Beta相對抗跌防禦"
    elif (rs_5d or 0.0) >= 2.0 and (rs_20d or 0.0) >= 3.0:
        tag = "🔥 強於大盤領漲強勢"
    elif beta_60d is not None and beta_60d >= 1.25 and (rs_5d or 0.0) <= -2.0:
        tag = "⚠️ 高Beta補跌弱勢"
    elif (rs_5d or 0.0) <= -2.0 and (rs_20d or 0.0) <= -3.0:
        tag = "⚠️ 弱於大盤修正"

    parts: list[str] = []
    if rs_5d is not None:
        parts.append(f"近5日超額報酬 {rs_5d:+.2f}%（個股 {s_ret_5d:+.2f}% vs 大盤 {b_ret_5d:+.2f}%）")
    if rs_20d is not None:
        parts.append(f"近20日超額報酬 {rs_20d:+.2f}%")
    if beta_60d is not None:
        parts.append(f"60日 Beta={beta_60d:.2f}（{tag}）")

    return {
        "rs_5d_pct": rs_5d,
        "rs_20d_pct": rs_20d,
        "beta_60d": beta_60d,
        "rs_tag": tag,
        "summary": "｜".join(parts),
    }


def summarize_tdcc_multiweek_trend(
    df_holding: pd.DataFrame | None,
    *,
    paid_in_capital_twd: float | None = None,
    close_price: float | None = None,
) -> dict[str, Any] | None:
    """將近 8~12 週集保股權分散表濃縮成高訊號單行摘要（含動態 400/1000 張門檻與資料基準日）。

    動態大戶門檻：
    - 股價 > 200 元 或 股本 < 50 億（含缺股本）➔ 採 400 張以上大戶
    - 股本 >= 50 億 且 股價 <= 200 元 ➔ 採 1,000 張以上大戶
    """
    if df_holding is None or not isinstance(df_holding, pd.DataFrame) or df_holding.empty:
        return None
    frame = df_holding.copy()
    if "date" not in frame.columns or "HoldingSharesLevel" not in frame.columns:
        return None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["percent"] = pd.to_numeric(frame.get("percent"), errors="coerce")
    frame = frame.dropna(subset=["date", "percent"])
    if frame.empty:
        return None

    dates = sorted(frame["date"].unique())
    if not dates:
        return None

    use_1000 = (
        paid_in_capital_twd is not None
        and paid_in_capital_twd >= 5_000_000_000
        and (close_price is None or close_price <= 200.0)
    )
    big_lots = 1000 if use_1000 else 400
    threshold_shares = big_lots * 1000

    def _calc_day_stats(day: object) -> tuple[float, float, int | None]:
        day_rows = frame.loc[frame["date"] == day]
        major_pct = 0.0
        retail_pct = 0.0
        for row in day_rows.itertuples(index=False):
            lvl_raw = str(row.HoldingSharesLevel).strip()
            lower = _holding_level_lower_bound(lvl_raw)
            if lower is None:
                continue
            pct_v = float(row.percent)
            if lower >= threshold_shares:
                major_pct += pct_v
            # 10 張以下散戶：級距 1 (1-999)、2 (1,000-5,000)、3 (5,001-10,000) -> lower <= 5001
            if lower <= 5001.0:
                retail_pct += pct_v

        people_total: int | None = None
        if "people" in frame.columns:
            lvl_s = day_rows["HoldingSharesLevel"].astype(str).str.strip().str.lower()
            tot_rows = day_rows.loc[(lvl_s == "17") | lvl_s.str.contains("total|合計", regex=True)]
            if not tot_rows.empty:
                p_vals = pd.to_numeric(tot_rows["people"], errors="coerce").dropna()
                if not p_vals.empty and float(p_vals.iloc[0]) > 0:
                    people_total = int(round(float(p_vals.iloc[0])))
            if people_total is None:
                non_diff = day_rows.loc[~lvl_s.isin(["16"]) & ~lvl_s.str.contains("差異", regex=False)]
                p_sum = float(pd.to_numeric(non_diff["people"], errors="coerce").sum())
                if p_sum > 0:
                    people_total = int(round(p_sum))
        return round(major_pct, 2), round(retail_pct, 2), people_total

    weekly_stats: list[tuple[pd.Timestamp, float, float, int | None]] = []
    for d in dates:
        m_pct, r_pct, ppl = _calc_day_stats(d)
        weekly_stats.append((pd.Timestamp(d), m_pct, r_pct, ppl))

    latest_dt, latest_major, latest_retail, latest_people = weekly_stats[-1]
    base_date = latest_dt.strftime("%Y-%m-%d")

    def _pick_ago(weeks_ago: int) -> tuple[float, float, int | None] | None:
        if len(weekly_stats) <= weeks_ago:
            return None
        _, mp, rp, pp = weekly_stats[-(weeks_ago + 1)]
        return mp, rp, pp

    w1 = _pick_ago(1)
    w4 = _pick_ago(4)
    w8 = _pick_ago(8)
    w12 = _pick_ago(12)

    diff_1w = round(latest_major - w1[0], 2) if w1 else None
    diff_4w = round(latest_major - w4[0], 2) if w4 else None
    diff_8w = round(latest_major - w8[0], 2) if w8 else None
    diff_12w = round(latest_major - w12[0], 2) if w12 else None

    retail_diff_4w = round(latest_retail - w4[1], 2) if w4 else None
    people_diff_4w = (
        int(latest_people - w4[2])
        if (w4 and latest_people is not None and w4[2] is not None)
        else None
    )

    # 計算大戶連續增減週數
    streak_text = ""
    if len(weekly_stats) >= 2:
        last_step = round(weekly_stats[-1][1] - weekly_stats[-2][1], 4)
        if last_step != 0:
            direction = 1 if last_step > 0 else -1
            streak = 1
            for idx in range(len(weekly_stats) - 2, 0, -1):
                step = round(weekly_stats[idx][1] - weekly_stats[idx - 1][1], 4)
                if (direction > 0 and step > 0) or (direction < 0 and step < 0):
                    streak += 1
                else:
                    break
            if streak >= 2:
                streak_text = f"，連 {streak} 週{'增' if direction > 0 else '減'}"

    major_diffs: list[str] = []
    if diff_1w is not None:
        major_diffs.append(f"近1週 {diff_1w:+.1f}%")
    if diff_4w is not None:
        major_diffs.append(f"近4週 {diff_4w:+.1f}%")
    if diff_8w is not None:
        major_diffs.append(f"近8週 {diff_8w:+.1f}%")
    elif diff_12w is not None:
        major_diffs.append(f"近12週 {diff_12w:+.1f}%")
    major_diff_str = f"（{' / '.join(major_diffs)}{streak_text}）" if major_diffs else "（僅有單週快照）"

    retail_part = f"10張以下散戶 {latest_retail:.1f}%"
    if retail_diff_4w is not None:
        retail_part += f"（近4週 {retail_diff_4w:+.1f}%）"

    people_part = ""
    if latest_people is not None:
        people_part = f"｜總股東人數 {latest_people:,} 人"
        if people_diff_4w is not None:
            people_part += f"（近4週 {people_diff_4w:+,} 人）"

    summary = (
        f"基準日 {base_date}：{big_lots}張以上大戶 {latest_major:.1f}%{major_diff_str}"
        f"｜{retail_part}{people_part}"
    )
    return {
        "tdcc_base_date": base_date,
        "major_threshold_lots": big_lots,
        "latest_major_pct": latest_major,
        "diff_1w_pct": diff_1w,
        "diff_4w_pct": diff_4w,
        "diff_8w_pct": diff_8w,
        "latest_retail_pct": latest_retail,
        "retail_diff_4w_pct": retail_diff_4w,
        "latest_total_shareholders": latest_people,
        "people_diff_4w": people_diff_4w,
        "weeks_available": len(weekly_stats),
        "summary": summary,
    }


def build_strategic_tech_and_chip_package(
    stock_id: str,
    stock_name: str,
    prices: pd.DataFrame,
    *,
    daily_chip_df: pd.DataFrame | None = None,
    benchmark_prices: pd.DataFrame | None = None,
    tdcc_holding_df: pd.DataFrame | None = None,
    paid_in_capital_twd: float | None = None,
    forward_eps: float | None = None,
    peer_median_pe: float | None = None,
    annual_cash_dividend: float | None = None,
    selection_mode: str = "trend",
) -> dict[str, object]:
    """SASD v6.1：為 AI #3 組裝單一績優股之「20日逐日價量均線指標籌碼合一表」與「三大大量支撐定位器 + 60/120日結構錨點」。"""
    norm = _normalize_ohlcv(prices)
    if norm.empty:
        return {"prompt_block": f"### {stock_id} {stock_name}（無日線資料）"}

    enriched = add_momentum_indicators(norm)
    for period in MOVING_AVERAGE_PERIODS:
        col = f"MA{period}"
        if col not in enriched.columns or enriched[col].isna().all():
            enriched[col] = enriched["close"].rolling(window=period, min_periods=1).mean()
        else:
            enriched[col] = pd.to_numeric(enriched[col], errors="coerce").fillna(
                enriched["close"].rolling(window=period, min_periods=1).mean()
            )
    if "vol_lots" not in enriched.columns:
        vol_s = pd.to_numeric(enriched["volume"], errors="coerce").fillna(0.0)
        is_shares = "Trading_Volume" in prices.columns or ("vol_lots" not in prices.columns and vol_s.max() > 500_000)
        enriched["vol_lots"] = (vol_s / 1000.0).round(0) if is_shares else vol_s.round(0)
    else:
        enriched["vol_lots"] = pd.to_numeric(enriched["vol_lots"], errors="coerce").fillna(0.0).round(0)
    enriched["vol_ma5_lots"] = enriched["vol_lots"].rolling(window=5, min_periods=1).mean().round(0)
    enriched["vol_ma20_lots"] = enriched["vol_lots"].rolling(window=20, min_periods=1).mean().round(0)
    enriched["date_str"] = enriched["date"].dt.strftime("%Y-%m-%d")
    enriched["pct_chg"] = (enriched["close"].pct_change() * 100.0).fillna(0.0).round(2)

    # 合併逐日三大法人與融資明細
    chip_cols = ["foreign_net_lots", "trust_net_lots", "dealer_net_lots", "margin_chg_lots", "margin_balance_lots"]
    if daily_chip_df is not None and not daily_chip_df.empty and "date" in daily_chip_df.columns:
        c_df = daily_chip_df.copy()
        c_df["date_str"] = pd.to_datetime(c_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        c_df = c_df.dropna(subset=["date_str"]).drop_duplicates(subset=["date_str"], keep="last")
        keep_c = ["date_str", *[c for c in chip_cols if c in c_df.columns]]
        enriched = pd.merge(enriched, c_df[keep_c], on="date_str", how="left")
    for c in chip_cols:
        if c not in enriched.columns:
            enriched[c] = float("nan")
        enriched[c] = pd.to_numeric(enriched[c], errors="coerce")

    close_t = float(enriched["close"].iloc[-1])
    atr = compute_atr(enriched) or round(close_t * 0.03, 2)
    w60 = enriched.tail(60).copy()
    w120 = enriched.tail(120).copy()

    # 1) 三大大量支撐定位器 (High-Volume Support Locator)
    # 1A. 60日分價量表 (Volume Profile: POC / VAH / VAL + 現價下方最大量支撐區 Lower Support POC #1 & #2)
    poc_60d = compute_volume_poc(w60) or close_t
    w60["typical_price"] = (w60["high"] + w60["low"] + w60["close"]) / 3.0
    total_v60 = float(w60["vol_lots"].sum()) or 1.0
    p_min_60, p_max_60 = float(w60["low"].min()), float(w60["high"].max())
    n_bins = 12
    lower_support_nodes: list[str] = []
    vah_60d, val_60d = round_tw_tick(p_max_60), round_tw_tick(p_min_60)
    if p_max_60 > p_min_60 and len(w60) >= 5:
        bin_edges = [p_min_60 + i * (p_max_60 - p_min_60) / n_bins for i in range(n_bins + 1)]
        bin_vols: list[tuple[float, float, float, float]] = []
        for b_idx in range(n_bins):
            b_lo, b_hi = bin_edges[b_idx], bin_edges[b_idx + 1]
            b_mid = (b_lo + b_hi) / 2.0
            mask = (w60["typical_price"] >= b_lo) & (
                w60["typical_price"] <= b_hi if b_idx == n_bins - 1 else w60["typical_price"] < b_hi
            )
            v_sum = float(w60.loc[mask, "vol_lots"].sum())
            bin_vols.append((b_lo, b_hi, b_mid, v_sum))
        sorted_bins = sorted(bin_vols, key=lambda x: x[3], reverse=True)
        acc_v = 0.0
        va_lows, va_highs = [], []
        for b_lo, b_hi, _b_mid, v_sum in sorted_bins:
            va_lows.append(b_lo)
            va_highs.append(b_hi)
            acc_v += v_sum
            if acc_v >= total_v60 * 0.70:
                break
        if va_lows and va_highs:
            val_60d = round_tw_tick(min(va_lows))
            vah_60d = round_tw_tick(max(va_highs))
        below_bins = [b for b in sorted_bins if b[1] <= close_t and b[3] > 0]
        if not below_bins:
            # 若現價正落於最低分箱內，退而允許區間中點 <= 現價
            below_bins = [b for b in sorted_bins if b[2] <= close_t and b[3] > 0]
        for rank_i, (b_lo, b_hi, _b_mid, v_sum) in enumerate(below_bins[:2], start=1):
            share_pct = round(v_sum / total_v60 * 100.0, 1)
            lo_t, hi_t = round_tw_tick(b_lo), round_tw_tick(b_hi)
            zone_txt = f"{lo_t:.2f}" if lo_t == hi_t else f"{lo_t:.2f}~{hi_t:.2f}"
            lower_support_nodes.append(
                f"下檔大量支撐區 #{rank_i}：{zone_txt} 元"
                f"（累計 {int(v_sum):,} 張，佔60日量 {share_pct}%）"
            )
    if not lower_support_nodes:
        lower_support_nodes.append(f"現價處於近60日底部區（60日最低 {round_tw_tick(p_min_60):.2f} 元）")

    # 1B. 近60日前3大「爆量紅K／長下影線承接日」防守低點
    bull_mask = (w60["vol_lots"] >= w60["vol_ma20_lots"] * 1.2) & (
        (w60["close"] >= w60["open"])
        | ((w60[["open", "close"]].min(axis=1) - w60["low"]) >= (w60["high"] - w60["low"]) * 0.38)
    )
    bull_bars = w60.loc[bull_mask].nlargest(3, "vol_lots")
    if bull_bars.empty:
        bull_bars = w60.nlargest(3, "vol_lots")
    key_bullish_bars: list[str] = []
    for _, brow in bull_bars.iterrows():
        f_txt = f"，外資{brow['foreign_net_lots']:+.0f}張" if pd.notna(brow.get("foreign_net_lots")) else ""
        t_txt = f"，投信{brow['trust_net_lots']:+.0f}張" if pd.notna(brow.get("trust_net_lots")) else ""
        key_bullish_bars.append(
            f"[{brow['date_str']}] 爆量 {int(brow['vol_lots']):,} 張（20MA量 {int(brow['vol_ma20_lots']):,} 張）"
            f"｜鐵板防守低點 {float(brow['low']):.2f} 元 ～ 收盤 {float(brow['close']):.2f} 元{t_txt}{f_txt}"
        )

    # 1C. 投信／外資近20日與60日買超加權成本均價 (Institutional VWAP) 與最大買超日
    def _calc_inst_vwap(df_window: pd.DataFrame, col_name: str) -> float | None:
        sub = df_window.dropna(subset=[col_name]).loc[lambda d: d[col_name] > 0]
        if sub.empty:
            return None
        w_sum = float(sub[col_name].sum())
        if w_sum <= 0:
            return None
        typ = (sub["high"] + sub["low"] + sub["close"]) / 3.0
        return round_tw_tick(float((typ * sub[col_name]).sum() / w_sum))

    w20 = enriched.tail(20).copy()
    trust_vwap_20 = _calc_inst_vwap(w20, "trust_net_lots")
    trust_vwap_60 = _calc_inst_vwap(w60, "trust_net_lots")
    foreign_vwap_20 = _calc_inst_vwap(w20, "foreign_net_lots")
    foreign_vwap_60 = _calc_inst_vwap(w60, "foreign_net_lots")

    top_inst_days: list[str] = []
    if w60["trust_net_lots"].notna().any() or w60["foreign_net_lots"].notna().any():
        w60["inst_buy_score"] = w60["trust_net_lots"].fillna(0.0) * 1.5 + w60["foreign_net_lots"].fillna(0.0)
        top_inst = w60.loc[w60["inst_buy_score"] > 0].nlargest(3, "inst_buy_score")
        for _, irow in top_inst.iterrows():
            t_v = 0.0 if pd.isna(irow.get("trust_net_lots")) else float(irow["trust_net_lots"])
            f_v = 0.0 if pd.isna(irow.get("foreign_net_lots")) else float(irow["foreign_net_lots"])
            top_inst_days.append(
                f"[{irow['date_str']}] 區間 {float(irow['low']):.2f}~{float(irow['close']):.2f} 元"
                f"（投信 {t_v:+.0f} 張 / 外資 {f_v:+.0f} 張）"
            )

    # 2) 近60/120日高低點、黃金分割回檔、未回補跳空缺口、均線斜率與未來5日扣抵價、基本面價值鐵底
    h60_idx, l60_idx = w60["high"].idxmax(), w60["low"].idxmin()
    h60_val, h60_date = float(w60.loc[h60_idx, "high"]), str(w60.loc[h60_idx, "date_str"])
    l60_val, l60_date = float(w60.loc[l60_idx, "low"]), str(w60.loc[l60_idx, "date_str"])
    h120_val = float(w120["high"].max())
    l120_val = float(w120["low"].min())
    span_60 = max(h60_val - l60_val, 0.01)
    fib_236 = round_tw_tick(h60_val - 0.236 * span_60)
    fib_382 = round_tw_tick(h60_val - 0.382 * span_60)
    fib_500 = round_tw_tick(h60_val - 0.500 * span_60)
    fib_618 = round_tw_tick(h60_val - 0.618 * span_60)
    fib_786 = round_tw_tick(h60_val - 0.786 * span_60)

    # 未回補跳空缺口 (Unfilled Gaps in 60d)
    unfilled_up_gaps: list[str] = []
    unfilled_down_gaps: list[str] = []
    w60_reset = w60.reset_index(drop=True)
    for idx in range(1, len(w60_reset)):
        prev_hi = float(w60_reset.loc[idx - 1, "high"])
        prev_lo = float(w60_reset.loc[idx - 1, "low"])
        cur_lo = float(w60_reset.loc[idx, "low"])
        cur_hi = float(w60_reset.loc[idx, "high"])
        g_date = str(w60_reset.loc[idx, "date_str"])
        subsequent = w60_reset.iloc[idx + 1 :]
        if cur_lo > prev_hi * 1.003:
            min_after = float(subsequent["low"].min()) if not subsequent.empty else cur_lo
            if min_after > prev_hi:
                unfilled_up_gaps.append(f"[{g_date} 向上突破缺口支撐] {prev_hi:.2f}~{cur_lo:.2f} 元")
        elif cur_hi < prev_lo * 0.997:
            max_after = float(subsequent["high"].max()) if not subsequent.empty else cur_hi
            if max_after < prev_lo:
                unfilled_down_gaps.append(f"[{g_date} 向下跳空缺口反壓] {cur_hi:.2f}~{prev_lo:.2f} 元")

    # 五線均線斜率與未來5日扣抵價
    last_r = enriched.iloc[-1]
    ma_deduction_parts: list[str] = []
    for p in (3, 17, 45, 117, 189):
        ma_v = float(last_r.get(f"MA{p}") or close_t)
        bias_pct = (close_t - ma_v) / ma_v * 100.0 if ma_v > 0 else 0.0
        prev5_idx = max(0, len(enriched) - 6)
        ma_prev5 = float(enriched.iloc[prev5_idx].get(f"MA{p}") or ma_v)
        slope5 = (ma_v - ma_prev5) / ma_prev5 * 100.0 if ma_prev5 > 0 else 0.0
        if p >= 5 and len(enriched) >= p:
            ded_idx = min(len(enriched) - 1, max(0, len(enriched) - p + 4))
            ded_price = float(enriched.iloc[ded_idx]["close"])
            ded_txt = f"/5日後扣抵{ded_price:.2f}"
        else:
            ded_txt = ""
        ma_deduction_parts.append(
            f"MA{p}={ma_v:.2f}(乖離{bias_pct:+.1f}%/5日斜率{slope5:+.1f}%{ded_txt})"
        )

    # 基本面價值鐵底與前瞻 EPS 參考（明數輸出，避免 AI 反推算術誤差）
    val_floors: list[str] = []
    if forward_eps and forward_eps > 0:
        conservative_pe = max(10.0, (peer_median_pe or 16.0) * 0.78)
        pe_floor = round_tw_tick(forward_eps * conservative_pe)
        if peer_median_pe and peer_median_pe > 0:
            fair_pe_val = round_tw_tick(forward_eps * peer_median_pe)
            val_floors.append(
                f"本業前瞻EPS={forward_eps:.2f}元（同業中位{peer_median_pe:.1f}x PE合理價={fair_pe_val:.2f}元／保守底{conservative_pe:.1f}x PE={pe_floor:.2f}元）"
            )
        else:
            val_floors.append(
                f"本業前瞻EPS={forward_eps:.2f}元（保守估值底{conservative_pe:.1f}x PE={pe_floor:.2f}元）"
            )
    if annual_cash_dividend and annual_cash_dividend > 0:
        div_4pct_floor = round_tw_tick(annual_cash_dividend / 0.04)
        val_floors.append(f"4%殖利率保護價={div_4pct_floor:.2f}元")

    # 3) 組裝近 20 個交易日「價、量、五線均線、KD/RSI/MACD、外資與投信、融資」合一逐日大表
    table_lines = [
        "| 日期 | 開盤 | 最高 | 最低 | 收盤 | 漲跌% | K棒特徵 | 量(張)/20MA量 | MA3 | MA17 | MA45 | MA117 | MA189 | K9/D9 | RSI14 | MACD柱 | 外資(張) | 投信(張) | 融資增減(餘額) |",
        "|:---|---:|---:|---:|---:|---:|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in w20.iterrows():
        c_tag = _classify_candle_shadow(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
        f_str = f"{r['foreign_net_lots']:+.0f}" if pd.notna(r.get("foreign_net_lots")) else "-"
        t_str = f"{r['trust_net_lots']:+.0f}" if pd.notna(r.get("trust_net_lots")) else "-"
        if pd.notna(r.get("margin_chg_lots")) and pd.notna(r.get("margin_balance_lots")):
            m_str = f"{r['margin_chg_lots']:+.0f}({int(r['margin_balance_lots'])})"
        elif pd.notna(r.get("margin_balance_lots")):
            m_str = f"-({int(r['margin_balance_lots'])})"
        else:
            m_str = "-"
        table_lines.append(
            f"| {r['date_str']} | {float(r['open']):.2f} | {float(r['high']):.2f} | {float(r['low']):.2f} | "
            f"{float(r['close']):.2f} | {float(r['pct_chg']):+.2f}% | {c_tag} | "
            f"{int(r['vol_lots'])}/{int(r['vol_ma20_lots'])} | {float(r['MA3']):.2f} | {float(r['MA17']):.2f} | "
            f"{float(r['MA45']):.2f} | {float(r['MA117']):.2f} | {float(r['MA189']):.2f} | "
            f"{float(r['K9']):.1f}/{float(r['D9']):.1f} | {float(r['RSI14']):.1f} | {float(r['MACD_OSC']):+.2f} | "
            f"{f_str} | {t_str} | {m_str} |"
        )

    # 預設三階回檔錨點供 Python 兜底與 AI #3 參考
    if (selection_mode or "").strip().lower() == "rebound":
        base_setup = compute_rebound_trade_setup(enriched) or {}
        anchor_tiers = base_setup.get("anchor_tiers") or {}
        t1_ref = str(anchor_tiers.get("tier1_aggressive_support") or f"{round_tw_tick(close_t * 0.98):.2f}")
        t2_ref = str(anchor_tiers.get("tier2_steady_washout") or f"{fib_618:.2f}~{fib_500:.2f}")
        t3_ref = str(anchor_tiers.get("tier3_extreme_left_foot") or f"{l60_val:.2f}~{fib_786:.2f}")
        t1_price = float(base_setup.get("tier1_price") or base_setup.get("ideal_buy_zone_high") or round_tw_tick(close_t * 0.98))
        t2_price = float(base_setup.get("tier2_price") or round_tw_tick(max(fib_618, fib_500)))
        t3_price = float(base_setup.get("tier3_price") or round_tw_tick(max(l60_val, fib_786)))
        chase_lim = float(base_setup.get("chase_limit_price") or round_tw_tick(close_t))
        hard_sl = float(base_setup.get("hard_stop_loss") or round_tw_tick(l60_val * 0.98))
        tp1_def = float(base_setup.get("tp1_price") or round_tw_tick(float(last_r["MA17"])))
    else:
        ma17_v = float(last_r["MA17"])
        ma45_v = float(last_r["MA45"])
        ma117_v = float(last_r["MA117"])
        ma189_v = float(last_r.get("MA189") or ma117_v)
        is_broken_below_ma = (close_t < ma17_v) or (close_t < ma45_v)

        def _fmt_zone(a: float, b: float) -> str:
            lo_v, hi_v = min(a, b), max(a, b)
            return f"{lo_v:.2f}" if abs(hi_v - lo_v) < 1e-6 else f"{lo_v:.2f}~{hi_v:.2f}"

        if is_broken_below_ma:
            # 破線防禦模式：現價已跌破 17MA 或 45MA，嚴禁將上方均線當支撐，改依現價下方結構階梯承接
            t1_high = round_tw_tick(close_t * 0.99)
            t1_low = round_tw_tick(min(t1_high, close_t - 0.5 * atr))
            t2_high = round_tw_tick(min(t1_low * 0.99, close_t - 1.0 * atr))
            lower_ma_anchor = max(v for v in (ma117_v, ma189_v, l60_val) if v < t2_high) if any(
                v < t2_high for v in (ma117_v, ma189_v, l60_val)
            ) else round_tw_tick(t2_high - 0.5 * atr)
            t2_low = round_tw_tick(min(t2_high, max(lower_ma_anchor, close_t - 1.5 * atr)))
            t3_high = round_tw_tick(min(t2_low * 0.99, close_t - 1.8 * atr))
            t3_low = round_tw_tick(min(t3_high, max(l60_val * 0.98, close_t - 2.3 * atr)))
            t1_ref = f"{_fmt_zone(t1_low, t1_high)}（⚠️破線防禦：現價下方止穩試單區，上方17MA/45MA已轉反壓）"
            t2_ref = f"{_fmt_zone(t2_low, t2_high)}（下檔大量支撐/MA117核心承接區）"
            t3_ref = f"{_fmt_zone(t3_low, t3_high)}（左側極限黃金坑/MA189與前低防守區）"
            t1_price = max(t1_low, t1_high)
            t2_price = max(t2_low, t2_high)
            t3_price = max(t3_low, t3_high)
            chase_lim = round_tw_tick(close_t)
            hard_sl = round_tw_tick(min(min(t3_low, t3_high) * 0.97, ma189_v * 0.97, l60_val * 0.97))
            tp1_def = round_tw_tick(max(ma17_v, ma45_v, close_t + 1.5 * atr))
        else:
            # 正常多頭排列模式：強制嚴格遞減 t3 < t2 < t1 <= close_t
            t1_high = round_tw_tick(min(close_t * 0.995, max(ma17_v, close_t - 0.3 * atr)))
            t1_low = round_tw_tick(min(t1_high, max(ma17_v, min(fib_236, close_t * 0.985))))
            t2_high = round_tw_tick(min(t1_low * 0.995, max(ma17_v, fib_382) if max(ma17_v, fib_382) < t1_low else t1_low * 0.98))
            t2_low = round_tw_tick(min(t2_high, min(ma17_v, fib_382) if min(ma17_v, fib_382) <= t2_high else t2_high * 0.98))
            t3_high = round_tw_tick(min(t2_low * 0.995, max(ma45_v, fib_500) if max(ma45_v, fib_500) < t2_low else t2_low * 0.98))
            t3_low = round_tw_tick(min(t3_high, min(ma45_v, fib_618) if min(ma45_v, fib_618) <= t3_high else t3_high * 0.98))
            t1_ref = f"{_fmt_zone(t1_low, t1_high)}（回測MA3~MA17/0.236回檔量縮區）"
            t2_ref = f"{_fmt_zone(t2_low, t2_high)}（回測17MA與0.382黃金分割核心支撐區）"
            t3_ref = f"{_fmt_zone(t3_low, t3_high)}（大盤急殺回測45MA季線/0.618黃金坑防守區）"
            t1_price = max(t1_low, t1_high)
            t2_price = max(t2_low, t2_high)
            t3_price = max(t3_low, t3_high)
            chase_lim = round_tw_tick(min(close_t, ma17_v * 1.06))
            hard_sl = round_tw_tick(min(min(t3_low, t3_high) * 0.97, ma45_v * 0.97, ma117_v, close_t - 2.2 * atr))
            tp1_def = round_tw_tick(max(h60_val, close_t + 2.5 * atr))

    # 最終單調性護欄：確保 t3_price < t2_price < t1_price <= close_t 且 hard_sl < t3_price
    if t1_price > close_t:
        t1_price = round_tw_tick(close_t * 0.995)
    if t2_price >= t1_price:
        t2_price = round_tw_tick(t1_price * 0.98)
    if t3_price >= t2_price:
        t3_price = round_tw_tick(t2_price * 0.98)
    if hard_sl >= t3_price:
        hard_sl = round_tw_tick(t3_price * 0.97)

    rs_beta_info = compute_relative_strength_and_beta(prices, benchmark_prices)
    tdcc_summary_info = summarize_tdcc_multiweek_trend(
        tdcc_holding_df,
        paid_in_capital_twd=paid_in_capital_twd,
        close_price=close_t,
    )

    prompt_lines = [
        f"#### 📌 【{stock_id} {stock_name} — 技術、籌碼與大量支撐定位全覽】",
        f"- **最新收盤**：{close_t:.2f} 元｜ATR(14)：{atr:.2f} 元｜布林通道(20,2)：上軌 {float(last_r['BB_UPPER']):.2f} / 中軌 {float(last_r['BB_MID']):.2f} / 下軌 {float(last_r['BB_LOWER']):.2f}",
        f"- **五線均線位階、斜率與未來5日扣抵價**：{' ｜ '.join(ma_deduction_parts)}",
        f"- **動能指標現況**：K9={float(last_r['K9']):.2f}, D9={float(last_r['D9']):.2f}｜RSI(14)={float(last_r['RSI14']):.2f}｜MACD_DIF={float(last_r['MACD_DIF']):+.3f}, DEA={float(last_r['MACD_DEA']):+.3f}, OSC柱={float(last_r['MACD_OSC']):+.3f}",
        *(
            [f"- **⚖️ 相對大盤強弱度 (RS / 60日Beta)**：{rs_beta_info['summary']}"]
            if rs_beta_info
            else []
        ),
        *(
            [f"- **📦 集保大戶籌碼**：{tdcc_summary_info['summary']}"]
            if tdcc_summary_info
            else []
        ),
        "- **🎯 【Python 大量支撐定位器（一眼看出下檔鐵板支撐價位）】**：",
        f"  1. **60日分價量密集區 (Volume Profile)**：全區最大量 POC={round_tw_tick(poc_60d):.2f} 元｜70%籌碼密集區 VAL={val_60d:.2f} ~ VAH={vah_60d:.2f} 元｜" + "；".join(lower_support_nodes),
        "  2. **近60日關鍵爆量紅K／長下影承接防守價**：" + ("；".join(key_bullish_bars) if key_bullish_bars else "無明顯爆量紅K"),
        (
            f"  3. **法人買超持倉成本錨點 (VWAP)**：投信 20日成本={f'{trust_vwap_20:.2f}元' if trust_vwap_20 else '無買超'} / 60日成本={f'{trust_vwap_60:.2f}元' if trust_vwap_60 else '無買超'}"
            f"｜外資 20日成本={f'{foreign_vwap_20:.2f}元' if foreign_vwap_20 else '無買超'} / 60日成本={f'{foreign_vwap_60:.2f}元' if foreign_vwap_60 else '無買超'}"
            + (f"｜法人大買日區間：{'；'.join(top_inst_days)}" if top_inst_days else "")
        ),
        (
            f"- **📐 波段高低點與黃金分割回檔位**：近60日高點 {h60_val:.2f}({h60_date}) ～ 低點 {l60_val:.2f}({l60_date})"
            f"（120日高低：{h120_val:.2f}~{l120_val:.2f}）｜回檔 0.236={fib_236:.2f} / 0.382={fib_382:.2f} / 0.500={fib_500:.2f} / 0.618={fib_618:.2f} / 0.786={fib_786:.2f}"
        ),
        (
            "- **🕳️ 近60日未回補跳空缺口**："
            + ("；".join(unfilled_up_gaps[-2:] + unfilled_down_gaps[-2:]) if (unfilled_up_gaps or unfilled_down_gaps) else "無未回補缺口")
        ),
        "- **🛡️ 基本面價值鐵底參考**：" + (" ｜ ".join(val_floors) if val_floors else "依長均線與前波低點防守"),
        f"- **🧭 Python 預設三階回檔低接參考區（禁止現價 {close_t:.2f} 追高）**：第1批(30%) {t1_ref}｜第2批(40%) {t2_ref}｜第3批(30%) {t3_ref}｜硬停損參考 {hard_sl:.2f}｜TP1參考 {tp1_def:.2f}",
        "- **📊 近 20 個交易日「價、量、五線歷史均線、KD/RSI/MACD、外資與投信、融資」逐日對齊明細表**：",
        "",
        *table_lines,
    ]

    return {
        "stock_id": stock_id,
        "close_t": round(close_t, 2),
        "atr_14": round(atr, 2),
        "poc_60d": round_tw_tick(poc_60d),
        "val_60d": val_60d,
        "vah_60d": vah_60d,
        "lower_support_nodes": lower_support_nodes,
        "key_bullish_bars": key_bullish_bars,
        "trust_vwap_20d": trust_vwap_20,
        "foreign_vwap_20d": foreign_vwap_20,
        "tier1_default": t1_ref,
        "tier2_default": t2_ref,
        "tier3_default": t3_ref,
        "tier1_price": t1_price,
        "tier2_price": t2_price,
        "tier3_price": t3_price,
        "chase_limit_price": chase_lim,
        "hard_stop_loss": hard_sl,
        "tp1_price": tp1_def,
        "rs_beta": rs_beta_info,
        "tdcc_summary": tdcc_summary_info,
        "prompt_block": "\n".join(prompt_lines),
    }


def build_market_daily_trend_block(
    prices: pd.DataFrame | None,
    *,
    benchmark_id: str = "0050",
    margin_history_path: Path = Path("finmind_reports/.state/market_margin_history.json"),
    days: int = 10,
) -> str:
    """SASD v6.1：組裝台股大盤基準近 10 日「收盤、漲跌%、成交量、五線均線、大盤整體融資餘額与增減」逐日趨勢表。"""
    margin_map: dict[str, dict[str, object]] = {}
    if margin_history_path.is_file():
        try:
            raw_m = json.loads(margin_history_path.read_text(encoding="utf-8"))
            if isinstance(raw_m, dict):
                margin_map = raw_m
        except Exception:  # noqa: BLE001
            margin_map = {}

    sorted_m_dates = sorted(margin_map.keys())
    margin_chg_by_date: dict[str, float] = {}
    for idx, d_key in enumerate(sorted_m_dates):
        rec = margin_map.get(d_key) or {}
        explicit_chg = _parse_optional_float(rec.get("margin_chg_1d_yi"))
        if explicit_chg is not None:
            margin_chg_by_date[d_key] = round(explicit_chg, 2)
            continue
        cur_tot = _parse_optional_float(rec.get("total_margin_yi"))
        prev_tot = _parse_optional_float((margin_map.get(sorted_m_dates[idx - 1]) or {}).get("total_margin_yi")) if idx > 0 else None
        if cur_tot is not None and prev_tot is not None:
            margin_chg_by_date[d_key] = round(cur_tot - prev_tot, 2)

    if prices is None or prices.empty:
        return "（無本地大盤逐日歷史日線）"

    sub = prices.loc[prices["stock_id"].astype(str) == str(benchmark_id)].copy()
    if sub.empty:
        sub = prices.loc[prices["stock_id"].astype(str).isin(["TAIEX", "0050"])].copy()
    if sub.empty:
        return "（prices.csv 未包含大盤基準日線）"

    norm = add_momentum_indicators(_normalize_ohlcv(sub))
    for p in MOVING_AVERAGE_PERIODS:
        c = f"MA{p}"
        if c not in norm.columns or norm[c].isna().all():
            norm[c] = norm["close"].rolling(window=p, min_periods=1).mean()
    norm["date_str"] = norm["date"].dt.strftime("%Y-%m-%d")
    norm["pct_chg"] = (norm["close"].pct_change() * 100.0).fillna(0.0).round(2)
    recent = norm.tail(days)

    lines = [
        f"【台股大盤基準 ({benchmark_id}) 近 {len(recent)} 交易日價量、五線均線、KD/RSI/MACD 與大盤整體融資趨勢】",
        "",
        "| 日期 | 收盤 | 漲跌% | MA3 | MA17 | MA45 | MA117 | MA189 | K9/D9 | RSI14 | MACD柱 | 大盤總融資(億) | 融資單日增減(億) |",
        "|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in recent.iterrows():
        ds = str(r["date_str"])
        m_rec = margin_map.get(ds) or {}
        m_tot = _parse_optional_float(m_rec.get("total_margin_yi"))
        m_chg = margin_chg_by_date.get(ds)
        lines.append(
            f"| {ds} | {float(r['close']):.2f} | {float(r['pct_chg']):+.2f}% | "
            f"{float(r['MA3']):.2f} | {float(r['MA17']):.2f} | {float(r['MA45']):.2f} | "
            f"{float(r['MA117']):.2f} | {float(r['MA189']):.2f} | "
            f"{float(r['K9']):.1f}/{float(r['D9']):.1f} | {float(r['RSI14']):.1f} | {float(r['MACD_OSC']):+.2f} | "
            f"{f'{m_tot:.1f}' if m_tot is not None else '-'} | {f'{m_chg:+.1f}' if m_chg is not None else '-'} |"
        )

    # 補充 OTC 櫃買指數近 5 日摘要（若有本地日線則提供，若無則明確聲明以避免 AI 腦補）
    otc_sub = prices.loc[prices["stock_id"].astype(str).isin(["TPEx", "OTC"])].copy()
    if not otc_sub.empty and str(benchmark_id) not in ("TPEx", "OTC"):
        otc_norm = add_momentum_indicators(_normalize_ohlcv(otc_sub))
        for p in MOVING_AVERAGE_PERIODS:
            c = f"MA{p}"
            if c not in otc_norm.columns or otc_norm[c].isna().all():
                otc_norm[c] = otc_norm["close"].rolling(window=p, min_periods=1).mean()
        otc_norm["date_str"] = otc_norm["date"].dt.strftime("%Y-%m-%d")
        otc_norm["pct_chg"] = (otc_norm["close"].pct_change() * 100.0).fillna(0.0).round(2)
        otc_recent = otc_norm.tail(min(5, days))
        lines.extend([
            "",
            f"【櫃買指數 (OTC) 近 {len(otc_recent)} 交易日價量與五線均線摘要】",
            "| 日期 | OTC收盤 | 漲跌% | MA3 | MA17 | MA45 | MA117 | MA189 |",
            "|:---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for _, orow in otc_recent.iterrows():
            lines.append(
                f"| {orow['date_str']} | {float(orow['close']):.2f} | {float(orow['pct_chg']):+.2f}% | "
                f"{float(orow['MA3']):.2f} | {float(orow['MA17']):.2f} | {float(orow['MA45']):.2f} | "
                f"{float(orow['MA117']):.2f} | {float(orow['MA189']):.2f} |"
            )
    elif str(benchmark_id) not in ("TPEx", "OTC"):
        lines.append("")
        lines.append("（註：本次本地日線僅提供主基準指數，無 OTC 櫃買指數日線數據，請勿自行編造 OTC 點位。）")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# SASD v5.4.0 Phase 3：台股微結構過濾（流動性中位數、處置風險、除權息補償、籌碼效率）
# --------------------------------------------------------------------------- #
MIN_MEDIAN_TURNOVER_20D_TWD = 50_000_000  # 近 20 日成交金額中位數硬門檻（5,000 萬）
DISPOSITION_RISK_TAG = (
    "【🚨 處置股過熱預警：短線急漲或高週轉，隨時面臨證交所注意/分盤處置導致流動性驟降，嚴禁右側追高】"
)
MARGIN_CALENDAR_NOTE = (
    "【ℹ️ 制度性停資券窗口：近期資券減少受股東會/除權息強制回補影響，降低籌碼沉澱加分權重】"
)


def passes_liquidity_median_guard(
    df_kline: pd.DataFrame,
    min_median_turnover: float = MIN_MEDIAN_TURNOVER_20D_TWD,
) -> bool:
    """近 20 日成交金額「中位數」是否達標（嚴禁使用平均數，防單日爆量誘多的殭屍股）。

    缺少 ``Trading_money`` 欄位或有效資料不足 20 筆時視為無法判斷而放行（不誤殺）。
    """
    if df_kline is None or "Trading_money" not in df_kline.columns:
        return True
    money = pd.to_numeric(df_kline["Trading_money"], errors="coerce").dropna().tail(20)
    if len(money) < 20:
        return True
    return float(money.median()) >= float(min_median_turnover)


def evaluate_disposition_risk(
    df_kline: pd.DataFrame,
    shares_outstanding: float | None = None,
) -> str:
    """注意／處置股（分盤交易）過熱預警；觸發回傳 `DISPOSITION_RISK_TAG`，否則空字串。

    - 短線急漲：近 5 日累積漲幅 >= 25% 或近 10 日 >= 40%。
    - 高週轉：近 3 日平均週轉率 >= 15% 且當日振幅 >= 8%（需提供 ``shares_outstanding``）。
    """
    if df_kline is None or df_kline.empty or "close" not in df_kline.columns:
        return ""
    frame = _normalize_ohlcv(df_kline)
    if len(frame) < 6:
        fallback = df_kline.copy()
        fallback["date"] = pd.to_datetime(fallback.get("date"), errors="coerce")
        fallback["close"] = pd.to_numeric(fallback["close"], errors="coerce")
        fallback = fallback.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        if len(fallback) < 6:
            return ""
        c_ser = fallback["close"]
        c_t = float(c_ser.iloc[-1])
        r5 = (c_t / float(c_ser.iloc[-6]) - 1.0) * 100.0
        r10 = (c_t / float(c_ser.iloc[-11]) - 1.0) * 100.0 if len(c_ser) >= 11 else 0.0
        return DISPOSITION_RISK_TAG if (r5 >= 25.0 or r10 >= 40.0) else ""
    close = frame["close"]
    close_t = float(close.iloc[-1])
    ret5 = (close_t / float(close.iloc[-6]) - 1.0) * 100.0
    ret10 = (
        (close_t / float(close.iloc[-11]) - 1.0) * 100.0 if len(frame) >= 11 else 0.0
    )
    if ret5 >= 25.0 or ret10 >= 40.0:
        return DISPOSITION_RISK_TAG

    if shares_outstanding and shares_outstanding > 0 and "volume" in frame.columns:
        vol_shares = frame["volume"].tail(3)
        # _normalize_ohlcv 的 volume 可能是股數（Trading_Volume）或張數（vol_lots）
        if "Trading_Volume" not in df_kline.columns and "vol_lots" in df_kline.columns:
            vol_shares = vol_shares * 1000.0
        turnover = float((vol_shares / float(shares_outstanding)).mean()) * 100.0
        prev_close = float(close.iloc[-2])
        amplitude = (
            (float(frame["high"].iloc[-1]) - float(frame["low"].iloc[-1])) / prev_close * 100.0
            if prev_close > 0 else 0.0
        )
        if turnover >= 15.0 and amplitude >= 8.0:
            return DISPOSITION_RISK_TAG
    return ""


def _build_dividend_event_map(df_dividend: pd.DataFrame | None) -> dict[str, dict[str, float]]:
    """相容標準化 `(ex_date/ex_dividend_date, cash_dividend, stock_dividend)` 與 FinMind 原始 `TaiwanStockDividend`。"""
    if df_dividend is None or not isinstance(df_dividend, pd.DataFrame) or df_dividend.empty:
        return {}
    div_map: dict[str, dict[str, float]] = {}

    def _add(dt_val: object, cash: float, stock: float) -> None:
        if dt_val is None or str(dt_val).strip() in ("", "None", "nan", "NaT"):
            return
        ts = pd.to_datetime(dt_val, errors="coerce")
        if pd.isna(ts):
            return
        key = ts.strftime("%Y-%m-%d")
        bucket = div_map.setdefault(key, {"cash_dividend": 0.0, "stock_dividend": 0.0})
        bucket["cash_dividend"] += max(0.0, cash)
        bucket["stock_dividend"] += max(0.0, stock)

    for record in df_dividend.to_dict("records"):
        ex_dt = record.get("ex_date") or record.get("ex_dividend_date")
        if ex_dt is not None and str(ex_dt).strip() not in ("", "None", "nan"):
            c_val = float(record.get("cash_dividend", 0.0) or 0.0)
            s_val = float(record.get("stock_dividend", 0.0) or 0.0)
            _add(ex_dt, c_val, s_val)
            continue
        # Raw FinMind TaiwanStockDividend columns
        c_earn = float(record.get("CashEarningsDistribution", 0.0) or 0.0)
        c_surp = float(record.get("CashStatutorySurplus", 0.0) or 0.0)
        s_earn = float(record.get("StockEarningsDistribution", 0.0) or 0.0)
        s_surp = float(record.get("StockStatutorySurplus", 0.0) or 0.0)
        if c_earn + c_surp > 0:
            _add(record.get("CashExDividendTradingDate"), c_earn + c_surp, 0.0)
        if s_earn + s_surp > 0:
            _add(record.get("StockExDividendTradingDate"), 0.0, s_earn + s_surp)
    return div_map


def extract_ex_dividend_dates(df_dividend: pd.DataFrame | None) -> list[date]:
    """從股利表擷取所有有效除權息日期，供強制回補日曆窗口檢核使用。"""
    div_map = _build_dividend_event_map(df_dividend)
    out: list[date] = []
    for k in sorted(div_map.keys()):
        try:
            out.append(date.fromisoformat(k))
        except ValueError:
            continue
    return out


def compensate_ex_dividend_gaps(
    df_kline: pd.DataFrame,
    df_dividend: pd.DataFrame | None,
) -> pd.DataFrame:
    """對 K 線做除權息「前複權回溯補償」（以最新交易日 T 為 1.0 基準）。

    ``df_dividend`` 支援 ``(ex_date/ex_dividend_date, cash_dividend, stock_dividend)`` 或 FinMind
    ``TaiwanStockDividend`` 原始欄位。除權息日之前的 OHLC（含 FinMind ``max`` / ``min``）乘上
    ``(前收 - 現金股利) / (1 + 股票股利/10) / 前收``；原價保留於 ``raw_*``。
    """
    df = df_kline.sort_values("date").copy().reset_index(drop=True)
    div_map = _build_dividend_event_map(df_dividend)
    if not div_map:
        return df

    df["adj_factor"] = 1.0
    date_keys = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    for idx in range(1, len(df)):
        record = div_map.get(date_keys.iloc[idx])
        if record is None:
            continue
        prev_close = float(df.loc[idx - 1, "close"])
        cash_div = float(record.get("cash_dividend", 0.0) or 0.0)
        stock_div_per_share = float(record.get("stock_dividend", 0.0) or 0.0) / 10.0
        if prev_close > cash_div and (1.0 + stock_div_per_share) > 0:
            ex_ref_price = (prev_close - cash_div) / (1.0 + stock_div_per_share)
            df.loc[: idx - 1, "adj_factor"] *= ex_ref_price / prev_close

    for col in ("open", "high", "low", "close", "max", "min"):
        if col in df.columns:
            df[f"raw_{col}"] = df[col]
            df[col] = (pd.to_numeric(df[col], errors="coerce") * df["adj_factor"]).round(2)
    return df


def is_mandatory_margin_covering_window(
    as_of: date,
    ex_dividend_dates: list[date] | None = None,
    *,
    trading_days_before_ex: int = 7,
) -> bool:
    """3/15~4/25 股東會停資券期，或除權息日前 7 個交易日（以工作日近似）內為 True。"""
    if date(as_of.year, 3, 15) <= as_of <= date(as_of.year, 4, 25):
        return True
    for ex_date in ex_dividend_dates or []:
        if ex_date < as_of:
            continue
        if len(pd.bdate_range(as_of, ex_date)) - 1 <= trading_days_before_ex:
            return True
    return False


TDCC_OPENDATA_CSV_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"

# TDCC 集保官方持股分級代碼 (1~15) 對應持股下限（股）
TDCC_LEVEL_LOWER_BOUNDS: dict[str, float] = {
    "1": 1.0,
    "2": 1000.0,
    "3": 5001.0,
    "4": 10001.0,
    "5": 15001.0,
    "6": 20001.0,
    "7": 30001.0,
    "8": 40001.0,
    "9": 50001.0,
    "10": 100001.0,
    "11": 200001.0,
    "12": 400001.0,
    "13": 600001.0,
    "14": 800001.0,
    "15": 1000001.0,
}

_TDCC_OPENDATA_MEM_CACHE: dict[str, dict[str, dict[str, Any]]] = {}


def _holding_level_lower_bound(level: object) -> float | None:
    """解析 FinMind 或 TDCC 官方股權分散級距的持股下限（股）。"""
    raw = str(level).strip()
    if raw in TDCC_LEVEL_LOWER_BOUNDS:
        return TDCC_LEVEL_LOWER_BOUNDS[raw]
    if raw in ("16", "17"):
        return None
    text = raw.lower().replace(",", "")
    if "total" in text or "差異" in text or "合計" in text:
        return None
    numbers = re.findall(r"\d+", text)
    if not numbers:
        return None
    return float(numbers[0])


def fetch_tdcc_opendata_map(
    *,
    as_of: date | None = None,
    cache_dir: Path = Path("finmind_reports/.state"),
    timeout: int = 60,
) -> dict[str, dict[str, Any]]:
    """抓取台灣集保結算所（TDCC）官方免費 Open Data 全市場股權分散表（週五更新）。

    每日最多下載一次並快取於記憶體與 ``tdcc_opendata_latest.json``，回傳
    ``{stock_id: {"tdcc_date": "YYYY-MM-DD", "pct_400_lots": float,
    "pct_1000_lots": float, "total_shareholders": int, "rows": list[dict]}}``。
    """
    today = as_of or _today_in_taipei()
    today_str = today.isoformat()
    if today_str in _TDCC_OPENDATA_MEM_CACHE:
        return _TDCC_OPENDATA_MEM_CACHE[today_str]

    snapshot_path = cache_dir / "tdcc_opendata_latest.json"
    if snapshot_path.is_file():
        try:
            saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("fetched_at") == today_str and isinstance(saved.get("stocks"), dict):
                _TDCC_OPENDATA_MEM_CACHE[today_str] = saved["stocks"]
                return saved["stocks"]
        except Exception:  # noqa: BLE001
            pass

    import csv
    import http.client
    import io
    import socket
    import ssl
    import urllib.request

    class _IPv4HTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
            last_err: OSError | None = None
            for family, socktype, proto, _, sockaddr in infos:
                sock = None
                try:
                    sock = socket.socket(family, socktype, proto)
                    sock.settimeout(self.timeout)
                    sock.connect(sockaddr)
                    self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                    return
                except OSError as exc:
                    last_err = exc
                    if sock is not None:
                        sock.close()
            if last_err is not None:
                raise last_err

    class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: urllib.request.Request):  # type: ignore[override]
            return self.do_open(_IPv4HTTPSConnection, req, context=self._context)

    req = Request(
        TDCC_OPENDATA_CSV_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    opener = urllib.request.build_opener(_IPv4HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(req, timeout=timeout) as resp:
        csv_text = resp.read().decode("utf-8-sig", errors="replace")

    stocks: dict[str, dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for r in reader:
        sid = str(r.get("證券代號") or "").strip()
        if not sid:
            continue
        raw_dt = str(r.get("資料日期") or "").strip()
        fmt_dt = f"{raw_dt[:4]}-{raw_dt[4:6]}-{raw_dt[6:8]}" if len(raw_dt) == 8 and raw_dt.isdigit() else raw_dt
        lvl = str(r.get("持股分級") or "").strip()
        try:
            people = int(float(str(r.get("人數") or 0).replace(",", "")))
        except ValueError:
            people = 0
        try:
            pct = float(str(r.get("占集保庫存數比例%") or 0).replace(",", ""))
        except ValueError:
            pct = 0.0

        entry = stocks.setdefault(
            sid,
            {
                "tdcc_date": fmt_dt,
                "pct_400_lots": 0.0,
                "pct_1000_lots": 0.0,
                "total_shareholders": 0,
                "rows": [],
            },
        )
        if lvl in ("12", "13", "14", "15"):
            entry["pct_400_lots"] = round(float(entry["pct_400_lots"]) + pct, 2)
        if lvl == "15":
            entry["pct_1000_lots"] = round(pct, 2)
        if lvl == "17":
            entry["total_shareholders"] = people
        if lvl != "16":
            entry["rows"].append(
                {
                    "date": fmt_dt,
                    "HoldingSharesLevel": lvl,
                    "percent": pct,
                    "people": people,
                }
            )

    if stocks:
        _TDCC_OPENDATA_MEM_CACHE[today_str] = stocks
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(
                json.dumps({"fetched_at": today_str, "stocks": stocks}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to write TDCC opendata snapshot cache: %s", exc)
    return stocks


def evaluate_weekly_tdcc_anchor(
    df_holding: pd.DataFrame | None,
    paid_in_capital_twd: float | None = None,
) -> dict[str, object] | None:
    """週級別集保大戶底氣（股權分散表最新兩週比較）。

    實收資本額 >= 30 億採 1,000 張以上，否則 400 張以上持股比例。輸出含 ``tdcc_base_date``，
    避免 AI 將上週五集保誤認為今日盤後籌碼。資料不足（不足兩週）回傳 None。
    """
    if df_holding is None or not isinstance(df_holding, pd.DataFrame) or df_holding.empty:
        return None
    frame = df_holding.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["percent"] = pd.to_numeric(frame.get("percent"), errors="coerce")
    frame = frame.dropna(subset=["date", "percent"])
    dates = sorted(frame["date"].unique())
    if len(dates) < 2:
        return None

    big_lots = 1000 if (paid_in_capital_twd or 0) >= 3_000_000_000 else 400
    threshold_shares = big_lots * 1000

    def _major_pct(day: object) -> float:
        day_rows = frame.loc[frame["date"] == day]
        total = 0.0
        for row in day_rows.itertuples(index=False):
            lower = _holding_level_lower_bound(row.HoldingSharesLevel)
            # 級距下限 >= 門檻 (如 "400,001-600,000" 之 400001 >= 400000)
            if lower is not None and lower >= threshold_shares:
                total += float(row.percent)
        return total

    def _people(day: object) -> float | None:
        if "people" not in frame.columns:
            return None
        rows = frame.loc[frame["date"] == day]
        lvl = rows["HoldingSharesLevel"].astype(str).str.strip().str.lower()
        total_rows = rows.loc[(lvl == "17") | lvl.str.contains("total|合計", regex=True)]
        if not total_rows.empty:
            val = float(pd.to_numeric(total_rows["people"], errors="coerce").dropna().iloc[0])
            return val if val > 0 else None
        non_diff = rows.loc[~lvl.isin(["16"]) & ~lvl.str.contains("差異", regex=False)]
        return float(pd.to_numeric(non_diff["people"], errors="coerce").sum()) or None

    latest_day = dates[-1]
    prev_day = dates[-2] if len(dates) < 3 else dates[-3]
    latest_pct = _major_pct(latest_day)
    prev_pct = _major_pct(prev_day)
    people_latest, people_prev = _people(latest_day), _people(prev_day)
    people_chg = (
        round((people_latest / people_prev - 1.0) * 100.0, 2)
        if people_latest and people_prev else None
    )
    base_date = pd.Timestamp(latest_day).strftime("%Y-%m-%d")
    diff = round(latest_pct - prev_pct, 2)
    return {
        "tdcc_base_date": base_date,
        "major_threshold_lots": big_lots,
        "latest_major_pct": round(latest_pct, 2),
        "major_holder_2w_diff": diff,
        "total_shareholders_chg_pct": people_chg,
        "summary": (
            f"集保基準日 {base_date}：{big_lots} 張以上大戶持股 {latest_pct:.2f}%"
            f"（近兩週 {diff:+.2f} 個百分點）"
        ),
    }


def fetch_or_cache_tdcc_holding(
    stock_id: str,
    *,
    as_of: date | None = None,
    cache_path: Path = Path("finmind_reports/.state/tdcc_holding_cache.json"),
    loader: DataLoader | None = None,
    ttl_days: int = 6,
) -> pd.DataFrame | None:
    """讀取或抓取集保股權分散表（優先 FinMind，權限不足或失敗時自動改採 TDCC 官方免費 Open Data）。"""
    today = as_of or _today_in_taipei()
    sid = str(stock_id).strip()
    store: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                store = loaded
        except Exception:  # noqa: BLE001
            store = {}
    entry = store.get(sid)
    existing_rows: list[dict[str, Any]] = []
    if isinstance(entry, Mapping):
        if isinstance(entry.get("rows"), list):
            existing_rows = [r for r in entry["rows"] if isinstance(r, dict)]
        try:
            fetched_dt = date.fromisoformat(str(entry.get("fetched_at", ""))[:10])
            if (today - fetched_dt).days <= ttl_days and existing_rows:
                return pd.DataFrame(existing_rows)
        except ValueError:
            pass

    records: list[dict[str, Any]] = []
    if loader is not None and hasattr(loader, "taiwan_stock_holding_shares_per"):
        try:
            start_str = (today - timedelta(days=95)).isoformat()
            df = loader.taiwan_stock_holding_shares_per(
                stock_id=sid, start_date=start_str, end_date=today.isoformat()
            )
            if isinstance(df, pd.DataFrame) and not df.empty:
                cols = [c for c in ("date", "HoldingSharesLevel", "percent", "people") if c in df.columns]
                slim_rows = df[cols].copy()
                slim_rows["date"] = pd.to_datetime(slim_rows["date"], errors="coerce").dt.strftime("%Y-%m-%d")
                records = slim_rows.dropna(subset=["date"]).to_dict("records")
        except Exception as exc:  # noqa: BLE001
            logger.debug("FinMind TDCC fetch skipped for %s (%s); falling back to TDCC Open Data", sid, exc)

    if not records:
        try:
            tdcc_map = fetch_tdcc_opendata_map(as_of=today, cache_dir=cache_path.parent)
            tdcc_entry = tdcc_map.get(sid)
            if isinstance(tdcc_entry, Mapping) and isinstance(tdcc_entry.get("rows"), list):
                new_rows = [dict(r) for r in tdcc_entry["rows"]]
                new_dates = {str(r.get("date", "")) for r in new_rows}
                kept_old = [r for r in existing_rows if str(r.get("date", "")) not in new_dates]
                records = kept_old + new_rows
        except Exception as exc:  # noqa: BLE001
            logger.debug("TDCC Open Data fallback skipped for %s: %s", sid, exc)

    if not records:
        return pd.DataFrame(existing_rows) if existing_rows else None

    # 僅保留最新 13 週去重紀錄，避免本地快取無限膨脹
    unique_dates = sorted({str(r.get("date", "")) for r in records if r.get("date")})
    if len(unique_dates) > 13:
        keep_dates = set(unique_dates[-13:])
        records = [r for r in records if str(r.get("date", "")) in keep_dates]

    store[sid] = {"fetched_at": today.isoformat(), "rows": records}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to persist TDCC cache for %s: %s", sid, exc)
    return pd.DataFrame(records)


# Spec aliases for SASD v5.4.0 Phase 3 internal helpers
_evaluate_disposition_risk = evaluate_disposition_risk
_compensate_ex_dividend_gaps = compensate_ex_dividend_gaps
_is_mandatory_margin_covering_window = is_mandatory_margin_covering_window
_evaluate_weekly_tdcc_anchor = evaluate_weekly_tdcc_anchor


@dataclass(frozen=True)
class MarginMicrostructureResult:
    margin_pattern: str  # MAIN_FORCE_MARGIN_LOCK / CLEAN_INSTITUTIONAL_LOCK / RETAIL_MARGIN_CHASING_TRAP / CHIP_NEUTRAL
    semantic_tag: str


def classify_daily_margin_efficiency(
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float,
    prev_close_p: float,
    volume_lots: float,
    margin_net_change_lots: float,
    inst_net_buy_lots: float,
    trust_holding_pct: float,
    trust_3d_net_buy_lots: float,
    return_10d_pct: float,
) -> MarginMicrostructureResult:
    """日融資增減 × K 線實體效率四象限分類。量與融資/法人單位皆為「張」。"""
    candle_range = max(high_p - low_p, 0.01)
    close_pos_in_range = (close_p - low_p) / candle_range
    upper_shadow_ratio = (high_p - max(open_p, close_p)) / candle_range
    day_ret_pct = (close_p - prev_close_p) / prev_close_p * 100.0

    margin_vol_ratio = margin_net_change_lots / max(volume_lots, 1.0)
    inst_vol_ratio = inst_net_buy_lots / max(volume_lots, 1.0)

    if inst_vol_ratio >= 0.15 and margin_net_change_lots <= 0:
        sweet_spot_note = ""
        if 0.5 <= trust_holding_pct <= 8.0 and trust_3d_net_buy_lots > 0:
            sweet_spot_note = f"（投信持股比 {trust_holding_pct:.1f}% 處於初買鎖碼甜蜜點）"
        return MarginMicrostructureResult(
            "CLEAN_INSTITUTIONAL_LOCK",
            f"【💎 法人鎖碼散戶下車：三大法人買超佔成交量 {inst_vol_ratio*100:.1f}% 且融資逆勢減少 "
            f"{abs(int(margin_net_change_lots))} 張{sweet_spot_note}】",
        )

    if margin_vol_ratio >= 0.12 and margin_net_change_lots >= 200:
        if (
            day_ret_pct >= 3.0
            and close_p > open_p
            and close_pos_in_range >= 0.75
            and upper_shadow_ratio <= 0.25
            and return_10d_pct <= 16.0
        ):
            return MarginMicrostructureResult(
                "MAIN_FORCE_MARGIN_LOCK",
                f"【🔥 主力融資鎖碼：起漲初期帶量長紅收高 (收盤位階 {close_pos_in_range*100:.0f}%) "
                f"伴隨融資大增 {int(margin_net_change_lots)} 張，屬內資大戶槓桿建倉】",
            )
        if close_p <= open_p or upper_shadow_ratio >= 0.40 or return_10d_pct > 18.0:
            return MarginMicrostructureResult(
                "RETAIL_MARGIN_CHASING_TRAP",
                f"【🚨 散戶融資套牢警訊：融資暴增 {int(margin_net_change_lots)} 張 "
                f"(佔量 {margin_vol_ratio*100:.1f}%) 卻留上影線/收弱或處於波段高檔，籌碼極度凌亂】",
            )

    return MarginMicrostructureResult(
        "CHIP_NEUTRAL",
        f"【➖ 日籌碼中性：法人淨買賣佔量 {inst_vol_ratio*100:.1f}%，融資增減 {int(margin_net_change_lots)} 張】",
    )

def fetch_news_excerpt(url: str, *, timeout: float = 3.0, max_chars: int = 250) -> str:
    """擷取新聞正文前 max_chars 字；逾時或失敗回傳空字串（呼叫端優雅降級回標題）。"""
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(200_000).decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _classify_news_reaction(
    *, vol_ratio_5d: float, close: float, open_: float, high: float, day_return_pct: float, upper_shadow_ratio: float
) -> str:
    if vol_ratio_5d >= 1.5 and (close < open_ or upper_shadow_ratio >= 0.45):
        return NEWS_TAG_DISTRIBUTION
    if day_return_pct >= 2.5 and close >= high * 0.985:
        return NEWS_TAG_STRONG_BUY
    return NEWS_TAG_NEUTRAL


def align_news_with_price_action(
    news_items: list[Mapping[str, object]],
    prices: pd.DataFrame,
    *,
    excerpt_fetcher: Callable[[str], str] | None = None,
    max_chars: int = 250,
) -> list[dict[str, object]]:
    """將新聞發布日對齊日 K，生成量價反應標籤（發布日無交易則取其後第一個交易日）。

    news_items 欄位：title、pub_date（或 pubDate）、url（或 link）、summary（選填）。
    vol_ratio_5d 以「反應日前 5 個交易日」均量為分母。
    """
    frame = _normalize_ohlcv(prices)
    aligned: list[dict[str, object]] = []
    for item in news_items:
        title = str(item.get("title") or "").strip()
        raw_pub = item.get("pub_date") or item.get("pubDate") or ""
        url = str(item.get("url") or item.get("link") or "").strip()
        summary = str(item.get("summary") or item.get("description") or "").strip()
        if not summary and url and excerpt_fetcher is not None:
            try:
                summary = (excerpt_fetcher(url) or "").strip()
            except Exception:  # noqa: BLE001
                summary = ""
        summary = (summary or title)[:max_chars]
        entry: dict[str, object] = {
            "title": title,
            "url": url,
            "pub_date": str(raw_pub),
            "summary": summary,
            "reaction_tag": NEWS_TAG_NEUTRAL,
            "reaction_date": "",
        }
        pub = pd.to_datetime(raw_pub, errors="coerce", utc=False)
        if pd.notna(pub) and getattr(pub, "tzinfo", None) is not None:
            pub = pub.tz_convert("Asia/Taipei").tz_localize(None)
        if pd.isna(pub) or frame.empty:
            aligned.append(entry)
            continue
        positions = frame.index[frame["date"] >= pub.normalize()]
        if len(positions) == 0:
            aligned.append(entry)
            continue
        pos = int(positions[0])
        bar = frame.iloc[pos]
        prev_close = float(frame["close"].iloc[pos - 1]) if pos > 0 else float(bar["open"])
        day_return = (float(bar["close"]) - prev_close) / prev_close * 100.0 if prev_close else 0.0
        span = float(bar["high"]) - float(bar["low"])
        upper_shadow = (
            (float(bar["high"]) - max(float(bar["open"]), float(bar["close"]))) / span if span > 0 else 0.0
        )
        prior_vol = frame["volume"].iloc[max(0, pos - 5) : pos].dropna()
        vol_ratio = (
            float(bar["volume"]) / float(prior_vol.mean())
            if len(prior_vol) and float(prior_vol.mean()) > 0 and pd.notna(bar["volume"])
            else 1.0
        )
        entry.update(
            {
                "reaction_date": bar["date"].date().isoformat(),
                "day_return_pct": round(day_return, 2),
                "upper_shadow_ratio": round(upper_shadow, 2),
                "vol_ratio_5d": round(vol_ratio, 2),
                "reaction_tag": _classify_news_reaction(
                    vol_ratio_5d=vol_ratio,
                    close=float(bar["close"]),
                    open_=float(bar["open"]),
                    high=float(bar["high"]),
                    day_return_pct=day_return,
                    upper_shadow_ratio=upper_shadow,
                ),
            }
        )
        aligned.append(entry)
    return aligned


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


def _ma_slope_non_negative(series: pd.Series, lookback: int = 5) -> bool:
    valid = pd.to_numeric(series, errors="coerce").dropna()
    if len(valid) < 2:
        return True
    prev_idx = max(0, len(valid) - 1 - lookback)
    return float(valid.iloc[-1]) >= float(valid.iloc[prev_idx])


def classify_rebound_signal(
    analyzed_prices: pd.DataFrame,
    *,
    lookback_days: int = REBOUND_LOOKBACK_DAYS,
) -> dict[str, object] | None:
    """型態 A（SASD v6.0 雙劇本引擎）：高點回跌 10/30/100/300 元（且跌幅 >= 10%）回踩或 W 底回測長均線（45MA/117MA/189MA）。

    - 劇本 R1 (REBOUND_TOUCH_HOLD)：當日回踩任一上揚長均線 (-1.5% ~ +2.5%) 止穩，收盤強度 >= 0.55，且非綠柱加速擴張+KD死叉接刀。
    - 劇本 R2 (W_BOTTOM_PULLBACK_SETUP)：近 12 日曾觸及長均線見底、日 KD 金叉 (K9 > D9 且 K9 < 75)、MACD 動能收斂、反彈逼近上方壓力后量縮/留上影線，預掛右腳回測買點。
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

    # 1. 長線護體：MA45 >= MA117 > MA189 或權值股深跌特例 (MA117 > MA189 且 MA189 斜率 > 0 且收盤 > MA189 * 0.985)
    ma189_valid = pd.to_numeric(usable["MA189"], errors="coerce").dropna()
    ma189_slope_pos = len(ma189_valid) >= 2 and float(ma189_valid.iloc[-1]) > float(
        ma189_valid.iloc[max(0, len(ma189_valid) - 6)]
    )
    standard_bull = ma45 >= ma117 and ma117 > ma189
    deep_pullback_bull = ma117 > ma189 and ma189_slope_pos and close > (ma189 * 0.985)
    if not (standard_bull or deep_pullback_bull):
        return None

    # 2. 近 60 個交易日高點與回跌門檻
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

    # K 線收盤強度
    day_max = _parse_optional_float(latest.get("max"))
    day_min = _parse_optional_float(latest.get("min"))
    if day_max is not None and day_min is not None:
        if day_max > day_min:
            close_strength = (close - day_min) / (day_max - day_min)
        elif day_max == day_min and close >= day_max:
            close_strength = 1.0
        else:
            close_strength = 0.0
    else:
        close_strength = 1.0

    # 指標：K9, D9, MACD_OSC
    k9 = _parse_optional_float(latest.get("K9"))
    d9 = _parse_optional_float(latest.get("D9"))
    osc_t = _parse_optional_float(latest.get("MACD_OSC"))
    osc_prev = _parse_optional_float(usable.iloc[-2].get("MACD_OSC")) if len(usable) >= 2 else None
    osc_prev2 = _parse_optional_float(usable.iloc[-3].get("MACD_OSC")) if len(usable) >= 3 else None

    vol_metrics = _extract_latest_volume_metrics(usable)
    vol_lots_num = _parse_optional_float(vol_metrics.get("vol_lots"))
    vol_ma5_num = _parse_optional_float(vol_metrics.get("vol_ma5_lots"))

    support_candidates = [
        ("45MA", ma45, _ma_slope_non_negative(usable["MA45"])),
        ("117MA", ma117, _ma_slope_non_negative(usable["MA117"])),
        ("189MA", ma189, _ma_slope_non_negative(usable["MA189"])),
    ]

    # 3A. 嘗試劇本 R1：REBOUND_TOUCH_HOLD（當日回踩支撐止穩）
    r1_matches: list[tuple[str, float, float]] = []
    for ma_name, ma_val, slope_ok in support_candidates:
        if not slope_ok or ma_val <= 0:
            continue
        gap = (close - ma_val) / ma_val * 100.0
        if REBOUND_SUPPORT_MIN_PCT <= round(gap, 2) <= REBOUND_SUPPORT_MAX_PCT:
            r1_matches.append((ma_name, ma_val, gap))

    rebound_subtype: str | None = None
    support_ma = ""
    support_price = 0.0
    support_gap_pct = 0.0
    potential_return_pct = (ma17 - close) / close * 100.0 if close > 0 else 0.0

    if r1_matches and close < ma17 and round(potential_return_pct, 4) >= REBOUND_MIN_RETURN_TO_MA17_PCT:
        if round(close_strength, 4) >= REBOUND_MIN_CLOSE_STRENGTH:
            # 動能防接刀：拒絕在「連跌兩日以上且 MACD 綠柱負向擴大 + KD 死叉」時盲目接刀
            prev_falling = len(usable) >= 3 and float(usable.iloc[-2]["close"]) < float(usable.iloc[-3]["close"])
            falling_knife = (
                prev_falling
                and osc_t is not None
                and osc_prev is not None
                and osc_t < 0
                and osc_prev < 0
                and osc_t < osc_prev
                and k9 is not None
                and d9 is not None
                and k9 < d9
            )
            if not falling_knife:
                support_ma, support_price, support_gap_pct = min(r1_matches, key=lambda x: abs(x[2]))
                rebound_subtype = "REBOUND_TOUCH_HOLD"

    # 3B. 若未觸發 R1，嘗試劇本 R2：W_BOTTOM_PULLBACK_SETUP（W 底左腳完成，預掛右腳回測買點）
    if rebound_subtype is None and len(usable) >= 3:
        recent_12 = analyzed_prices.tail(12)
        if "min" in recent_12.columns and recent_12["min"].notna().any():
            l12 = float(pd.to_numeric(recent_12["min"], errors="coerce").dropna().min())
        else:
            l12 = float(pd.to_numeric(recent_12["close"], errors="coerce").dropna().min())

        r2_matches: list[tuple[str, float, float]] = []
        for ma_name, ma_val, slope_ok in support_candidates:
            if not slope_ok or ma_val <= 0:
                continue
            l12_gap = (l12 - ma_val) / ma_val * 100.0
            close_gap = (close - ma_val) / ma_val * 100.0
            if l12_gap <= 2.0 and 2.5 < round(close_gap, 2) <= 12.0:
                r2_matches.append((ma_name, ma_val, close_gap))

        kd_bull = k9 is not None and d9 is not None and k9 > d9 and k9 < 75.0
        osc_ref = osc_prev2 if osc_prev2 is not None else osc_prev
        macd_improving = osc_t is not None and osc_ref is not None and osc_t > osc_ref

        recent_3 = analyzed_prices.tail(3)
        if "max" in recent_3.columns and recent_3["max"].notna().any():
            high_3d = float(pd.to_numeric(recent_3["max"], errors="coerce").dropna().max())
        else:
            high_3d = float(pd.to_numeric(recent_3["close"], errors="coerce").dropna().max())

        overhead_res = [r for r in (ma17, ma117) if r > close * 0.975]
        near_resistance = any((abs(r - high_3d) / r * 100.0) < 2.5 or high_3d >= r * 0.975 for r in overhead_res)
        pullback_clue = (
            (vol_lots_num is not None and vol_ma5_num is not None and vol_lots_num < vol_ma5_num)
            or close_strength < REBOUND_MIN_CLOSE_STRENGTH
        )

        if r2_matches and kd_bull and macd_improving and near_resistance and pullback_clue and close <= ma17 * 1.02:
            # 挑選最貼近 L12 的長均線作為支撐基準
            cand_ma, cand_val, cand_gap = min(r2_matches, key=lambda x: abs((l12 - x[1]) / x[1]))
            # 以支撐均線價計算至上方壓力 (MA17) 的潛在空間 >= 5%
            room_from_support = (ma17 - cand_val) / cand_val * 100.0
            if round(room_from_support, 4) >= REBOUND_MIN_RETURN_TO_MA17_PCT:
                support_ma, support_price, support_gap_pct = cand_ma, cand_val, cand_gap
                potential_return_pct = room_from_support
                rebound_subtype = "W_BOTTOM_PULLBACK_SETUP"

    if rebound_subtype is None:
        return None

    date_val = (
        latest["date"].strftime("%Y-%m-%d")
        if hasattr(latest["date"], "strftime")
        else str(latest["date"])[:10]
    )
    setup_info = (
        compute_rebound_trade_setup(
            analyzed_prices,
            support_ma=support_ma,
            support_price=support_price,
            rebound_subtype=rebound_subtype,
        )
        or {}
    )

    return {
        "date": date_val,
        "close": round(close, 2),
        "high_60d": round(high_60d, 2),
        "drop_amount": round(drop_amount, 2),
        "drop_pct": round(drop_pct, 2),
        "rebound_subtype": rebound_subtype,
        "support_ma": support_ma,
        "support_price": round(support_price, 2),
        "support_gap_pct": round(support_gap_pct, 2),
        "target_ma17": round(ma17, 2),
        "potential_return_pct": round(potential_return_pct, 2),
        "close_strength": round(close_strength, 2),
        "K9": round(k9, 2) if k9 is not None else "",
        "D9": round(d9, 2) if d9 is not None else "",
        "MACD_OSC": round(osc_t, 4) if osc_t is not None else "",
        "MA3": round(ma3, 2),
        "MA17": round(ma17, 2),
        "MA45": round(ma45, 2),
        "MA117": round(ma117, 2),
        "MA189": round(ma189, 2),
        "ideal_buy_zone_low": setup_info.get("ideal_buy_zone_low", ""),
        "ideal_buy_zone_high": setup_info.get("ideal_buy_zone_high", ""),
        "chase_limit_price": setup_info.get("chase_limit_price", ""),
        "hard_stop_loss": setup_info.get("hard_stop_loss", ""),
        "tp1_price": setup_info.get("tp1_price", ""),
        "deterministic_rr_ratio": setup_info.get("deterministic_rr_ratio", ""),
        "vol_lots": vol_metrics.get("vol_lots", ""),
        "vol_ma5_lots": vol_metrics.get("vol_ma5_lots", ""),
        "vol_ma20_lots": vol_metrics.get("vol_ma20_lots", ""),
        "vol_status": vol_metrics.get("vol_status", "-"),
    }


def _parse_tw_api_date(raw: Any) -> str | None:
    """解析 TWSE/TPEx API 回傳之西元或民國日期字串為 YYYY-MM-DD。"""
    s = str(raw or "").strip()
    if not s:
        return None
    # 西元 YYYY-MM-DD 或 YYYY/MM/DD
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    # 西元 YYYYMMDD
    if re.fullmatch(r"\d{8}", s):
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8])).isoformat()
        except ValueError:
            return None
    # 民國 YYY/MM/DD 或 YYY-MM-DD
    m_roc = re.match(r"^(\d{2,3})[-/](\d{1,2})[-/](\d{1,2})$", s)
    if m_roc:
        try:
            return date(int(m_roc.group(1)) + 1911, int(m_roc.group(2)), int(m_roc.group(3))).isoformat()
        except ValueError:
            return None
    # 民國 YYYMMDD (7 碼)
    if re.fullmatch(r"\d{7}", s):
        try:
            return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7])).isoformat()
        except ValueError:
            return None
    return None


def fetch_market_margin_snapshot(
    history_path: Path | None = None,
    as_of: date | None = None,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """官方免費抓取上市櫃融資總表（TWSE / TPEx RWD 與 OpenAPI 備援），並以 API 實際交易日入帳。

    - 解析 TWSE / TPEx 回傳之實際交易日 (`date`)，避免 19:00 尚未公布當日融資時將前一日 (T-1)
      數據誤寫為當日 (T) 導致整條融資時間軸錯位。
    - 若上市與上櫃公告日期不同步（例如一市已更新 T 日、另一市仍停在 T-1 日），自動將較新市場
      退回使用其「前日餘額」對齊至同一交易日 (`min(twse_date, tpex_date)`)，不混加跨日數據。
    - 不消耗 FinMind 配額，網路異常 Graceful Degradation 回傳空 dict。
    """
    today = as_of or _today_in_taipei()
    today_str = today.isoformat()

    twse_today_yi: float | None = None
    twse_prev_yi: float | None = None
    twse_date: str | None = None
    tpex_today_yi: float | None = None
    tpex_prev_yi: float | None = None
    tpex_date: str | None = None

    # 1. 上市 (TWSE) 信用交易統計：優先使用含全市場合計與明確交易日期之 RWD 端點
    twse_urls = [
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json",
        "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN",
    ]
    for url in twse_urls:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict):
                parsed_d = _parse_tw_api_date(data.get("date"))
                tables = data.get("tables", [])
                for t in tables:
                    table_data = t.get("data", [])
                    for row in table_data:
                        if isinstance(row, list) and len(row) >= 6 and "融資金額" in str(row[0]):
                            prev_val = str(row[4]).replace(",", "").strip()
                            today_val = str(row[5]).replace(",", "").strip()
                            twse_prev_yi = float(prev_val) / 100000.0
                            twse_today_yi = float(today_val) / 100000.0
                            twse_date = parsed_d
                            break
                    if twse_today_yi is not None:
                        break
            elif isinstance(data, list):
                for row in data:
                    item_name = str(row.get("項目") or row.get("股票名稱") or "")
                    if "融資金額" in item_name or "融資(仟元)" in item_name:
                        prev_val = str(row.get("前日餘額") or row.get("融資前日餘額") or "").replace(",", "").strip()
                        today_val = str(row.get("今日餘額") or row.get("融資今日餘額") or "").replace(",", "").strip()
                        if prev_val and today_val:
                            twse_prev_yi = float(prev_val) / 100000.0
                            twse_today_yi = float(today_val) / 100000.0
                            twse_date = _parse_tw_api_date(row.get("日期") or row.get("Date"))
                            break
            if twse_today_yi is not None:
                break
        except Exception:
            logger.debug("Failed to fetch TWSE margin from %s", url, exc_info=True)

    # 2. 上櫃 (TPEx) 信用交易統計：優先使用含全市場合計與明確交易日期之 RWD 端點
    tpex_urls = [
        "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=json",
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance",
    ]
    for url in tpex_urls:
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict):
                top_d = _parse_tw_api_date(data.get("date"))
                tables = data.get("tables", [])
                for t in tables:
                    table_d = _parse_tw_api_date(t.get("date")) or top_d
                    summary = t.get("summary", [])
                    for srow in summary:
                        if isinstance(srow, list) and len(srow) >= 7 and "融資" in str(srow[1]):
                            prev_val = str(srow[2]).replace(",", "").strip()
                            today_val = str(srow[6]).replace(",", "").strip()
                            if prev_val and today_val:
                                tpex_prev_yi = float(prev_val) / 100000.0
                                tpex_today_yi = float(today_val) / 100000.0
                                tpex_date = table_d
                                break
                    if tpex_today_yi is not None:
                        break
            elif isinstance(data, list):
                for row in data:
                    cname = str(row.get("CompanyName") or "")
                    if "融資" in cname or "合計" in cname:
                        prev_val = str(row.get("MarginPurchaseBalancePreviousDay") or "").replace(",", "").strip()
                        today_val = str(row.get("MarginPurchaseBalance") or "").replace(",", "").strip()
                        if prev_val and today_val:
                            tpex_prev_yi = float(prev_val) / 100000.0
                            tpex_today_yi = float(today_val) / 100000.0
                            tpex_date = _parse_tw_api_date(row.get("Date"))
                            break
            if tpex_today_yi is not None:
                break
        except Exception:
            logger.debug("Failed to fetch TPEx margin from %s", url, exc_info=True)

    if twse_today_yi is None and tpex_today_yi is None:
        return {}

    # 若兩市公告日期不一致（如 20:10 僅單一市場更新），自動對齊至較舊共同交易日，避免跨日混加
    aligned_fallback = False
    if twse_date and tpex_date and twse_date != tpex_date:
        if twse_date > tpex_date and twse_prev_yi is not None:
            twse_today_yi = twse_prev_yi
            twse_prev_yi = None
            data_date = tpex_date
            aligned_fallback = True
        elif tpex_date > twse_date and tpex_prev_yi is not None:
            tpex_today_yi = tpex_prev_yi
            tpex_prev_yi = None
            data_date = twse_date
            aligned_fallback = True
        else:
            data_date = min(twse_date, tpex_date)
            aligned_fallback = True
    else:
        data_date = twse_date or tpex_date or today_str

    twse_margin_yi = round(twse_today_yi or 0.0, 2)
    tpex_margin_yi = round(tpex_today_yi or 0.0, 2)
    total_margin_yi = round(twse_margin_yi + tpex_margin_yi, 2)

    # 若雙市皆未跨日回退且各有前日餘額，直接以官方前日餘額計算最精確單日增減
    official_chg_1d: float | None = None
    if not aligned_fallback:
        if twse_today_yi is not None and tpex_today_yi is not None:
            if twse_prev_yi is not None and tpex_prev_yi is not None:
                official_chg_1d = round(total_margin_yi - (twse_prev_yi + tpex_prev_yi), 2)
        elif twse_today_yi is not None and twse_prev_yi is not None:
            official_chg_1d = round(twse_margin_yi - twse_prev_yi, 2)
        elif tpex_today_yi is not None and tpex_prev_yi is not None:
            official_chg_1d = round(tpex_margin_yi - tpex_prev_yi, 2)

    # 3. 本地滾動歷史紀錄 (保留近 30 個交易日，嚴格以實際交易日 data_date 為 key)
    hist_file = history_path or Path("finmind_reports/.state/market_margin_history.json")
    history: dict[str, dict[str, Any]] = {}
    if hist_file.is_file():
        try:
            loaded = json.loads(hist_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                history = loaded
            elif isinstance(loaded, list):
                history = {str(item.get("date")): item for item in loaded if item.get("date")}
        except Exception:
            logger.debug("Failed to load existing margin history from %s", hist_file, exc_info=True)

    entry: dict[str, Any] = {
        "date": data_date,
        "twse_margin_yi": twse_margin_yi,
        "tpex_margin_yi": tpex_margin_yi,
        "total_margin_yi": total_margin_yi,
    }
    if official_chg_1d is not None:
        entry["margin_chg_1d_yi"] = official_chg_1d
    history[data_date] = entry

    sorted_dates = sorted(history.keys())
    if len(sorted_dates) > 30:
        sorted_dates = sorted_dates[-30:]
        history = {d: history[d] for d in sorted_dates}

    try:
        hist_file.parent.mkdir(parents=True, exist_ok=True)
        hist_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("Failed to persist margin history to %s", hist_file, exc_info=True)

    cur_idx = sorted_dates.index(data_date) if data_date in sorted_dates else len(sorted_dates) - 1
    if official_chg_1d is not None:
        margin_chg_1d_yi = official_chg_1d
    elif cur_idx >= 1:
        prev_d = sorted_dates[cur_idx - 1]
        prev_tot = float(history[prev_d].get("total_margin_yi") or 0.0)
        margin_chg_1d_yi = round(total_margin_yi - prev_tot, 2)
    else:
        margin_chg_1d_yi = 0.0

    if cur_idx >= 5:
        d5 = sorted_dates[cur_idx - 5]
        tot5 = float(history[d5].get("total_margin_yi") or 0.0)
        margin_chg_5d_yi = round(total_margin_yi - tot5, 2)
    elif cur_idx > 0:
        d0 = sorted_dates[0]
        tot0 = float(history[d0].get("total_margin_yi") or 0.0)
        margin_chg_5d_yi = round(total_margin_yi - tot0, 2)
    else:
        margin_chg_5d_yi = margin_chg_1d_yi

    return {
        "date": data_date,
        "as_of_date": today_str,
        "is_stale": bool(data_date < today_str),
        "twse_margin_yi": twse_margin_yi,
        "tpex_margin_yi": tpex_margin_yi,
        "total_margin_yi": total_margin_yi,
        "margin_chg_1d_yi": margin_chg_1d_yi,
        "margin_chg_5d_yi": margin_chg_5d_yi,
    }


def evaluate_market_regime(
    benchmark_prices: pd.DataFrame,
    benchmark_label: str = "0050",
    margin_info: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """依據大盤基準（0050 或 TAIEX）日線均線位階與融資水位計算大盤環境紅綠燈。

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
        "MA189": None,
        "margin_info": dict(margin_info) if margin_info else None,
        "margin_regime": None,
        "recommended_max_exposure_pct": None,
        "recommended_top_picks_cap": 5,
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
    ma189_raw = latest.get("MA189")
    ma189 = float(ma189_raw) if pd.notna(ma189_raw) else None

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

    ma189_text = f"{ma189:,.2f}" if ma189 is not None else "資料不足"
    summary = (
        f"{light}｜{benchmark_label} 收盤 {close:,.2f}"
        f"（17MA {ma17:,.2f}／45MA {ma45:,.2f}／117MA {ma117:,.2f}／189MA {ma189_text}）— {status}"
    )

    margin_regime: str | None = None
    recommended_max_exposure_pct: str | None = None
    recommended_top_picks_cap = 5

    if margin_info and margin_info.get("total_margin_yi") is not None:
        total_yi = float(margin_info.get("total_margin_yi") or 0.0)
        chg_1d = float(margin_info.get("margin_chg_1d_yi") or 0.0)
        chg_5d = float(margin_info.get("margin_chg_5d_yi") or 0.0)

        if light in ("🟡黃燈", "🔴紅燈"):
            if chg_1d <= -40.0 or chg_5d <= -90.0:
                margin_regime = "⚡ 融資斷頭宣洩（留意止穩黃金坑）"
                recommended_max_exposure_pct = "35% ~ 50%"
                recommended_top_picks_cap = 3
            elif chg_1d > 0 or chg_5d > -15.0:
                margin_regime = "🚨 散戶凹單未洗清（提防多殺多斷頭）"
                recommended_max_exposure_pct = "<= 25%"
                recommended_top_picks_cap = 2
            else:
                margin_regime = "🟢 籌碼逐步沉澱"
                recommended_max_exposure_pct = "35% ~ 50%"
                recommended_top_picks_cap = 3
        else:  # 🟢綠燈
            if chg_5d >= 80.0:
                margin_regime = "⚠️ 高檔散戶槓桿過熱"
                recommended_max_exposure_pct = "50%"
                recommended_top_picks_cap = 3
            else:
                margin_regime = "🟢 籌碼健康"
                recommended_max_exposure_pct = "70% ~ 80%"
                recommended_top_picks_cap = 5

        stale_prefix = ""
        m_date_str = str(margin_info.get("date") or "").strip()
        if margin_info.get("is_stale") and m_date_str:
            mm_dd = m_date_str[5:].replace("-", "/") if len(m_date_str) >= 10 else m_date_str
            stale_prefix = f"截至 {mm_dd}，"

        summary += (
            f"｜💳 大盤融資 {total_yi:,.1f} 億"
            f"（{stale_prefix}單日 {chg_1d:+.1f} 億 / 5日 {chg_5d:+.1f} 億｜{margin_regime}）"
            f"｜🛡️ 建議總持倉上限：{recommended_max_exposure_pct}"
        )

    return {
        "light": light,
        "status": status,
        "benchmark": benchmark_label,
        "close": round(close, 2),
        "MA17": round(ma17, 2),
        "MA45": round(ma45, 2),
        "MA117": round(ma117, 2),
        "MA189": round(ma189, 2) if ma189 is not None else None,
        "margin_info": dict(margin_info) if margin_info else None,
        "margin_regime": margin_regime,
        "recommended_max_exposure_pct": recommended_max_exposure_pct,
        "recommended_top_picks_cap": recommended_top_picks_cap,
        "summary": summary,
    }


def _attach_dual_index_regime_quota(
    regime: dict[str, object],
    *,
    as_of: date,
    enable_live_router: bool,
    prices_df: pd.DataFrame | None = None,
    futures_surge_lots: float = 3500.0,
    futures_total_limit: float = 42000.0,
) -> dict[str, object]:
    """為大盤紅綠燈字典附加 SASD v5.4.0 Phase 3 `regime_quota` 策略配額。"""
    try:
        from macro_context import RegimeRouter, quota_from_market_regime
    except Exception:  # noqa: BLE001
        return regime

    if enable_live_router:
        try:
            t_closes: list[float] | None = None
            o_closes: list[float] | None = None
            if prices_df is not None and not prices_df.empty and "stock_id" in prices_df.columns:
                t_sub = prices_df.loc[prices_df["stock_id"].astype(str).str.strip() == "TAIEX"]
                if len(t_sub) >= 20:
                    t_closes = pd.to_numeric(t_sub.sort_values("date")["close"], errors="coerce").dropna().tolist()
                o_sub = prices_df.loc[prices_df["stock_id"].astype(str).str.strip().isin(("TPEx", "OTC"))]
                if len(o_sub) >= 20:
                    o_closes = pd.to_numeric(o_sub.sort_values("date")["close"], errors="coerce").dropna().tolist()
            dual = RegimeRouter.fetch_live_dual_regime(
                taiex_closes=t_closes,
                otc_closes=o_closes,
                as_of_date=as_of.isoformat(),
                futures_net_short_surge_lots=futures_surge_lots,
                futures_total_net_short_limit=futures_total_limit,
            )
            if dual and isinstance(dual.get("quota"), dict):
                q_dict = dual["quota"]
                regime["regime_quota"] = q_dict
                regime["dual_index_detail"] = dual
                tag = str(q_dict.get("regime_summary_tag") or "").strip()
                if tag and tag not in str(regime.get("summary") or ""):
                    regime["summary"] = f"{regime.get('summary', '')}｜🧭 雙核心路由：{tag}".lstrip("｜")
                if q_dict.get("regime_state") == "RED_DEFENSIVE":
                    regime["light"] = "🔴紅燈"
                elif q_dict.get("regime_state") == "YELLOW_ROTATION" and "🟢" in str(regime.get("light") or ""):
                    regime["light"] = "🟡黃燈"
                return regime
        except Exception:  # noqa: BLE001
            logger.debug("Dual-index regime router live fetch failed, using fallback", exc_info=True)

    regime["regime_quota"] = quota_from_market_regime(regime).to_dict()
    return regime


def fetch_or_load_market_regime(
    *,
    daily_directory: Path | None = None,
    as_of: date | None = None,
    loader: DataLoader | None = None,
    benchmark_id: str = "0050",
    margin_info: Mapping[str, Any] | None = None,
    enable_dual_index_router: bool = False,
    futures_surge_lots: float = 3500.0,
    futures_total_limit: float = 42000.0,
) -> dict[str, object]:
    """優先從當日緩存或本地 prices.csv 讀取大盤紅綠燈，若無則透過 FinMind 抓取基準日線並快取。"""
    today = as_of or _today_in_taipei()
    cache_path = (
        daily_directory / f"{today.isoformat()}_market_regime.json"
        if daily_directory is not None
        else None
    )
    today_str = today.isoformat()
    hist_p = daily_directory.parent / ".state" / "market_margin_history.json" if daily_directory else None

    if cache_path is not None and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("light") and "MA189" in cached:
                cached_margin = cached.get("margin_info") if isinstance(cached.get("margin_info"), dict) else None
                cached_margin_date = str((cached_margin or {}).get("date") or "").strip()
                margin_is_stale = margin_info is None and (not cached_margin or cached_margin_date < today_str)

                refreshed_margin: dict[str, Any] | None = None
                if margin_is_stale:
                    try:
                        cand = fetch_market_margin_snapshot(hist_p, as_of=today)
                        cand_date = str(cand.get("date") or "").strip() if isinstance(cand, dict) else ""
                        if cand and cand.get("total_margin_yi") is not None and cand_date > cached_margin_date:
                            refreshed_margin = cand
                    except Exception:
                        refreshed_margin = None

                if refreshed_margin is not None:
                    # 官方已公布更新交易日之融資數據 -> 走下方流程用新融資重算並刷新快取
                    margin_info = refreshed_margin
                elif ("margin_regime" in cached or margin_info is None) and (
                    not enable_dual_index_router or "regime_quota" in cached
                ):
                    if "regime_quota" not in cached:
                        _attach_dual_index_regime_quota(cached, as_of=today, enable_live_router=False)
                    return cached
        except Exception:
            pass

    if margin_info is None:
        try:
            margin_info = fetch_market_margin_snapshot(hist_p, as_of=today)
        except Exception:
            margin_info = None

    local_prices_df: pd.DataFrame | None = None
    if daily_directory is not None:
        prices_path = daily_directory / f"{today.isoformat()}_prices.csv"
        if prices_path.is_file():
            try:
                local_prices_df = pd.read_csv(prices_path, dtype={"stock_id": str})
                sub = local_prices_df.loc[local_prices_df["stock_id"].astype(str).str.strip() == benchmark_id].copy()
                if not sub.empty:
                    regime = evaluate_market_regime(sub, benchmark_label=benchmark_id, margin_info=margin_info)
                    _attach_dual_index_regime_quota(
                        regime,
                        as_of=today,
                        enable_live_router=enable_dual_index_router,
                        prices_df=local_prices_df,
                        futures_surge_lots=futures_surge_lots,
                        futures_total_limit=futures_total_limit,
                    )
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
                regime = evaluate_market_regime(df, benchmark_label=benchmark_id, margin_info=margin_info)
                _attach_dual_index_regime_quota(
                    regime,
                    as_of=today,
                    enable_live_router=enable_dual_index_router,
                    prices_df=local_prices_df,
                    futures_surge_lots=futures_surge_lots,
                    futures_total_limit=futures_total_limit,
                )
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps(regime, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                return regime
        except Exception:
            logger.warning("Failed to fetch market regime benchmark %s", benchmark_id, exc_info=True)

    fallback_regime = evaluate_market_regime(pd.DataFrame(), benchmark_label=benchmark_id, margin_info=margin_info)
    _attach_dual_index_regime_quota(
        fallback_regime,
        as_of=today,
        enable_live_router=enable_dual_index_router,
        prices_df=local_prices_df,
        futures_surge_lots=futures_surge_lots,
        futures_total_limit=futures_total_limit,
    )
    return fallback_regime


TAXONOMY_CACHE_DEFAULT_PATH = Path("finmind_reports/.state/stock_taxonomy.json")


def _load_taxonomy_cache_readonly(
    path: Path = TAXONOMY_CACHE_DEFAULT_PATH,
) -> dict[str, dict[str, object]]:
    """唯讀載入本地細分產業快取（不觸發網路），失敗時回傳空字典。"""
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        logger.warning("無法讀取細分產業快取 %s", path, exc_info=True)
    return {}


def _taxonomy_report_line(
    stock_id: object, taxonomy_map: Mapping[str, Mapping[str, object]]
) -> str:
    """回傳縮排的細分產業與同業標籤（無資料回傳空字串）。"""
    rec = taxonomy_map.get(str(stock_id).strip())
    if not rec:
        return ""
    try:
        from taxonomy_service import format_taxonomy_badge

        badge = format_taxonomy_badge(rec)
    except Exception:  # noqa: BLE001
        return ""
    return f"   └─ {badge}" if badge else ""


def _compact_vol_tag(vol_status: object) -> str:
    """將量能狀態濃縮為適合手機與 LINE 閱讀的短標籤（如 '💧量縮 0.46x'）。"""
    if vol_status is None or pd.isna(vol_status):
        return ""
    s = str(vol_status).strip()
    if not s or s in ("-", "nan"):
        return ""
    ratio_match = re.search(r"(\d+(?:\.\d+)?x)", s)
    ratio_part = f" {ratio_match.group(1)}" if ratio_match else ""
    short_map = (
        ("量縮整理", "💧量縮"),
        ("量能平穩", "➡️平穩"),
        ("溫和放量", "📈溫和放量"),
        ("放量點火", "🔥放量"),
        ("天量過熱", "⚠️天量"),
        ("低量冷門", "⚠️低量"),
    )
    for key, label in short_map:
        if key in s:
            return f"{label}{ratio_part}"
    return s


def _compact_industry(industry: object) -> str:
    """濃縮台股產業分類名稱，減少手機版面折行。"""
    if industry is None or pd.isna(industry):
        return ""
    s = str(industry).strip()
    if not s or s == "nan":
        return ""
    s = s.replace("電腦及週邊設備業", "電腦週邊").replace("電子零組件業", "電子零組件")
    if len(s) > 2 and s.endswith("工業"):
        return s[:-2]
    if len(s) > 2 and s.endswith(("業", "類")):
        return s[:-1]
    return s


def _format_compact_regime_tag(market_regime: Mapping[str, object] | None) -> str:
    """回傳精簡的大盤燈號標籤（如 '🟡黃燈（震盪盤整）'）。"""
    if not market_regime:
        return ""
    light = str(market_regime.get("light", "")).strip()
    status_full = str(market_regime.get("status", "")).strip()
    if light and light != "⚪未知":
        short_status = status_full.split("（", 1)[0].replace("環境", "").strip()
        return f"{light}（{short_status}）" if short_status else light
    summary = str(market_regime.get("summary", "")).strip()
    return summary


def _format_compact_regime_note(
    market_regime: Mapping[str, object] | None, *, mode: str = "alignment"
) -> str:
    """回傳單行精簡大盤操作提示。"""
    if not market_regime:
        return ""
    light = str(market_regime.get("light", "")).strip()
    status_full = str(market_regime.get("status", "")).strip()
    detail = (
        status_full.split("（", 1)[1].rstrip("）").strip()
        if "（" in status_full
        else ""
    )
    if "無基準日線" in detail:
        detail = ""
    if mode == "rebound":
        if "🔴" in light:
            return "🛡️ 警示：大盤跌破 45MA 且下彎，搶反彈請嚴濾現金流並嚴守停損"
        if "🟡" in light:
            return f"💡 說明：{detail or '跌破或測試 17MA 但守長均線，提高反彈權重'}"
    if detail:
        return f"💡 說明：{detail}"
    return ""


def format_alignment_report(
    alignment_signals: pd.DataFrame,
    as_of: date | None = None,
    failed_count: int = 0,
    market_regime: Mapping[str, object] | None = None,
) -> list[str]:
    from github_uploader import get_github_pages_base_url

    report_date = (as_of or _today_in_taipei()).isoformat()
    count = len(alignment_signals)
    regime_tag = _format_compact_regime_tag(market_regime)
    regime_part = f" ｜ 🚦 大盤：{regime_tag}" if regime_tag else ""
    portal_base = get_github_pages_base_url()
    heading_lines = []
    if portal_base:
        heading_lines.append(f"🌐 [開啟今日量化戰情總部]({portal_base}/?date={report_date})")
    heading_lines.extend(
        [
            f"📊【MA 均線排列起漲訊號】{report_date}",
            f"🎯 符合標的：{count} 檔{regime_part}",
        ]
    )
    regime_note = _format_compact_regime_note(market_regime, mode="alignment")
    if regime_note:
        heading_lines.append(regime_note)
    if failed_count:
        heading_lines.append(
            f"⚠️ 有 {failed_count} 檔股票資料抓取失敗，以下僅列成功分析的股票。"
        )
    heading_lines.append("━━━━━━━━━━━━━━━━━━━━")
    heading = "\n".join(heading_lines)
    if alignment_signals.empty:
        return [heading + "\n今日沒有符合條件的股票。"]

    taxonomy_map = _load_taxonomy_cache_readonly()
    chunks: list[str] = []
    current = heading
    for idx, row in enumerate(alignment_signals.itertuples(index=False), start=1):
        signal_label = getattr(row, "signal", None)
        signal_part = f"｜{signal_label}" if signal_label else ""
        trend_label = getattr(row, "ma17_trend", None)

        vol_lots = _parse_optional_float(getattr(row, "vol_lots", None))
        vol_status = getattr(row, "vol_status", None)
        vol_tag = _compact_vol_tag(vol_status)
        vol_part = ""
        if vol_lots is not None:
            status_str = f" ({vol_tag})" if vol_tag else ""
            vol_part = f"｜量 {int(round(vol_lots)):,}張{status_str}"

        sub_lines: list[str] = []
        if trend_label and str(trend_label).strip() not in ("", "-", "nan"):
            sub_lines.append(f"   └ 17MA：{trend_label}")
        tax_line = _taxonomy_report_line(row.stock_id, taxonomy_map)
        if tax_line:
            sub_lines.append(tax_line)

        sub_block = ("\n" + "\n".join(sub_lines)) if sub_lines else ""
        line = (
            f"\n{idx}. {row.stock_id} {row.stock_name}{signal_part}"
            f"｜收盤 {float(row.close):,.2f}{vol_part}"
            f"{sub_block}"
        )
        if len(current) + len(line) > 1800:
            chunks.append(current)
            current = heading_lines[0] + "\n（續）"
            line = (
                f"\n{idx}. {row.stock_id} {row.stock_name}{signal_part}"
                f"｜收盤 {float(row.close):,.2f}{vol_part}"
                f"{sub_block}"
            )
        current += line

    footer = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "📌 完整均線數據已記錄於 Sheets「MA_Signal_Tracker」"
    )
    if len(current) + len(footer) <= 1950:
        current += footer
    chunks.append(current)
    return chunks


def _trim_reply_continuation(content: str, *, marker: str = "（續）", scan_lines: int = 50) -> str:
    """Trim repeated heading for continuation chunks.

    Some scheduled Discord reports format continuation chunks as "<heading>\n（續）\n...".
    When the chunk is posted as a reply to the first message (message_reference),
    the heading context is already visible, so we strip everything up to and
    including the marker line.
    """

    lines = content.splitlines()
    for idx, line in enumerate(lines[:scan_lines]):
        if line.strip() == marker:
            trimmed = "\n".join(lines[idx + 1 :]).lstrip("\n")
            return trimmed or content
    return content


def format_compact_alignment_notification(
    alignment_signals: pd.DataFrame,
    as_of: date | None = None,
    failed_count: int = 0,
    market_regime: Mapping[str, object] | None = None,
    *,
    max_preview: int = 15,
) -> list[str]:
    """精簡單則 19:00 均線掃描 Discord 通知（完整名單導流至戰情總部網頁）。"""
    from github_uploader import get_github_pages_base_url

    report_date = (as_of or _today_in_taipei()).isoformat()
    count = len(alignment_signals)
    regime_tag = _format_compact_regime_tag(market_regime)
    regime_part = f" ｜ 🚦 大盤：{regime_tag}" if regime_tag else ""
    portal_base = get_github_pages_base_url()

    lines: list[str] = []
    if portal_base:
        lines.append(f"🌐 [開啟今日量化戰情總部]({portal_base}/?date={report_date})")
    lines.append(f"📊 **【MA 均線排列初選快報】{report_date}**")
    lines.append(f"🎯 符合標的：**{count} 檔**{regime_part}")
    if failed_count:
        lines.append(f"⚠️ {failed_count} 檔資料抓取失敗（已略過）")

    if alignment_signals.empty:
        lines.append("今日沒有符合條件的股票。")
    else:
        for idx, row in enumerate(alignment_signals.head(max_preview).itertuples(index=False), start=1):
            sig = str(getattr(row, "signal", "") or "").strip()
            vol_lots = _parse_optional_float(getattr(row, "vol_lots", None))
            vol_tag = _compact_vol_tag(getattr(row, "vol_status", None))
            vol_part = f"｜量 {int(round(vol_lots)):,}張" + (f"({vol_tag})" if vol_tag else "") if vol_lots is not None else ""
            sig_part = f"｜{sig}" if sig else ""
            lines.append(f"{idx}. `{row.stock_id}` {row.stock_name} `{float(row.close):,.1f}`{sig_part}{vol_part}")
        if count > max_preview:
            lines.append(f"… 另有 {count - max_preview} 檔，完整清單請點上方戰情總部。")
        lines.append("⏳ 21:30 AI 深度財報與三階低接點位將自動更新至總部網頁。")

    text = "\n".join(lines)
    return [text[:1950]]


def post_alignment_report_to_discord(
    alignment_signals: pd.DataFrame,
    channel_id: str,
    bot_token: str,
    as_of: date | None = None,
    failed_count: int = 0,
    market_regime: Mapping[str, object] | None = None,
    *,
    compact: bool = False,
) -> int:
    if not channel_id.isdigit():
        raise ValueError("STOCK_CHANNEL_ID 必須是 Discord 頻道 ID 數字。")
    if not bot_token.strip():
        raise ValueError("DISCORD_TOKEN (or BOT_TOKEN) is required to post the report.")

    if compact:
        messages = format_compact_alignment_notification(
            alignment_signals,
            as_of,
            failed_count=failed_count,
            market_regime=market_regime,
        )
    else:
        messages = format_alignment_report(
            alignment_signals,
            as_of,
            failed_count=failed_count,
            market_regime=market_regime,
        )
    sent_count = 0
    first_message_id: str | None = None
    for index, content in enumerate(messages):
        content_to_send = content
        if index > 0 and first_message_id:
            content_to_send = _trim_reply_continuation(content)

        payload_dict: dict[str, Any] = {
            "content": content_to_send,
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
    """格式化型態 A（高點急跌回踩長均線反彈）Discord 快報（手機/LINE 雙行卡片版）。"""
    from github_uploader import get_github_pages_base_url

    report_date = (as_of or _today_in_taipei()).isoformat()
    count = len(rebound_signals)
    regime_tag = _format_compact_regime_tag(market_regime)
    regime_part = f" ｜ 🚦 大盤：{regime_tag}" if regime_tag else ""
    portal_base = get_github_pages_base_url()
    heading_lines = []
    if portal_base:
        heading_lines.append(f"🌐 [開啟今日量化戰情總部]({portal_base}/?date={report_date})")
    heading_lines.extend(
        [
            f"🪃【型態 A：高點回跌回踩長均線反彈清單】{report_date}",
            f"🎯 符合標的：{count} 檔{regime_part}",
        ]
    )
    regime_note = _format_compact_regime_note(market_regime, mode="rebound")
    if regime_note:
        heading_lines.append(regime_note)
    if failed_count:
        heading_lines.append(f"⚠️ 有 {failed_count} 檔股票分析過程發生異常。")
    heading_lines.append("━━━━━━━━━━━━━━━━━━━━")
    heading = "\n".join(heading_lines)

    if rebound_signals.empty:
        return [heading + "\n今日沒有符合型態 A 反彈條件的股票。"]

    taxonomy_map = _load_taxonomy_cache_readonly()
    chunks: list[str] = []
    current = heading
    for idx, row in enumerate(rebound_signals.itertuples(index=False), start=1):
        ind_short = _compact_industry(getattr(row, "industry_category", ""))
        ind_part = f"（{ind_short}）" if ind_short else ""
        vol_lots = _parse_optional_float(getattr(row, "vol_lots", None))
        vol_status = getattr(row, "vol_status", "-")
        vol_tag = _compact_vol_tag(vol_status)
        if vol_lots is not None:
            tag_str = f" ({vol_tag})" if vol_tag else ""
            vol_str = f" ｜ 量 {int(round(vol_lots)):,}張{tag_str}"
        else:
            vol_str = ""

        subtype_raw = str(getattr(row, "rebound_subtype", "") or "").strip()
        subtype_badge = (
            " [R2 W底右腳預掛]"
            if subtype_raw == "W_BOTTOM_PULLBACK_SETUP"
            else (" [R1 觸線止穩]" if subtype_raw == "REBOUND_TOUCH_HOLD" else "")
        )
        sub_lines = [
            f"   └ 踩 {row.support_ma} ({float(row.support_gap_pct):+.1f}%){subtype_badge}"
            f" ｜ 距 17MA +{float(row.potential_return_pct):.1f}%{vol_str}"
        ]
        b_low = _parse_optional_float(getattr(row, "ideal_buy_zone_low", None))
        b_high = _parse_optional_float(getattr(row, "ideal_buy_zone_high", None))
        h_sl = _parse_optional_float(getattr(row, "hard_stop_loss", None))
        tp1_v = _parse_optional_float(getattr(row, "tp1_price", None))
        rr_v = _parse_optional_float(getattr(row, "deterministic_rr_ratio", None))
        if b_low is not None and b_high is not None and h_sl is not None and tp1_v is not None:
            rr_str = f" (RR {rr_v:.2f})" if rr_v is not None else ""
            sub_lines.append(
                f"   └ 🎯 預掛 {b_low:.2f}~{b_high:.2f} ｜ 🛡️ 硬停損 {h_sl:.2f} ｜ TP1 {tp1_v:.2f}{rr_str}"
            )
        tax_line = _taxonomy_report_line(row.stock_id, taxonomy_map)
        if tax_line:
            sub_lines.append(tax_line)
        sub_block = "\n".join(sub_lines)
        line = (
            f"\n{idx}. {row.stock_id} {row.stock_name}{ind_part}"
            f"｜收盤 {float(row.close):,.2f}｜強度 {float(row.close_strength):.2f}\n"
            f"{sub_block}"
        )
        if len(current) + len(line) > 1800:
            chunks.append(current)
            current = heading_lines[0] + "\n（續）"
            line = (
                f"\n{idx}. {row.stock_id} {row.stock_name}{ind_part}"
                f"｜收盤 {float(row.close):,.2f}｜強度 {float(row.close_strength):.2f}\n"
                f"{sub_block}"
            )
        current += line

    if not ai_executed and count > 0:
        footer = (
            "\n━━━━━━━━━━━━━━━━━━━━\n"
            "📌 詳細篩選條件已記錄於 Sheets「Rebound_Watchlist」"
        )
        if len(current) + len(footer) <= 1950:
            current += footer
    chunks.append(current)
    return chunks


def format_compact_rebound_notification(
    rebound_signals: pd.DataFrame,
    as_of: date | None = None,
    failed_count: int = 0,
    market_regime: Mapping[str, object] | None = None,
    ai_executed: bool = False,
    *,
    max_preview: int = 12,
) -> list[str]:
    """精簡單則 22:00 型態 A 反彈掃描 Discord 通知（完整名單導流至戰情總部網頁）。"""
    from github_uploader import get_github_pages_base_url

    report_date = (as_of or _today_in_taipei()).isoformat()
    count = len(rebound_signals)
    regime_tag = _format_compact_regime_tag(market_regime)
    regime_part = f" ｜ 🚦 大盤：{regime_tag}" if regime_tag else ""
    portal_base = get_github_pages_base_url()

    lines: list[str] = []
    if portal_base:
        lines.append(f"🌐 [開啟今日量化戰情總部]({portal_base}/?date={report_date})")
    lines.append(f"🪃 **【型態 A：長均線反彈快報】{report_date}**")
    lines.append(f"🎯 符合標的：**{count} 檔**{regime_part}")
    if failed_count:
        lines.append(f"⚠️ {failed_count} 檔分析過程異常（已略過）")

    if rebound_signals.empty:
        lines.append("今日沒有符合型態 A 反彈條件的股票。")
    else:
        for idx, row in enumerate(rebound_signals.head(max_preview).itertuples(index=False), start=1):
            sup_ma = getattr(row, "support_ma", "") or ""
            drop_pct = _parse_optional_float(getattr(row, "drop_pct", None))
            drop_part = f"｜跌 -{drop_pct:.1f}%" if drop_pct is not None else ""
            sup_part = f"｜踩 {sup_ma}" if sup_ma else ""
            lines.append(f"{idx}. `{row.stock_id}` {row.stock_name} `{float(row.close):,.1f}`{drop_part}{sup_part}")
        if count > max_preview:
            lines.append(f"… 另有 {count - max_preview} 檔，完整清單請點上方戰情總部。")
        if ai_executed:
            lines.append("✅ 已完成 AI 深度診斷與三階低接布局（詳見總部網頁與 AI 嚴選快報）。")

    text = "\n".join(lines)
    return [text[:1950]]


def post_rebound_report_to_discord(
    rebound_signals: pd.DataFrame,
    channel_id: str,
    bot_token: str,
    as_of: date | None = None,
    failed_count: int = 0,
    market_regime: Mapping[str, object] | None = None,
    ai_executed: bool = False,
    *,
    compact: bool = False,
) -> int:
    if not channel_id.isdigit():
        raise ValueError("STOCK_CHANNEL_ID 必須是 Discord 頻道 ID 數字。")
    if not bot_token.strip():
        raise ValueError("DISCORD_TOKEN (or BOT_TOKEN) is required to post the report.")

    if compact:
        messages = format_compact_rebound_notification(
            rebound_signals,
            as_of=as_of,
            failed_count=failed_count,
            market_regime=market_regime,
            ai_executed=ai_executed,
        )
    else:
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
        content_to_send = content
        if index > 0 and first_message_id:
            content_to_send = _trim_reply_continuation(content)

        payload_dict: dict[str, Any] = {
            "content": content_to_send,
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
    if resolved_credentials and Path(resolved_credentials).is_file():
        pass
    elif Path("certs/sheetbot.json").is_file():
        resolved_credentials = "certs/sheetbot.json"
    elif Path("sheetbot.json").is_file():
        resolved_credentials = "sheetbot.json"
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


def _fetch_or_cache_dividend_events(
    loader: DataLoader | None,
    stock_id: str,
    start_date: str,
    end_date: str,
    *,
    cache_path: Path = Path("finmind_reports/.state/dividend_events_cache.json"),
    ttl_days: int = 30,
) -> pd.DataFrame | None:
    """快取個股除權息事件表（TTL 30 日），僅於 K 線出現除權息等級向下跳空時查詢。"""
    sid = str(stock_id).strip()
    today = _today_in_taipei()
    store: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                store = loaded
        except Exception:  # noqa: BLE001
            store = {}
    cached = store.get(sid)
    if isinstance(cached, Mapping):
        try:
            fetched_dt = date.fromisoformat(str(cached.get("fetched_at", ""))[:10])
            if (today - fetched_dt).days <= ttl_days and isinstance(cached.get("events"), list):
                return pd.DataFrame(cached["events"])
        except ValueError:
            pass
    if loader is None or not hasattr(loader, "taiwan_stock_dividend"):
        return None
    try:
        raw_div = loader.taiwan_stock_dividend(stock_id=sid, start_date=start_date, end_date=end_date)
        if not isinstance(raw_div, pd.DataFrame):
            return None
        div_map = _build_dividend_event_map(raw_div)
        events = [{"ex_date": k, **v} for k, v in sorted(div_map.items())]
        store[sid] = {"fetched_at": today.isoformat(), "events": events}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
        return pd.DataFrame(events)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Dividend events fetch skipped for %s: %s", sid, exc)
        return None


def scan_industry(
    loader: DataLoader,
    stocks: pd.DataFrame,
    market: str,
    industry: str,
    output_dir: Path,
    as_of: date | None = None,
    request_pause_seconds: float | None = REQUEST_PAUSE_SECONDS,
    min_median_turnover: float = MIN_MEDIAN_TURNOVER_20D_TWD,
    enable_ex_dividend_compensation: bool = False,
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
            if not passes_liquidity_median_guard(prices, min_median_turnover):
                raise ValueError("近 20 日成交金額中位數低於門檻，流動性不足")
            if enable_ex_dividend_compensation and "close" in prices.columns:
                c_series = pd.to_numeric(prices.sort_values("date")["close"], errors="coerce")
                if (c_series.pct_change() <= -0.02).any():
                    div_df = _fetch_or_cache_dividend_events(
                        loader, stock_id, fetch_start_date, end_date
                    )
                    if div_df is not None and not div_df.empty:
                        prices = compensate_ex_dividend_gaps(prices, div_df)
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
    for channel_key in ("alignment_channel_id", "rebound_channel_id"):
        if channel_key in config:
            if not isinstance(config.get(channel_key), str) or not str(
                config[channel_key]
            ).isdigit():
                raise ValueError(f"設定 {channel_key} 必須是數字 ID 字串。")
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
    for bool_key in (
        "enable_ex_dividend_compensation",
        "ai_picker_red_blue_audit",
        "enable_dual_index_regime_router",
        "enable_signal_audit",
        "enable_portal_publish",
        "discord_compact_mode",
    ):
        if bool_key in config and not isinstance(config.get(bool_key), bool):
            raise ValueError(f"設定 {bool_key} 必須是布林值。")
    for num_key in (
        "min_median_turnover_20d_twd",
        "futures_net_short_surge_lots",
        "futures_total_net_short_limit",
        "portal_retention_trading_days",
    ):
        if num_key in config and (
            not isinstance(config.get(num_key), (int, float))
            or isinstance(config.get(num_key), bool)
        ):
            raise ValueError(f"設定 {num_key} 必須是數字。")
    return config


def _configured_universe(
    stock_info: pd.DataFrame,
    config: Mapping[str, object],
    daily_snapshot: Mapping[str, Mapping[str, float]] | None = None,
    registry_stocks: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[tuple[str, str], pd.DataFrame]:
    _validate_stock_info(stock_info)
    excluded_ids = {item["stock_id"] for item in config["excluded_stocks"]}
    if registry_stocks:
        for sid, info in registry_stocks.items():
            if isinstance(info, Mapping) and info.get("status") == "DELISTED":
                excluded_ids.add(str(sid))

    excluded_categories = EXCLUDED_INDUSTRY_CATEGORIES.union(
        config["excluded_industry_categories"]
    )
    only_4digits = bool(config.get("only_common_stocks_4digits", False))
    enable_prefilter = bool(config.get("enable_twse_tpex_prefilter", False))
    min_lots = int(config.get("min_trading_volume_lots", 1000))
    min_turnover = float(config.get("min_turnover_twd", 100_000_000.0))
    vol_mode = str(config.get("volume_filter_mode", "today_or_ma20_or_turnover"))

    configured_industries = list(config["industries"])
    available_industries = sorted(
        set(stock_info["industry_category"].dropna().astype(str).str.strip())
        - excluded_categories
    )
    if "ALL" in configured_industries or "*" in configured_industries:
        target_industries = available_industries
    else:
        missing_industries = sorted(
            set(configured_industries).difference(
                set(stock_info["industry_category"].dropna().astype(str))
            )
        )
        if missing_industries:
            raise ValueError(
                "FinMind 股票清單不存在設定的產業：" + "、".join(missing_industries)
            )
        target_industries = configured_industries

    from taxonomy_service import FOUR_DIGIT_RE, passes_dual_track_liquidity

    universe: dict[tuple[str, str], pd.DataFrame] = {}
    for market in config["markets"]:
        market_stocks = _filter_market(stock_info, market)
        sid_series = market_stocks["stock_id"].astype(str).str.strip()
        mask = (
            ~market_stocks["industry_category"].isin(excluded_categories)
            & ~sid_series.isin(excluded_ids)
        )
        if only_4digits:
            mask = mask & sid_series.str.match(FOUR_DIGIT_RE)
        market_stocks = market_stocks.loc[mask].copy()

        if enable_prefilter and (daily_snapshot is not None or registry_stocks is not None):
            snap = daily_snapshot or {}
            reg_map = registry_stocks or {}
            keep_ids = {
                str(sid).strip()
                for sid in market_stocks["stock_id"]
                if passes_dual_track_liquidity(
                    str(sid).strip(),
                    snap,
                    reg_map,
                    min_lots=min_lots,
                    min_turnover_twd=min_turnover,
                    mode=vol_mode,
                )
            }
            market_stocks = market_stocks.loc[
                market_stocks["stock_id"].astype(str).str.strip().isin(keep_ids)
            ].copy()

        for industry in target_industries:
            selected = market_stocks.loc[
                market_stocks["industry_category"].astype(str).str.strip() == industry
            ].copy()
            universe[(market, industry)] = (
                selected.drop_duplicates(subset=["stock_id"]).reset_index(drop=True)
            )

    # SASD v5.0: 全市場掃描時（ALL 或包含多產業），避免同一檔個股在不同群組重複掃描
    if len(target_industries) > 1:
        seen_stock_ids: set[str] = set()
        dedup_universe: dict[tuple[str, str], pd.DataFrame] = {}
        for key, df in universe.items():
            if df.empty:
                dedup_universe[key] = df
                continue
            mask = ~df["stock_id"].astype(str).str.strip().isin(seen_stock_ids)
            filtered = df.loc[mask].reset_index(drop=True)
            seen_stock_ids.update(filtered["stock_id"].astype(str).str.strip())
            dedup_universe[key] = filtered
        return dedup_universe

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

    # SASD v3.5.1: also cleanup consolidated AI call logs (finmind_reports/logs/YYYY-MM-DD_ai_calls.log)
    logs_dir = daily_directory.parent / "logs"
    ai_log_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})_ai_calls\.log$")
    for path in daily_directory.iterdir():
        match = filename_pattern.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        file_date = date.fromisoformat(path.name[:10])
        if file_date < cutoff:
            path.unlink()

    # SASD v5.3.0: cleanup local Vertex AI Batch input JSONL (finmind_reports/.state/batch/*.jsonl)
    batch_dir = daily_directory.parent / ".state" / "batch"
    if batch_dir.is_dir():
        cutoff_ts = datetime.combine(cutoff, datetime.min.time()).timestamp()
        for path in batch_dir.iterdir():
            if path.is_file() and path.suffix == ".jsonl" and path.stat().st_mtime < cutoff_ts:
                path.unlink()

    if logs_dir.is_dir():
        for path in logs_dir.iterdir():
            if not path.is_file():
                continue
            match = ai_log_pattern.fullmatch(path.name)
            if match is None:
                continue
            try:
                file_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
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
            elif not set(("K9", "D9", "MACD_OSC")).issubset(working.columns):
                working = add_momentum_indicators(working)
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
    audit_records: Mapping[str, Mapping[str, object]] | None = None,
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
        ai_model_version = ""
        war_room_url = ""
        if ai_results and sid in ai_results:
            item = ai_results[sid]
            ai_summary = str(item.get("summary", "") or "")
            report = item.get("report")
            ai_model_version = str(getattr(report, "model_version", "") or item.get("model_version", "") or "")
            war_room_url = str(getattr(report, "war_room_url", "") or item.get("war_room_url", "") or "")
        elif existing_master_map and sid in existing_master_map:
            m_row = existing_master_map[sid]
            ai_summary = str(m_row.get("ai_summary_json", "") or "")
            ai_model_version = str(m_row.get("ai_model_version", "") or "")
            war_room_url = str(m_row.get("war_room_url", "") or "")

        aud = (audit_records or {}).get(sid) or {}
        b_low = _parse_optional_float(row.get("ideal_buy_zone_low"))
        b_high = _parse_optional_float(row.get("ideal_buy_zone_high"))
        default_ideal = (
            f"{b_low:.2f}~{b_high:.2f}"
            if (b_low is not None and b_high is not None and b_low != b_high)
            else (f"{b_high:.2f}" if b_high is not None else "")
        )
        ideal_buy = str(aud.get("Ideal_Buy_Or_Ref") or default_ideal).strip()
        chase_limit = aud.get("Chase_Limit_Price")
        if chase_limit in (None, ""):
            chase_limit = b_high if b_high is not None else ""
        hard_sl = aud.get("Hard_Stop_Loss")
        if hard_sl in (None, ""):
            sl_val = _parse_optional_float(row.get("hard_stop_loss"))
            hard_sl = sl_val if sl_val is not None else ""
        tp1_target = aud.get("TP1_Target_Or_POC")
        if tp1_target in (None, ""):
            tp1_val = _parse_optional_float(row.get("tp1_price")) or _parse_optional_float(row.get("target_ma17"))
            tp1_target = tp1_val if tp1_val is not None else ""
        det_rr = aud.get("Deterministic_RR")
        if det_rr in (None, ""):
            rr_val = _parse_optional_float(row.get("deterministic_rr_ratio"))
            det_rr = rr_val if rr_val is not None else ""
        triggers_raw = aud.get("Invalidation_Triggers") or []
        triggers_str = (
            "；".join(str(t) for t in triggers_raw)
            if isinstance(triggers_raw, (list, tuple))
            else str(triggers_raw or "")
        )

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
                "rebound_subtype": str(row.get("rebound_subtype") or aud.get("Rebound_Subtype") or "").strip(),
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
                "ai_model_version": ai_model_version,
                "war_room_url": war_room_url,
                "Audit_Grade": str(aud.get("Audit_Grade") or "").strip(),
                "Execution_Tactic": str(aud.get("Execution_Tactic") or "STYLE_B_PULLBACK_LIMIT").strip(),
                "Ideal_Buy_Or_Ref": ideal_buy,
                "Chase_Limit_Price": chase_limit,
                "Hard_Stop_Loss": hard_sl,
                "TP1_Target_Or_POC": tp1_target,
                "Deterministic_RR": det_rr,
                "Bear_Case_Warning": str(aud.get("Bear_Case_Warning") or "").strip(),
                "Invalidation_Triggers": triggers_str,
                "Allocation_Weight_Pct": aud.get("Allocation_Weight_Pct", ""),
                "Tier1_Entry_Plan": str(aud.get("Tier1_Entry_Plan") or "").strip(),
                "Tier2_Entry_Plan": str(aud.get("Tier2_Entry_Plan") or "").strip(),
                "Tier3_Entry_Plan": str(aud.get("Tier3_Entry_Plan") or "").strip(),
            }
        )
    return rows


def sync_rebound_watchlist_to_gsheet(
    rebound_df: pd.DataFrame,
    *,
    as_of: date | None = None,
    market_light: str = "🟡黃燈",
    ai_results: Mapping[str, Mapping[str, object]] | None = None,
    audit_records: Mapping[str, Mapping[str, object]] | None = None,
    spreadsheet_id: str | None = None,
    credentials_path: str | None = None,
    tab_name: str = REBOUND_WATCHLIST_SHEET_NAME,
    replace_date: bool = True,
) -> int:
    """將型態 A 反彈清單同步寫入 Google Sheets Rebound_Watchlist。"""
    resolved_credentials = (
        credentials_path
        if credentials_path is not None
        else os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    )
    if resolved_credentials and Path(resolved_credentials).is_file():
        pass
    elif Path("certs/sheetbot.json").is_file():
        resolved_credentials = "certs/sheetbot.json"
    elif Path("sheetbot.json").is_file():
        resolved_credentials = "sheetbot.json"
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
        audit_records=audit_records,
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
    daily_snapshot: dict[str, dict[str, float]] | None = None
    registry_stocks: dict[str, dict[str, object]] | None = None
    if config.get("enable_twse_tpex_prefilter"):
        try:
            from taxonomy_service import (
                fetch_twse_tpex_daily_snapshot,
                update_registry_with_daily_snapshot,
            )

            daily_snapshot = fetch_twse_tpex_daily_snapshot()
            registry = update_registry_with_daily_snapshot(
                Path(
                    str(
                        config.get(
                            "universe_registry_path",
                            output_root / ".state" / "stock_universe_registry.json",
                        )
                    )
                ),
                stock_info,
                daily_snapshot,
                today,
                int(config.get("delisted_missing_days_threshold", 5)),
            )
            registry_stocks = registry.get("stocks", {})
        except Exception:
            logger.warning("Stage 1 全市場快照預篩失敗，退回不預篩", exc_info=True)
    universe = _configured_universe(
        stock_info, config, daily_snapshot, registry_stocks
    )

    all_prices: list[Path] = []
    all_alignments: list[Path] = []
    all_failures: list[Path] = []
    failed_groups: list[str] = []

    with tempfile.TemporaryDirectory(prefix="finmind-daily-") as temp_path:
        temporary_output = Path(temp_path)
        with _rate_limit_finmind_http_requests(limiter, only_hosts=("finmindtrade.com",)):
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
                        min_median_turnover=float(
                            config.get("min_median_turnover_20d_twd", MIN_MEDIAN_TURNOVER_20D_TWD)
                        ),
                        enable_ex_dividend_compensation=bool(
                            config.get("enable_ex_dividend_compensation", False)
                        ),
                    )
                except Exception:
                    logger.exception("Failed to scan %s / %s", market, industry)
                    failed_groups.append(f"{market}/{industry}")
                    continue
                all_prices.append(paths[0])
                all_alignments.append(paths[1])
                if paths[2] is not None:
                    all_failures.append(paths[2])

        raw_prices = _combine_csvs(
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
        )
        raw_signals = _combine_csvs(
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
        )
        report_files = {
            "prices": raw_prices.drop_duplicates(subset=["stock_id", "date"]).reset_index(drop=True),
            "alignment_signals": raw_signals.drop_duplicates(subset=["stock_id"]).reset_index(drop=True),
            "failures": _combine_csvs(
                all_failures, ["stock_id", "stock_name", "error"]
            ).drop_duplicates(subset=["stock_id"]).reset_index(drop=True),
        }

        # 若設定了大盤基準（例如 TAIEX），在掃描完成後一併抓取並快取大盤日線，同時追加進 prices.csv
        market_regime: dict[str, object] | None = None
        if config.get("enable_market_regime"):
            benchmark_id = str(config.get("market_benchmark_id") or "TAIEX")
            with _rate_limit_finmind_http_requests(limiter, only_hosts=("finmindtrade.com",)):
                market_regime = fetch_or_load_market_regime(
                    daily_directory=daily_directory,
                    as_of=today,
                    loader=loader,
                    benchmark_id=benchmark_id,
                    enable_dual_index_router=bool(config.get("enable_dual_index_regime_router", False)),
                    futures_surge_lots=float(config.get("futures_net_short_surge_lots", 3500.0)),
                    futures_total_limit=float(config.get("futures_total_net_short_limit", 42000.0)),
                )
            # 若 prices.csv 尚未包含該大盤代號，將大盤基準日線追加至 prices.csv，方便後續 AI 嚴選流程秒級重算
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

    # Stage 3: 為入榜訊號股補齊細分產業與同業標籤（失敗不影響主流程）
    if not alignment_signals.empty:
        try:
            from taxonomy_service import TaxonomyService

            valid_stock_map = dict(
                zip(
                    stock_info["stock_id"].astype(str).str.strip(),
                    stock_info["stock_name"].astype(str).str.strip(),
                )
            )
            TaxonomyService(
                cache_path=Path(
                    str(
                        config.get(
                            "taxonomy_cache_path",
                            output_root / ".state" / "stock_taxonomy.json",
                        )
                    )
                ),
                ttl_days=int(config.get("taxonomy_ttl_days", 90)),
                sheet_tab_name=str(
                    config.get("taxonomy_gsheet_tab_name", "08_Stock_Taxonomy")
                ),
            ).ensure_batch(alignment_signals, valid_stock_map, as_of=today)
        except Exception:
            logger.warning("細分產業知識庫補齊失敗", exc_info=True)

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
        with _rate_limit_finmind_http_requests(limiter, only_hosts=("finmindtrade.com",)):
            market_regime = fetch_or_load_market_regime(
                daily_directory=daily_directory,
                as_of=today,
                loader=loader,
                benchmark_id=benchmark_id,
                enable_dual_index_router=bool(config.get("enable_dual_index_regime_router", False)),
                futures_surge_lots=float(config.get("futures_net_short_surge_lots", 3500.0)),
                futures_total_limit=float(config.get("futures_total_net_short_limit", 42000.0)),
            )

    # SASD v6.2.0：盤後自動回填 signal_audit_log 之 T+1~T+10 實盤路徑（含速率控管之 FinMind 備援）並同步 Google Sheets
    if config.get("enable_signal_audit"):
        try:
            run_signal_audit_backfill(
                config,
                as_of=today,
                daily_directory=daily_directory,
                extra_prices_df=report_files["prices"],
                loader=loader,
                limiter=limiter,
                sync_gsheet=bool(config.get("sync_alignment_to_gsheet") or config.get("sync_signals_to_gsheet")),
            )
        except Exception:
            logger.warning("盤後事後審計 (signal_audit) 回填略過", exc_info=True)

    # SASD v6.2.0：先發布戰情總部網頁再發 Discord，確保點開連結即為最新資料
    if config.get("enable_portal_publish"):
        try:
            from portal_publisher import publish_portal_data

            publish_portal_data(
                config,
                as_of=today,
                output_root=output_directory,
                commit_message=f"Update quant portal 19:00 scan ({today.isoformat()})",
            )
        except Exception:
            logger.warning("19:00 戰情總部發布失敗（略過，不影響主流程）", exc_info=True)

    if config["post_alignment_to_discord"]:
        bot_token = (
            os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or ""
        ).strip()
        target_channel = str(
            config.get("alignment_channel_id")
            or config.get("stock_channel_id")
            or os.getenv("ALIGNMENT_SIGNALS_CHANNEL_ID")
            or os.getenv("STOCK_CHANNEL_ID")
            or ""
        ).strip()
        post_alignment_report_to_discord(
            alignment_signals,
            channel_id=target_channel,
            bot_token=bot_token,
            as_of=today,
            failed_count=len(failures) + len(failed_groups),
            market_regime=market_regime,
            compact=bool(config.get("discord_compact_mode", False)),
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

    不呼叫 FinMind API；prices.csv 內的 MA3~MA189 為盤後掃描以近一年資料算好的值，
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

    if not alignments.empty and not dry_run:
        try:
            from taxonomy_service import TaxonomyService

            stock_map = dict(
                zip(
                    prices["stock_id"].astype(str).str.strip(),
                    prices["stock_name"].astype(str).str.strip()
                    if "stock_name" in prices.columns
                    else prices["stock_id"].astype(str).str.strip(),
                )
            )
            tax_path = Path(
                str(
                    config.get(
                        "taxonomy_cache_path",
                        output_root / ".state" / "stock_taxonomy.json",
                    )
                )
            )
            TaxonomyService(
                cache_path=tax_path,
                ttl_days=int(config.get("taxonomy_ttl_days", 90)),
                sheet_tab_name=str(
                    config.get("taxonomy_gsheet_tab_name", "08_Stock_Taxonomy")
                ),
            ).ensure_batch(alignments, stock_map, as_of=today)
        except Exception:
            logger.warning("[recalc] 細分產業知識庫補齊失敗", exc_info=True)

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
        target_channel = str(
            config.get("alignment_channel_id")
            or config.get("stock_channel_id")
            or os.getenv("ALIGNMENT_SIGNALS_CHANNEL_ID")
            or os.getenv("STOCK_CHANNEL_ID")
            or ""
        ).strip()
        post_alignment_report_to_discord(
            alignments,
            channel_id=target_channel,
            bot_token=bot_token,
            as_of=today,
            failed_count=failed_count,
        )
        print("[recalc] 已重發 Discord 均線快報。")
    return alignments


def init_universe_registry(
    loader: DataLoader,
    stock_info: pd.DataFrame,
    config: Mapping[str, object],
    *,
    output_dir: Path | None = None,
    as_of: date | None = None,
    request_limiter: FinMindRequestLimiter | None = None,
    skip_recent_days: int = 7,
    stale_days: int = 15,
) -> dict[str, object]:
    """以 FinMind 近 30 日日線初始化 stock_universe_registry.json（支援中斷續傳）。

    - registry 內 last_seen 在 skip_recent_days 內者自動跳過。
    - 無日線或最後交易日距今 > stale_days 者標記為 DELISTED。
    - 計算近 20 日均量寫入 vol_ma20_lots 與 vol_history_20d。
    """
    from taxonomy_service import (
        FOUR_DIGIT_RE,
        _atomic_write_json,
        load_universe_registry,
    )

    today = as_of or _today_in_taipei()
    output_root = output_dir or Path(str(config["output_directory"]))
    registry_path = Path(
        str(
            config.get(
                "universe_registry_path",
                output_root / ".state" / "stock_universe_registry.json",
            )
        )
    )
    limiter = request_limiter or FinMindRequestLimiter(
        output_root / ".state" / "finmind_request_history.sqlite3",
        int(config["max_requests_per_hour"]),
        float(config["min_request_interval_seconds"]),
    )
    registry = load_universe_registry(registry_path)
    stocks_map: dict[str, dict[str, object]] = registry.setdefault("stocks", {})
    start_date = (pd.Timestamp(today) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")

    candidates = stock_info.drop_duplicates(subset=["stock_id"])
    processed = skipped = delisted = 0
    with _rate_limit_finmind_http_requests(limiter, only_hosts=("finmindtrade.com",)):
        for row in candidates.itertuples(index=False):
            sid = str(row.stock_id).strip()
            if not FOUR_DIGIT_RE.match(sid):
                continue
            if str(row.industry_category).strip() in EXCLUDED_INDUSTRY_CATEGORIES.union(
                config.get("excluded_industry_categories", [])
            ):
                continue
            entry = stocks_map.get(sid)
            last_seen = str((entry or {}).get("last_seen", "")).strip()
            if last_seen:
                try:
                    if (today - date.fromisoformat(last_seen)).days <= skip_recent_days:
                        skipped += 1
                        continue
                except ValueError:
                    pass
            try:
                prices = _fetch_stock_prices(loader, sid, start_date, today.isoformat())
            except Exception:
                logger.warning("init-universe 抓取 %s 失敗", sid, exc_info=True)
                continue
            entry = stocks_map.setdefault(
                sid,
                {
                    "stock_name": str(row.stock_name).strip(),
                    "industry_category": str(row.industry_category).strip(),
                    "status": "ACTIVE",
                    "missing_count": 0,
                    "vol_history_20d": [],
                    "vol_ma20_lots": 0.0,
                    "latest_vol_lots": 0.0,
                    "latest_turnover_twd": 0.0,
                    "last_seen": "",
                },
            )
            entry["stock_name"] = str(row.stock_name).strip()
            entry["industry_category"] = str(row.industry_category).strip()
            last_trade: date | None = None
            if prices is not None and not prices.empty:
                frame = prices.sort_values("date")
                last_trade = pd.Timestamp(frame["date"].iloc[-1]).date()
            if last_trade is None or (today - last_trade).days > stale_days:
                entry["status"] = "DELISTED"
                entry["reason"] = "init-universe：無近期日線（判定下市/終止買賣）"
                delisted += 1
            else:
                volumes = (
                    pd.to_numeric(frame["Trading_Volume"], errors="coerce")
                    .fillna(0.0)
                    .div(1000.0)
                    .round(2)
                    .tolist()
                )[-20:]
                entry["status"] = "ACTIVE"
                entry["missing_count"] = 0
                entry["vol_history_20d"] = volumes
                entry["vol_ma20_lots"] = round(sum(volumes) / len(volumes), 1)
                entry["latest_vol_lots"] = volumes[-1]
                if "Trading_money" in frame.columns:
                    entry["latest_turnover_twd"] = float(
                        pd.to_numeric(frame["Trading_money"], errors="coerce")
                        .fillna(0.0)
                        .iloc[-1]
                    )
                entry["last_seen"] = last_trade.isoformat()
            processed += 1
            if processed % 25 == 0:
                registry["updated_at"] = datetime.now(TAIPEI_TIMEZONE).isoformat()
                _atomic_write_json(registry_path, registry)  # 斷點續傳

    registry["updated_at"] = datetime.now(TAIPEI_TIMEZONE).isoformat()
    _atomic_write_json(registry_path, registry)
    return {
        "path": registry_path,
        "processed": processed,
        "skipped": skipped,
        "delisted": delisted,
    }


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
        default=(
            os.getenv("ALIGNMENT_SIGNALS_CHANNEL_ID", "").strip()
            or os.getenv("STOCK_CHANNEL_ID", "").strip()
        ),
        help="Discord 頻道 ID（預設讀取 ALIGNMENT_SIGNALS_CHANNEL_ID 或 STOCK_CHANNEL_ID 環境變數）",
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
        help="AI 嚴選流程：對當日 alignment_signals 做財報/法說分析並由 AI 嚴選進場標的",
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
    analyze_parser.add_argument(
        "--dump-prompt-only",
        action="store_true",
        help="0-Token 全真預覽：完整執行所有免費/快取資料流並匯出 AI #3 Prompt 至 daily/{date}_preview_trend_prompt.md，不呼叫任何 AI",
    )
    analyze_parser.add_argument("--stocks", help="搭配 --dump-prompt-only 指定預覽股號（逗號分隔，如 3293,2207）")
    analyze_parser.add_argument(
        "--batch",
        dest="batch",
        action="store_true",
        default=None,
        help="強制啟用 Vertex AI Batch（未指定時讀 ai_picker_use_vertex_batch 與 VERTEX_BATCH_ENABLED）",
    )
    analyze_parser.add_argument("--no-batch", dest="batch", action="store_false", help="強制改用即時 API，不使用 Vertex AI Batch")

    init_parser = subparsers.add_parser(
        "init-universe",
        help="初始化全市場 stock_universe_registry.json（近 30 日日線、20 日均量、下市標記；支援斷點續傳）",
    )
    init_parser.add_argument(
        "--config",
        type=Path,
        default=Path("finmind_scanner.json"),
        help="JSON 設定檔路徑（預設：finmind_scanner.json）",
    )
    init_parser.add_argument("--output-dir", type=Path, help="覆寫設定檔的 output_directory")

    rebound_parser = subparsers.add_parser(
        "analyze-rebound",
        help="型態 A 反彈流程：執行型態 A（高點回跌回踩長均線反彈）掃描、寫入 Rebound_Watchlist 並彈性決定是否啟動 AI 嚴選",
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
    rebound_parser.add_argument(
        "--dump-prompt-only",
        action="store_true",
        help="0-Token 全真預覽：完整執行所有免費/快取資料流並匯出反彈 AI #3 Prompt 至 daily/{date}_preview_rebound_prompt.md，不呼叫任何 AI",
    )
    rebound_parser.add_argument("--stocks", help="搭配 --dump-prompt-only 指定預覽股號（逗號分隔，如 3293,2207）")
    rebound_parser.add_argument("--no-gsheet", action="store_true", help="不更新 Google Sheets Rebound_Watchlist")
    rebound_parser.add_argument("--no-discord", action="store_true", help="不推播 Discord 訊息")
    rebound_parser.add_argument(
        "--batch",
        dest="batch",
        action="store_true",
        default=None,
        help="強制啟用 Vertex AI Batch（未指定時讀 ai_picker_use_vertex_batch 與 VERTEX_BATCH_ENABLED）",
    )
    rebound_parser.add_argument("--no-batch", dest="batch", action="store_false", help="強制改用即時 API，不使用 Vertex AI Batch")

    audit_parser = subparsers.add_parser(
        "audit-signals",
        help="SASD v5.4.0 Phase 3：盤後回填 signal_audit.db 事後績效、同步 Signal_Audit_Log 並產出審計週報",
    )
    audit_parser.add_argument(
        "--config",
        type=Path,
        default=Path("finmind_scanner.json"),
        help="JSON 設定檔路徑（預設：finmind_scanner.json）",
    )
    audit_parser.add_argument("--date", help="指定審計基準日期 YYYY-MM-DD（預設今日）")
    audit_parser.add_argument("--db-path", type=Path, help="覆寫 signal_audit.db 路徑")
    audit_parser.add_argument("--weekly-summary", action="store_true", help="輸出 Weekly Quantitative Audit Summary")
    audit_parser.add_argument("--post-to-discord", action="store_true", help="將審計週報推播至 Discord")
    audit_parser.add_argument("--no-gsheet", action="store_true", help="不同步 Google Sheets Signal_Audit_Log")
    audit_parser.add_argument("--dry-run", action="store_true", help="僅顯示審計摘要，不同步 Sheets / Discord")

    portal_parser = subparsers.add_parser(
        "publish-portal",
        help="SASD v6.2.0 Step 3：打包近 30 個交易日戰情總部 JSON 並透過 GitHub Git Trees API 單次發布與清理舊檔",
    )
    portal_parser.add_argument(
        "--config",
        type=Path,
        default=Path("finmind_scanner.json"),
        help="JSON 設定檔路徑（預設：finmind_scanner.json）",
    )
    portal_parser.add_argument("--date", help="指定發布基準日期 YYYY-MM-DD（預設今日）")
    portal_parser.add_argument("--output-dir", type=Path, help="覆寫 finmind_reports 根目錄")
    portal_parser.add_argument("--dry-run", action="store_true", help="僅於本地 finmind_reports/portal 產出預覽 JSON，不上傳 GitHub")
    return parser


def run_signal_audit_backfill(
    config: Mapping[str, object],
    *,
    as_of: date | None = None,
    daily_directory: Path | None = None,
    extra_prices_df: pd.DataFrame | None = None,
    loader: DataLoader | None = None,
    limiter: FinMindRequestLimiter | None = None,
    db_path: Path | None = None,
    sync_gsheet: bool = True,
    emit_weekly_summary: bool = False,
    post_discord: bool = False,
) -> dict[str, Any]:
    """執行 `SignalAuditStore` 盤後 T+1~T+10 路徑回填、Google Sheets 同步與週報輸出。"""
    from signal_audit_service import (
        DEFAULT_AUDIT_DB_PATH,
        SIGNAL_AUDIT_GSHEET_TAB,
        SignalAuditStore,
        build_local_and_finmind_bars_fetcher,
        format_weekly_summary_markdown,
    )

    today = as_of or _today_in_taipei()
    output_root = Path(str(config.get("output_directory") or "finmind_reports"))
    daily_dir = daily_directory or (output_root / "daily")
    resolved_db = db_path or Path(str(config.get("signal_audit_db_path") or DEFAULT_AUDIT_DB_PATH))
    tab_name = str(config.get("signal_audit_gsheet_tab_name") or SIGNAL_AUDIT_GSHEET_TAB)

    store = SignalAuditStore(resolved_db)
    fetcher = build_local_and_finmind_bars_fetcher(
        daily_dir, loader=loader, extra_prices_df=extra_prices_df
    )
    if loader is not None:
        resolved_limiter = limiter or FinMindRequestLimiter(
            output_root / ".state" / "finmind_request_history.sqlite3",
            int(config.get("max_requests_per_hour") or DEFAULT_MAX_REQUESTS_PER_HOUR),
            float(config.get("min_request_interval_seconds") or DEFAULT_MIN_REQUEST_INTERVAL_SECONDS),
        )
        with _rate_limit_finmind_http_requests(resolved_limiter, only_hosts=("finmindtrade.com",)):
            updated_count = store.backfill_pending(fetcher)
    else:
        updated_count = store.backfill_pending(fetcher)
    synced_rows = store.sync_to_gsheet(tab_name=tab_name) if sync_gsheet else 0

    is_friday = today.weekday() == 4
    summary = store.build_weekly_summary()
    summary_md = ""
    if emit_weekly_summary or is_friday:
        summary_md = format_weekly_summary_markdown(summary, as_of_str=today.isoformat())
        if post_discord and summary_md:
            bot_token = (os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
            channel_id = str(config.get("stock_channel_id") or os.getenv("STOCK_CHANNEL_ID") or "").strip()
            if bot_token and channel_id.isdigit():
                req = Request(
                    f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages",
                    data=json.dumps({"content": summary_md, "flags": 4}, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Authorization": f"Bot {bot_token}",
                        "Content-Type": "application/json",
                        "User-Agent": "DiscordService (signal audit)",
                    },
                    method="POST",
                )
                with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS):  # noqa: S310
                    pass

    return {
        "updated_count": updated_count,
        "synced_rows": synced_rows,
        "summary": summary,
        "summary_markdown": summary_md,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
    load_dotenv()
    args = _make_parser().parse_args(argv)
    api_token = os.getenv("FINMIND_API_TOKEN", "").strip()

    try:
        if args.command == "analyze-alignment":
            if getattr(args, "dump_prompt_only", False):
                from alignment_ai_picker import dump_strategic_layout_prompt_only

                sids = [s.strip() for s in str(args.stocks or "").split(",") if s.strip()] or None
                dump_strategic_layout_prompt_only(
                    config_path=args.config,
                    target_date=args.date,
                    selection_mode="trend",
                    stock_ids=sids,
                )
                return 0
            from alignment_ai_picker import run_from_cli

            return run_from_cli(
                config_path=args.config,
                target_date=args.date,
                force_ai=args.force_ai,
                dry_run=args.dry_run,
                use_batch=args.batch,
            )

        if args.command == "analyze-rebound":
            if getattr(args, "dump_prompt_only", False):
                from alignment_ai_picker import dump_strategic_layout_prompt_only

                sids = [s.strip() for s in str(args.stocks or "").split(",") if s.strip()] or None
                dump_strategic_layout_prompt_only(
                    config_path=args.config,
                    target_date=args.date,
                    selection_mode="rebound",
                    stock_ids=sids,
                    output_dir=args.output_dir,
                )
                return 0
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
                use_batch=args.batch,
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

        if args.command == "audit-signals":
            config = load_scan_config(args.config)
            as_of_dt = date.fromisoformat(args.date) if args.date else _today_in_taipei()
            loader_opt = DataLoader(token=api_token) if (api_token and not args.dry_run) else None
            audit_res = run_signal_audit_backfill(
                config,
                as_of=as_of_dt,
                loader=loader_opt,
                db_path=args.db_path,
                sync_gsheet=(not args.no_gsheet and not args.dry_run),
                emit_weekly_summary=True,
                post_discord=(args.post_to_discord and not args.dry_run),
            )
            print(
                f"[audit-signals] 已更新回填 {audit_res['updated_count']} 筆訊號"
                f"｜同步 Google Sheets {audit_res['synced_rows']} 筆。"
            )
            if audit_res["summary_markdown"]:
                print("\n" + audit_res["summary_markdown"])
            return 0

        if args.command == "publish-portal":
            from portal_publisher import publish_portal_data

            config = load_scan_config(args.config)
            as_of_dt = date.fromisoformat(args.date) if args.date else _today_in_taipei()
            pub_cfg = dict(config)
            pub_cfg["enable_portal_publish"] = True
            res = publish_portal_data(
                pub_cfg,
                as_of=as_of_dt,
                output_root=args.output_dir,
                dry_run=bool(args.dry_run),
                raise_on_error=True,
            )
            if args.dry_run:
                print(
                    f"[publish-portal] --dry-run 完成：共打包 {res.get('dates_count', 0)} 個交易日"
                    f"（{res.get('files_count', 0)} 個檔案）至 {res.get('mirror_dir')}。"
                )
            else:
                print(
                    f"[publish-portal] 發布完成：上傳 {len(res.get('uploaded', []))} 檔、"
                    f"略過未變更 {len(res.get('skipped_unchanged', []))} 檔、"
                    f"清理過期 {len(res.get('deleted', []))} 檔｜Commit: {res.get('commit_sha', '')[:8]}"
                    + (f"｜網址：{res.get('portal_url')}" if res.get("portal_url") else "")
                )
            return 0

        if args.command == "init-universe":
            config = load_scan_config(args.config)
            output_dir = args.output_dir or Path(config["output_directory"])
            request_limiter = FinMindRequestLimiter(
                output_dir / ".state" / "finmind_request_history.sqlite3",
                int(config["max_requests_per_hour"]),
                float(config["min_request_interval_seconds"]),
            )
            with _rate_limit_finmind_http_requests(
                request_limiter, only_hosts=("finmindtrade.com",)
            ):
                loader = DataLoader(token=api_token)
                stock_info = loader.taiwan_stock_info(timeout=REQUEST_TIMEOUT_SECONDS)
            if stock_info.empty:
                raise RuntimeError("FinMind 股票總覽沒有回傳任何資料。")
            result = init_universe_registry(
                loader,
                stock_info,
                config,
                output_dir=output_dir,
                request_limiter=request_limiter,
            )
            print(
                f"init-universe 完成：處理 {result['processed']} 檔、"
                f"跳過 {result['skipped']} 檔、標記下市 {result['delisted']} 檔 → {result['path']}"
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
            with _rate_limit_finmind_http_requests(request_limiter, only_hosts=("finmindtrade.com",)):
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
