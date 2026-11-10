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


# --------------------------------------------------------------------------- #
# SASD v5.4.0-Phase3：加權 + 櫃買 (OTC) 雙核心紅綠燈策略路由
# --------------------------------------------------------------------------- #
from collections.abc import Sequence  # noqa: E402
from dataclasses import asdict, dataclass  # noqa: E402
import os  # noqa: E402
from typing import Literal  # noqa: E402

FUTURES_NET_SHORT_SURGE_LOTS = 3_500
FUTURES_TOTAL_NET_SHORT_LIMIT = 42_000


@dataclass(frozen=True)
class RegimeQuotaConfig:
    regime_state: Literal["GREEN_BULL", "YELLOW_ROTATION", "RED_DEFENSIVE"]
    max_right_breakout_picks: int      # 右側帶量突破單每日入選上限
    max_style_b_pullback_picks: int    # Style B 支撐回測低接單每日入選上限
    min_sector_breadth_pct: float      # 所屬細分產業站上 MA17 家數比最低門檻 (%)
    max_allowed_sector_pe_ratio: float # 允許之最高同產業相對本益比比值
    regime_summary_tag: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_market_regime_quota(
    *,
    taiex_close: float,
    taiex_ma20: float,
    taiex_ma20_slope: float,
    otc_close: float,
    otc_ma20: float,
    otc_ma20_slope: float,
    foreign_futures_net_short_change: float = 0.0,
    foreign_futures_total_net_short: float = 0.0,
    futures_net_short_surge_lots: float = FUTURES_NET_SHORT_SURGE_LOTS,
    futures_total_net_short_limit: float = FUTURES_TOTAL_NET_SHORT_LIMIT,
) -> RegimeQuotaConfig:
    """依加權、櫃買與外資台指期淨空單，輸出當日策略配額。

    - `*_slope`：MA20 今日減前一日（< 0 代表下彎）。
    - `foreign_futures_net_short_change`：外資台指期單日淨空單增加口數（正值＝增加空單）。
    - `foreign_futures_total_net_short`：外資台指期總淨空單口數（正值）。
    """
    taiex_below = taiex_close < taiex_ma20
    otc_below = otc_close < otc_ma20
    both_below = taiex_below and otc_below
    slopes_down = taiex_ma20_slope < 0 and otc_ma20_slope < 0

    if both_below and slopes_down:
        return RegimeQuotaConfig(
            "RED_DEFENSIVE", 0, 1, 50.0, 0.85,
            "🔴 空頭防禦：加權與櫃買雙破 MA20 且下彎，關閉右側追高，Style B 最多 1 檔",
        )

    futures_stress = (
        foreign_futures_net_short_change >= futures_net_short_surge_lots
        or foreign_futures_total_net_short > futures_total_net_short_limit
    )
    if taiex_below or otc_below or futures_stress:
        return RegimeQuotaConfig(
            "YELLOW_ROTATION", 1, 3, 45.0, 1.05,
            "🟡 震盪分歧：僅限最強族群龍頭右側 1 檔，Style B 低接 3 檔",
        )

    return RegimeQuotaConfig(
        "GREEN_BULL", 3, 2, 30.0, 1.30,
        "🟢 多頭進攻：加權與櫃買站上 MA20，右側突破 3 檔、回測低接 2 檔",
    )


