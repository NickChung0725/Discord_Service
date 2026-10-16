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

import pandas as pd
import requests
from dotenv import load_dotenv
from FinMind.data import DataLoader

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
    usable = analyzed_prices.dropna(subset=["MA17", "MA45"])
    if usable.empty:
        return None

    latest = usable.iloc[-1]
    ma17 = float(latest["MA17"])
    ma45 = float(latest["MA45"])
    close = float(latest["close"])
    if ma17 <= 0:
        return None

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
        "MA117": None if pd.isna(latest["MA117"]) else float(latest["MA117"]),
        "MA189": None if pd.isna(latest["MA189"]) else float(latest["MA189"]),
        "gap_percent": gap_percent,
        "signal": signal,
    }


def classify_latest_alignment_signal(
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
    if min(ma17, ma45, ma117, ma189) <= 0:
        return None

    gap_percent = abs(ma17 - ma45) / ma45 * 100
    if not (
        ma17 > ma117
        and ma17 > ma189
        and ma45 > ma117
        and ma45 > ma189
        and gap_percent <= ALIGNMENT_MAX_GAP_PERCENT
    ):
        return None

    return {
        "date": latest["date"].strftime("%Y-%m-%d"),
        "close": float(latest["close"]),
        "MA17": ma17,
        "MA45": ma45,
        "MA117": ma117,
        "MA189": ma189,
        "MA17_MA45_gap_percent": gap_percent,
    }


def format_alignment_report(
    alignment_signals: pd.DataFrame,
    as_of: date | None = None,
    failed_count: int = 0,
) -> list[str]:
    report_date = (as_of or _today_in_taipei()).isoformat()
    heading = (
        f"📊 {report_date} MA 均線排列訊號"
        "\n條件：MA17、MA45 均高於 MA117、MA189；"
        "MA17 與 MA45 差距 <= 1%"
    )
    if failed_count:
        heading += f"\n⚠️ 有 {failed_count} 檔股票資料抓取失敗，以下僅列成功分析的股票。"
    if alignment_signals.empty:
        return [heading + "\n今日沒有符合條件的股票。"]

    chunks: list[str] = []
    current = heading
    for row in alignment_signals.itertuples(index=False):
        line = (
            f"\n{row.stock_id} {row.stock_name} | 收盤 {float(row.close):,.2f}"
            f" | MA17 {float(row.MA17):,.2f}"
            f" | MA45 {float(row.MA45):,.2f}"
            f" | MA117 {float(row.MA117):,.2f}"
            f" | MA189 {float(row.MA189):,.2f}"
            f" | 17/45 差 {float(row.MA17_MA45_gap_percent):.2f}%"
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
    alignment_signals = report_files["alignment_signals"]
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
        print(f"\n符合 MA45 已超過或距 MA17 不超過 1%：{signal_count} 檔")
        if signal_count:
            signals = pd.read_csv(signal_path)
            print(signals.to_string(index=False))
        print(
            f"\n符合 MA17、MA45 高於 MA117/MA189 且 MA17/MA45 差距 <= 1%："
            f"{alignment_count} 檔"
        )
        if alignment_count:
            print(pd.read_csv(alignment_path).to_string(index=False))
        print(f"\n近六個月交易資料（均線使用近一年計算）：{price_path}")
        print(f"訊號清單：{signal_path}")
        print(f"均線排列特別清單：{alignment_path}")
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
