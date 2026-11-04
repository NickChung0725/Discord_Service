"""Global Macro & Overnight US Tech / FX Sensor (`taishin/macro_context.py`).

SASD v5.2.0:
- Uses zero extra third-party packages (`requests` + `xml.etree.ElementTree`).
- Fetches overnight US indices (`^SOX`, `^IXIC`, `^GSPC`), key AI/TW bellwethers (`TSM`, `NVDA`),
  risk gauge (`^VIX`), and FX (`TWD=X`) from Yahoo Finance v8 Chart API.
- Fetches and cleans 48h international macro / semiconductor headlines via Google News RSS.
- Degrades gracefully on network errors so scheduled morning-brief runs never crash.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import logging
import math
import re
from typing import Any
from urllib.parse import quote
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger(__name__)

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
GOOGLE_NEWS_MACRO_RSS_URL = (
    "https://news.google.com/rss/search?"
    "q=%28%E8%B2%BB%E5%8D%8A+OR+%E5%8F%B0%E7%A9%8D%E9%9B%BBADR+OR+%E8%BC%9D%E9%81%94+"
    "OR+%E8%81%AF%E6%BA%96%E6%9C%83+OR+%E7%BE%8E%E8%82%A1+OR+%E9%97%9C%E7%A8%85%29+when%3A2d"
    "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
)
DEFAULT_TIMEOUT_SECONDS = 12
USER_AGENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

MACRO_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    ("^SOX", "SOX", "費半指數"),
    ("^IXIC", "IXIC", "那斯達克"),
    ("^GSPC", "SP500", "標普500"),
    ("TSM", "TSM", "台積電ADR"),
    ("NVDA", "NVDA", "輝達NVDA"),
    ("^VIX", "VIX", "VIX恐慌"),
    ("TWD=X", "USDTWD", "美元/台幣"),
)

_MACRO_NOISE_PATTERNS = (
    "股市爆料同學會",
    "盤中焦點",
    "即時新聞】",
    "早盤",
    "今日股價與討論",
)


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def fetch_yahoo_quote_snapshot(
    yahoo_symbol: str,
    *,
    session: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Fetch latest daily close and 1-day change % for `yahoo_symbol` from Yahoo v8 Chart API."""
    encoded = quote(yahoo_symbol, safe="")
    url = YAHOO_CHART_URL.format(symbol=encoded)
    http = session or requests
    try:
        resp = http.get(
            url,
            params={"range": "10d", "interval": "1d"},
            headers=USER_AGENT_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as err:  # noqa: BLE001
        logger.warning("Macro Yahoo quote fetch failed for %s: %s", yahoo_symbol, err)
        return None

    try:
        result_list = (payload or {}).get("chart", {}).get("result") or []
        if not result_list:
            return None
        res0 = result_list[0]
        meta = res0.get("meta") or {}
        timestamps = res0.get("timestamp") or []
        quote_list = ((res0.get("indicators") or {}).get("quote") or [{}])[0]
        raw_closes = quote_list.get("close") or []

        valid_points: list[tuple[int | None, float]] = []
        for idx, raw_c in enumerate(raw_closes):
            c_val = _safe_float(raw_c)
            if c_val is not None and c_val > 0:
                ts = int(timestamps[idx]) if idx < len(timestamps) and timestamps[idx] is not None else None
                valid_points.append((ts, c_val))

        latest_price = _safe_float(meta.get("regularMarketPrice"))
        latest_ts: int | None = None
        if valid_points:
            latest_ts, last_close = valid_points[-1]
            if latest_price is None or latest_price <= 0:
                latest_price = last_close

        if latest_price is None or latest_price <= 0:
            return None

        prev_close: float | None = None
        if len(valid_points) >= 2:
            # Prefer second-to-last daily bar when last bar matches regularMarketPrice closely
            if abs(valid_points[-1][1] - latest_price) / latest_price < 0.03:
                prev_close = valid_points[-2][1]
            else:
                prev_close = valid_points[-1][1]
        if prev_close is None or prev_close <= 0:
            prev_close = _safe_float(meta.get("previousClose")) or _safe_float(meta.get("chartPreviousClose"))

        change = (latest_price - prev_close) if (prev_close and prev_close > 0) else 0.0
        change_pct = ((change / prev_close) * 100.0) if (prev_close and prev_close > 0) else 0.0

        as_of_date = ""
        market_ts = meta.get("regularMarketTime") or latest_ts
        if market_ts:
            try:
                as_of_date = datetime.fromtimestamp(int(market_ts), tz=timezone.utc).strftime("%Y-%m-%d")
            except (OSError, TypeError, ValueError):
                as_of_date = ""

        return {
            "symbol": yahoo_symbol,
            "price": round(latest_price, 2),
            "prev_close": round(prev_close, 2) if prev_close else None,
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "as_of_date": as_of_date,
        }
    except Exception as err:  # noqa: BLE001
        logger.warning("Failed to parse Yahoo macro response for %s: %s", yahoo_symbol, err)
        return None


def fetch_macro_news_headlines(
    *,
    max_items: int = 5,
    max_age_hours: int = 72,
    session: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    now_utc: datetime | None = None,
) -> list[dict[str, str]]:
    """Fetch and clean recent international macro / US semiconductor news headlines."""
    http = session or requests
    try:
        resp = http.get(GOOGLE_NEWS_MACRO_RSS_URL, headers=USER_AGENT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        xml_text = resp.text
    except Exception as err:  # noqa: BLE001
        logger.warning("Macro RSS fetch failed: %s", err)
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as err:
        logger.warning("Macro RSS XML parse error: %s", err)
        return []

    ref_now = now_utc or datetime.now(timezone.utc)
    cutoff = timedelta(hours=max_age_hours)
    seen_titles: set[str] = set()
    cleaned: list[dict[str, str]] = []

    for item in root.findall(".//item"):
        raw_title = html.unescape((item.findtext("title") or "").strip())
        if not raw_title:
            continue
        if any(pat in raw_title for pat in _MACRO_NOISE_PATTERNS):
            continue

        # Google News often appends " - 媒體名稱" at the end
        source = ""
        title_body = raw_title
        if " - " in raw_title:
            parts = raw_title.rsplit(" - ", 1)
            title_body = parts[0].strip()
            source = parts[1].strip()

        norm_key = re.sub(r"\s+", "", title_body.lower())[:36]
        if not norm_key or norm_key in seen_titles:
            continue

        pub_raw = (item.findtext("pubDate") or "").strip()
        pub_str = ""
        if pub_raw:
            try:
                pub_dt = parsedate_to_datetime(pub_raw)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if (ref_now - pub_dt) > cutoff:
                    continue
                pub_str = pub_dt.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
            except (TypeError, ValueError):
                pub_str = ""

        seen_titles.add(norm_key)
        cleaned.append(
            {
                "title": title_body,
                "source": source,
                "pub_time": pub_str,
            }
        )
        if len(cleaned) >= max_items:
            break

    return cleaned


def classify_macro_risk_tone(quotes: Mapping[str, Mapping[str, Any]]) -> tuple[str, str]:
    """Classify overnight international sentiment into 🔴 / 🟢 / 🟡 with tactical guidance."""
    if not quotes:
        return "🟡 國際資料待更新", "外部國際指數暫無即時報價，請以台股大盤紅綠燈與個股防守均線為主。"

    sox_chg = _safe_float((quotes.get("SOX") or {}).get("change_pct")) or 0.0
    tsm_chg = _safe_float((quotes.get("TSM") or {}).get("change_pct")) or 0.0
    ixic_chg = _safe_float((quotes.get("IXIC") or {}).get("change_pct")) or 0.0
    vix_price = _safe_float((quotes.get("VIX") or {}).get("price")) or 0.0
    vix_chg = _safe_float((quotes.get("VIX") or {}).get("change_pct")) or 0.0

    if sox_chg <= -2.5 or tsm_chg <= -3.0 or ixic_chg <= -2.2 or vix_price >= 26.0 or vix_chg >= 15.0:
        return (
            "🔴 國際逆風警戒",
            "隔夜費半／台積電ADR顯著回檔或VIX恐慌升溫，早盤嚴防權值與AI族群跳空賣壓，達標股優先鎖利、跌破防守線嚴禁凹單。",
        )

    if (sox_chg >= 1.2 or tsm_chg >= 1.5) and ixic_chg >= 0.5 and (vix_price == 0.0 or vix_price < 23.0):
        return (
            "🟢 國際順風偏多",
            "隔夜美股科技與半導體走強，有利台股多頭續航；惟早盤若大幅開高逼近目標價，切忌追高，可順勢執行達標分批鎖利。",
        )

    return (
        "🟡 國際中性震盪",
        "隔夜國際盤表現溫和或漲跌互見，早盤回歸個股基本面劇本與關鍵均線支撐紀律操作。",
    )


def fetch_global_macro_context(
    *,
    session: Any | None = None,
    include_news: bool = True,
) -> dict[str, Any]:
    """Assemble overnight US indices, TSM/NVDA, VIX, USD/TWD, and macro news into a unified context."""
    quotes: dict[str, dict[str, Any]] = {}
    for yahoo_sym, key, label in MACRO_SYMBOLS:
        snap = fetch_yahoo_quote_snapshot(yahoo_sym, session=session)
        if snap is not None:
            quotes[key] = {**snap, "key": key, "label": label}

    headlines = fetch_macro_news_headlines(session=session) if include_news else []
    risk_tone, risk_note = classify_macro_risk_tone(quotes)

    chip_parts: list[str] = []
    for _, key, label in MACRO_SYMBOLS:
        q = quotes.get(key)
        if not q:
            continue
        if key in ("VIX", "USDTWD"):
            chip_parts.append(f"{label} {q['price']} ({q['change_pct']:+.2f}%)")
        else:
            chip_parts.append(f"{label} {q['change_pct']:+.2f}%")

    compact_line = " ｜ ".join(chip_parts) if chip_parts else "國際指數暫無連線報價"

    prompt_lines: list[str] = [
        f"- 國際盤氣候：{risk_tone}（{risk_note}）",
        f"- 隔夜關鍵指標：{compact_line}",
    ]
    if headlines:
        prompt_lines.append("- 近 48 小時國際財經與產業要聞：")
        for idx, h in enumerate(headlines, start=1):
            src = f" [{h['source']}]" if h.get("source") else ""
            tm = f" ({h['pub_time']})" if h.get("pub_time") else ""
            prompt_lines.append(f"  {idx}. {h['title']}{src}{tm}")
    else:
        prompt_lines.append("- 近 48 小時國際財經與產業要聞：無重大異常標題")

    return {
        "risk_tone": risk_tone,
        "risk_note": risk_note,
        "quotes": quotes,
        "headlines": headlines,
        "compact_line": compact_line,
        "prompt_block": "\n".join(prompt_lines),
    }
