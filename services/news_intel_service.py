"""Unified News Intelligence Service (MVP — High Cohesion, Low Coupling).

Single source of truth for crawling, cleaning, caching, and free-tier lightweight AI
fact distillation across:
- 08:15 ``morning-brief`` (taishin_portfolio_service + macro_context)
- 21:30 ``ai-pick`` (alignment_ai_picker + fundamental_analyzer)
- 22:00 ``rebound`` (alignment_ai_picker + fundamental_analyzer)
- Discord ``/stock_ai`` (fundamental_analyzer)

Design invariants:
1. Pure ``requests`` + Python stdlib HTML/XML parsing (zero bs4 / headless browser deps).
2. Strict zero-cost guardrail: uses Google AI Studio FREE tier ONLY
   (``GEMINI_API_KEY`` + ``NEWS_INTEL_MODEL`` / ``AI_MODEL_BASE``). NEVER falls back
   to paid Vertex AI. On rate-limit/API failure, gracefully degrades to Python-cleaned
   article excerpts.
3. 1 API call per macro sweep; 1 API call per single stock (recent + FactSet + 180d
   outlook consolidated together -> zero cross-stock hallucination).
4. Local differentiated TTL cache under ``finmind_reports/.state/news_intel_cache/``:
   - macro: 2 hours (shared between 21:30 & 22:00; refreshed next morning at 08:15)
   - stock recent: 6 hours (shared across 21:30/22:00 stages; refreshed at 08:15)
   - stock outlook (180d): 7 days (early-invalidated if recent news contains new
     earnings-call / guidance announcements)
5. Prompt/output logging to ``ai_prompt_logs`` (local + BigQuery) is controlled by
   ``NEWS_INTEL_LOG_PROMPTS`` (default ``true`` for initial verification).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Mapping
import xml.etree.ElementTree as ET

from pydantic import BaseModel, Field
import requests

logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))

# --------------------------------------------------------------------------- #
# Endpoints & Default Tunables
# --------------------------------------------------------------------------- #
CNYES_US_STOCK_URL = "https://api.cnyes.com/media/api/v1/newslist/category/us_stock"
CNYES_WD_MACRO_URL = "https://api.cnyes.com/media/api/v1/newslist/category/wd_macro"
CNYES_STOCK_NEWS_URL_TMPL = "https://api.cnyes.com/media/api/v1/newslist/TWS:{stock_id}:STOCK/news"
CNYES_ARTICLE_URL_TMPL = "https://news.cnyes.com/news/id/{news_id}"
YAHOO_STOCK_RSS_URL = "https://tw.stock.yahoo.com/rss"

DEFAULT_HTTP_TIMEOUT_SEC = 10
DEFAULT_BODY_TIMEOUT_SEC = 8
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {"User-Agent": DEFAULT_USER_AGENT}

MACRO_CACHE_TTL_SECONDS = 2 * 3600       # 2h: 21:30 & 22:00 reuse; 08:15 refreshes
STOCK_RECENT_CACHE_TTL_SECONDS = 6 * 3600  # 6h: 21:30 & 22:00 reuse; 08:15 refreshes
STOCK_OUTLOOK_CACHE_TTL_SECONDS = 7 * 86400  # 7d: early-invalidated on new 法說/財測

DEFAULT_MIN_AI_INTERVAL_SEC = 5.0  # <= 12 RPM pacing (Free Tier limit is 15 RPM)

_RATE_LOCK = threading.Lock()
_LAST_AI_CALL_MONO: float = 0.0

# --------------------------------------------------------------------------- #
# Anti-Noise & MOPS Routing Rules
# --------------------------------------------------------------------------- #
_CNYES_SKIP_CATEGORIES = frozenset({"台股盤中", "台股盤前", "盤中速報"})

_SPAM_TITLE_PATTERNS = (
    "盤中速報",
    "⊕",
    "雙亮燈",
    "漲停鎖死",
    "殺盤嚇不倒",
    "這關鍵!!",
    "量大強漲股整理",
    "這兩檔",
    "這幾檔",
    "這3檔",
    "這5檔",
    "ETF 飆",
    "飆逾",
    "黑馬股",
    "必買",
    "〈台股盤後〉",
    "〈台股開盤〉",
    "〈台股盤前要聞〉",
    "投信近3日",
    "投信近5日",
    "外資近3日",
    "外資近5日",
    "自營商近",
    "法人近",
    "焦點股",
    "熱門股",
    "三大法人買賣超",
    "搶先佈局",
    "退休門檻",
    "老後焦慮",
    "樂透",
    "彩券",
    "新權證發行",
    "權證發行申請案",
    "發行權證",
)

_MACRO_CLICKBAIT_PATTERNS = (
    "這兩檔",
    "這幾檔",
    "飆逾",
    "ETF 飆",
    "退休門檻",
    "老後焦慮",
    "怎麼買最划算",
    "買這檔",
    "存股族",
    "高股息ETF",
    "盤中速報",
)

_CALENDAR_TITLE_RE = re.compile(
    r"(法人說明會|機構投資人說明會|投資人說明會|投資人會議|業績發表會|法說會|Corporate Day|Conference|除權息交易日|除息交易日)",
    re.IGNORECASE,
)

_ROUTINE_MOPS_SKIP_RE = re.compile(
    r"(通過.*財務報告|董事會決議通過.*季|董事異動|監察人異動|獨立董事|內部稽核|會計主管|"
    r"公司治理主管|發言人異動|代理發言人|合併營收|營收速報|\d+月營收.*1[—\-~]\d+月達|資金貸與|背書保證|"
    r"取得或處分.*理財|買賣.*金融商品|股東常會.*紀念品|無償配股基準日|更正.*申報)",
    re.IGNORECASE,
)

_OUTLOOK_KEYWORDS = (
    "法說",
    "展望",
    "董座",
    "董事長",
    "總座",
    "總經理",
    "執行長",
    "訂單",
    "產能",
    "毛利率",
    "資本支出",
    "財測",
    "指引",
    "能見度",
    "放量",
    "擴產",
    "接單",
    "AI",
    "光通訊",
    "投資人會議",
    "目標價",
    "Factset",
    "FactSet",
)

_EARLY_OUTLOOK_INVALIDATION_RE = re.compile(
    r"(法說|投資人會議|投資人說明會|業績發表會|財測|全年展望|明年展望|目標價)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Env Helpers
# --------------------------------------------------------------------------- #
def is_news_intel_ai_enabled(override: bool | None = None) -> bool:
    """Master switch for free-tier AI fact distillation (env ENABLE_NEWS_INTEL_AI, default True)."""
    if override is not None:
        return bool(override)
    raw = (os.getenv("ENABLE_NEWS_INTEL_AI") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _get_min_ai_interval_sec() -> float:
    raw = (os.getenv("NEWS_INTEL_MIN_INTERVAL_SEC") or "").strip()
    if raw:
        try:
            val = float(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
    return DEFAULT_MIN_AI_INTERVAL_SEC


def _default_cache_dir() -> Path:
    base = (os.getenv("FINMIND_REPORTS_DIR") or "").strip()
    root = Path(base) if base else (Path(__file__).resolve().parent.parent / "finmind_reports")
    return root / ".state" / "news_intel_cache"


def _pace_ai_call(min_interval_sec: float | None = None) -> None:
    """Enforce polite pacing (default <= 12 RPM) to stay well within Gemini Free Tier 15 RPM."""
    global _LAST_AI_CALL_MONO
    interval = _get_min_ai_interval_sec() if min_interval_sec is None else max(0.0, float(min_interval_sec))
    if interval <= 0:
        return
    with _RATE_LOCK:
        now = time.monotonic()
        wait = (_LAST_AI_CALL_MONO + interval) - now
        if wait > 0:
            time.sleep(wait)
        _LAST_AI_CALL_MONO = time.monotonic()


# --------------------------------------------------------------------------- #
# Structured Data Models (Pydantic AI Schemas + Cacheable Fact Cards)
# --------------------------------------------------------------------------- #
class MacroIntelAIOutput(BaseModel):
    core_drivers: list[str] = Field(
        default_factory=list,
        description="2~4 條昨晚美股/國際盤漲跌的核心事件主因與關鍵數據，每條結尾須附 [MM-DD｜來源]。",
    )
    unresolved_risks: list[str] = Field(
        default_factory=list,
        description="1~3 條尚未解除之風險、政策變數、調查或即將公布之重大事件，每條結尾須附 [MM-DD｜來源]。",
    )
    sector_impacts: list[str] = Field(
        default_factory=list,
        description="1~3 條對台股科技/半導體/AI/傳產或外資資金面之實質傳導事實，每條結尾須附 [MM-DD｜來源]。",
    )


class StockIntelAIOutput(BaseModel):
    factset_consensus: list[str] = Field(
        default_factory=list,
        description="0~2 條 FactSet 或外資機構對 EPS / 目標價之最新預估調整事實，每條結尾須附 [MM-DD｜來源]；無則空陣列。",
    )
    recent_catalysts: list[str] = Field(
        default_factory=list,
        description="0~3 條近 30 天內有實質基本面意義的新聞催化劑（新產品/大單/擴產/處分/自結損益等），每條結尾須附 [MM-DD｜來源]；若無實質內容則留空陣列。",
    )
    risk_flags: list[str] = Field(
        default_factory=list,
        description="0~3 條原文明確提及的營運、毛利、供應鏈、法規或籌碼風險事實，每條結尾須附 [MM-DD｜來源]；無則空陣列。",
    )
    outlook_claims: list[str] = Field(
        default_factory=list,
        description="0~4 條近 180 天內管理層公開談話或法說報導中，對訂單能見度、產能、毛利率、擴產或全年展望的具體事實與數字，每條結尾須附 [YYYY-MM-DD｜來源]；若原文無具體內容則留空陣列。",
    )
    outlook_has_substance: bool = Field(
        default=False,
        description="唯有當 outlook_claims 具備具體數字、產能/訂單進度或管理層實質說法時才為 true，否則為 false。",
    )


@dataclass
class MacroIntelCard:
    as_of: str
    mode: str  # "ai" | "fallback_excerpt" | "empty"
    model_version: str = ""
    core_drivers: list[str] = field(default_factory=list)
    unresolved_risks: list[str] = field(default_factory=list)
    sector_impacts: list[str] = field(default_factory=list)
    raw_articles: list[dict[str, Any]] = field(default_factory=list)
    fetched_at_epoch: float = 0.0
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, from_cache: bool = False) -> MacroIntelCard:
        return cls(
            as_of=str(data.get("as_of") or ""),
            mode=str(data.get("mode") or "empty"),
            model_version=str(data.get("model_version") or ""),
            core_drivers=[str(x) for x in (data.get("core_drivers") or []) if str(x).strip()],
            unresolved_risks=[str(x) for x in (data.get("unresolved_risks") or []) if str(x).strip()],
            sector_impacts=[str(x) for x in (data.get("sector_impacts") or []) if str(x).strip()],
            raw_articles=list(data.get("raw_articles") or []),
            fetched_at_epoch=float(data.get("fetched_at_epoch") or 0.0),
            from_cache=from_cache,
        )


@dataclass
class StockIntelCard:
    stock_id: str
    stock_name: str
    as_of: str
    mode: str  # "ai" | "fallback_excerpt" | "empty"
    model_version: str = ""
    calendar_events: list[dict[str, str]] = field(default_factory=list)
    factset_consensus: list[str] = field(default_factory=list)
    recent_catalysts: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    outlook_claims: list[str] = field(default_factory=list)
    outlook_has_substance: bool = False
    raw_recent_articles: list[dict[str, Any]] = field(default_factory=list)
    raw_outlook_articles: list[dict[str, Any]] = field(default_factory=list)
    recent_fetched_at_epoch: float = 0.0
    outlook_fetched_at_epoch: float = 0.0
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, from_cache: bool = False) -> StockIntelCard:
        return cls(
            stock_id=str(data.get("stock_id") or ""),
            stock_name=str(data.get("stock_name") or ""),
            as_of=str(data.get("as_of") or ""),
            mode=str(data.get("mode") or "empty"),
            model_version=str(data.get("model_version") or ""),
            calendar_events=list(data.get("calendar_events") or []),
            factset_consensus=[str(x) for x in (data.get("factset_consensus") or []) if str(x).strip()],
            recent_catalysts=[str(x) for x in (data.get("recent_catalysts") or []) if str(x).strip()],
            risk_flags=[str(x) for x in (data.get("risk_flags") or []) if str(x).strip()],
            outlook_claims=[str(x) for x in (data.get("outlook_claims") or []) if str(x).strip()],
            outlook_has_substance=bool(data.get("outlook_has_substance", False)),
            raw_recent_articles=list(data.get("raw_recent_articles") or []),
            raw_outlook_articles=list(data.get("raw_outlook_articles") or []),
            recent_fetched_at_epoch=float(data.get("recent_fetched_at_epoch") or 0.0),
            outlook_fetched_at_epoch=float(data.get("outlook_fetched_at_epoch") or 0.0),
            from_cache=from_cache,
        )


# --------------------------------------------------------------------------- #
# Text Cleaning, MOPS Header Stripping & Calendar Extraction
# --------------------------------------------------------------------------- #
def strip_html_to_text(raw_html: str) -> str:
    """Strip HTML tags/scripts, invisible zero-width chars, and collapse whitespace (stdlib only)."""
    if not raw_html:
        return ""
    unescaped = html.unescape(raw_html)
    cleaned = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", unescaped)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"[\u200b-\u200f\ufeff]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_yahoo_and_cnyes_chrome(text: str) -> str:
    """Remove Yahoo Google-source banners, inline ticker badges, and cnyes footer chrome."""
    if not text:
        return ""
    s = text.strip()
    # Yahoo top promo banner
    s = re.sub(
        r"^.*?在\s*Google\s*上查看更多我們的精彩報導\s*",
        "",
        s,
    )
    # Yahoo inline ticker quote badge like "聯詠 3034.TW 435.5 ( 1.04% )"
    s = re.sub(
        r"^[^\s]{1,12}\s+\d{4}\.TW[O]?\s+[\d,.]+\s*\(\s*[+-]?[\d.,]+%?\s*\)\s*",
        "",
        s,
    )
    # cnyes leading byline "鉅亨網新聞中心 2026-11-19 17:34"
    s = re.sub(r"^鉅亨網[^\s]{0,12}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*", "", s)
    # cnyes trailing tag / nav junk
    s = re.sub(r"\s*#\s*[^\n]{1,30}\s+[\d.]+\s*%\s*上一篇\s*下一篇.*$", "", s)
    s = re.sub(r"\s*上一篇\s*下一篇.*$", "", s)
    s = re.sub(
        r"\s*完整財務業務資訊請至公開資訊觀測站之法人說明會一覽表或法說會項目下查閱。?",
        "",
        s,
    )
    return re.sub(r"\s+", " ", s).strip()


def strip_mops_header(text: str) -> str:
    """Strip repetitive MOPS filing headers so actual numbered points start at char 0."""
    if not text:
        return ""
    s = clean_yahoo_and_cnyes_chrome(text)
    # Prefer jumping right after explicit "說 明：" or "發言人：XXX"
    m_desc = re.search(r"說\s*明\s*[：:]\s*", s)
    if m_desc:
        body = s[m_desc.end():].strip()
    else:
        m_spokes = re.search(r"發言人\s*[：:]\s*[^\s]{2,12}\s+", s)
        body = s[m_spokes.end():].strip() if m_spokes else s
    # Remove leading clause boilerplate "第12款 符合條款第四條第XX款：12 事實發生日：115/11/23"
    body = re.sub(r"^(?:第\d+款\s*)?符合條款第[^：\s]+條第?[^：\s]*款[：:]\s*\d*\s*", "", body)
    body = re.sub(r"^事實發生日[：:]\s*[\d/年\-月日]+\s*", "", body)
    return re.sub(r"\s+", " ", body).strip()


def extract_event_date_from_text(text: str, fallback_ymd: str = "") -> str:
    """Extract ROC (e.g. 115/11/24) or AD (e.g. 2026/11/24) event date as YYYY-MM-DD."""
    if text:
        # Prefer explicit conference/event date fields first
        priority_patterns = (
            r"召開法人說明會之日期[：:\s]*(\d{3,4})[/年\-](\d{1,2})[/月\-](\d{1,2})",
            r"除[權息]+交易日[：:\s]*(\d{3,4})[/年\-](\d{1,2})[/月\-](\d{1,2})",
            r"事實發生日[：:\s]*(\d{3,4})[/年\-](\d{1,2})[/月\-](\d{1,2})",
            r"(\d{3})[/年](\d{1,2})[/月](\d{1,2})日?",
        )
        for pat in priority_patterns:
            m = re.search(pat, text)
            if m:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if y < 1900:
                    y += 1911
                if 2020 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
    return fallback_ymd


def is_spam_or_noise_title(title: str, category: str = "", *, is_macro: bool = False) -> bool:
    """Filter clickbait, intraday bot alerts, and stock-guru spam."""
    if not title:
        return True
    if category in _CNYES_SKIP_CATEGORIES:
        return True
    patterns = _MACRO_CLICKBAIT_PATTERNS if is_macro else _SPAM_TITLE_PATTERNS
    return any(p in title for p in patterns)


def classify_mops_or_notice(title: str, category: str = "") -> str:
    """Classify a stock item into 'calendar' | 'routine_skip' | 'factset' | 'editorial' | 'substantive_mops'."""
    t = (title or "").strip()
    if category == "台股預估" or "Factset" in t or "FactSet" in t:
        return "factset"
    if _CALENDAR_TITLE_RE.search(t):
        return "calendar"
    is_mops = (
        category == "台股公告"
        or t.startswith("【公告】")
        or bool(re.match(r"^[^\s:：]{2,8}\s*[:：]\s*(公告|本公司|代重要子公司|董事會|更正|補充)", t))
    )
    if is_mops:
        if _ROUTINE_MOPS_SKIP_RE.search(t):
            return "routine_skip"
        return "substantive_mops"
    if _ROUTINE_MOPS_SKIP_RE.search(t):
        return "routine_skip"
    return "editorial"


def _norm_title_key(title: str) -> str:
    s = re.sub(r"^【[^】]+】\s*", "", title or "")
    s = re.sub(r"^[^\s:：]{2,8}\s*[:：]\s*(?:公告本公司|本公司|公告)?", "", s)
    s = re.sub(r"[\W_]+", "", s).lower()
    return s[:30]


def _ts_to_taipei_dt(ts: int | float | str | None) -> datetime | None:
    if ts is None or ts == "":
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(TAIPEI_TZ)
    except (TypeError, ValueError, OSError):
        return None


def _rfc2822_to_taipei_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TAIPEI_TZ)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Crawlers (cnyes JSON API + cnyes/Yahoo <article><p> extractor)
# --------------------------------------------------------------------------- #
def _http_get_with_retry(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    session: Any | None = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SEC,
    max_attempts: int = 2,
    retry_delay: float = 0.8,
) -> Any:
    """GET with lightweight retry to survive occasional TLS/EOF resets on cnyes/Yahoo."""
    http = session or requests
    last_err: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            kwargs: dict[str, Any] = {"headers": HTTP_HEADERS, "timeout": timeout}
            if params is not None:
                kwargs["params"] = params
            return http.get(url, **kwargs)
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt + 1 < max_attempts:
                time.sleep(retry_delay)
                continue
            raise
    assert last_err is not None
    raise last_err


def fetch_article_body_html(
    url: str,
    *,
    session: Any | None = None,
    timeout: int = DEFAULT_BODY_TIMEOUT_SEC,
    max_chars: int = 1200,
) -> str:
    """Extract clean paragraph body from a cnyes or Yahoo Stock article page (stdlib only)."""
    if not url:
        return ""
    try:
        resp = _http_get_with_retry(url, session=session, timeout=timeout, max_attempts=2)
        if getattr(resp, "status_code", 0) != 200:
            return ""
        raw_html = resp.text
    except Exception as err:  # noqa: BLE001
        logger.debug("Article body fetch failed (%s): %s", url, err)
        return ""

    # Prefer <article> container (works cleanly on both news.cnyes.com and tw.stock.yahoo.com)
    m_art = re.search(r"(?is)<article[^>]*>(.*?)</article>", raw_html)
    scope = m_art.group(1) if m_art else raw_html
    paragraphs = [
        strip_html_to_text(p)
        for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", scope)
    ]
    paragraphs = [p for p in paragraphs if len(p) >= 6]
    joined = " ".join(paragraphs) if paragraphs else strip_html_to_text(scope)
    cleaned = clean_yahoo_and_cnyes_chrome(joined)
    return cleaned[:max_chars]


def fetch_macro_articles(
    *,
    max_items: int = 8,
    max_age_hours: int = 48,
    session: Any | None = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SEC,
    now_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent US stock & global macro articles WITH full body text from cnyes JSON API."""
    ref_now = (now_utc or datetime.now(timezone.utc)).astimezone(TAIPEI_TZ)
    cutoff = ref_now - timedelta(hours=max_age_hours)

    sources = (
        (CNYES_US_STOCK_URL, {"limit": 15}, "鉅亨網美股"),
        (CNYES_WD_MACRO_URL, {"limit": 10}, "鉅亨網國際"),
    )

    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    collected: list[dict[str, Any]] = []

    for url, params, default_src in sources:
        try:
            resp = _http_get_with_retry(url, params=params, session=session, timeout=timeout, max_attempts=2)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as err:  # noqa: BLE001
            logger.warning("cnyes macro fetch failed (%s): %s", url, err)
            continue

        data = ((payload.get("items") or {}).get("data") or []) if isinstance(payload, dict) else []
        for item in data:
            if not isinstance(item, dict):
                continue
            nid = str(item.get("newsId") or "").strip()
            title = strip_html_to_text(str(item.get("title") or ""))
            cat = str(item.get("categoryName") or "").strip()
            if not title or is_spam_or_noise_title(title, cat, is_macro=True):
                continue
            if nid and nid in seen_ids:
                continue
            key = _norm_title_key(title)
            if not key or key in seen_keys:
                continue

            pub_dt = _ts_to_taipei_dt(item.get("publishAt"))
            if pub_dt is not None and pub_dt < cutoff:
                continue

            body_raw = str(item.get("content") or item.get("summary") or "")
            body = clean_yahoo_and_cnyes_chrome(strip_html_to_text(body_raw))[:900]
            if not body:
                continue

            if nid:
                seen_ids.add(nid)
            seen_keys.add(key)

            collected.append(
                {
                    "news_id": nid,
                    "title": title,
                    "source": default_src,
                    "category": cat,
                    "pub_date": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else "",
                    "mm_dd": pub_dt.strftime("%m-%d") if pub_dt else "",
                    "pub_ts": pub_dt.timestamp() if pub_dt else 0.0,
                    "url": CNYES_ARTICLE_URL_TMPL.format(news_id=nid) if nid else "",
                    "body": body,
                }
            )

    collected.sort(key=lambda x: float(x.get("pub_ts") or 0.0), reverse=True)
    return collected[:max_items]


