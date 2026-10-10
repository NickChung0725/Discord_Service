"""Fetch Taiwan stock prices and render interactive Plotly charts."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
from FinMind.data import DataLoader
from plotly.subplots import make_subplots

_STOCK_ID_PATTERN = re.compile(r"^\d{4,6}$")
_REQUIRED_COLUMNS = ("date", "open", "max", "min", "close", "Trading_Volume")
_OUTPUT_DIR = Path(__file__).resolve().parent / "generated_charts"
_CHART_HISTORY_MONTHS = 6
_INDICATOR_HISTORY_MONTHS = 12
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_REQUEST_TIMEOUT_SECONDS = 30
_RANGE_SUMMARY_SCRIPT = r"""
(() => {
    const chart = document.getElementById('{plot_id}');
    if (!chart) return;

    const panel = document.createElement('div');
    panel.id = 'selected-price-range-summary';
    panel.innerHTML = [
        '<strong>選取區間價格</strong>',
        '<div>最高：<span data-value="high">--</span></div>',
        '<div>最低：<span data-value="low">--</span></div>',
        '<div>高低均價：<span data-value="average">--</span></div>',
        '<small data-value="dates">拖曳 K 線圖選取區間</small>'
    ].join('');

    const style = document.createElement('style');
    style.textContent = `
        #selected-price-range-summary {
            position: fixed;
            z-index: 1000;
            top: 78px;
            right: 20px;
            min-width: 190px;
            padding: 10px 14px;
            border: 1px solid #48515a;
            border-radius: 6px;
            background: rgba(20, 24, 28, 0.96);
            color: #f5f7fa;
            font: 14px/1.55 Arial, sans-serif;
            pointer-events: none;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
        }
        #selected-price-range-summary strong {
            display: block;
            margin-bottom: 3px;
            color: #c9d1d9;
        }
        #selected-price-range-summary [data-value="average"] {
            color: #ffca28;
            font-weight: 700;
        }
        #selected-price-range-summary small {
            display: block;
            margin-top: 3px;
            color: #9aa4ad;
        }
        @media (max-width: 600px) {
            #selected-price-range-summary {
                top: 86px;
                right: 8px;
                min-width: 160px;
                padding: 7px 10px;
                font-size: 12px;
            }
        }
    `;
    document.head.appendChild(style);
    document.body.appendChild(panel);

    const asDay = value => {
        const dateValue = typeof value === 'string'
            ? value.match(/^(\d{4}-\d{2}-\d{2})/)?.[1]
            : value;
        if (dateValue === undefined) return Number.NaN;
        const parsed = new Date(
            typeof dateValue === 'string' ? `${dateValue}T00:00:00Z` : dateValue
        );
        if (Number.isNaN(parsed.getTime())) return Number.NaN;
        return Date.UTC(parsed.getUTCFullYear(), parsed.getUTCMonth(), parsed.getUTCDate());
    };
    const values = __PRICE_RANGE_DATA__.map(item => ({
        day: asDay(item.date),
        high: Number(item.high),
        low: Number(item.low)
    })).filter(item =>
        Number.isFinite(item.day) &&
        Number.isFinite(item.high) &&
        Number.isFinite(item.low)
    );
    if (!values.length) return;

    const fullStart = Math.min(...values.map(item => item.day));
    const fullEnd = Math.max(...values.map(item => item.day));
    let selectedStart = fullStart;
    let selectedEnd = fullEnd;

    const formatPrice = value => new Intl.NumberFormat('zh-TW', {
        maximumFractionDigits: 2
    }).format(value);
    const formatDate = value => new Date(value).toLocaleDateString('zh-TW', {
        timeZone: 'UTC',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
    });
    const updateSummary = () => {
        const start = Math.min(selectedStart, selectedEnd);
        const end = Math.max(selectedStart, selectedEnd);
        const selected = values.filter(item => item.day >= start && item.day <= end);
        const highElement = panel.querySelector('[data-value="high"]');
        const lowElement = panel.querySelector('[data-value="low"]');
        const averageElement = panel.querySelector('[data-value="average"]');
        const datesElement = panel.querySelector('[data-value="dates"]');
        if (!selected.length) {
            highElement.textContent = '--';
            lowElement.textContent = '--';
            averageElement.textContent = '--';
            datesElement.textContent = '選取範圍內沒有交易日';
            return;
        }

        const high = Math.max(...selected.map(item => item.high));
        const low = Math.min(...selected.map(item => item.low));
        highElement.textContent = formatPrice(high);
        lowElement.textContent = formatPrice(low);
        averageElement.textContent = formatPrice((high + low) / 2);
        datesElement.textContent = `${formatDate(start)} - ${formatDate(end)}`;
    };

    const handleRelayout = event => {
        if (event['xaxis.autorange'] || event['xaxis2.autorange'] ||
            event['xaxis3.autorange'] || event['xaxis4.autorange']) {
            selectedStart = fullStart;
            selectedEnd = fullEnd;
            updateSummary();
            return;
        }

        for (const [key, value] of Object.entries(event)) {
            const rangeMatch = key.match(/^xaxis\d*\.range(?:\[(0|1)\])?$/);
            if (!rangeMatch) continue;

            if (rangeMatch[1] === undefined && Array.isArray(value)) {
                const start = asDay(value[0]);
                const end = asDay(value[1]);
                if (Number.isFinite(start) && Number.isFinite(end)) {
                    selectedStart = start;
                    selectedEnd = end;
                }
            } else if (rangeMatch[1] === '0') {
                const start = asDay(value);
                if (Number.isFinite(start)) selectedStart = start;
            } else if (rangeMatch[1] === '1') {
                const end = asDay(value);
                if (Number.isFinite(end)) selectedEnd = end;
            }
        }
        updateSummary();
    };

    chart.on('plotly_relayout', handleRelayout);
    chart.on('plotly_relayouting', handleRelayout);
    updateSummary();
})();
"""


class InvalidStockIDError(ValueError):
    """Raised when the stock identifier is not a Taiwan stock code."""


class StockNotFoundError(ValueError):
    """Raised when FinMind returns no usable daily prices for a stock."""


def _validate_stock_id(stock_id: str) -> str:
    normalized_id = stock_id.strip()
    if not _STOCK_ID_PATTERN.fullmatch(normalized_id):
        raise InvalidStockIDError("股票代號必須是 4 至 6 位數字。")
    return normalized_id


def _fetch_yahoo_volume(stock_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    start_timestamp = int(pd.Timestamp(start_date, tz="UTC").timestamp())
    end_timestamp = int(
        (pd.Timestamp(end_date) + pd.Timedelta(1, unit="D"))
        .tz_localize("UTC")
        .timestamp()
    )
    response = requests.get(
        _YAHOO_CHART_URL.format(symbol=f"{stock_id}.TW"),
        params={
            "period1": start_timestamp,
            "period2": end_timestamp,
            "interval": "1d",
            "events": "history",
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    try:
        chart_result = response.json()["chart"]["result"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("Yahoo Finance 回傳了無法辨識的成交量資料。") from error
    if not chart_result:
        raise StockNotFoundError(f"Yahoo Finance 查無股票 {stock_id} 的日線資料。")

    quote = chart_result[0]
    timestamps = quote.get("timestamp")
    quote_data = quote.get("indicators", {}).get("quote")
    if not timestamps or not quote_data or "volume" not in quote_data[0]:
        raise RuntimeError(f"Yahoo Finance 未回傳股票 {stock_id} 的成交量欄位。")

    dates = (
        pd.to_datetime(timestamps, unit="s", utc=True)
        .tz_convert("Asia/Taipei")
        .tz_localize(None)
        .normalize()
    )
    return pd.DataFrame(
        {
            "date": dates,
            "Regular_Trading_Volume": quote_data[0]["volume"],
        }
    )


def _fetch_stock_data(stock_id: str) -> pd.DataFrame:
    today = date.today()
    start_date = (
        pd.Timestamp(today) - pd.DateOffset(months=_INDICATOR_HISTORY_MONTHS)
    ).strftime("%Y-%m-%d")
    loader = DataLoader()
    api_token = os.getenv("FINMIND_API_TOKEN", "").strip()
    if api_token:
        loader.login_by_token(api_token=api_token)

    frame = loader.taiwan_stock_daily(
        stock_id=stock_id,
        start_date=start_date,
        end_date=today.isoformat(),
        timeout=30,
    )
    if frame.empty:
        raise StockNotFoundError(f"查無股票 {stock_id} 的近半年日線資料。")

    missing_columns = [column for column in _REQUIRED_COLUMNS if column not in frame]
    if missing_columns:
        raise RuntimeError(
            "FinMind 回傳的日線資料缺少必要欄位：" + ", ".join(missing_columns)
        )

    frame = frame.loc[:, _REQUIRED_COLUMNS].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    numeric_columns = ("open", "max", "min", "close", "Trading_Volume")
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=_REQUIRED_COLUMNS).sort_values("date").reset_index(drop=True)
    if frame.empty:
        raise StockNotFoundError(f"股票 {stock_id} 沒有可用的日線資料。")

    yahoo_volumes = _fetch_yahoo_volume(stock_id, start_date, today.isoformat())
    frame = frame.merge(yahoo_volumes, on="date", how="left", validate="one_to_one")
    missing_volume_dates = frame.loc[
        frame["Regular_Trading_Volume"].isna(), "date"
    ]
    if not missing_volume_dates.empty:
        missing_dates = ", ".join(
            missing_volume_dates.dt.strftime("%Y-%m-%d").head(5).tolist()
        )
        raise RuntimeError(
            "Yahoo Finance 缺少以下交易日的成交量資料："
            f"{missing_dates}"
        )
    frame["Regular_Trading_Volume"] = pd.to_numeric(
        frame["Regular_Trading_Volume"], errors="coerce"
    )
    return frame


def _add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for period in (17, 45, 117, 189):
        frame[f"MA{period}"] = frame["close"].rolling(
            window=period, min_periods=period
        ).mean()
    frame["VolumeLots"] = frame["Regular_Trading_Volume"] / 1_000

    lowest_low = frame["min"].rolling(window=9, min_periods=9).min()
    highest_high = frame["max"].rolling(window=9, min_periods=9).max()
    price_range = highest_high - lowest_low
    rsv = ((frame["close"] - lowest_low) / price_range * 100).where(price_range != 0)
    frame["K"] = rsv.ewm(alpha=1 / 3, adjust=False, min_periods=1).mean()
    frame["D"] = frame["K"].ewm(alpha=1 / 3, adjust=False, min_periods=1).mean()

    ema12 = frame["close"].ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = frame["close"].ewm(span=26, adjust=False, min_periods=26).mean()
    frame["MACD"] = ema12 - ema26
    frame["MACDSignal"] = frame["MACD"].ewm(
        span=9, adjust=False, min_periods=9
    ).mean()
    frame["MACDHistogram"] = frame["MACD"] - frame["MACDSignal"]
    return frame


def _generate_stock_chart(stock_id: str) -> tuple[str, float, str]:
    normalized_id = _validate_stock_id(stock_id)
    frame = _add_indicators(_fetch_stock_data(normalized_id))
    chart_start_date = pd.Timestamp(date.today()) - pd.DateOffset(
        months=_CHART_HISTORY_MONTHS
    )
    frame = frame.loc[frame["date"] >= chart_start_date].reset_index(drop=True)
    if frame.empty:
        raise StockNotFoundError(
            f"股票 {normalized_id} 近半年沒有可顯示的日線資料。"
        )
    latest = frame.iloc[-1]
    generated_at = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")

    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.52, 0.16, 0.16, 0.16],
        subplot_titles=(
            "股價與均線",
            "Yahoo Finance 成交量（張）",
            "KD 指標（9, 3, 3）",
            "MACD（12, 26, 9）",
        ),
    )
    figure.add_trace(
        go.Candlestick(
            x=frame["date"],
            open=frame["open"],
            high=frame["max"],
            low=frame["min"],
            close=frame["close"],
            name="日 K",
            increasing_line_color="#ef5350",
            decreasing_line_color="#26a69a",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["MA17"],
            name="17 日均線",
            line={"color": "#ffca28"},
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["MA45"],
            name="45 日均線",
            line={"color": "#0abab5"},
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["MA117"],
            name="117 日均線",
            line={"color": "#ab47bc"},
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["MA189"],
            name="189 日均線",
            line={"color": "#ffffff"},
        ),
        row=1,
        col=1,
    )

    volume_colors = [
        "#ef5350" if close >= opening else "#26a69a"
        for close, opening in zip(frame["close"], frame["open"])
    ]
    figure.add_trace(
        go.Bar(
            x=frame["date"],
            y=frame["VolumeLots"],
            name="成交量（張）",
            marker_color=volume_colors,
        ),
        row=2,
        col=1,
    )

    figure.add_trace(
        go.Scatter(x=frame["date"], y=frame["K"], name="K", line={"color": "#ffca28"}),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=frame["date"], y=frame["D"], name="D", line={"color": "#42a5f5"}),
        row=3,
        col=1,
    )
    figure.add_hline(y=80, line_dash="dot", line_color="#777", row=3, col=1)
    figure.add_hline(y=20, line_dash="dot", line_color="#777", row=3, col=1)

    histogram_colors = [
        "#ef5350" if value >= 0 else "#26a69a"
        for value in frame["MACDHistogram"].fillna(0)
    ]
    figure.add_trace(
        go.Bar(
            x=frame["date"],
            y=frame["MACDHistogram"],
            name="柱狀圖",
            marker_color=histogram_colors,
        ),
        row=4,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=frame["date"], y=frame["MACD"], name="DIF", line={"color": "#ffca28"}),
        row=4,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"], y=frame["MACDSignal"], name="Signal", line={"color": "#42a5f5"}
        ),
        row=4,
        col=1,
    )

    figure.update_layout(
        title={
            "text": (
                f"{normalized_id} 台股近半年技術分析"
                f"<br><sup>HTML 產出時間（台灣）：{generated_at}</sup>"
            ),
            "x": 0.01,
            "xanchor": "left",
            "y": 0.99,
            "yanchor": "top",
        },
        template="plotly_dark",
        height=1_000,
        autosize=True,
        dragmode="zoom",
        hovermode="x unified",
        margin={"l": 48, "r": 24, "t": 190, "b": 40},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        xaxis_rangeslider_visible=False,
    )
    figure.update_yaxes(title_text="價格（元）", row=1, col=1)
    figure.update_yaxes(title_text="張", row=2, col=1)
    figure.update_yaxes(title_text="數值", range=[0, 100], row=3, col=1)
    figure.update_yaxes(title_text="數值", row=4, col=1)
    trading_dates = pd.DatetimeIndex(frame["date"].dt.normalize().unique())
    calendar_dates = pd.date_range(
        start=trading_dates.min(),
        end=trading_dates.max(),
        freq="D",
    )
    non_trading_dates = calendar_dates.difference(trading_dates).strftime("%Y-%m-%d")
    figure.update_xaxes(
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikecolor="#ffca28",
        spikethickness=1,
        rangebreaks=[{"values": non_trading_dates.tolist()}],
    )
    figure.update_yaxes(
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikecolor="#ffca28",
        spikethickness=1,
    )

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = _OUTPUT_DIR / f"{normalized_id}.html"
    range_data = [
        {
            "date": trading_date.strftime("%Y-%m-%d"),
            "high": float(high),
            "low": float(low),
        }
        for trading_date, high, low in zip(frame["date"], frame["max"], frame["min"])
    ]
    figure.write_html(
        str(output_path),
        include_plotlyjs="cdn",
        full_html=True,
        config={"responsive": True, "displaylogo": False},
        post_script=_RANGE_SUMMARY_SCRIPT.replace(
            "__PRICE_RANGE_DATA__", json.dumps(range_data)
        ),
    )
    return str(output_path), float(latest["close"]), latest["date"].strftime("%Y-%m-%d")


def generate_stock_html(stock_id: str) -> str:
    """Generate the stock's HTML chart and return its local file path."""
    file_path, _, _ = _generate_stock_chart(stock_id)
    return file_path


def generate_stock_html_with_summary(stock_id: str) -> tuple[str, float, str]:
    """Generate the chart once and return its path, latest close, and trading date."""
    return _generate_stock_chart(stock_id)