def fetch_yahoo_daily_closes(
    yahoo_symbol: str,
    *,
    range_str: str = "60d",
    session: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[float]:
    """從 Yahoo Finance v8 Chart API 取得近期有效日收盤價序列（用於 `^TWII` 加權與 `^TWOII` 櫃買 MA20）。"""
    encoded = quote(yahoo_symbol, safe="")
    url = YAHOO_CHART_URL.format(symbol=encoded)
    http = session or requests
    try:
        resp = http.get(
            url,
            params={"range": range_str, "interval": "1d"},
            headers=USER_AGENT_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        result_list = (payload or {}).get("chart", {}).get("result") or []
        if not result_list:
            return []
        quote_list = ((result_list[0].get("indicators") or {}).get("quote") or [{}])[0]
        raw_closes = quote_list.get("close") or []
        return [val for raw in raw_closes if (val := _safe_float(raw)) is not None and val > 0]
    except Exception as err:  # noqa: BLE001
        logger.debug("Yahoo daily closes fetch failed for %s: %s", yahoo_symbol, err)
        return []


def fetch_foreign_tx_futures_net_short(
    *,
    as_of_date: str | None = None,
    finmind_token: str | None = None,
    session: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[float, float]:
    """查詢 FinMind `TaiwanFuturesInstitutionalInvestors` 外資台指期 (TX) 未平倉淨空單與單日增減。

    回傳 ``(net_short_change_1d_lots, total_net_short_lots)``；失敗或無資料時優雅降級回 ``(0.0, 0.0)``。
    """
    token = (finmind_token if finmind_token is not None else os.getenv("FINMIND_API_TOKEN", "")).strip()
    end_dt = datetime.strptime(as_of_date[:10], "%Y-%m-%d") if as_of_date else datetime.now(timezone.utc)
    start_str = (end_dt - timedelta(days=14)).strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")
    params: dict[str, str] = {
        "dataset": "TaiwanFuturesInstitutionalInvestors",
        "data_id": "TX",
        "start_date": start_str,
        "end_date": end_str,
    }
    if token:
        params["token"] = token

    http = session or requests
    try:
        resp = http.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=timeout)
        resp.raise_for_status()
        rows = (resp.json() or {}).get("data") or []
    except Exception as err:  # noqa: BLE001
        logger.debug("FinMind TX futures institutional fetch skipped/failed: %s", err)
        return 0.0, 0.0

    by_date: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or row.get("institutional_investors") or "").strip()
        if name not in ("外資", "外資及陸資", "Foreign_Investor"):
            continue
        dt = str(row.get("date") or "").strip()[:10]
        if not dt:
            continue
        # FinMind 欄位：open_interest_net / long_open_interest_balance_volume - short_open_interest_balance_volume
        net_oi = _safe_float(row.get("open_interest_net"))
        if net_oi is None:
            long_oi = _safe_float(row.get("long_open_interest_balance_volume")) or 0.0
            short_oi = _safe_float(row.get("short_open_interest_balance_volume")) or 0.0
            net_oi = long_oi - short_oi
        # 淨空單以正值表達（若外資為淨多單則淨空單為 0，但增減仍反映空方壓力變化）
        net_short = -float(net_oi)
        by_date[dt] = net_short

    if not by_date:
        return 0.0, 0.0
    sorted_dates = sorted(by_date.keys())
    latest_short = by_date[sorted_dates[-1]]
    prev_short = by_date[sorted_dates[-2]] if len(sorted_dates) >= 2 else latest_short
    chg = round(latest_short - prev_short, 1)
    return chg, round(max(0.0, latest_short), 1)


def quota_from_market_regime(market_regime: Mapping[str, Any] | None) -> RegimeQuotaConfig:
    """從 `market_regime` 字典還原 `RegimeQuotaConfig`；若無雙核心欄位則依燈號保守推導。"""
    if isinstance(market_regime, Mapping):
        raw_q = market_regime.get("regime_quota")
        if isinstance(raw_q, RegimeQuotaConfig):
            return raw_q
        if isinstance(raw_q, Mapping) and raw_q.get("regime_state") in (
            "GREEN_BULL",
            "YELLOW_ROTATION",
            "RED_DEFENSIVE",
        ):
            return RegimeQuotaConfig(
                regime_state=raw_q["regime_state"],
                max_right_breakout_picks=int(raw_q.get("max_right_breakout_picks", 1)),
                max_style_b_pullback_picks=int(raw_q.get("max_style_b_pullback_picks", 2)),
                min_sector_breadth_pct=float(raw_q.get("min_sector_breadth_pct", 45.0)),
                max_allowed_sector_pe_ratio=float(raw_q.get("max_allowed_sector_pe_ratio", 1.05)),
                regime_summary_tag=str(raw_q.get("regime_summary_tag") or ""),
            )
        light = str(market_regime.get("light") or "")
        if "🔴" in light:
            return RegimeQuotaConfig(
                "RED_DEFENSIVE", 0, 1, 50.0, 0.85,
                "🔴 空頭防禦：大盤紅燈防禦態勢，關閉右側追高，Style B 最多 1 檔",
            )
        if "🟢" in light:
            return RegimeQuotaConfig(
                "GREEN_BULL", 3, 2, 30.0, 1.30,
                "🟢 多頭進攻：大盤綠燈偏多，右側突破 3 檔、回測低接 2 檔",
            )
    return RegimeQuotaConfig(
        "YELLOW_ROTATION", 1, 3, 45.0, 1.05,
        "🟡 震盪分歧：僅限最強族群龍頭右側 1 檔，Style B 低接 3 檔",
    )


class RegimeRouter:
    """SASD v5.4.0 Phase 3 加權 + 櫃買 (OTC) 雙核心紅綠燈與外資期貨避險路由器。"""

    @staticmethod
    def compute_ma20_and_slope(closes: Sequence[float]) -> tuple[float, float, float] | None:
        clean = [float(c) for c in closes if _safe_float(c) is not None and float(c) > 0]
        if len(clean) < 20:
            return None
        close_t = clean[-1]
        ma20_t = sum(clean[-20:]) / 20.0
        ma20_prev = (sum(clean[-21:-1]) / 20.0) if len(clean) >= 21 else ma20_t
        return round(close_t, 2), round(ma20_t, 2), round(ma20_t - ma20_prev, 4)

    @classmethod
    def evaluate_from_series(
        cls,
        taiex_closes: Sequence[float],
        otc_closes: Sequence[float],
        *,
        foreign_futures_net_short_change: float = 0.0,
        foreign_futures_total_net_short: float = 0.0,
        futures_net_short_surge_lots: float = FUTURES_NET_SHORT_SURGE_LOTS,
        futures_total_net_short_limit: float = FUTURES_TOTAL_NET_SHORT_LIMIT,
    ) -> dict[str, Any] | None:
        t_stats = cls.compute_ma20_and_slope(taiex_closes)
        o_stats = cls.compute_ma20_and_slope(otc_closes)
        if t_stats is None or o_stats is None:
            return None
        quota = evaluate_market_regime_quota(
            taiex_close=t_stats[0],
            taiex_ma20=t_stats[1],
            taiex_ma20_slope=t_stats[2],
            otc_close=o_stats[0],
            otc_ma20=o_stats[1],
            otc_ma20_slope=o_stats[2],
            foreign_futures_net_short_change=foreign_futures_net_short_change,
            foreign_futures_total_net_short=foreign_futures_total_net_short,
            futures_net_short_surge_lots=futures_net_short_surge_lots,
            futures_total_net_short_limit=futures_total_net_short_limit,
        )
        return {
            "quota": quota.to_dict(),
            "taiex": {"close": t_stats[0], "ma20": t_stats[1], "ma20_slope": t_stats[2]},
            "otc": {"close": o_stats[0], "ma20": o_stats[1], "ma20_slope": o_stats[2]},
            "futures": {
                "foreign_net_short_change": foreign_futures_net_short_change,
                "foreign_total_net_short": foreign_futures_total_net_short,
            },
        }

    @classmethod
    def fetch_live_dual_regime(
        cls,
        *,
        taiex_closes: Sequence[float] | None = None,
        otc_closes: Sequence[float] | None = None,
        as_of_date: str | None = None,
        finmind_token: str | None = None,
        session: Any | None = None,
        futures_net_short_surge_lots: float = FUTURES_NET_SHORT_SURGE_LOTS,
        futures_total_net_short_limit: float = FUTURES_TOTAL_NET_SHORT_LIMIT,
    ) -> dict[str, Any] | None:
        t_series = list(taiex_closes) if taiex_closes else fetch_yahoo_daily_closes("^TWII", session=session)
        o_series = list(otc_closes) if otc_closes else fetch_yahoo_daily_closes("^TWOII", session=session)
        if len(t_series) < 20 or len(o_series) < 20:
            return None
        fut_chg, fut_tot = fetch_foreign_tx_futures_net_short(
            as_of_date=as_of_date, finmind_token=finmind_token, session=session
        )
        return cls.evaluate_from_series(
            t_series,
            o_series,
            foreign_futures_net_short_change=fut_chg,
            foreign_futures_total_net_short=fut_tot,
            futures_net_short_surge_lots=futures_net_short_surge_lots,
            futures_total_net_short_limit=futures_total_net_short_limit,
        )