def _fetch_cnyes_stock_raw(
    stock_id: str,
    *,
    days: int = 180,
    limit: int = 40,
    session: Any | None = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SEC,
    now_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    ref_utc = now_utc or datetime.now(timezone.utc)
    end_ts = int(ref_utc.timestamp())
    start_ts = end_ts - days * 86400
    url = CNYES_STOCK_NEWS_URL_TMPL.format(stock_id=stock_id)
    try:
        resp = _http_get_with_retry(
            url,
            params={"limit": limit, "startAt": start_ts, "endAt": end_ts},
            session=session,
            timeout=timeout,
            max_attempts=2,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as err:  # noqa: BLE001
        logger.warning("cnyes stock list fetch failed (%s): %s", stock_id, err)
        return []

    data = ((payload.get("items") or {}).get("data") or []) if isinstance(payload, dict) else []
    items: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        nid = str(row.get("newsId") or "").strip()
        title = strip_html_to_text(str(row.get("title") or ""))
        cat = str(row.get("categoryName") or "").strip()
        pub_dt = _ts_to_taipei_dt(row.get("publishAt"))
        if not title or not nid:
            continue
        items.append(
            {
                "news_id": nid,
                "title": title,
                "category": cat,
                "source": "鉅亨網" if cat != "台股預估" else "鉅亨速報/FactSet",
                "pub_dt": pub_dt,
                "pub_date": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else "",
                "ymd": pub_dt.strftime("%Y-%m-%d") if pub_dt else "",
                "mm_dd": pub_dt.strftime("%m-%d") if pub_dt else "",
                "pub_ts": pub_dt.timestamp() if pub_dt else 0.0,
                "url": CNYES_ARTICLE_URL_TMPL.format(news_id=nid),
                "rss_desc": "",
            }
        )
    return items


def _fetch_yahoo_stock_rss_raw(
    stock_id: str,
    *,
    session: Any | None = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SEC,
) -> list[dict[str, Any]]:
    try:
        resp = _http_get_with_retry(
            YAHOO_STOCK_RSS_URL,
            params={"s": stock_id},
            session=session,
            timeout=timeout,
            max_attempts=2,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as err:  # noqa: BLE001
        logger.warning("Yahoo stock RSS fetch failed (%s): %s", stock_id, err)
        return []

    items: list[dict[str, Any]] = []
    for elem in root.findall("./channel/item"):
        title = strip_html_to_text(elem.findtext("title") or "")
        link = (elem.findtext("link") or "").strip()
        pub_dt = _rfc2822_to_taipei_dt((elem.findtext("pubDate") or "").strip())
        desc = strip_html_to_text(elem.findtext("description") or "")
        if not title or not link:
            continue
        items.append(
            {
                "news_id": "",
                "title": title,
                "category": "台股公告" if title.startswith("【公告】") else "Yahoo股市",
                "source": "公開資訊觀測站/Yahoo" if title.startswith("【公告】") else "Yahoo股市",
                "pub_dt": pub_dt,
                "pub_date": pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else "",
                "ymd": pub_dt.strftime("%Y-%m-%d") if pub_dt else "",
                "mm_dd": pub_dt.strftime("%m-%d") if pub_dt else "",
                "pub_ts": pub_dt.timestamp() if pub_dt else 0.0,
                "url": link,
                "rss_desc": desc,
            }
        )
    return items


def fetch_stock_news_bundle(
    stock_id: str,
    stock_name: str = "",
    *,
    recent_days: int = 30,
    outlook_days: int = 180,
    max_recent: int = 5,
    max_outlook: int = 5,
    max_factset: int = 2,
    max_calendar: int = 3,
    session: Any | None = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SEC,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Fetch, deduplicate, classify, and hydrate full bodies for one stock.

    Returns dict with keys: ``calendar_events``, ``factset_articles``,
    ``recent_articles``, ``outlook_articles``.
    """
    ref_now = (now_utc or datetime.now(timezone.utc)).astimezone(TAIPEI_TZ)
    recent_cutoff = ref_now - timedelta(days=recent_days)
    outlook_cutoff = ref_now - timedelta(days=outlook_days)

    raw_all = _fetch_cnyes_stock_raw(
        stock_id, days=outlook_days, limit=40, session=session, timeout=timeout, now_utc=now_utc
    ) + _fetch_yahoo_stock_rss_raw(stock_id, session=session, timeout=timeout)

    raw_all.sort(key=lambda x: float(x.get("pub_ts") or 0.0), reverse=True)

    seen_keys: set[str] = set()
    calendar_candidates: list[dict[str, Any]] = []
    factset_candidates: list[dict[str, Any]] = []
    recent_candidates: list[dict[str, Any]] = []
    outlook_candidates: list[dict[str, Any]] = []

    for it in raw_all:
        title = it["title"]
        cat = it["category"]
        if is_spam_or_noise_title(title, cat, is_macro=False):
            continue
        key = _norm_title_key(title)
        if not key or key in seen_keys:
            continue

        pub_dt = it.get("pub_dt")
        if pub_dt is not None and pub_dt < outlook_cutoff:
            continue

        kind = classify_mops_or_notice(title, cat)
        if kind == "routine_skip":
            continue

        seen_keys.add(key)

        if kind == "calendar":
            # Only keep reasonably recent / upcoming calendar notices (last 21 days)
            if pub_dt is None or pub_dt >= (ref_now - timedelta(days=21)):
                calendar_candidates.append(it)
            continue

        if kind == "factset":
            factset_candidates.append(it)
            continue

        is_recent = pub_dt is None or pub_dt >= recent_cutoff
        has_outlook_kw = any(k in title for k in _OUTLOOK_KEYWORDS)

        if is_recent:
            recent_candidates.append({**it, "_kind": kind})

        # Editorial articles within 180d are outlook candidates (prioritize keyword hits)
        if kind == "editorial" or has_outlook_kw:
            outlook_candidates.append({**it, "_kind": kind, "_outlook_priority": 1 if has_outlook_kw else 0})

    outlook_candidates.sort(
        key=lambda x: (int(x.get("_outlook_priority", 0)), float(x.get("pub_ts") or 0.0)),
        reverse=True,
    )

    # Hydrate calendar event dates from full notice body
    calendar_events: list[dict[str, str]] = []
    seen_cal_keys: set[str] = set()
    for c_it in calendar_candidates[:max_calendar]:
        body = fetch_article_body_html(c_it["url"], session=session, max_chars=700)
        event_date = extract_event_date_from_text(body or c_it.get("rss_desc", ""), fallback_ymd=c_it["ymd"])
        clean_title = re.sub(r"^【公告】\s*", "", c_it["title"])
        clean_title = re.sub(r"^[^\s:：]{2,8}\s*[:：]\s*(?:公告本公司|本公司|公告)?", "", clean_title).strip()
        if stock_name and clean_title.startswith(stock_name):
            clean_title = clean_title[len(stock_name):].lstrip(" :：")
        clean_title = re.sub(r"^(?:公告本公司|本公司|公告)\s*", "", clean_title).strip()
        cal_key = f"{event_date}|{re.sub(r'[\W_]+', '', clean_title).lower()[:20]}"
        if cal_key in seen_cal_keys:
            continue
        seen_cal_keys.add(cal_key)
        calendar_events.append(
            {
                "date": event_date,
                "event": clean_title[:80],
                "source": c_it["source"],
            }
        )

    body_cache: dict[str, str] = {}

    def _hydrate(item: dict[str, Any], bucket: str) -> dict[str, Any]:
        u = item["url"]
        if u not in body_cache:
            raw_body = fetch_article_body_html(u, session=session, max_chars=1200)
            if not raw_body:
                raw_body = item.get("rss_desc") or ""
            body_cache[u] = strip_mops_header(raw_body)[:900]
        return {
            "news_id": item.get("news_id") or "",
            "title": item["title"],
            "source": item["source"],
            "category": item["category"],
            "bucket": bucket,
            "pub_date": item["pub_date"],
            "ymd": item["ymd"],
            "mm_dd": item["mm_dd"],
            "pub_ts": item["pub_ts"],
            "url": u,
            "body": body_cache[u],
        }

    factset_articles = [_hydrate(x, "factset") for x in factset_candidates[:max_factset]]
    recent_articles = [_hydrate(x, "recent") for x in recent_candidates[:max_recent]]
    outlook_articles = [_hydrate(x, "outlook") for x in outlook_candidates[:max_outlook]]

    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "calendar_events": calendar_events,
        "factset_articles": factset_articles,
        "recent_articles": recent_articles,
        "outlook_articles": outlook_articles,
    }


# --------------------------------------------------------------------------- #
# Free-Tier AI Fact Distillation Prompts & Fallback Builders
# --------------------------------------------------------------------------- #
_MACRO_SYSTEM_INSTRUCTION = (
    "你是專業財經新聞事實編輯。任務是將多篇國際與美股財經新聞正文去重、濃縮為高密度的純事實重點。\n"
    "鐵則：\n"
    "1. 只准依據提供的原文撰寫，嚴禁補充任何原文沒寫的數據、政策或新聞。\n"
    "2. 嚴禁給出買進/賣出/抄底/逃命等投資操作建議。\n"
    "3. 每條重點控制在 45 字內，結尾必須附上溯源標籤 [MM-DD｜來源]。\n"
    "4. 忽略農場標題、無關八卦或重複報導。"
)

_STOCK_SYSTEM_INSTRUCTION = (
    "你是極度嚴謹的台股基本面新聞事實核查編輯。任務是將單一檔個股的新聞、FactSet 調查與法說報導正文，"
    "萃取為結構化的純事實清單，供下游資深分析師使用。\n"
    "鐵則：\n"
    "1. 嚴格忠於原文：只准提取原文明確出現的數字、客戶/產品進度、法人預估或管理層原話；嚴禁推測或腦補。\n"
    "2. 寧缺勿濫：若某類別無實質新聞事實（例如僅例行公告或空泛標題），對應陣列必須輸出空陣列 []，嚴禁硬湊。\n"
    "3. 嚴禁給出買賣建議、目標價預測或主觀情緒評論。\n"
    "4. 每條重點控制在 55 字內，結尾必須附上溯源標籤（近期用 [MM-DD｜來源]，180 日法說展望用 [YYYY-MM-DD｜來源]）。\n"
    "5. 唯有當原文有管理層具體談話或量化營運展望（產能/訂單/毛利/放量時程）時，outlook_has_substance 才設為 true。"
)


def _build_macro_distill_prompt(articles: list[dict[str, Any]]) -> str:
    lines = ["以下是近 48 小時國際總經與美股重點新聞原文，請依規則萃取純事實重點 JSON：", ""]
    for idx, art in enumerate(articles, start=1):
        lines.append(
            f"--- 文章 {idx} [{art.get('mm_dd') or '近期'}｜{art.get('source') or '鉅亨網'}] ---\n"
            f"標題：{art.get('title')}\n"
            f"內文：{art.get('body')}\n"
        )
    return "\n".join(lines)


def _build_stock_distill_prompt(
    stock_id: str,
    stock_name: str,
    bundle: Mapping[str, Any],
) -> str:
    label = f"{stock_id} {stock_name}".strip()
    lines = [f"目標個股：【{label}】", "請僅針對此標的萃取純事實重點 JSON（若內文提及他股請忽略）：", ""]

    def _append_section(header: str, arts: list[dict[str, Any]]) -> None:
        if not arts:
            return
        lines.append(f"=== {header} ===")
        for idx, art in enumerate(arts, start=1):
            date_tag = art.get("ymd") or art.get("mm_dd") or "近期"
            lines.append(
                f"[{idx}] ({date_tag}｜{art.get('source')}) 標題：{art.get('title')}\n"
                f"    正文：{art.get('body') or '（無內文）'}"
            )
        lines.append("")

    _append_section("A. FactSet / 機構預估調查", list(bundle.get("factset_articles") or []))
    _append_section("B. 近 30 天即時新聞與實質重訊", list(bundle.get("recent_articles") or []))
    _append_section("C. 近 180 天深度報導與法說展望", list(bundle.get("outlook_articles") or []))
    return "\n".join(lines)


def _fallback_excerpt_bullet(art: Mapping[str, Any], *, full_date: bool = False, max_chars: int = 160) -> str:
    d_tag = (art.get("ymd") if full_date else art.get("mm_dd")) or art.get("mm_dd") or "近期"
    src = art.get("source") or "財經媒體"
    title = str(art.get("title") or "").strip()
    body = str(art.get("body") or "").strip()
    snippet = body[:max_chars].rstrip()
    if snippet and snippet not in title:
        return f"{title}：{snippet} [{d_tag}｜{src}]"
    return f"{title} [{d_tag}｜{src}]"


def _parse_json_safe(raw_text: str) -> dict[str, Any]:
    s = (raw_text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    parsed = json.loads(s)
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
# Local Differentiated TTL Cache
# --------------------------------------------------------------------------- #
def _read_json_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _write_json_cache(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as err:  # noqa: BLE001
        logger.warning("Failed to write news cache %s: %s", path, err)


# --------------------------------------------------------------------------- #
# Public High-Level Service API
# --------------------------------------------------------------------------- #
def get_macro_intel(
    *,
    force_refresh: bool = False,
    enable_ai: bool | None = None,
    log_prompts: bool | None = None,
    cache_dir: Path | None = None,
    ttl_seconds: int = MACRO_CACHE_TTL_SECONDS,
    session: Any | None = None,
    now_utc: datetime | None = None,
) -> MacroIntelCard:
    """Get cached or freshly crawled + free-tier AI distilled global macro news card."""
    ref_now = now_utc or datetime.now(timezone.utc)
    now_epoch = ref_now.timestamp()
    c_dir = cache_dir or _default_cache_dir()
    cache_path = c_dir / "macro.json"

    if not force_refresh:
        cached = _read_json_cache(cache_path)
        if cached and (now_epoch - float(cached.get("fetched_at_epoch") or 0.0)) < ttl_seconds:
            return MacroIntelCard.from_dict(cached, from_cache=True)

    articles = fetch_macro_articles(session=session, now_utc=ref_now)
    as_of = ref_now.astimezone(TAIPEI_TZ).isoformat(timespec="seconds")

    if not articles:
        card = MacroIntelCard(as_of=as_of, mode="empty", fetched_at_epoch=now_epoch)
        _write_json_cache(cache_path, card.to_dict())
        return card

    if is_news_intel_ai_enabled(enable_ai):
        try:
            from services.ai_service import call_google_ai_structured

            prompt = _build_macro_distill_prompt(articles)
            _pace_ai_call()
            resp = call_google_ai_structured(
                prompt,
                response_schema=MacroIntelAIOutput,
                temperature=0.1,
                system_instruction=_MACRO_SYSTEM_INSTRUCTION,
                tag="news_intel_macro",
                task_type="NEWS_INTEL_MACRO",
                stock_id="MACRO",
                log_prompts=log_prompts,
            )
            parsed = _parse_json_safe(resp.text)
            validated = MacroIntelAIOutput.model_validate(parsed)
            card = MacroIntelCard(
                as_of=as_of,
                mode="ai",
                model_version=resp.model_version,
                core_drivers=validated.core_drivers[:4],
                unresolved_risks=validated.unresolved_risks[:3],
                sector_impacts=validated.sector_impacts[:3],
                raw_articles=articles,
                fetched_at_epoch=now_epoch,
                from_cache=False,
            )
            _write_json_cache(cache_path, card.to_dict())
            return card
        except Exception as err:  # noqa: BLE001
            logger.warning("Free-tier AI macro distillation failed (degrading to Python excerpts): %s", err)

    # Zero-cost graceful fallback: Python-cleaned top excerpts (NEVER calls Vertex AI)
    card = MacroIntelCard(
        as_of=as_of,
        mode="fallback_excerpt",
        core_drivers=[_fallback_excerpt_bullet(a, full_date=False, max_chars=140) for a in articles[:5]],
        raw_articles=articles,
        fetched_at_epoch=now_epoch,
        from_cache=False,
    )
    _write_json_cache(cache_path, card.to_dict())
    return card


def get_stock_intel(
    stock_id: str,
    stock_name: str = "",
    *,
    force_refresh: bool = False,
    enable_ai: bool | None = None,
    log_prompts: bool | None = None,
    cache_dir: Path | None = None,
    recent_ttl_seconds: int = STOCK_RECENT_CACHE_TTL_SECONDS,
    outlook_ttl_seconds: int = STOCK_OUTLOOK_CACHE_TTL_SECONDS,
    session: Any | None = None,
    now_utc: datetime | None = None,
) -> StockIntelCard:
    """Get cached or freshly distilled StockIntelCard with differentiated 6h/7d TTLs."""
    sid = str(stock_id).strip()
    ref_now = now_utc or datetime.now(timezone.utc)
    now_epoch = ref_now.timestamp()
    c_dir = cache_dir or _default_cache_dir()
    cache_path = c_dir / f"stock_{sid}.json"

    cached = None if force_refresh else _read_json_cache(cache_path)
    if cached:
        recent_age = now_epoch - float(cached.get("recent_fetched_at_epoch") or 0.0)
        outlook_age = now_epoch - float(cached.get("outlook_fetched_at_epoch") or 0.0)
        if recent_age < recent_ttl_seconds and outlook_age < outlook_ttl_seconds:
            card = StockIntelCard.from_dict(cached, from_cache=True)
            if stock_name and not card.stock_name:
                card.stock_name = stock_name
            return card

    # Determine if 7d outlook cache is still valid (unless force_refresh or early-invalidated)
    prev_outlook_ts = float((cached or {}).get("outlook_fetched_at_epoch") or 0.0)
    outlook_still_fresh = (
        cached is not None
        and prev_outlook_ts > 0
        and (now_epoch - prev_outlook_ts) < outlook_ttl_seconds
    )

    bundle = fetch_stock_news_bundle(
        sid,
        stock_name=stock_name,
        session=session,
        now_utc=ref_now,
    )

    # Event-driven early invalidation of 7d outlook cache if any brand-new article
    # mentions earnings call / guidance / target price after previous outlook fetch
    new_guidance_trigger = any(
        float(a.get("pub_ts") or 0.0) > prev_outlook_ts
        and _EARLY_OUTLOOK_INVALIDATION_RE.search(str(a.get("title") or ""))
        for a in (bundle["recent_articles"] + bundle["factset_articles"])
    )
    reuse_prev_outlook = outlook_still_fresh and not new_guidance_trigger

    as_of = ref_now.astimezone(TAIPEI_TZ).isoformat(timespec="seconds")
    raw_recent = bundle["factset_articles"] + bundle["recent_articles"]
    raw_outlook = (
        list((cached or {}).get("raw_outlook_articles") or [])
        if reuse_prev_outlook
        else list(bundle["outlook_articles"])
    )

    has_any_articles = bool(raw_recent or raw_outlook)
    if not has_any_articles:
        card = StockIntelCard(
            stock_id=sid,
            stock_name=stock_name,
            as_of=as_of,
            mode="empty",
            calendar_events=bundle["calendar_events"],
            recent_fetched_at_epoch=now_epoch,
            outlook_fetched_at_epoch=prev_outlook_ts if reuse_prev_outlook else now_epoch,
            from_cache=False,
        )
        _write_json_cache(cache_path, card.to_dict())
        return card

    if is_news_intel_ai_enabled(enable_ai):
        try:
            from services.ai_service import call_google_ai_structured

            prompt_bundle = {
                "factset_articles": bundle["factset_articles"],
                "recent_articles": bundle["recent_articles"],
                "outlook_articles": [] if reuse_prev_outlook else bundle["outlook_articles"],
            }
            prompt = _build_stock_distill_prompt(sid, stock_name, prompt_bundle)
            _pace_ai_call()
            resp = call_google_ai_structured(
                prompt,
                response_schema=StockIntelAIOutput,
                temperature=0.1,
                system_instruction=_STOCK_SYSTEM_INSTRUCTION,
                tag=f"news_intel_stock_{sid}",
                task_type="NEWS_INTEL_STOCK",
                stock_id=sid,
                log_prompts=log_prompts,
            )
            parsed = _parse_json_safe(resp.text)
            validated = StockIntelAIOutput.model_validate(parsed)

            if reuse_prev_outlook and cached is not None:
                outlook_claims = [str(x) for x in (cached.get("outlook_claims") or []) if str(x).strip()]
                outlook_has_substance = bool(cached.get("outlook_has_substance", False))
                outlook_ts = prev_outlook_ts
            else:
                outlook_claims = validated.outlook_claims[:4]
                outlook_has_substance = bool(validated.outlook_has_substance and outlook_claims)
                outlook_ts = now_epoch

            card = StockIntelCard(
                stock_id=sid,
                stock_name=stock_name,
                as_of=as_of,
                mode="ai",
                model_version=resp.model_version,
                calendar_events=bundle["calendar_events"],
                factset_consensus=validated.factset_consensus[:2],
                recent_catalysts=validated.recent_catalysts[:3],
                risk_flags=validated.risk_flags[:3],
                outlook_claims=outlook_claims,
                outlook_has_substance=outlook_has_substance,
                raw_recent_articles=raw_recent,
                raw_outlook_articles=raw_outlook,
                recent_fetched_at_epoch=now_epoch,
                outlook_fetched_at_epoch=outlook_ts,
                from_cache=False,
            )
            _write_json_cache(cache_path, card.to_dict())
            return card
        except Exception as err:  # noqa: BLE001
            logger.warning("Free-tier AI stock distillation failed for %s (degrading to Python excerpts): %s", sid, err)

    # Zero-cost graceful fallback: Python-cleaned excerpts (NEVER calls Vertex AI)
    fallback_factset = [_fallback_excerpt_bullet(a, max_chars=150) for a in bundle["factset_articles"][:2]]
    fallback_recent = [_fallback_excerpt_bullet(a, max_chars=160) for a in bundle["recent_articles"][:3]]
    fallback_outlook = (
        [str(x) for x in ((cached or {}).get("outlook_claims") or [])]
        if reuse_prev_outlook
        else [_fallback_excerpt_bullet(a, full_date=True, max_chars=180) for a in bundle["outlook_articles"][:4]]
    )
    card = StockIntelCard(
        stock_id=sid,
        stock_name=stock_name,
        as_of=as_of,
        mode="fallback_excerpt",
        calendar_events=bundle["calendar_events"],
        factset_consensus=fallback_factset,
        recent_catalysts=fallback_recent,
        risk_flags=[],
        outlook_claims=fallback_outlook,
        outlook_has_substance=bool(fallback_outlook),
        raw_recent_articles=raw_recent,
        raw_outlook_articles=raw_outlook,
        recent_fetched_at_epoch=now_epoch,
        outlook_fetched_at_epoch=prev_outlook_ts if reuse_prev_outlook else now_epoch,
        from_cache=False,
    )
    _write_json_cache(cache_path, card.to_dict())
    return card


# --------------------------------------------------------------------------- #
# View Adapters (4 Consumer-Specific Prompt Renderers)
# --------------------------------------------------------------------------- #
def render_macro_prompt_block(card: MacroIntelCard) -> str:
    """Render concise, high-density international macro news block for macro_context.py."""
    if card.mode == "empty" or not (card.core_drivers or card.unresolved_risks or card.sector_impacts):
        return "- 近 48 小時國際財經與產業事實摘要：無重大異常新聞"

    tag = "AI 事實整理" if card.mode == "ai" else "原文清洗摘錄"
    lines = [f"- 近 48 小時國際財經與產業事實摘要（{tag}）："]
    if card.core_drivers:
        lines.append("  * 【核心事件主因】")
        for item in card.core_drivers:
            lines.append(f"    - {item}")
    if card.unresolved_risks:
        lines.append("  * 【尚未解除風險／變數】")
        for item in card.unresolved_risks:
            lines.append(f"    - {item}")
    if card.sector_impacts:
        lines.append("  * 【產業與資金傳導】")
        for item in card.sector_impacts:
            lines.append(f"    - {item}")
    return "\n".join(lines)


def render_stage1_stock_block(card: StockIntelCard) -> str:
    """Render Stage 1 single-stock audit block (feeds Step 3A/3B outlook verification)."""
    tag = "免費輕量 AI 純事實整理" if card.mode == "ai" else "原文清洗摘錄"
    lines: list[str] = [f"\n■ 近期法說展望、機構預估與實質新聞事實卡（{tag}）"]
    lines.append("以下內容均自新聞完整正文萃取並附 [日期｜來源]；請只引用本區事實，不得自行補充未記載展望。")

    if card.calendar_events:
        cal_parts = [f"{e.get('date', '')} {e.get('event', '')}".strip() for e in card.calendar_events]
        lines.append(f"- 📅 近期法說／重大事件日曆：{'；'.join(cal_parts)}")

    if card.factset_consensus:
        lines.append("- 📊 FactSet／機構預估共識：")
        for item in card.factset_consensus:
            lines.append(f"  * {item}")

    lines.append("- 🎙️ 近 180 天法說會與管理層展望實據（供 Step 3A/3B 驗證）：")
    if card.outlook_has_substance and card.outlook_claims:
        for item in card.outlook_claims:
            lines.append(f"  * {item}")
    else:
        lines.append("  * （近 180 天無具體管理層量化展望或法說實質內文，Step 3A 評級請維持「無資料」）")

    lines.append("- 📰 近 30 天實質新聞催化劑與風險：")
    if card.recent_catalysts or card.risk_flags:
        for item in card.recent_catalysts:
            lines.append(f"  * [催化] {item}")
        for item in card.risk_flags:
            lines.append(f"  * [風險] {item}")
    else:
        lines.append("  * （近 30 天無實質基本面催化劑或重大異常新聞）")

    return "\n".join(lines)


def render_cross_stock_block(
    card: StockIntelCard,
    *,
    aligned_reactions: list[Mapping[str, Any]] | None = None,
) -> str:
    """Render compact news & reaction block for AI #2 Quality Select / Red-Blue Audit / AI #3."""
    parts: list[str] = []
    if card.calendar_events:
        cal_str = "；".join(f"{e.get('date', '')} {e.get('event', '')}".strip() for e in card.calendar_events[:2])
        parts.append(f"  - 📅 事件日曆：{cal_str}")
    for item in card.factset_consensus[:2]:
        parts.append(f"  - 📊 機構預估：{item}")
    for item in card.recent_catalysts[:2]:
        parts.append(f"  - 📰 催化劑：{item}")
    for item in card.risk_flags[:2]:
        parts.append(f"  - ⚠️ 風險提示：{item}")
    if aligned_reactions:
        for r in aligned_reactions[:3]:
            parts.append(
                f"  - [{str(r.get('pub_date', ''))[:10]}] {r.get('title', '')} ➔ {r.get('reaction_tag', '')}"
            )
    if not parts:
        return "  - 近期無實質重大新聞催化劑（免過度解讀消息面）"
    return "\n".join(parts)


def render_morning_brief_stock_block(
    card: StockIntelCard,
    *,
    aligned_reactions: list[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Render concise high-signal bullets for 08:15 morning-brief portfolio holdings."""
    lines: list[str] = []
    if card.calendar_events:
        cal_str = "；".join(f"{e.get('date', '')} {e.get('event', '')}".strip() for e in card.calendar_events[:2])
        lines.append(f"📅 近期事件日曆：{cal_str}")
    for item in card.factset_consensus[:1]:
        lines.append(f"📊 機構預估：{item}")
    for item in card.recent_catalysts[:2]:
        lines.append(f"⚡ 近期催化：{item}")
    for item in card.risk_flags[:2]:
        lines.append(f"⚠️ 風險紅旗：{item}")
    if aligned_reactions:
        for r in aligned_reactions[:2]:
            tag = str(r.get("reaction_tag") or "")
            # Only append reaction line if non-neutral or no distilled catalysts exist
            if ("中性" not in tag) or not (card.recent_catalysts or card.factset_consensus):
                lines.append(f"[{str(r.get('pub_date', ''))[:10]}] {r.get('title', '')} ➔ {tag}")
    if not lines:
        lines.append("近期無實質基本面新聞催化劑（僅例行公告，已自動減噪）")
    return lines


# --------------------------------------------------------------------------- #
# Standalone MVP CLI (For Direct Verification on GCP VM)
# --------------------------------------------------------------------------- #
def _parse_stock_specs(raw: str) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    for token in (raw or "").split(","):
        tok = token.strip()
        if not tok:
            continue
        if ":" in tok:
            sid, sname = tok.split(":", 1)
            specs.append((sid.strip(), sname.strip()))
        else:
            specs.append((tok, ""))
    return specs


def main(argv: list[str] | None = None) -> int:
    # Load .env if python-dotenv is available (matches project entrypoints)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(
        description="Unified News Intelligence Service MVP CLI (crawler + free-tier Gemini fact distillation)"
    )
    parser.add_argument("--macro", action="store_true", help="Fetch and distill global macro / US stock news.")
    parser.add_argument(
        "--stocks",
        type=str,
        default="",
        help="Comma-separated stock IDs (optionally id:name), e.g. 2006:東和鋼鐵,3034:聯詠,3293:鈊象,6526:達發",
    )
    parser.add_argument("--force-refresh", action="store_true", help="Bypass local TTL cache and re-crawl/re-distill.")
    parser.add_argument("--no-ai", action="store_true", help="Disable AI distillation and test pure Python crawler/cleaning.")
    parser.add_argument("--no-log-prompts", action="store_true", help="Do not write this run to ai_prompt_logs.")
    parser.add_argument(
        "--view",
        choices=("all", "stage1", "cross", "morning"),
        default="all",
        help="Which View Adapter output(s) to preview for stocks.",
    )
    parser.add_argument("--json", action="store_true", help="Print raw Fact Card JSON output.")
    args = parser.parse_args(argv)

    enable_ai = False if args.no_ai else None
    log_prompts = False if args.no_log_prompts else None

    if not args.macro and not args.stocks:
        args.macro = True
        args.stocks = "2006:東和鋼鐵,3034:聯詠,3293:鈊象,6526:達發"

    output_bundle: dict[str, Any] = {}

    if args.macro:
        t0 = time.perf_counter()
        macro_card = get_macro_intel(
            force_refresh=args.force_refresh,
            enable_ai=enable_ai,
            log_prompts=log_prompts,
        )
        elapsed = time.perf_counter() - t0
        output_bundle["macro"] = macro_card.to_dict()
        if not args.json:
            print("=" * 72)
            print(
                f"[MACRO] mode={macro_card.mode} model={macro_card.model_version or '-'} "
                f"from_cache={macro_card.from_cache} articles={len(macro_card.raw_articles)} elapsed={elapsed:.2f}s"
            )
            print("=" * 72)
            print(render_macro_prompt_block(macro_card))
            print()

    for sid, sname in _parse_stock_specs(args.stocks):
        t0 = time.perf_counter()
        card = get_stock_intel(
            sid,
            sname,
            force_refresh=args.force_refresh,
            enable_ai=enable_ai,
            log_prompts=log_prompts,
        )
        elapsed = time.perf_counter() - t0
        output_bundle[sid] = card.to_dict()
        if not args.json:
            print("=" * 72)
            print(
                f"[STOCK {sid} {sname}] mode={card.mode} model={card.model_version or '-'} "
                f"from_cache={card.from_cache} recent={len(card.raw_recent_articles)} "
                f"outlook={len(card.raw_outlook_articles)} cal={len(card.calendar_events)} elapsed={elapsed:.2f}s"
            )
            print("=" * 72)
            if args.view in ("all", "stage1"):
                print("--- [View: Stage 1 單股審計 / /stock_ai] ---")
                print(render_stage1_stock_block(card))
                print()
            if args.view in ("all", "cross"):
                print("--- [View: AI #2 跨股 / 紅藍軍 / AI #3] ---")
                print(render_cross_stock_block(card))
                print()
            if args.view in ("all", "morning"):
                print("--- [View: 08:15 盤前晨會] ---")
                for line in render_morning_brief_stock_block(card):
                    print(f"  - {line}")
                print()

    if args.json:
        print(json.dumps(output_bundle, ensure_ascii=False, indent=2))

    # Flush background BigQuery prompt archive threads cleanly before exit
    try:
        from services.ai_service import flush_ai_archive_threads

        flush_ai_archive_threads(timeout=10.0)
    except Exception:  # noqa: BLE001
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
