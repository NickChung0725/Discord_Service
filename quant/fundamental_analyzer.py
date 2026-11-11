"""Fundamental analyzer for /stock_ai.

Responsibilities:
- Slice sliding windows for AI and charts
- Build markdown context from cached Sheets data (using in-memory cache)
- Call Gemini via ai_service.ask_gemini_with_meta / ask_gemini_diagnosis_with_meta
- Render Plotly war-room HTML and publish to GitHub Pages under /charts
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import difflib
import html
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import sys

_ROOT = Path(__file__).resolve().parent.parent
for _sub in (_ROOT, _ROOT / "services", _ROOT / "quant", _ROOT / "taishin"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

logger = logging.getLogger(__name__)

from ai_service import ask_gemini, ask_gemini_diagnosis_with_meta, ask_gemini_with_meta
from finmind_stock_scanner import (
    MOVING_AVERAGE_PERIODS,
    _extract_latest_volume_metrics,
    classify_latest_signal,
    describe_ma17_trend,
)
from github_uploader import upload_to_github
from fundamental_cache import FundamentalCacheManager
from fundamental_config import (
    DEFAULT_FUNDAMENTAL_ENGINE_CONFIG,
    FundamentalEngineConfig,
    PROMPT_TEMPLATES,
)


TAIPEI_TIMEZONE = timezone(timedelta(hours=8))
_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "generated_charts"
_DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent.parent / "finmind_reports"

# --------------------------------------------------------------------------- #
# SASD v4.0 Stage 0：Google News RSS 雜訊清洗（純 Python、0 Token）
# --------------------------------------------------------------------------- #
NOISE_TITLE_PATTERNS = [
    "股市爆料同學會",
    "今日股價與討論",
    "盤中焦點",
    "即時新聞】",
    "亮燈漲停",
    "股價走強",
    "股價上攻",
    "早盤",
]
RSS_MAX_AGE_DAYS = 180
RSS_TITLE_SUMMARY_DUP_RATIO = 0.85
RSS_NO_CONTENT_NOTE = "（僅有媒體標題，無公開具體展望數據，嚴禁據此推論）"

# Stage 0 硬性排雷門檻（/stock_ai 軟性警示；排程選股硬性攔截）
HARD_MIN_VOLUME_LOTS = 300
HARD_MIN_CONTRACT_LIAB_YI = 1.0


def _rss_item_too_old(item: dict, *, now: datetime | None = None) -> bool:
    raw = str(item.get("pubDate", "") or "").strip()
    if not raw:
        return False
    try:
        published = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return (reference - published) > timedelta(days=RSS_MAX_AGE_DAYS)


def filter_and_clean_rss(
    news_items: list[dict],
    is_guidance: bool = False,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """清洗 RSS 新聞，回傳最多 5 筆。

    1. 剔除 CMoney 盤中自動生成的看圖說故事廢文與討論區連結（NOISE_TITLE_PATTERNS）。
    2. 剔除發布時間超過 180 天之過期舊聞。
    3. 摘要為空、或與標題重複度 > 85%（如富果空殼摘要）時，強制標註無內文，防止 AI 腦補。
       （is_guidance=True 時，摘要為空或與標題完全相同亦視為空殼。）
    摘要欄位優先使用 `summary`，其次為 RSS 解析出的 `description`。
    """
    valid_items: list[dict] = []
    for original in news_items:
        item = dict(original)
        title = str(item.get("title", "") or "")
        if any(pat in title for pat in NOISE_TITLE_PATTERNS):
            continue
        if _rss_item_too_old(item, now=now):
            continue

        key = "summary" if "summary" in item else "description"
        summary = str(item.get(key, "") or "")
        is_shell = False
        if is_guidance and (not summary.strip() or summary.strip() == title.strip()):
            is_shell = True
        elif (
            summary.strip()
            and difflib.SequenceMatcher(None, title.strip(), summary.strip()).ratio()
            > RSS_TITLE_SUMMARY_DUP_RATIO
        ):
            is_shell = True
        if is_shell:
            item[key] = RSS_NO_CONTENT_NOTE
        valid_items.append(item)
    return valid_items[:5]


def build_hard_warnings(metrics: dict) -> list[str]:
    """Stage 0 硬傷判斷（共用）：回傳觸發的警示文字清單；資料缺漏者不判定。

    metrics 鍵：vol_lots、op_margin_pct、contract_liab（億）、three_inst_net_5d_lots、margin_chg_5d_lots、margin_utilization_pct、est_margin_maint_pct。
    """
    warnings: list[str] = []
    vol = metrics.get("vol_lots")
    if vol is not None and vol < HARD_MIN_VOLUME_LOTS:
        warnings.append(f"最新日成交量 {vol:,.0f} 張 < {HARD_MIN_VOLUME_LOTS} 張（流動性陷阱）")
    op_margin = metrics.get("op_margin_pct")
    contract_liab = metrics.get("contract_liab")
    if op_margin is not None and op_margin < 0 and (
        contract_liab is None or contract_liab < HARD_MIN_CONTRACT_LIAB_YI
    ):
        cl_text = "N/A" if contract_liab is None else f"{contract_liab:,.2f} 億"
        warnings.append(
            f"最近一季本業虧損（營益率 {op_margin:.2f}%）且合約負債 {cl_text} < {HARD_MIN_CONTRACT_LIAB_YI:g} 億，無訂單保護"
        )
    # v5.5.0 籌碼與融資斷頭風控硬傷
    inst_5d = metrics.get("three_inst_net_5d_lots")
    m_chg_5d = metrics.get("margin_chg_5d_lots")
    m_util = metrics.get("margin_utilization_pct")
    if (
        inst_5d is not None
        and inst_5d < -500
        and m_chg_5d is not None
        and m_chg_5d > 500
        and m_util is not None
        and m_util >= 30.0
    ):
        warnings.append(
            f"籌碼背離硬傷：近5日三大法人賣超 {abs(inst_5d):,.0f} 張但融資逆勢大增 {m_chg_5d:,.0f} 張（使用率 {m_util:.1f}%），屬主力倒貨散戶接刀結構"
        )

    est_mmr = metrics.get("est_margin_maint_pct")
    if (
        est_mmr is not None
        and 130.0 <= est_mmr <= 142.0
        and m_chg_5d is not None
        and m_chg_5d > 0
    ):
        warnings.append(
            f"斷頭踩踏警戒：估算融資維持率僅 {est_mmr:.1f}% 逼近 130% 追繳線且融資尚未退場，極易引發多殺多斷頭賣壓"
        )
    return warnings


# --------------------------------------------------------------------------- #
# SASD v4.0 Stage 1 SUMMARY_JSON 強制防呆（Sanitizer）
# --------------------------------------------------------------------------- #
_NO_GUIDANCE_CLAIMS = {"", "無", "无", "無資料", "None", "none", "N/A", "null"}
_BAD_QUARTER_RE = re.compile(r"(\d{4})\s*Q(0?[5-9]|1[0-2])(?!\d)")
_MAX_LIST_ITEMS = 4
_TREND_TAG_KEYS = ("margin_trend", "rev_momentum", "fcf_nature")
_TRADE_PLAN_KEYS = (
    "tactical_type",
    "entry_trigger",
    "target_price_1",
    "target_price_2",
    "stop_loss_price",
    "invalidation",
)
_PRICE_RE = re.compile(r"\d[\d,]*\.?\d*")
_NON_PRICE_TOKEN_RE = re.compile(
    r"(?:\b\d+\s*MA\b|[+-]?\d+(?:\.\d+)?\s*%|\b\d+(?:\.\d+)?\s*[日天季月年倍檔xX])",
    flags=re.IGNORECASE,
)
_EXEMPT_TARGET_KEYWORDS = ("超漲", "不設", "重估", "觀望", "N/A", "NA", "無")


def _fix_bad_quarters(value: object) -> object:
    """把模型誤將月份寫成季度（如 2026Q9）校正為 2026-09。"""
    if isinstance(value, str):
        return _BAD_QUARTER_RE.sub(lambda m: f"{m.group(1)}-{int(m.group(2)):02d}", value)
    if isinstance(value, list):
        return [_fix_bad_quarters(v) for v in value]
    if isinstance(value, dict):
        return {k: _fix_bad_quarters(v) for k, v in value.items()}
    return value


def _first_price(text: object) -> float | None:
    raw = str(text or "").strip()
    if not raw or raw.upper().startswith("N/A"):
        return None
    cleaned = _NON_PRICE_TOKEN_RE.sub(" ", raw)
    match = _PRICE_RE.search(cleaned)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def check_revenue_staleness(
    rev_ym: object,
    *,
    reference_date: date | None = None,
    max_stale_days: int = 65,
) -> tuple[bool, str]:
    """檢查月營收期別 (YYYY-MM) 是否落後基準日超過 max_stale_days (預設 65 天)。

    回傳 (is_stale, warning_message)。若格式無法解析或未過期則回傳 (False, "")。
    """
    ym_str = str(rev_ym or "").strip()
    match = re.match(r"^(\d{4})[-/](\d{1,2})$", ym_str)
    if not match:
        return False, ""
    year, month = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12):
        return False, ""
    # 以該月 28 日近似月底，對比基準日（台股月營收於次月 10 日前公佈，落後 >65 天代表至少缺漏兩個月以上）
    rev_approx_end = date(year, month, 28)
    ref = reference_date or datetime.now(TAIPEI_TIMEZONE).date()
    lag_days = (ref - rev_approx_end).days
    if lag_days > max_stale_days:
        return (
            True,
            f"⚠️ 資料缺漏/過期（最新月營收停留在 {year:04d}-{month:02d}，落後基準日 {lag_days} 天 > {max_stale_days} 天）",
        )
    return False, ""


def sanitize_stage1_json(summary_json: dict, latest_close: float | None = None) -> dict:
    """解析 SUMMARY_JSON 後的硬校正，避免小模型違規輸出污染快取。

    - 法說無實質內容 → claims="無"、verdict="無資料"。
    - highlights/risks 至少 1 筆、至多 4 筆。
    - 補齊 trend_tags / trade_plan 結構；目標價不高於最新收盤價時加註警示（惟「🚫 觀望避開」或明確標註「超漲不設目標價/待拉回重估/N/A」者豁免，解除超漲股防呆死鎖）。
    - 期別防呆：2026Q9 → 2026-09。
    """
    summary_json = _fix_bad_quarters(summary_json)  # type: ignore[assignment]

    guidance = summary_json.get("guidance")
    guidance = dict(guidance) if isinstance(guidance, dict) else {}
    claims = str(guidance.get("claims", "")).strip()
    if claims in _NO_GUIDANCE_CLAIMS or "無公開" in claims or len(claims) < 6:
        guidance["claims"] = "無"
        guidance["verdict"] = "無資料"
    summary_json["guidance"] = guidance

    highlights = summary_json.get("highlights")
    if not isinstance(highlights, list) or not highlights:
        highlights = ["無顯著基本面亮點"]
    summary_json["highlights"] = highlights[:_MAX_LIST_ITEMS]
    risks = summary_json.get("risks")
    if not isinstance(risks, list) or not risks:
        risks = ["需留意大盤系統性波動風險"]
    summary_json["risks"] = risks[:_MAX_LIST_ITEMS]

    tags = summary_json.get("trend_tags")
    tags = dict(tags) if isinstance(tags, dict) else {}
    for key in _TREND_TAG_KEYS:
        tags.setdefault(key, "")
    summary_json["trend_tags"] = tags

    plan = summary_json.get("trade_plan")
    plan = dict(plan) if isinstance(plan, dict) else {}
    for key in _TRADE_PLAN_KEYS:
        plan.setdefault(key, "")
    tactical_type = str(plan.get("tactical_type", "")).strip()
    is_avoid_tactic = "觀望避開" in tactical_type or "觀望" in tactical_type
    if latest_close and latest_close > 0 and not is_avoid_tactic:
        for key in ("target_price_1", "target_price_2"):
            text = str(plan.get(key, "")).strip()
            if any(kw in text for kw in _EXEMPT_TARGET_KEYWORDS):
                continue
            price = _first_price(text)
            if price is not None and price <= latest_close and not text.startswith("⚠️"):
                plan[key] = f"⚠️目標價未高於最新收盤 {latest_close:,.2f}，視為無效｜{text}"
    summary_json["trade_plan"] = plan
    return summary_json


def _to_float(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


# --------------------------------------------------------------------------- #
# SASD v5.4.0 Phase 2：無盲點基本面特徵工程（股數還原、春節豁免營收、前瞻EPS、三率語意）
# --------------------------------------------------------------------------- #
class _AutoDictResult(dict):
    """同時支援 dict 取值、屬性存取 (.status/.mode/.tag) 與字串包含/相等比對之結果容器。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __contains__(self, item: object) -> bool:
        if super().__contains__(item):
            return True
        if isinstance(item, str):
            for key in ("status", "mode", "tag", "semantic_tag", "label", "warning_tag", "qtd_tag", "summary_line"):
                val = self.get(key)
                if val is not None and (item == val or item in str(val)):
                    return True
        return item in self.values()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return other in (
                self.get("status"),
                self.get("tag"),
                self.get("mode"),
                self.get("semantic_tag"),
            )
        return super().__eq__(other)


def _normalize_statements_newest_first(
    quarterly_statements: list[dict[str, Any]] | pd.DataFrame | None,
) -> list[dict[str, Any]]:
    if quarterly_statements is None:
        return []
    if isinstance(quarterly_statements, pd.DataFrame):
        if quarterly_statements.empty:
            return []
        df = quarterly_statements.copy()
        for sort_col in ("raw_date", "date", "year_qtr"):
            if sort_col in df.columns:
                df = df.sort_values(sort_col, ascending=False)
                break
        return df.to_dict("records")
    records = [dict(r) for r in quarterly_statements if isinstance(r, Mapping)]
    if len(records) >= 2:
        first_dt = str(records[0].get("date") or records[0].get("year_qtr") or records[0].get("raw_date") or "").strip()
        last_dt = str(records[-1].get("date") or records[-1].get("year_qtr") or records[-1].get("raw_date") or "").strip()
        if first_dt and last_dt and first_dt < last_dt:
            records = list(reversed(records))
    return records


def _extract_quarter_eps_and_ni(q_data: Mapping[str, Any]) -> tuple[float, float]:
    raw_eps = (
        q_data.get("EPS")
        if "EPS" in q_data
        else q_data.get("eps", q_data.get("BasicEarningsPerShare", 0.0))
    )
    raw_ni = (
        q_data.get("IncomeAfterTaxes")
        if "IncomeAfterTaxes" in q_data
        else q_data.get(
            "net_income_parent",
            q_data.get("net_income_total", q_data.get("NetIncome", 0.0)),
        )
    )
    eps_val = _to_float(raw_eps) or 0.0
    ni_val = _to_float(raw_ni) or 0.0
    return eps_val, ni_val


def _infer_safe_weighted_shares(
    quarterly_statements: list[dict[str, Any]] | pd.DataFrame,
    fallback_market_cap: float | None = None,
    latest_close_price: float | None = None,
    *,
    config: FundamentalEngineConfig = DEFAULT_FUNDAMENTAL_ENGINE_CONFIG,
) -> tuple[float, str]:
    """三層防呆真實流通股數還原（3-Tier Safe Share Count Inference）。

    回傳 (推算之真實流通在外股數, 推算來源標籤)
    quarterly_statements 需按季度由新到舊排序，包含 'IncomeAfterTaxes' (元) 與 'EPS' (元)
    """
    statements = _normalize_statements_newest_first(quarterly_statements)
    min_eps = config.MIN_SAFE_EPS_FOR_SHARE_INFERENCE
    max_lookback = config.MAX_HISTORICAL_QUARTERS_LOOKBACK

    # Tier 1: 檢查最新一季 |EPS| 是否 >= 0.25 元 (確保四捨五入誤差 < 2%)
    if statements:
        latest = statements[0]
        latest_eps, latest_net_income = _extract_quarter_eps_and_ni(latest)
        if abs(latest_eps) >= min_eps and abs(latest_net_income) > 0:
            shares = abs(latest_net_income / latest_eps)
            return shares, "TIER1_LATEST_QUARTER_EXACT"

    # Tier 2: 若最新季為微利或損益兩平 (|EPS| < 0.25)，向過去最多 8 季回溯最近一個 |EPS| >= 0.25 的季度
    for q_data in statements[1:max_lookback]:
        q_eps, q_net_income = _extract_quarter_eps_and_ni(q_data)
        if abs(q_eps) >= min_eps and abs(q_net_income) > 0:
            shares = abs(q_net_income / q_eps)
            q_label = (
                q_data.get("date")
                or q_data.get("year_qtr")
                or q_data.get("raw_date")
                or "HIST"
            )
            return shares, f"TIER2_LOOKBACK_{q_label}"

    # Tier 3: 若過去 8 季皆處於微利或研發期，使用 市值 / 最新收盤價 作為安全保底
    if fallback_market_cap and latest_close_price and latest_close_price > 0:
        shares = float(fallback_market_cap) / float(latest_close_price)
        return shares, "TIER3_MARKET_CAP_FALLBACK"

    raise ValueError("無法安全推算真實流通股數：財報無 |EPS| >= 0.25 之季度且無市值保底資料")


def _extract_rev_ym(rec: Mapping[str, Any]) -> tuple[int | None, int | None]:
    for ym_key in ("rev_ym", "year_month", "ym"):
        val = str(rec.get(ym_key, "") or "").strip()
        m = re.match(r"^(\d{4})[-/](\d{1,2})$", val)
        if m:
            return int(m.group(1)), int(m.group(2))
    y_val = _to_float(rec.get("revenue_year", rec.get("year")))
    m_val = _to_float(rec.get("revenue_month", rec.get("month")))
    if y_val is not None and m_val is not None:
        return int(y_val), int(m_val)
    if m_val is not None:
        return None, int(m_val)
    for dt_key in ("date", "raw_date"):
        val = str(rec.get(dt_key, "") or "").strip()
        m = re.match(r"^(\d{4})[-/](\d{1,2})", val)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def _normalize_monthly_revenues_newest_first(
    monthly_revenues: list[dict[str, Any]] | pd.DataFrame | Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if monthly_revenues is None:
        return []
    if isinstance(monthly_revenues, pd.DataFrame):
        if monthly_revenues.empty:
            return []
        records = monthly_revenues.to_dict("records")
    elif isinstance(monthly_revenues, Mapping):
        records = [dict(monthly_revenues)]
    else:
        records = [dict(r) for r in monthly_revenues if isinstance(r, Mapping)]

    if len(records) >= 2:
        y0, m0 = _extract_rev_ym(records[0])
        y_last, m_last = _extract_rev_ym(records[-1])
        if y0 is not None and m0 is not None and y_last is not None and m_last is not None:
            records = sorted(
                records,
                key=lambda r: (_extract_rev_ym(r)[0] or 0, _extract_rev_ym(r)[1] or 0),
                reverse=True,
            )
    return records


def _analyze_revenue_momentum(
    monthly_revenues: list[dict[str, Any]] | pd.DataFrame | Mapping[str, Any],
    last_q4_yoy_pct: float | None = None,
    prev_quarter_revenue: float | None = None,
    *,
    jan_feb_combined_yoy_pct: float | None = None,
    as_of: date | None = None,
    config: FundamentalEngineConfig = DEFAULT_FUNDAMENTAL_ENGINE_CONFIG,
) -> _AutoDictResult:
    """含農曆春節豁免之右側營收動能引擎与本季達成率 (QTD Run-Rate)。"""
    records = _normalize_monthly_revenues_newest_first(monthly_revenues)
    if not records:
        return _AutoDictResult(
            mode="NORMAL_MONTH_MODE",
            status="MOMENTUM_WEAK",
            momentum_status="MOMENTUM_WEAK",
            latest_rev_month=0,
            latest_rev_ym="",
            weighted_3m_yoy_pct=None,
            jan_feb_combined_yoy_pct=None,
            vs_ma3_pct=None,
            ma3_revenue=None,
            qtd_months=0,
            qtd_run_rate=None,
            qtd_run_rate_pct=None,
            qtd_tag="",
            warning_tag="",
            implied_annual_revenue=0.0,
            summary_line="無月營收資料",
            qtd_line="無本季達成進度資料",
        )

    latest = records[0]
    latest_year, latest_month = _extract_rev_ym(latest)
    if latest_month is None:
        if as_of is not None:
            latest_month = 12 if as_of.month == 1 else as_of.month - 1
            latest_year = as_of.year - 1 if as_of.month == 1 else as_of.year
        else:
            latest_month = 12
    latest_ym_str = (
        str(latest.get("rev_ym") or "").strip()
        or (f"{latest_year:04d}-{latest_month:02d}" if latest_year else f"{latest_month}月")
    )

    if last_q4_yoy_pct is None:
        for k in ("last_q4_yoy_pct", "last_q4_yoy", "q4_yoy_pct", "q4_yoy", "prev_quarter_yoy_pct"):
            if k in latest and _to_float(latest.get(k)) is not None:
                last_q4_yoy_pct = _to_float(latest.get(k))
                break

    if jan_feb_combined_yoy_pct is None:
        for k in (
            "jan_feb_combined_yoy_pct",
            "jan_feb_combined_yoy",
            "cumulative_yoy_pct",
            "combined_yoy_pct",
            "ytd_yoy_pct",
        ):
            if k in latest and _to_float(latest.get(k)) is not None:
                jan_feb_combined_yoy_pct = _to_float(latest.get(k))
                break

    revs: list[float] = [
        v for v in (_to_float(r.get("revenue")) for r in records) if v is not None and v > 0
    ]
    latest_rev = revs[0] if revs else 0.0
    revs_3m = revs[:3]
    ma3_revenue = (sum(revs_3m) / len(revs_3m)) if revs_3m else None
    vs_ma3_pct = (
        ((latest_rev / ma3_revenue) - 1.0) * 100.0
        if (ma3_revenue is not None and ma3_revenue > 0 and latest_rev > 0)
        else None
    )

    yoys_3m: list[float] = []
    for r in records[:3]:
        y_val = _to_float(r.get("yoy_pct") if "yoy_pct" in r else r.get("yoy"))
        if y_val is not None:
            yoys_3m.append(y_val)

    if len(yoys_3m) >= 3:
        weighted_3m_yoy = 0.50 * yoys_3m[0] + 0.30 * yoys_3m[1] + 0.20 * yoys_3m[2]
    elif len(yoys_3m) == 2:
        weighted_3m_yoy = (0.50 * yoys_3m[0] + 0.30 * yoys_3m[1]) / 0.80
    elif len(yoys_3m) == 1:
        weighted_3m_yoy = yoys_3m[0]
    else:
        weighted_3m_yoy = None

    latest_yoy = yoys_3m[0] if yoys_3m else None
    warning_tag = ""

    # 1. 春節月份判定 (1~2月營收)
    if latest_month in (1, 2):
        mode = "LUNAR_NEW_YEAR_EXEMPT_MODE"
        if latest_month == 2:
            if jan_feb_combined_yoy_pct is None:
                # 嘗試從列表內的 1月 與 2月 營收推算 1~2 月累計年增率
                feb_rec = records[0]
                jan_rec = next(
                    (r for r in records[1:3] if _extract_rev_ym(r)[1] == 1),
                    records[1] if len(records) >= 2 else None,
                )
                feb_rev = _to_float(feb_rec.get("revenue"))
                jan_rev = _to_float(jan_rec.get("revenue")) if jan_rec else None
                feb_yoy = _to_float(feb_rec.get("yoy_pct") if "yoy_pct" in feb_rec else feb_rec.get("yoy"))
                jan_yoy = (
                    _to_float(jan_rec.get("yoy_pct") if "yoy_pct" in jan_rec else jan_rec.get("yoy"))
                    if jan_rec
                    else None
                )
                # 若有去年 1~2 月原始營收
                ly_feb_rec = (
                    next(
                        (
                            r
                            for r in records
                            if _extract_rev_ym(r) == ((latest_year - 1), 2)
                        ),
                        None,
                    )
                    if latest_year
                    else None
                )
                ly_jan_rec = (
                    next(
                        (
                            r
                            for r in records
                            if _extract_rev_ym(r) == ((latest_year - 1), 1)
                        ),
                        None,
                    )
                    if latest_year
                    else None
                )
                if (
                    feb_rev
                    and jan_rev
                    and ly_feb_rec
                    and ly_jan_rec
                    and _to_float(ly_feb_rec.get("revenue"))
                    and _to_float(ly_jan_rec.get("revenue"))
                ):
                    ly_sum = float(_to_float(ly_feb_rec["revenue"]) or 0.0) + float(
                        _to_float(ly_jan_rec["revenue"]) or 0.0
                    )
                    if ly_sum > 0:
                        jan_feb_combined_yoy_pct = ((feb_rev + jan_rev) / ly_sum - 1.0) * 100.0
                elif (
                    feb_rev
                    and jan_rev
                    and feb_yoy is not None
                    and jan_yoy is not None
                    and (1.0 + feb_yoy / 100.0) > 0
                    and (1.0 + jan_yoy / 100.0) > 0
                ):
                    ly_feb = feb_rev / (1.0 + feb_yoy / 100.0)
                    ly_jan = jan_rev / (1.0 + jan_yoy / 100.0)
                    if (ly_feb + ly_jan) > 0:
                        jan_feb_combined_yoy_pct = ((feb_rev + jan_rev) / (ly_feb + ly_jan) - 1.0) * 100.0
                elif feb_yoy is not None and jan_yoy is not None:
                    jan_feb_combined_yoy_pct = (feb_yoy + jan_yoy) / 2.0
                else:
                    jan_feb_combined_yoy_pct = latest_yoy

            if jan_feb_combined_yoy_pct is not None and jan_feb_combined_yoy_pct > 0 and (
                last_q4_yoy_pct is None or jan_feb_combined_yoy_pct > last_q4_yoy_pct
            ):
                status = "RIGHT_SIDE_EXPANDING"
            else:
                status = "MOMENTUM_WEAK"
            q4_cmp_text = (
                f"（高於去年 Q4 YoY {last_q4_yoy_pct:+.1f}%）" if last_q4_yoy_pct is not None else ""
            )
            comb_str = f"{jan_feb_combined_yoy_pct:+.1f}%" if jan_feb_combined_yoy_pct is not None else "N/A"
            summary_line = (
                f"[{status}] 春節豁免模式 (LUNAR_NEW_YEAR_EXEMPT_MODE)："
                f"1~2 月累計營收 YoY {comb_str}{q4_cmp_text}"
            )
        else:
            # latest_month == 1
            ly_jan_feb_avg = _to_float(latest.get("last_year_jan_feb_avg_revenue"))
            if ly_jan_feb_avg is None and latest_year:
                ly_jan_rec = next(
                    (r for r in records if _extract_rev_ym(r) == (latest_year - 1, 1)),
                    None,
                )
                ly_feb_rec = next(
                    (r for r in records if _extract_rev_ym(r) == (latest_year - 1, 2)),
                    None,
                )
                ly_vals = [
                    v
                    for v in (
                        _to_float(ly_jan_rec.get("revenue")) if ly_jan_rec else None,
                        _to_float(ly_feb_rec.get("revenue")) if ly_feb_rec else None,
                    )
                    if v is not None and v > 0
                ]
                if len(ly_vals) == 2:
                    ly_jan_feb_avg = sum(ly_vals) / 2.0
            cond_yoy = (
                latest_yoy is not None
                and latest_yoy > 0
                and (last_q4_yoy_pct is None or latest_yoy > last_q4_yoy_pct)
            )
            cond_avg = (
                ly_jan_feb_avg is not None
                and ly_jan_feb_avg > 0
                and latest_rev >= ly_jan_feb_avg * 1.15
            )
            status = "RIGHT_SIDE_EXPANDING" if (cond_yoy or cond_avg) else "MOMENTUM_WEAK"
            y_str = f"{latest_yoy:+.1f}%" if latest_yoy is not None else "N/A"
            summary_line = (
                f"[{status}] 春節豁免模式 (LUNAR_NEW_YEAR_EXEMPT_MODE)：1 月營收 YoY {y_str}"
            )
    else:
        # 2. 一般月份判定 (3~12月)
        mode = "NORMAL_MONTH_MODE"
        is_6m_second_high = False
        if len(revs) >= 6 and latest_rev > 0:
            sorted_6m = sorted(revs[:6], reverse=True)
            is_6m_second_high = latest_rev >= sorted_6m[1]

        if (
            weighted_3m_yoy is not None
            and weighted_3m_yoy > 20.0
            and ma3_revenue is not None
            and ma3_revenue > 0
            and latest_rev < ma3_revenue * 0.90
        ):
            status = "HIGH_BASE_DECELERATING"
            warning_tag = "【⚠️ 營收高基期滑落警訊：YoY 雖高但 MoM 已失速見頂】"
        elif (
            weighted_3m_yoy is not None
            and weighted_3m_yoy > 0
            and ma3_revenue is not None
            and ma3_revenue > 0
            and (latest_rev >= ma3_revenue * config.NORMAL_MONTH_MA3_TOLERANCE or is_6m_second_high)
        ):
            status = "RIGHT_SIDE_EXPANDING"
        else:
            status = "MOMENTUM_WEAK"

        w_str = f"{weighted_3m_yoy:+.1f}%" if weighted_3m_yoy is not None else "N/A"
        if vs_ma3_pct is not None:
            dir_word = "高於" if vs_ma3_pct >= 0 else "低於"
            ma_part = f"，{latest_month} 月營收{dir_word}近 3 月均線 {vs_ma3_pct:+.1f}%"
        else:
            ma_part = ""
        summary_line = f"[{status}] 近 3 月加權 YoY {w_str}{ma_part}"
        if warning_tag:
            summary_line += f" {warning_tag}"

    # 3. 本季達成率 (QTD Run-Rate) 與本業年化營收推算
    q_num = (latest_month - 1) // 3 + 1
    q_start_month = (q_num - 1) * 3 + 1
    months_in_q = latest_month - q_start_month + 1
    prev_q_num = 4 if q_num == 1 else q_num - 1

    qtd_revs = revs[:months_in_q]
    qtd_revenue_sum = sum(qtd_revs) if qtd_revs else 0.0

    if prev_quarter_revenue is None and len(revs) >= months_in_q + 3:
        prev_quarter_revenue = sum(revs[months_in_q : months_in_q + 3])

    qtd_run_rate: float | None = None
    qtd_run_rate_pct: float | None = None
    qtd_tag = ""
    if prev_quarter_revenue and prev_quarter_revenue > 0 and qtd_revenue_sum > 0:
        qtd_run_rate = qtd_revenue_sum / float(prev_quarter_revenue)
        qtd_run_rate_pct = round(qtd_run_rate * 100.0, 1)
        if months_in_q == 1 and qtd_run_rate >= config.QTD_STRONG_RUN_RATE_1M:
            qtd_tag = "【🔥 本季營收進度大幅超前 (1M Run-Rate > 36%)】"
            qtd_line = (
                f"🔥 Q{q_num} 第 1 個月營收已達 Q{prev_q_num} 總營收之 {qtd_run_rate_pct:.1f}% "
                "(超越 33.3% 基準，本季確定強勢季增)"
            )
        elif months_in_q == 2 and qtd_run_rate >= config.QTD_STRONG_RUN_RATE_2M:
            qtd_tag = "【🔥 本季確定強勢季增 (2M Run-Rate > 72%)】"
            qtd_line = (
                f"🔥 Q{q_num} 前 2 個月營收已達 Q{prev_q_num} 總營收之 {qtd_run_rate_pct:.1f}% "
                "(超越 66.7% 基準，本季確定強勢季增)"
            )
        else:
            qtd_line = f"Q{q_num} 前 {months_in_q} 個月累計營收達 Q{prev_q_num} 總營收之 {qtd_run_rate_pct:.1f}%"
    else:
        qtd_line = "本季累計達成率資料不足"

    # 年化營收推算：近 2 個月平均月營收（若遇 1~2 月春節則採近 3 個月平均）* 12
    avg_window = 3 if latest_month in (1, 2) else 2
    sample_revs = revs[:avg_window]
    avg_monthly_rev = (sum(sample_revs) / len(sample_revs)) if sample_revs else 0.0
    implied_annual_revenue = avg_monthly_rev * 12.0

    return _AutoDictResult(
        mode=mode,
        status=status,
        momentum_status=status,
        latest_rev_month=latest_month,
        latest_rev_ym=latest_ym_str,
        weighted_3m_yoy_pct=round(weighted_3m_yoy, 2) if weighted_3m_yoy is not None else None,
        jan_feb_combined_yoy_pct=(
            round(jan_feb_combined_yoy_pct, 2) if jan_feb_combined_yoy_pct is not None else None
        ),
        vs_ma3_pct=round(vs_ma3_pct, 2) if vs_ma3_pct is not None else None,
        ma3_revenue=round(ma3_revenue, 2) if ma3_revenue is not None else None,
        qtd_months=months_in_q,
        qtd_run_rate=round(qtd_run_rate, 4) if qtd_run_rate is not None else None,
        qtd_run_rate_pct=qtd_run_rate_pct,
        qtd_tag=qtd_tag,
        warning_tag=warning_tag,
        implied_annual_revenue=implied_annual_revenue,
        summary_line=summary_line,
        qtd_line=qtd_line,
    )


def _extract_quarter_opm_ratio(q_data: Mapping[str, Any]) -> float | None:
    for pct_key in ("op_margin_pct", "opm_pct", "operating_margin_pct"):
        if pct_key in q_data and _to_float(q_data.get(pct_key)) is not None:
            return float(_to_float(q_data.get(pct_key)) or 0.0) / 100.0
    for key in ("opm", "operating_margin", "OperatingMargin"):
        if key in q_data and _to_float(q_data.get(key)) is not None:
            val = float(_to_float(q_data.get(key)) or 0.0)
            return val / 100.0 if abs(val) > 1.0 else val
    op_inc = _to_float(
        q_data.get("OperatingIncome", q_data.get("operating_income", q_data.get("OperatingProfit")))
    )
    rev = _to_float(q_data.get("Revenue", q_data.get("revenue", q_data.get("OperatingRevenue"))))
    if op_inc is not None and rev is not None and rev > 0:
        return op_inc / rev
    return None


def _compute_forward_valuation(
    implied_annual_revenue: float,
    quarterly_statements: list[dict[str, Any]] | pd.DataFrame | None = None,
    safe_weighted_shares: float = 0.0,
    latest_close_price: float | None = None,
    *,
    opm_avg_2q: float | None = None,
    sector_benchmark_pe: float = 20.0,
    trailing_pe: float | None = None,
    config: FundamentalEngineConfig = DEFAULT_FUNDAMENTAL_ENGINE_CONFIG,
) -> _AutoDictResult:
    """本業前瞻 EPS (Implied Core Forward EPS) 與橫斷面同產業相對估值計算。"""
    if opm_avg_2q is None:
        statements = _normalize_statements_newest_first(quarterly_statements)
        opms = [
            v
            for v in (_extract_quarter_opm_ratio(q) for q in statements[:2])
            if v is not None
        ]
        opm_ratio = (sum(opms) / len(opms)) if opms else 0.0
    else:
        opm_ratio = opm_avg_2q / 100.0 if abs(opm_avg_2q) > 1.0 else opm_avg_2q

    implied_core_forward_eps = 0.0
    if safe_weighted_shares > 0 and implied_annual_revenue > 0:
        implied_core_forward_eps = (
            float(implied_annual_revenue)
            * float(opm_ratio)
            * config.EFFECTIVE_AFTER_TAX_FACTOR
            / float(safe_weighted_shares)
        )

    implied_forward_pe: float | None = None
    sector_pe_ratio: float | None = None
    if implied_core_forward_eps > 0 and latest_close_price is not None and latest_close_price > 0:
        implied_forward_pe = float(latest_close_price) / implied_core_forward_eps
        if sector_benchmark_pe > 0:
            sector_pe_ratio = implied_forward_pe / float(sector_benchmark_pe)

    valuation_tag = ""
    if implied_forward_pe is not None and sector_benchmark_pe > 0:
        if (
            trailing_pe is not None
            and trailing_pe > 30.0
            and implied_forward_pe < sector_benchmark_pe * config.SECTOR_PE_UNDERVALUED_RATIO
        ):
            valuation_tag = "【💎 轉機成長收斂股：歷史 PE 雖高，但營收爆發帶動前瞻 PE 低於同業中位數】"
        elif implied_forward_pe < sector_benchmark_pe * config.SECTOR_PE_UNDERVALUED_RATIO:
            valuation_tag = "【💎 轉機成長收斂股：前瞻估值顯著低於同業】"
        elif implied_forward_pe > sector_benchmark_pe * config.SECTOR_PE_OVERHEATED_RATIO:
            valuation_tag = "【⚠️ 估值溢價偏高：前瞻 PE 高於同產業中位數 35% 以上，需極強籌碼支撐】"
        else:
            valuation_tag = "【➖ 前瞻估值位階合理】"

    t_pe_str = f"{trailing_pe:.1f}x" if trailing_pe is not None else "N/A"
    f_pe_str = f"{implied_forward_pe:.1f}x" if implied_forward_pe is not None else "N/A"
    s_pe_str = f"{sector_benchmark_pe:.1f}x"
    r_str = f"{sector_pe_ratio:.2f}x" if sector_pe_ratio is not None else "N/A"
    summary_line = (
        f"歷史 Trailing PE {t_pe_str} ➡️ 本業年化前瞻 Implied Forward PE 僅 {f_pe_str} "
        f"(同業中位數 PE 為 {s_pe_str}，相對比值 {r_str}){valuation_tag}"
    )

    return _AutoDictResult(
        opm_avg_2q_pct=round(opm_ratio * 100.0, 2),
        implied_core_forward_eps=round(implied_core_forward_eps, 4),
        implied_forward_pe=round(implied_forward_pe, 2) if implied_forward_pe is not None else None,
        sector_benchmark_pe=round(sector_benchmark_pe, 2),
        sector_pe_ratio=round(sector_pe_ratio, 2) if sector_pe_ratio is not None else None,
        trailing_pe=round(trailing_pe, 2) if trailing_pe is not None else None,
        tag=valuation_tag,
        valuation_tag=valuation_tag,
        summary_line=summary_line,
    )


def _extract_gpm_opm_pct(q_data: Mapping[str, Any]) -> tuple[float | None, float | None]:
    gpm = _to_float(
        q_data.get("gross_margin_pct", q_data.get("gpm_pct", q_data.get("gpm", q_data.get("GrossMargin"))))
    )
    if gpm is None:
        gp = _to_float(q_data.get("gross_profit", q_data.get("GrossProfit")))
        rev = _to_float(q_data.get("revenue", q_data.get("Revenue")))
        if gp is not None and rev is not None and rev > 0:
            gpm = (gp / rev) * 100.0

    opm = _to_float(
        q_data.get("op_margin_pct", q_data.get("opm_pct", q_data.get("opm", q_data.get("OperatingMargin"))))
    )
    if opm is None:
        op = _to_float(q_data.get("operating_income", q_data.get("OperatingIncome")))
        rev = _to_float(q_data.get("revenue", q_data.get("Revenue")))
        if op is not None and rev is not None and rev > 0:
            opm = (op / rev) * 100.0
    return gpm, opm


def _evaluate_margin_trends(
    quarterly_statements: list[dict[str, Any]] | pd.DataFrame | Mapping[str, Any] | None = None,
    revenue_yoy_pct: float | None = None,
    *,
    gpm_latest: float | None = None,
    gpm_prev_q: float | None = None,
    gpm_last_year_q: float | None = None,
    opm_latest: float | None = None,
    opm_prev_q: float | None = None,
    opm_last_year_q: float | None = None,
    gpm_qoq_diff: float | None = None,
    gpm_yoy_diff: float | None = None,
    opm_qoq_diff: float | None = None,
    opm_yoy_diff: float | None = None,
    config: FundamentalEngineConfig = DEFAULT_FUNDAMENTAL_ENGINE_CONFIG,
) -> _AutoDictResult:
    """三率擴張與收縮語意工程 (_evaluate_margin_trends)。

    歸納為 4 種確定性語意標籤之一：
    - MARGIN_TRIPLE_EXPANSION
    - OPERATING_LEVERAGE_BREAKOUT
    - FAKE_REVENUE_DILUTION
    - MARGIN_NEUTRAL
    """
    if isinstance(quarterly_statements, Mapping):
        d = quarterly_statements
        if revenue_yoy_pct is None:
            revenue_yoy_pct = _to_float(
                d.get("revenue_yoy_pct", d.get("yoy_pct", d.get("rev_yoy_pct", d.get("revenue_yoy"))))
            )
        if gpm_qoq_diff is None:
            gpm_qoq_diff = _to_float(d.get("gpm_qoq_diff", d.get("gross_margin_qoq")))
        if opm_qoq_diff is None:
            opm_qoq_diff = _to_float(d.get("opm_qoq_diff", d.get("op_margin_qoq")))
        if gpm_yoy_diff is None:
            gpm_yoy_diff = _to_float(d.get("gpm_yoy_diff", d.get("gross_margin_yoy")))
        if opm_yoy_diff is None:
            opm_yoy_diff = _to_float(d.get("opm_yoy_diff", d.get("op_margin_yoy")))
        if gpm_latest is None:
            gpm_latest, opm_latest_extracted = _extract_gpm_opm_pct(d)
            if opm_latest is None:
                opm_latest = opm_latest_extracted
    elif quarterly_statements is not None:
        statements = _normalize_statements_newest_first(quarterly_statements)
        if statements:
            latest_q = statements[0]
            g_cur, o_cur = _extract_gpm_opm_pct(latest_q)
            if gpm_latest is None:
                gpm_latest = g_cur
            if opm_latest is None:
                opm_latest = o_cur
            if revenue_yoy_pct is None:
                revenue_yoy_pct = _to_float(
                    latest_q.get("qtr_rev_yoy_pct", latest_q.get("revenue_yoy_pct", latest_q.get("yoy_pct")))
                )
            if gpm_qoq_diff is None:
                gpm_qoq_diff = _to_float(latest_q.get("gpm_qoq_diff"))
            if opm_qoq_diff is None:
                opm_qoq_diff = _to_float(latest_q.get("opm_qoq_diff"))
        if len(statements) >= 2:
            g_prev, o_prev = _extract_gpm_opm_pct(statements[1])
            if gpm_prev_q is None:
                gpm_prev_q = g_prev
            if opm_prev_q is None:
                opm_prev_q = o_prev
        if len(statements) >= 5:
            g_ly, o_ly = _extract_gpm_opm_pct(statements[4])
            if gpm_last_year_q is None:
                gpm_last_year_q = g_ly
            if opm_last_year_q is None:
                opm_last_year_q = o_ly

    if gpm_qoq_diff is None and gpm_latest is not None and gpm_prev_q is not None:
        gpm_qoq_diff = gpm_latest - gpm_prev_q
    if opm_qoq_diff is None and opm_latest is not None and opm_prev_q is not None:
        opm_qoq_diff = opm_latest - opm_prev_q
    if gpm_yoy_diff is None and gpm_latest is not None and gpm_last_year_q is not None:
        gpm_yoy_diff = gpm_latest - gpm_last_year_q
    if opm_yoy_diff is None and opm_latest is not None and opm_last_year_q is not None:
        opm_yoy_diff = opm_latest - opm_last_year_q

    g_qoq = gpm_qoq_diff if gpm_qoq_diff is not None else 0.0
    o_qoq = opm_qoq_diff if opm_qoq_diff is not None else 0.0
    g_yoy = gpm_yoy_diff
    o_yoy = opm_yoy_diff
    rev_yoy = revenue_yoy_pct if revenue_yoy_pct is not None else 0.0

    # 判定規則 (SASD v5.4.0 Section 3.3)
    if (
        rev_yoy > 15.0
        and g_qoq <= config.MARGIN_CONTRACTION_BPS_THRESHOLD
        and o_qoq <= config.MARGIN_CONTRACTION_BPS_THRESHOLD
    ):
        tag = "FAKE_REVENUE_DILUTION"
        label = "【🚨 殺價搶單警訊：營收雖成長但毛利率與營益率顯著惡化，慎防財報見光死】"
    elif (g_qoq >= 0.5 and o_qoq >= 1.0) or (
        g_yoy is not None
        and o_yoy is not None
        and g_yoy >= config.MARGIN_EXPANSION_BPS_THRESHOLD
        and o_yoy >= config.MARGIN_EXPANSION_BPS_THRESHOLD
    ):
        tag = "MARGIN_TRIPLE_EXPANSION"
        label = "【🔥 本業獲利能力擴張：毛利率與營益率同步提升，具定價權與規模經濟】"
    elif o_qoq >= 2.0 and g_qoq > config.MARGIN_CONTRACTION_BPS_THRESHOLD:
        tag = "OPERATING_LEVERAGE_BREAKOUT"
        label = f"【🚀 營運槓桿顯現：營收放大帶動營益率跳升 (+{o_qoq:.1f}%)】"
    else:
        tag = "MARGIN_NEUTRAL"
        label = "【➖ 三率持穩：獲利結構變動於正常區間】"

    g_str = f"{gpm_latest:.1f}%" if gpm_latest is not None else "N/A"
    o_str = f"{opm_latest:.1f}%" if opm_latest is not None else "N/A"
    gq_str = f"QoQ {g_qoq:+.1f}%" if gpm_qoq_diff is not None else ""
    gy_str = f", YoY {g_yoy:+.1f}%" if g_yoy is not None else ""
    oq_str = f"QoQ {o_qoq:+.1f}%" if opm_qoq_diff is not None else ""
    summary_line = (
        f"[{tag}] 毛利率 {g_str} ({gq_str}{gy_str})、營益率 {o_str} ({oq_str}) {label}"
    )

    return _AutoDictResult(
        tag=tag,
        status=tag,
        semantic_tag=tag,
        label=label,
        gpm_latest=round(gpm_latest, 2) if gpm_latest is not None else None,
        opm_latest=round(opm_latest, 2) if opm_latest is not None else None,
        gpm_qoq_diff=round(g_qoq, 2) if gpm_qoq_diff is not None else None,
        gpm_yoy_diff=round(g_yoy, 2) if g_yoy is not None else None,
        opm_qoq_diff=round(o_qoq, 2) if opm_qoq_diff is not None else None,
        opm_yoy_diff=round(o_yoy, 2) if o_yoy is not None else None,
        summary_line=summary_line,
    )


@dataclass
class FundamentalFeatureSummary:
    stock_id: str
    stock_name: str
    sub_industry: str
    safe_weighted_shares: float | None
    share_inference_tier: str
    revenue_momentum: _AutoDictResult
    margin_trends: _AutoDictResult
    forward_valuation: _AutoDictResult
    prompt_block: str
    paid_in_capital_twd: float = 0.0

    def to_prompt_block(self) -> str:
        return self.prompt_block

    def __str__(self) -> str:
        return self.prompt_block


def _infer_last_q4_yoy_from_statements(statements: list[dict[str, Any]]) -> float | None:
    for idx, q_row in enumerate(statements):
        qtr = str(q_row.get("year_qtr") or "").strip().upper()
        raw_dt = str(q_row.get("raw_date") or q_row.get("date") or "").strip()
        is_q4 = qtr.endswith("Q4") or raw_dt.endswith("12-31")
        if not is_q4:
            continue
        for k in ("qtr_rev_yoy_pct", "rev_yoy_pct", "yoy_pct"):
            val = _to_float(q_row.get(k))
            if val is not None:
                return val
        rev_cur = _to_float(q_row.get("revenue", q_row.get("Revenue", q_row.get("rev_亿元"))))
        if rev_cur is not None and rev_cur > 0 and len(qtr) >= 6 and qtr[:4].isdigit():
            prev_q4 = f"{int(qtr[:4]) - 1}Q4"
            for older in statements[idx + 1 :]:
                if str(older.get("year_qtr") or "").strip().upper() == prev_q4:
                    rev_prev = _to_float(older.get("revenue", older.get("Revenue", older.get("rev_亿元"))))
                    if rev_prev is not None and rev_prev > 0:
                        return round(((rev_cur / rev_prev) - 1.0) * 100.0, 2)
    return None


def compute_fundamental_features(
    stock_id: str,
    stock_name: str = "",
    sub_industry: str = "未分類",
    *,
    quarterly_statements: list[dict[str, Any]] | pd.DataFrame | None = None,
    monthly_revenues: list[dict[str, Any]] | pd.DataFrame | None = None,
    latest_close_price: float | None = None,
    fallback_market_cap: float | None = None,
    sector_benchmark_pe: float = 20.0,
    trailing_pe: float | None = None,
    last_q4_yoy_pct: float | None = None,
    prev_quarter_revenue: float | None = None,
    paid_in_capital_twd: float = 0.0,
    config: FundamentalEngineConfig = DEFAULT_FUNDAMENTAL_ENGINE_CONFIG,
) -> FundamentalFeatureSummary:
    """組裝結構化 `FundamentalFeatureSummary` 與高密度語意特徵區塊 (SASD v5.4.0 Phase 2)。"""
    statements = _normalize_statements_newest_first(quarterly_statements)
    try:
        shares, share_tier = _infer_safe_weighted_shares(
            statements,
            fallback_market_cap=fallback_market_cap,
            latest_close_price=latest_close_price,
            config=config,
        )
        shares_yi = shares / 1e8
        share_line = f"{share_tier} (真實流通股數：{shares_yi:.2f} 億股，已排除面額誤差)"
        safe_shares_opt: float | None = shares
    except ValueError:
        shares = 0.0
        safe_shares_opt = None
        share_tier = "UNAVAILABLE"
        share_line = "UNAVAILABLE (財報無 |EPS| >= 0.25 之季度且無市值保底資料)"

    resolved_q4_yoy = (
        last_q4_yoy_pct
        if last_q4_yoy_pct is not None
        else _infer_last_q4_yoy_from_statements(statements)
    )

    rev_res = _analyze_revenue_momentum(
        monthly_revenues or [],
        last_q4_yoy_pct=resolved_q4_yoy,
        prev_quarter_revenue=prev_quarter_revenue,
        config=config,
    )
    rev_yoy_for_margin = (
        rev_res.get("weighted_3m_yoy_pct")
        if rev_res.get("weighted_3m_yoy_pct") is not None
        else rev_res.get("jan_feb_combined_yoy_pct")
    )
    margin_res = _evaluate_margin_trends(
        statements,
        revenue_yoy_pct=rev_yoy_for_margin,
        config=config,
    )

    if trailing_pe is None and latest_close_price and latest_close_price > 0 and statements:
        ttm_eps = _to_float(statements[0].get("ttm_eps"))
        if ttm_eps is None and len(statements) >= 4:
            eps_4q = [_extract_quarter_eps_and_ni(q)[0] for q in statements[:4]]
            ttm_eps = sum(eps_4q)
        if ttm_eps and ttm_eps > 0:
            trailing_pe = float(latest_close_price) / ttm_eps

    val_res = _compute_forward_valuation(
        implied_annual_revenue=float(rev_res.get("implied_annual_revenue") or 0.0),
        quarterly_statements=statements,
        safe_weighted_shares=shares,
        latest_close_price=latest_close_price,
        sector_benchmark_pe=sector_benchmark_pe,
        trailing_pe=trailing_pe,
        config=config,
    )

    header_name = f"{stock_id} {stock_name}".strip()
    lines = [
        f"【基本面與前瞻估值審計摘要 - {header_name} (細分產業：{sub_industry or '未分類'})】",
        f"- 股數還原基準：{share_line}",
        f"- 營收動能診斷：{rev_res['summary_line']}",
        f"- 本季達成進度：{rev_res['qtd_line']}",
        f"- 三率獲利品質：{margin_res['summary_line']}",
        f"- 前瞻估值對齊：{val_res['summary_line']}",
    ]
    prompt_block = "\n".join(lines)
    return FundamentalFeatureSummary(
        stock_id=str(stock_id).strip(),
        stock_name=str(stock_name).strip(),
        sub_industry=str(sub_industry or "未分類").strip(),
        safe_weighted_shares=safe_shares_opt,
        share_inference_tier=share_tier,
        revenue_momentum=rev_res,
        margin_trends=margin_res,
        forward_valuation=val_res,
        prompt_block=prompt_block,
        paid_in_capital_twd=float(paid_in_capital_twd or 0.0),
    )


def build_ai_fundamental_prompt_block(
    stock_id: str,
    stock_name: str = "",
    sub_industry: str = "未分類",
    *,
    quarterly_statements: list[dict[str, Any]] | pd.DataFrame | None = None,
    monthly_revenues: list[dict[str, Any]] | pd.DataFrame | None = None,
    latest_close_price: float | None = None,
    fallback_market_cap: float | None = None,
    sector_benchmark_pe: float = 20.0,
    trailing_pe: float | None = None,
    last_q4_yoy_pct: float | None = None,
    prev_quarter_revenue: float | None = None,
    config: FundamentalEngineConfig = DEFAULT_FUNDAMENTAL_ENGINE_CONFIG,
) -> str:
    """組裝供 alignment_ai_picker.py 直接注入 Prompt 之高密度語意特徵區塊 (Section 3.5)。"""
    return compute_fundamental_features(
        stock_id=stock_id,
        stock_name=stock_name,
        sub_industry=sub_industry,
        quarterly_statements=quarterly_statements,
        monthly_revenues=monthly_revenues,
        latest_close_price=latest_close_price,
        fallback_market_cap=fallback_market_cap,
        sector_benchmark_pe=sector_benchmark_pe,
        trailing_pe=trailing_pe,
        last_q4_yoy_pct=last_q4_yoy_pct,
        prev_quarter_revenue=prev_quarter_revenue,
        config=config,
    ).prompt_block


@dataclass
class DiagnosisReport:
    stock_id: str
    ai_text: str
    war_room_url: str | None
    cached: bool
    cache_period: str | None
    summary_json: str = ""
    model_version: str = ""


class FundamentalAnalyzer:
    def __init__(
        self,
        cache: FundamentalCacheManager,
        *,
        reports_dir: Path | None = None,
    ) -> None:
        self.cache = cache
        env_reports_dir = os.getenv("FINMIND_REPORTS_DIR", "").strip()
        self.reports_dir = (
            reports_dir
            if reports_dir is not None
            else (Path(env_reports_dir) if env_reports_dir else _DEFAULT_REPORTS_DIR)
        )

        self.ai_window_quarters = int(os.getenv("AI_WINDOW_QUARTERS", "8") or "8")
        self.ai_window_months = int(os.getenv("AI_WINDOW_MONTHS", "12") or "12")
        self.chart_window_quarters = int(os.getenv("CHART_WINDOW_QUARTERS", "16") or "16")
        self.chart_window_months = int(os.getenv("CHART_WINDOW_MONTHS", "36") or "36")

        self.news_timeout_seconds = float(os.getenv("NEWS_TIMEOUT_SECONDS", "8") or "8")
        self.news_max_items = int(os.getenv("NEWS_MAX_ITEMS", "8") or "8")

    def slice_sliding_window(self, df_full: pd.DataFrame, window_size: int) -> pd.DataFrame:
        if df_full.empty:
            return df_full
        if "raw_date" in df_full.columns:
            df_full = df_full.sort_values("raw_date")
        return df_full.tail(window_size).reset_index(drop=True)

    def _fetch_google_news_rss(self, query: str) -> list[dict[str, str]]:
        url = "https://news.google.com/rss/search"
        params = {
            "q": query,
            "hl": "zh-TW",
            "gl": "TW",
            "ceid": "TW:zh-Hant",
        }
        response = requests.get(url, params=params, timeout=self.news_timeout_seconds)
        response.raise_for_status()

        root = ET.fromstring(response.text)
        items = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            source = (item.findtext("source") or "").strip()
            description = html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))
            description = re.sub(r"\s+", " ", description).strip()
            if not title or not link:
                continue
            items.append(
                {
                    "title": title,
                    "link": link,
                    "pubDate": pub_date,
                    "source": source,
                    "description": description,
                }
            )
            if len(items) >= self.news_max_items:
                break
        return items

    def fetch_free_stock_news_and_search(self, stock_id: str, stock_name: str) -> str:
        query = f"{stock_id} {stock_name} 台股".strip()
        try:
            items = self._fetch_google_news_rss(query)
        except Exception:
            return "（本次新聞抓取失敗）"
        items = filter_and_clean_rss(items)

        if not items:
            return "（本次沒有抓到相關新聞）"

        lines = ["\n■ 即時新聞 (Google News RSS)"]
        for it in items:
            meta = ""
            if it.get("source") or it.get("pubDate"):
                meta = f" — {it.get('source', '').strip()} {it.get('pubDate', '').strip()}".strip()
            lines.append(f"- [{it['title']}]({it['link']}){meta}")
        return "\n".join(lines)

    _OUTLOOK_KEYWORDS = ("法說", "展望", "訂單", "產能", "資本支出", "毛利率", "營運", "財測", "指引", "能見度")

    def fetch_earnings_call_and_outlook(self, stock_id: str, stock_name: str) -> str:
        """定向檢索近期法說會/管理層展望報導（純 Python、不耗 AI 額度）。"""
        title_block = "\n■ 近期法說會與管理層未來展望檢索 (Google News RSS，近 180 天)"
        query = (
            f"{stock_name} {stock_id} (法說 OR 展望 OR 訂單 OR 產能 OR 資本支出 OR 毛利率 OR 財測) when:180d"
        ).strip()
        try:
            items = self._fetch_google_news_rss(query)
        except Exception:
            return title_block + "\n（近期無公開法說展望：檢索失敗）"

        relevant = [
            it
            for it in items
            if any(k in f"{it.get('title', '')}{it.get('description', '')}" for k in self._OUTLOOK_KEYWORDS)
        ]
        relevant = filter_and_clean_rss(relevant, is_guidance=True)
        if not relevant:
            return title_block + "\n（近期無公開法說展望：沒有找到相關報導）"

        has_substantive_summary = any(
            str(it.get("description") or it.get("summary") or "").strip() not in ("", RSS_NO_CONTENT_NOTE, str(it.get("title", "")).strip())
            for it in relevant
        )
        lines = [title_block, "以下為媒體對管理層說法的報導摘要；請只引用本區內容，不得自行補充展望。"]
        if not has_substantive_summary:
            lines.append(
                "⚠️ 系統檢核：以下檢索結果均僅有媒體空殼標題、無實質內文數據；請嚴守零腦補原則，於 SUMMARY_JSON 將 guidance.claims 填寫為「無」、verdict 填寫為「無資料」。"
            )
        for it in relevant:
            meta = f" — {it.get('source', '').strip()} {it.get('pubDate', '').strip()}".rstrip()
            lines.append(f"- {it['title']}{meta}")
            description = it.get("description", "").strip()
            if description and description != it["title"]:
                lines.append(f"  摘要：{description[:200]}")
        return "\n".join(lines)

    @staticmethod
    def _scale_cols_to_yi(
        frame: pd.DataFrame, target_cols: list[str] | tuple[str, ...]
    ) -> pd.DataFrame:
        """將原始「元」為單位的欄位統一除以 1e8 轉為「億元（小數點後兩位）」，消除 AI 位數誤判並節省 Token。"""
        if frame.empty:
            return frame
        out = frame.copy()
        for col in target_cols:
            if col in out.columns:
                out[col] = (pd.to_numeric(out[col], errors="coerce") / 1e8).round(2)
        return out

    def build_markdown_context(self, stock_id: str, include_outlook: bool = False) -> str:
        df_wide = self.cache._read_stock_rows(self.cache._get_worksheet("05_AI_Metrics_Wide"), stock_id)  # noqa: SLF001
        df_is = self.cache._read_stock_rows(self.cache._get_worksheet("02_Income_Statement"), stock_id)  # noqa: SLF001
        df_bs = self.cache._read_stock_rows(self.cache._get_worksheet("03_Balance_Sheet"), stock_id)  # noqa: SLF001
        df_cf = self.cache._read_stock_rows(self.cache._get_worksheet("04_Cash_Flow"), stock_id)  # noqa: SLF001
        df_rev = self.cache._read_stock_rows(self.cache._get_worksheet("01_Monthly_Revenue"), stock_id)  # noqa: SLF001
        df_div = self.cache._read_stock_rows(self.cache._get_worksheet("06_Dividend_Policy"), stock_id)  # noqa: SLF001
        df_chip = self.cache._read_stock_rows(self.cache._get_worksheet("07_Chip_Summary"), stock_id)  # noqa: SLF001

        df_wide = self.slice_sliding_window(df_wide, self.ai_window_quarters)
        df_is = self.slice_sliding_window(df_is, self.ai_window_quarters)
        df_bs = self.slice_sliding_window(df_bs, self.ai_window_quarters)
        df_cf = self.slice_sliding_window(df_cf, self.ai_window_quarters)
        df_rev = self.slice_sliding_window(df_rev, self.ai_window_months)
        df_div = self.slice_sliding_window(df_div, 5)
        df_chip = self.slice_sliding_window(df_chip, 8)

        # 從 00_Watchlist_Master 取得股名與官方產業分類
        stock_name = ""
        official_industry = ""
        try:
            watchlist = self.cache._get_worksheet("00_Watchlist_Master")  # noqa: SLF001
            records = self.cache._get_records_cached(watchlist)  # noqa: SLF001
            row = next((r for r in records if str(r.get("stock_id", "")).strip() == stock_id), None)
            if row:
                stock_name = str(row.get("stock_name", "")).strip()
                official_industry = str(row.get("industry_category", "")).strip()
        except Exception:
            stock_name = ""

        # Stage 0 ➔ Stage 1 交接棒：注入產業定錨、估值屬性 (valuation_regime) 與同業代號
        tax_header_line = ""
        try:
            from taxonomy_service import TaxonomyService, format_stage1_taxonomy_header

            tax_svc = TaxonomyService()
            tax_rec = tax_svc.get_or_create_sync(
                stock_id,
                stock_name=stock_name,
                official_industry=official_industry,
                sync_sheet=False,
            )
            if not stock_name and tax_rec:
                stock_name = str(tax_rec.get("stock_name", "")).strip()
            tax_header_line = format_stage1_taxonomy_header(tax_rec)
        except Exception:  # noqa: BLE001
            tax_header_line = ""

        # Python 預先計算估值與技術錨點（避免 Stage 1 AI 自行心算 PE / PB / 殖利率 / 17MA 乖離率）
        latest_close: float | None = None
        latest_ma17: float | None = None
        try:
            prices_df = self._find_latest_prices_csv_for_stock(stock_id)
            if not prices_df.empty:
                p_clean = prices_df.copy()
                p_clean["date"] = pd.to_datetime(p_clean["date"], errors="coerce")
                p_clean["close"] = pd.to_numeric(p_clean["close"], errors="coerce")
                p_clean = (
                    p_clean.dropna(subset=["date", "close"])
                    .sort_values("date")
                    .drop_duplicates(subset=["date"], keep="last")
                    .reset_index(drop=True)
                )
                if not p_clean.empty:
                    latest_close = float(p_clean.iloc[-1]["close"])
                    if "MA17" in p_clean.columns:
                        ma17_series = pd.to_numeric(p_clean["MA17"], errors="coerce")
                    else:
                        ma17_series = pd.Series(dtype=float)
                    if ma17_series.empty or ma17_series.isna().all():
                        ma17_series = p_clean["close"].rolling(window=17, min_periods=17).mean()
                    if not ma17_series.empty and not pd.isna(ma17_series.iloc[-1]):
                        latest_ma17 = float(ma17_series.iloc[-1])
        except Exception:  # noqa: BLE001
            pass

        ttm_eps_val: float | None = None
        if not df_wide.empty and "ttm_eps" in df_wide.columns:
            ttm_eps_val = _to_float(df_wide.iloc[-1].get("ttm_eps"))
        pe_str = (
            f"{latest_close / ttm_eps_val:,.2f}x（收盤 {latest_close:,.2f} / TTM EPS {ttm_eps_val:,.2f}）"
            if (latest_close and ttm_eps_val and ttm_eps_val > 0)
            else "N/A（TTM EPS <= 0 或缺漏）"
        )

        pb_str = "N/A"
        if not df_bs.empty and latest_close:
            bs_last = df_bs.iloc[-1]
            p_eq = _to_float(bs_last.get("parent_equity"))
            cap_stk = _to_float(bs_last.get("capital_stock"))
            if p_eq and cap_stk and p_eq > 0 and cap_stk > 0:
                bps = p_eq / (cap_stk / 10.0)
                if bps > 0:
                    pb_str = f"{latest_close / bps:,.2f}x（每股淨值 {bps:,.2f} 元）"

        div_yield_str = "N/A"
        if not df_div.empty and latest_close and latest_close > 0:
            cash_div = _to_float(df_div.iloc[-1].get("cash_dividend"))
            div_yr = str(df_div.iloc[-1].get("div_year", "")).strip()
            if cash_div is not None and cash_div >= 0:
                div_yield_str = f"{cash_div / latest_close * 100.0:.2f}%（{div_yr} 年現金股利 {cash_div:.2f} 元）"

        close_str = f"{latest_close:,.2f} 元" if latest_close else "N/A"
        stop_4pct_str = f"{latest_close * 0.96:,.2f} 元" if latest_close else "N/A"
        if latest_close and latest_ma17 and latest_ma17 > 0:
            bias_17ma = (latest_close - latest_ma17) / latest_ma17 * 100.0
            ma17_anchor_str = (
                f"{latest_ma17:,.2f} 元（距 17MA 乖離率 {bias_17ma:+.2f}%｜"
                f"17MA+12% 過熱極值價 {latest_ma17 * 1.12:,.2f} 元）"
            )
        else:
            ma17_anchor_str = "N/A"

        title_name = f"{stock_id} {stock_name}".strip()
        parts: list[str] = []
        parts.append(f"【標的：{title_name} — Google Sheets 法人級深度財報與籌碼數據包】")
        if tax_header_line:
            parts.append(f"【Stage 0 產業定錨】{tax_header_line}")
        parts.append(
            f"【Python 預先計算估值與技術錨點（請直接引用，嚴禁自行心算除法）】"
            f"最新收盤價：{close_str}｜最新 PE：{pe_str}｜PB：{pb_str}｜近一年現金殖利率：{div_yield_str}"
            f"｜17MA 錨點：{ma17_anchor_str}｜收盤 -4% 硬停損上限價：{stop_4pct_str}"
        )

        # Table 1: IS structure + expense ratio + attribution
        if not df_wide.empty or not df_is.empty:
            cols_wide = [
                "year_qtr",
                "rev_亿元",
                "gross_margin_pct",
                "op_expense_pct",
                "op_margin_pct",
                "non_op_share_pct",
                "parent_net_margin_pct",
                "eps",
                "ttm_eps",
                "qtr_rev_yoy_pct",
            ]
            show = df_wide[[c for c in cols_wide if c in df_wide.columns]].copy() if not df_wide.empty else pd.DataFrame()
            parts.append("\n■ 表 1：損益結構、營業費用率與本業/業外歸因 (近 8 季｜營收單位：億元，比率：%)")
            if not show.empty:
                parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 2: CCC + contract liabilities + financial assets (統一將原始元轉為億元)
        if not df_wide.empty or not df_bs.empty:
            cols = [
                "year_qtr",
                "inventory",
                "accounts_receivable",
                "accounts_payable",
                "contract_liab_current",
                "dio_days",
                "dso_days",
                "dpo_days",
                "ccc_days",
                "fvpl_financial_assets",
                "fvoci_financial_assets",
                "ac_financial_assets",
            ]
            money_cols_t2 = (
                "inventory",
                "accounts_receivable",
                "accounts_payable",
                "contract_liab_current",
                "fvpl_financial_assets",
                "fvoci_financial_assets",
                "ac_financial_assets",
            )

            base = pd.DataFrame()
            if not df_wide.empty and "year_qtr" in df_wide.columns:
                base = df_wide[[c for c in cols if c in df_wide.columns]].copy()

            if not df_bs.empty and "year_qtr" in df_bs.columns:
                bs_part = df_bs[[c for c in cols if c in df_bs.columns]].copy()
                base = bs_part if base.empty else base.merge(bs_part, on="year_qtr", how="left")

            show = base[[c for c in cols if c in base.columns]].copy() if not base.empty else pd.DataFrame()
            show = self._scale_cols_to_yi(show, money_cols_t2)
            parts.append("\n■ 表 2：營運週轉效率 (CCC)、合約負債與金融資產配置 (近 8 季｜金額單位：億元，週轉：天)")
            if not show.empty:
                parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 3: cash flow breakdown (統一將原始元轉為億元)
        if not df_cf.empty:
            cols = [
                "year_qtr",
                "operating_cf",
                "capex",
                "free_cf",
                "cf_change_inv",
                "cf_change_ar",
                "cf_change_ap",
                "depreciation",
                "amortization",
                "interest_income",
                "interest_expense",
                "investing_cf",
                "financing_cf",
            ]
            show = df_cf[[c for c in cols if c in df_cf.columns]].copy()
            show = self._scale_cols_to_yi(show, cols[1:])
            parts.append("\n■ 表 3：單季現金流量拆解、營運資金變動與含金量 (近 8 季｜金額單位：億元)")
            parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 4: leverage and solvency (統一將資產負債表原始元轉為億元，與 total_debt_亿元 / ebitda_亿元 一致)
        if not df_wide.empty or not df_bs.empty:
            cols_bs = [
                "year_qtr",
                "cash_and_equiv",
                "short_term_debt",
                "bonds_payable",
                "long_term_debt",
                "total_liabilities",
                "total_equity",
            ]
            cols_wide = [
                "year_qtr",
                "total_debt_亿元",
                "net_debt_ratio_pct",
                "current_ratio_pct",
                "quick_ratio_pct",
                "interest_coverage",
                "parent_roe_pct",
                "ocf_to_ni_pct",
                "ebitda_亿元",
            ]

            base = pd.DataFrame()
            if not df_wide.empty and "year_qtr" in df_wide.columns:
                base = df_wide[[c for c in cols_wide if c in df_wide.columns]].copy()

            if not df_bs.empty and "year_qtr" in df_bs.columns:
                bs_part = df_bs[[c for c in cols_bs if c in df_bs.columns]].copy()
                base = bs_part if base.empty else base.merge(bs_part, on="year_qtr", how="left")

            show_cols = [c for c in (cols_bs + cols_wide[1:]) if c in base.columns]
            show = base[show_cols].copy() if not base.empty else pd.DataFrame()
            show = self._scale_cols_to_yi(show, cols_bs[1:])
            parts.append("\n■ 表 4：財務槓桿、有息負債結構與償債能力 (近 8 季｜金額單位：億元，比率：%)")
            if not show.empty:
                parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 5: monthly revenue momentum (統一將原始元轉為億元 + Stale Data Guard 資料新鮮度檢查)
        if not df_rev.empty:
            cols = ["rev_ym", "revenue", "mom_pct", "yoy_pct", "raw_date"]
            show = df_rev[[c for c in cols if c in df_rev.columns]].copy()
            show = self._scale_cols_to_yi(show, ("revenue",))
            latest_rev_ym = show.iloc[-1].get("rev_ym") if "rev_ym" in show.columns else None
            is_stale, stale_msg = check_revenue_staleness(latest_rev_ym)
            parts.append("\n■ 表 5：近期月營收動能 (近 12 個月｜金額單位：億元)")
            if is_stale:
                parts.append(
                    f"- {stale_msg}｜請於 Step 1 標註月營收資料過期，並改以【表 1】最新季報 qtr_rev_yoy_pct 作為營收動能主依據。"
                )
            parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 6: dividend + chip summary (含 v5.5.0 融資融券與維持率欄位)
        parts.append("\n■ 表 6：歷年股利政策與近期外資/投信/自營商法人及信用交易籌碼動向")
        if not df_div.empty:
            cols = [
                "div_year",
                "cash_dividend",
                "stock_dividend",
                "total_dividend",
                "total_cash_dividend_亿元",
                "annual_eps",
                "payout_ratio_pct",
                "ex_dividend_date",
                "payment_date",
            ]
            show = df_div[[c for c in cols if c in df_div.columns]].copy()
            parts.append("\n- 【股利政策 (近 5 年)】")
            parts.append(show.to_markdown(index=False, floatfmt=".2f"))
        if not df_chip.empty:
            cols = [
                "trade_week",
                "raw_date",
                "foreign_holding_pct",
                "foreign_holding_chg_20d",
                "foreign_net_buy_5d_lots",
                "foreign_net_buy_20d_lots",
                "trust_net_buy_5d_lots",
                "trust_net_buy_20d_lots",
                "dealer_net_buy_5d_lots",
                "dealer_net_buy_20d_lots",
                "three_inst_net_5d_lots",
                "three_inst_net_20d_lots",
                "chip_quadrant_tag",
                "margin_balance_lots",
                "margin_utilization_pct",
                "margin_chg_5d_lots",
                "margin_chg_20d_lots",
                "short_margin_ratio_pct",
                "est_margin_maint_pct",
            ]
            show = df_chip[[c for c in cols if c in df_chip.columns]].copy()
            parts.append("\n- 【法人與信用籌碼 (近 8 週，含融資使用率/增減與估算維持率)】")
            parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 7: Optional latest prices.csv moving averages (if available locally)
        ma_context = self.build_latest_ma_context(stock_id)
        if ma_context:
            parts.append(ma_context)

        if stock_name:
            parts.append(self.fetch_free_stock_news_and_search(stock_id, stock_name))
            if include_outlook:
                parts.append(self.fetch_earnings_call_and_outlook(stock_id, stock_name))

        return "\n".join(parts)

    def _find_latest_prices_csv_for_stock(
        self,
        stock_id: str,
        *,
        base_dir: Path | None = None,
    ) -> pd.DataFrame:
        """從本地 finmind_reports 尋找最新包含該 stock_id 的 prices.csv，若無則回傳空 DataFrame。"""
        target_id = stock_id.strip()
        root_dir = base_dir if base_dir is not None else self.reports_dir
        if not target_id or not root_dir.is_dir():
            return pd.DataFrame()

        candidate_files: list[Path] = []
        daily_dir = root_dir / "daily"
        if daily_dir.is_dir():
            daily_files = sorted(
                daily_dir.glob("*_prices.csv"),
                key=lambda p: p.name,
                reverse=True,
            )
            candidate_files.extend(daily_files)

        single_files = sorted(
            root_dir.glob("prices_*.csv"),
            key=lambda p: p.name,
            reverse=True,
        )
        candidate_files.extend(single_files)

        other_files = sorted(
            (p for p in root_dir.rglob("*prices*.csv") if p not in candidate_files),
            key=lambda p: str(p),
            reverse=True,
        )
        candidate_files.extend(other_files)

        for csv_path in candidate_files:
            if not csv_path.is_file():
                continue
            try:
                df = pd.read_csv(csv_path, dtype={"stock_id": str})
            except Exception:
                continue
            if df.empty or "stock_id" not in df.columns or "close" not in df.columns:
                continue
            matched = df.loc[df["stock_id"].astype(str).str.strip() == target_id].copy()
            if not matched.empty:
                return matched
        return pd.DataFrame()

    def build_latest_ma_context(
        self,
        stock_id: str,
        *,
        base_dir: Path | None = None,
    ) -> str:
        """讀取最新 prices.csv 並計算/提取 MA3、MA17、MA45、MA117、MA189 與 17MA 走勢；若無資料則回傳空字串。"""
        try:
            stock_prices = self._find_latest_prices_csv_for_stock(stock_id, base_dir=base_dir)
            if stock_prices.empty:
                return ""

            stock_prices["date"] = pd.to_datetime(stock_prices["date"], errors="coerce")
            stock_prices["close"] = pd.to_numeric(stock_prices["close"], errors="coerce")
            stock_prices = (
                stock_prices.dropna(subset=["date", "close"])
                .sort_values("date")
                .drop_duplicates(subset=["date"], keep="last")
                .reset_index(drop=True)
            )
            if stock_prices.empty:
                return ""

            for period in MOVING_AVERAGE_PERIODS:
                col = f"MA{period}"
                if col in stock_prices.columns:
                    stock_prices[col] = pd.to_numeric(stock_prices[col], errors="coerce")
                if col not in stock_prices.columns or stock_prices[col].isna().all():
                    stock_prices[col] = (
                        stock_prices["close"]
                        .rolling(window=period, min_periods=period)
                        .mean()
                    )

            latest = stock_prices.iloc[-1]
            latest_date = latest["date"].strftime("%Y-%m-%d")
            close_val = float(latest["close"])

            def _fmt_ma(col_name: str) -> str:
                val = latest.get(col_name)
                if val is None or pd.isna(val):
                    return "N/A"
                return f"{float(val):,.2f}"

            ma17_val = latest.get("MA17")
            ma45_val = latest.get("MA45")
            gap_str = "N/A"
            bias_17ma_str = "N/A"
            if (
                ma17_val is not None
                and not pd.isna(ma17_val)
                and float(ma17_val) > 0
            ):
                ma17_f = float(ma17_val)
                bias_pct = (close_val - ma17_f) / ma17_f * 100.0
                bias_17ma_str = f"{bias_pct:+.2f}%（17MA+12%極值 {ma17_f * 1.12:,.2f}）"
                if ma45_val is not None and not pd.isna(ma45_val):
                    gap_pct = (ma17_f - float(ma45_val)) / ma17_f * 100.0
                    gap_str = f"{gap_pct:.2f}%"

            ma17_trend = describe_ma17_trend(stock_prices["MA17"])
            vol_metrics = _extract_latest_volume_metrics(stock_prices)
            vol_line = ""
            if vol_metrics.get("vol_lots") != "":
                vol_line = (
                    f"- 量能結構：今日 {int(vol_metrics['vol_lots']):,} 張"
                    f"｜5日均量 {int(vol_metrics['vol_ma5_lots']):,} 張"
                    f"｜20日均量 {int(vol_metrics['vol_ma20_lots']):,} 張"
                    f"｜量能狀態：{vol_metrics['vol_status']}"
                )

            signal_info = classify_latest_signal(stock_prices)
            if signal_info is not None:
                signal_status = f"✅ 符合強勢均線排列訊號（{signal_info['signal']}）"
            else:
                signal_status = "➖ 未同時滿足五線強勢排列條件"

            recent_5d = stock_prices.tail(5).copy()
            recent_5d["date"] = recent_5d["date"].dt.strftime("%Y-%m-%d")
            if "Trading_Volume" in recent_5d.columns and "vol_lots" not in recent_5d.columns:
                recent_5d["vol_lots"] = (
                    pd.to_numeric(recent_5d["Trading_Volume"], errors="coerce") / 1000.0
                ).round(0)
            display_cols = [
                c
                for c in ["date", "close", "vol_lots", *(f"MA{p}" for p in MOVING_AVERAGE_PERIODS)]
                if c in recent_5d.columns
            ]
            for c in display_cols[1:]:
                if c in recent_5d.columns:
                    recent_5d[c] = pd.to_numeric(recent_5d[c], errors="coerce").round(2)

            lines = [
                f"\n■ 表 7：近期日線與均線排列動態 (本地 prices.csv｜最新交易日：{latest_date})",
                (
                    f"- 最新收盤：{close_val:,.2f}｜MA3：{_fmt_ma('MA3')}｜MA17：{_fmt_ma('MA17')}"
                    f"｜MA45：{_fmt_ma('MA45')}｜MA117：{_fmt_ma('MA117')}｜MA189：{_fmt_ma('MA189')}"
                ),
                (
                    f"- 距17MA乖離率：{bias_17ma_str}｜17MA與45MA差距：{gap_str}"
                    f"｜17MA近期走勢：{ma17_trend}｜均線訊號：{signal_status}"
                ),
            ]
            if vol_line:
                lines.append(vol_line)
            lines.append(recent_5d[display_cols].to_markdown(index=False, floatfmt=".2f"))
            return "\n".join(lines)
        except Exception:
            return ""

    def get_hard_screen_metrics(self, stock_id: str) -> dict:
        """Stage 0：取最新收盤/日量（本地 prices.csv）與最近一季營益率、合約負債（億）。缺資料者為 None。"""
        metrics: dict = {
            "close": None,
            "vol_lots": None,
            "op_margin_pct": None,
            "contract_liab": None,
            "three_inst_net_5d_lots": None,
            "margin_chg_5d_lots": None,
            "margin_utilization_pct": None,
            "est_margin_maint_pct": None,
        }
        try:
            prices = self._find_latest_prices_csv_for_stock(stock_id)
            if not prices.empty:
                prices = prices.copy()
                prices["date"] = pd.to_datetime(prices["date"], errors="coerce")
                prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
                prices = (
                    prices.dropna(subset=["date", "close"])
                    .sort_values("date")
                    .drop_duplicates(subset=["date"], keep="last")
                    .reset_index(drop=True)
                )
                if not prices.empty:
                    metrics["close"] = float(prices.iloc[-1]["close"])
                    metrics["vol_lots"] = _to_float(_extract_latest_volume_metrics(prices).get("vol_lots"))
        except Exception:  # noqa: BLE001
            logger.debug("hard-screen price metrics unavailable for %s", stock_id, exc_info=True)
        try:
            wide = self.cache._read_stock_rows(self.cache._get_worksheet("05_AI_Metrics_Wide"), stock_id)  # noqa: SLF001
            if not wide.empty:
                if "raw_date" in wide.columns:
                    wide = wide.sort_values("raw_date")
                latest = wide.iloc[-1].to_dict()
                metrics["op_margin_pct"] = _to_float(latest.get("op_margin_pct"))
                metrics["contract_liab"] = _to_float(latest.get("contract_liab_亿元"))
        except Exception:  # noqa: BLE001
            logger.debug("hard-screen wide metrics unavailable for %s", stock_id, exc_info=True)
        try:
            chip_df = self.cache._read_stock_rows(self.cache._get_worksheet("07_Chip_Summary"), stock_id)  # noqa: SLF001
            if not chip_df.empty:
                if "raw_date" in chip_df.columns:
                    chip_df = chip_df.sort_values("raw_date")
                latest_chip = chip_df.iloc[-1].to_dict()
                metrics["three_inst_net_5d_lots"] = _to_float(latest_chip.get("three_inst_net_5d_lots"))
                metrics["margin_chg_5d_lots"] = _to_float(latest_chip.get("margin_chg_5d_lots"))
                metrics["margin_utilization_pct"] = _to_float(latest_chip.get("margin_utilization_pct"))
                metrics["est_margin_maint_pct"] = _to_float(latest_chip.get("est_margin_maint_pct"))
        except Exception:  # noqa: BLE001
            logger.debug("hard-screen chip metrics unavailable for %s", stock_id, exc_info=True)
        return metrics

    @staticmethod
    def _taxonomy_badge_sync(
        stock_id: str,
        stock_name: str = "",
        official_industry: str = "",
        *,
        cached_only: bool = False,
    ) -> str:
        """取得單股「🏷️ 細分產業｜🔗 相似同業」標籤；任何失敗皆回傳空字串，不影響診斷主流程。"""
        try:
            from taxonomy_service import TaxonomyService, format_taxonomy_badge

            service = TaxonomyService()
            if cached_only:
                rec = service.get_cached(stock_id)
            else:
                rec = service.get_or_create_sync(
                    stock_id,
                    stock_name=stock_name,
                    official_industry=official_industry,
                    sync_sheet=True,
                )
            return format_taxonomy_badge(rec) if rec else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("Taxonomy lookup failed for %s: %s", stock_id, exc)
            return ""

    @staticmethod
    def _inject_taxonomy_badge(text: str, badge: str) -> str:
        """把細分產業標籤插在『分析模型：』行下一行（無該行則置於最前）。"""
        if not badge:
            return text
        lines = text.split("\n", 1)
        if lines and lines[0].startswith("分析模型："):
            rest = lines[1] if len(lines) > 1 else ""
            return f"{lines[0]}\n{badge}\n{rest}"
        return f"{badge}\n{text}"

    async def prepare_diagnosis_context(
        self,
        stock_id: str,
        *,
        force_ai: bool = False,
        publish_war_room: bool = True,
    ) -> dict[str, Any]:
        """Data-prep half of the diagnosis: cache check + prompt assembly (no AI call)."""
        watchlist = self.cache._get_worksheet("00_Watchlist_Master")  # noqa: SLF001
        records = self.cache._get_records_cached(watchlist)  # noqa: SLF001
        row = next(
            (r for r in records if str(r.get("stock_id", "")).strip() == stock_id),
            None,
        )

        # cache_period 僅由資料水位組成：財報/月營收/股利/籌碼都沒更新時才算同一期。
        cache_period = None
        if row:
            last_fin = str(row.get("last_fin_qtr", "")).strip()
            last_rev = str(row.get("last_rev_ym", "")).strip()
            max_div = str(row.get("max_div_raw_date", "")).strip()
            max_chip = str(row.get("max_chip_raw_date", "")).strip()
            if last_fin and last_rev:
                cache_period = f"{last_fin}_{last_rev}_{max_div}_{max_chip}"

        tax_badge = await asyncio.to_thread(
            self._taxonomy_badge_sync,
            stock_id,
            str(row.get("stock_name", "")).strip() if row else "",
            str(row.get("industry_category", "")).strip() if row else "",
        )

        cached_summary = str(row.get("ai_summary_json", "")).strip() if row else ""
        cached_period = str(row.get("ai_cached_period", "")).strip() if row else ""

        # Schema check: 舊快取（缺 trade_plan / trend_tags）視為 cache miss，自動重新呼叫 Stage 1 更新。
        cached_data = self._parse_summary(cached_summary) if cached_summary else None
        schema_ok = bool(
            isinstance(cached_data, dict)
            and isinstance(cached_data.get("trade_plan"), dict)
            and isinstance(cached_data.get("trend_tags"), dict)
        )

        if (
            row
            and not force_ai
            and cache_period
            and cached_summary
            and cached_period == cache_period
            and schema_ok
        ):
            war_room_url = str(row.get("war_room_url", "")).strip() or None
            if publish_war_room and not war_room_url:
                try:
                    war_room_url = await self.render_and_publish_war_room_html(
                        stock_id, self._render_summary_text(cached_summary)
                    )
                    self.cache._update_watchlist_master(  # noqa: SLF001
                        watchlist,
                        stock_id,
                        {"war_room_url": war_room_url or ""},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to render war room on cache hit for %s: %s", stock_id, exc)
            cached_text = self._render_summary_text(cached_summary)
            if row and str(row.get("ai_model_version", "")).strip():
                cached_text = f"分析模型：{str(row.get('ai_model_version')).strip()}\n" + cached_text

            cached_text = self._inject_taxonomy_badge(cached_text, tax_badge)

            report = DiagnosisReport(
                stock_id=stock_id,
                ai_text=cached_text,
                war_room_url=war_room_url,
                cached=True,
                cache_period=cache_period,
                summary_json=cached_summary,
                model_version=str(row.get("ai_model_version", "")).strip() if row else "",
            )
            return {"cached": True, "report": report, "stock_id": stock_id}

        context = self.build_markdown_context(stock_id, include_outlook=True)
        # Stage 0 軟性警示：不擋股票，僅注入「系統硬傷警示」交由 AI 診斷。
        hard_metrics = self.get_hard_screen_metrics(stock_id)
        hard_warnings = build_hard_warnings(hard_metrics)
        if hard_warnings:
            context += "\n\n🚨 系統硬傷警示（Python 預篩，請於 Step 5~7 列入風險並反映於評分與操作定位）：\n" + "\n".join(
                f"- {w}" for w in hard_warnings
            )
        prompt = PROMPT_TEMPLATES["full_diagnosis"].replace("{DATA_PACKAGE_PLACEHOLDER}", context)
        return {
            "cached": False,
            "prompt": prompt,
            "stock_id": stock_id,
            "cache_period": cache_period,
            "latest_close": hard_metrics.get("close"),
            "tax_badge": tax_badge,
            "war_room_url": (str(row.get("war_room_url", "")).strip() or None) if row else None,
        }

    async def finalize_diagnosis_from_ai(
        self,
        stock_id: str,
        prep_data: Mapping[str, Any],
        ai_meta: Any,
        *,
        publish_war_room: bool = True,
    ) -> DiagnosisReport:
        """Post-processing half: parse AI output, publish war room, update watchlist cache."""
        watchlist = self.cache._get_worksheet("00_Watchlist_Master")  # noqa: SLF001
        cache_period = prep_data.get("cache_period")
        latest_close = prep_data.get("latest_close")
        tax_badge = str(prep_data.get("tax_badge") or "")

        raw_ai_text = ai_meta.text
        human_text, _parsed = self._split_ai_output(raw_ai_text, latest_close=latest_close)
        human_text = f"分析模型：{ai_meta.model_version}\n" + human_text

        war_room_url = prep_data.get("war_room_url")
        if publish_war_room:
            try:
                war_room_url = await self.render_and_publish_war_room_html(stock_id, human_text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to render/publish war room for %s: %s", stock_id, exc)

        summary = self._summarize_ai(raw_ai_text, model_version=ai_meta.model_version, latest_close=latest_close)
        self.cache._update_watchlist_master(  # noqa: SLF001
            watchlist,
            stock_id,
            {
                "ai_cache_day": datetime.now(TAIPEI_TIMEZONE).date().isoformat(),
                "ai_cached_period": cache_period or "",
                "ai_summary_json": summary,
                "ai_model_version": ai_meta.model_version,
                "war_room_url": war_room_url or "",
            },
        )

        rendered = human_text[:6000] or self._render_summary_text(summary)
        rendered = self._inject_taxonomy_badge(rendered, tax_badge)
        return DiagnosisReport(
            stock_id=stock_id,
            ai_text=rendered,
            war_room_url=war_room_url,
            cached=False,
            cache_period=cache_period,
            summary_json=summary,
            model_version=ai_meta.model_version,
        )

    async def run_full_diagnosis(
        self,
        stock_id: str,
        *,
        force_ai: bool = False,
        publish_war_room: bool = True,
        master: bool = False,
    ) -> DiagnosisReport:
        prep = await self.prepare_diagnosis_context(
            stock_id, force_ai=force_ai, publish_war_room=publish_war_room
        )
        if prep["cached"]:
            return prep["report"]
        meta = await ask_gemini_diagnosis_with_meta(
            prep["prompt"],
            tag=f"stock_diag:{stock_id}",
            master=master,
            task_type="STAGE1_GROUNDING",
            stock_id=stock_id,
            custom_id=f"STAGE1_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{stock_id}",
            blocking_bq_log=True,
        )
        return await self.finalize_diagnosis_from_ai(
            stock_id, prep, meta, publish_war_room=publish_war_room
        )

    async def run_targeted_qa(self, stock_id: str, user_prompt: str) -> str:
        context = self.build_markdown_context(stock_id)
        prompt = (PROMPT_TEMPLATES["system_rules"]
            + "\n\n==================================================\n"
            + context
            + "\n\n==================================================\n"
            + "【使用者詢問問題】\n"
            + user_prompt.strip()
            + "\n\n"
            + PROMPT_TEMPLATES["targeted_qa"]
        )
        meta = await ask_gemini_with_meta(prompt, tag=f"stock_qa:{stock_id}", task_type="SINGLE_QUERY", stock_id=stock_id, custom_id=f"CMD_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{stock_id}")
        return f"分析模型：{meta.model_version}\n" + meta.text

    async def render_and_publish_war_room_html(self, stock_id: str, ai_text: str) -> str:
        df_wide = self.cache._read_stock_rows(self.cache._get_worksheet("05_AI_Metrics_Wide"), stock_id)  # noqa: SLF001
        df_rev = self.cache._read_stock_rows(self.cache._get_worksheet("01_Monthly_Revenue"), stock_id)  # noqa: SLF001
        df_cf = self.cache._read_stock_rows(self.cache._get_worksheet("04_Cash_Flow"), stock_id)  # noqa: SLF001

        df_wide = self.slice_sliding_window(df_wide, self.chart_window_quarters)
        df_rev = self.slice_sliding_window(df_rev, self.chart_window_months)
        df_cf = self.slice_sliding_window(df_cf, self.chart_window_quarters)

        def _make_card_fig(title: str, height: int = 340, secondary_y: bool = False) -> go.Figure:
            card_fig = make_subplots(specs=[[{"secondary_y": secondary_y}]])
            card_fig.update_layout(
                template="plotly_dark",
                height=height,
                title={"text": title, "x": 0.01, "xanchor": "left", "y": 0.96, "yanchor": "top"},
                legend={
                    "orientation": "h",
                    "yanchor": "bottom",
                    "y": 1.02,
                    "xanchor": "right",
                    "x": 1.0,
                    "bgcolor": "rgba(17,24,39,0.7)",
                },
                hovermode="x unified",
                margin={"l": 50, "r": 50 if secondary_y else 28, "t": 72, "b": 38},
                paper_bgcolor="#111827",
                plot_bgcolor="#0b0f14",
            )
            return card_fig

        chart_blocks: list[tuple[str, str, go.Figure]] = []

        # 1. 月營收與 YoY
        fig_rev = _make_card_fig("① 月營收與年增率 YoY", secondary_y=True)
        if not df_rev.empty and {"rev_ym", "revenue"}.issubset(df_rev.columns):
            x = df_rev["rev_ym"].astype(str)
            y = pd.to_numeric(df_rev["revenue"], errors="coerce") / 1e8
            yoy = pd.to_numeric(df_rev.get("yoy_pct"), errors="coerce")
            fig_rev.add_trace(
                go.Bar(x=x, y=y, name="月營收 (億元)", marker_color="#3b82f6", opacity=0.85),
                secondary_y=False,
            )
            if yoy is not None:
                fig_rev.add_trace(
                    go.Scatter(
                        x=x,
                        y=yoy,
                        name="YoY 年增率 (%)",
                        mode="lines+markers",
                        line={"color": "#f97316", "width": 2.5},
                    ),
                    secondary_y=True,
                )
        fig_rev.update_yaxes(title_text="億元", secondary_y=False)
        fig_rev.update_yaxes(title_text="YoY (%)", secondary_y=True, showgrid=False)
        chart_blocks.append(
            (
                "月營收與 YoY 年增率",
                "💡 <b>名詞說明</b>：<b>YoY (Year-over-Year，年增率)</b> 為與去年同月相比之成長率，可排除淡旺季干擾。柱狀持續墊高且 YoY > 0% 代表營收動能擴張。",
                fig_rev,
            )
        )

        x_qtr = df_wide["year_qtr"].astype(str) if not df_wide.empty and "year_qtr" in df_wide.columns else pd.Series(dtype=str)

        # 2. 三率走勢
        fig_margins = _make_card_fig("② 獲利三率走勢（毛利率 / 營益率 / 母公司淨利率）")
        if not df_wide.empty:
            for col, name, color in (
                ("gross_margin_pct", "毛利率 (%)｜產品定價與成本競爭力", "#10b981"),
                ("op_margin_pct", "營益率 (%)｜本業真實獲利能力", "#a855f7"),
                ("parent_net_margin_pct", "母公司淨利率 (%)｜最終歸屬股東稅後獲利", "#f59e0b"),
            ):
                if col in df_wide.columns:
                    fig_margins.add_trace(
                        go.Scatter(
                            x=x_qtr,
                            y=pd.to_numeric(df_wide[col], errors="coerce"),
                            name=name,
                            mode="lines+markers",
                            line={"color": color, "width": 2.5},
                        )
                    )
        fig_margins.update_yaxes(title_text="%")
        chart_blocks.append(
            (
                "三率走勢",
                "💡 <b>名詞說明</b>：<b>毛利率</b>（扣除直接生產成本）看產品競爭力；<b>營益率</b>（再扣除研發/推銷/管理費用）看本業賺錢實力；<b>淨利率</b>（加計業外與扣稅後）看最終入袋獲利。「三率三升」為極佳基本面訊號。",
                fig_margins,
            )
        )

        # 3. 營業費用率、業外佔比與 ROE
        fig_roe = _make_card_fig("③ 股東權益報酬率 ROE、費用率與業外佔比")
        if not df_wide.empty:
            for col, name, color in (
                ("parent_roe_pct", "母公司 ROE (%)｜股東資金獲利效率", "#84cc16"),
                ("op_expense_pct", "營業費用率 (%)｜管銷研費用佔營收比", "#06b6d4"),
                ("non_op_share_pct", "業外佔稅前比 (%)｜非本業獲利依賴度", "#ec4899"),
            ):
                if col in df_wide.columns:
                    fig_roe.add_trace(
                        go.Scatter(
                            x=x_qtr,
                            y=pd.to_numeric(df_wide[col], errors="coerce"),
                            name=name,
                            mode="lines+markers",
                            line={"color": color, "width": 2.5},
                        )
                    )
        fig_roe.update_yaxes(title_text="%")
        chart_blocks.append(
            (
                "ROE、營業費用率與業外佔比",
                "💡 <b>名詞說明</b>：<b>ROE (股東權益報酬率)</b> 衡量公司替股東賺錢的效率（越穩定高檔越好）；<b>營業費用率</b>下降代表規模經濟顯現；<b>業外佔稅前淨利比</b>若過高（如 >30%），需留意獲利是否靠匯兌或一次性處分撐起。",
                fig_roe,
            )
        )

        # 4. CCC 現金轉換週期
        fig_ccc = _make_card_fig("④ 營運週轉天數（CCC / DIO 存貨 / DSO 應收 / DPO 應付）")
        if not df_wide.empty:
            for col, name, color in (
                ("ccc_days", "CCC 現金轉換週期 (天)", "#e879f9"),
                ("dio_days", "DIO 存貨銷售天數 (天)", "#eab308"),
                ("dso_days", "DSO 應收帳款收現天數 (天)", "#6366f1"),
                ("dpo_days", "DPO 應付帳款付現天數 (天)", "#ef4444"),
            ):
                if col in df_wide.columns:
                    fig_ccc.add_trace(
                        go.Scatter(
                            x=x_qtr,
                            y=pd.to_numeric(df_wide[col], errors="coerce"),
                            name=name,
                            mode="lines+markers",
                            line={"color": color, "width": 2.5},
                        )
                    )
        fig_ccc.update_yaxes(title_text="天數")
        chart_blocks.append(
            (
                "CCC 現金轉換週期",
                "💡 <b>名詞說明</b>：<b>DIO (存貨週轉天數)</b> 越低代表去化越快、不塞貨；<b>DSO (應收帳款天數)</b> 越短代表客戶越快付款；<b>DPO (應付帳款天數)</b> 代表對供應商議價力；<b>CCC = DIO + DSO - DPO</b>（公司投入現金到收回現金的天數，越短代表資金週轉越靈活）。",
                fig_ccc,
            )
        )

        # 5. 單季 EPS 與 TTM EPS
        fig_eps = _make_card_fig("⑤ 每股盈餘（單季 EPS 與近四季累積 TTM EPS）", secondary_y=True)
        if not df_wide.empty:
            if "eps" in df_wide.columns:
                fig_eps.add_trace(
                    go.Bar(
                        x=x_qtr,
                        y=pd.to_numeric(df_wide["eps"], errors="coerce"),
                        name="單季 EPS (元)",
                        marker_color="#10b981",
                        opacity=0.85,
                    ),
                    secondary_y=False,
                )
            if "ttm_eps" in df_wide.columns:
                fig_eps.add_trace(
                    go.Scatter(
                        x=x_qtr,
                        y=pd.to_numeric(df_wide["ttm_eps"], errors="coerce"),
                        name="TTM EPS 近四季合計 (元)",
                        mode="lines+markers",
                        line={"color": "#a855f7", "width": 3},
                    ),
                    secondary_y=True,
                )
        fig_eps.update_yaxes(title_text="單季 EPS (元)", secondary_y=False)
        fig_eps.update_yaxes(title_text="TTM EPS (元)", secondary_y=True, showgrid=False)
        chart_blocks.append(
            (
                "單季 EPS 與 TTM EPS",
                "💡 <b>名詞說明</b>：<b>EPS (每股稅後盈餘)</b> 為每 1 股賺多少錢；<b>TTM EPS (Trailing Twelve Months，近四季滾動累計 EPS)</b> 能撫平單季淡旺季波動，紫色線趨勢往上代表公司長期獲利能力持續創高。",
                fig_eps,
            )
        )

        # 6. 現金流量與獲利含金量
        fig_cf = _make_card_fig("⑥ 現金流量（OCF / CapEx / FCF）與獲利含金量（OCF/NI）", secondary_y=True)
        if not df_cf.empty and "year_qtr" in df_cf.columns:
            x_cf = df_cf["year_qtr"].astype(str)
            for col, name, color in (
                ("operating_cf", "OCF 營業現金流 (億)", "#06b6d4"),
                ("capex", "CapEx 資本支出 (億)", "#f43f5e"),
                ("free_cf", "FCF 自由現金流 (億)", "#a3e635"),
            ):
                if col in df_cf.columns:
                    fig_cf.add_trace(
                        go.Bar(
                            x=x_cf,
                            y=pd.to_numeric(df_cf[col], errors="coerce") / 1e8,
                            name=name,
                            marker_color=color,
                        ),
                        secondary_y=False,
                    )
        if not df_wide.empty and "ocf_to_ni_pct" in df_wide.columns:
            fig_cf.add_trace(
                go.Scatter(
                    x=x_qtr,
                    y=pd.to_numeric(df_wide["ocf_to_ni_pct"], errors="coerce"),
                    name="OCF/NI 獲利含金量 (%)",
                    mode="lines+markers",
                    line={"color": "#fb923c", "width": 2.5},
                ),
                secondary_y=True,
            )
        fig_cf.update_yaxes(title_text="億元", secondary_y=False)
        fig_cf.update_yaxes(title_text="獲利含金量 (%)", secondary_y=True, showgrid=False)
        chart_blocks.append(
            (
                "現金流與獲利含金量",
                "💡 <b>名詞說明</b>：<b>OCF (營業現金流)</b> 為本業實際收進來的現金；<b>CapEx (資本支出)</b> 為擴廠買設備支出；<b>FCF (自由現金流 = OCF - CapEx)</b> 為可自由配息或還債的真錢；<b>獲利含金量 (OCF/NI)</b> = 營業現金流 ÷ 稅後淨利（大於 80%~100% 代表帳面獲利有真正變成現金入袋，非紙上富貴）。",
                fig_cf,
            )
        )

        # 技術面 K 線圖嵌入（若外部日線查詢失敗不阻斷財報戰情室發布）
        tech_html_section = ""
        try:
            from chart_generator import build_stock_technical_chart_html

            tech_fragment = await asyncio.to_thread(
                build_stock_technical_chart_html, stock_id, include_plotlyjs=False
            )
            tech_html_section = (
                "<section id='sec-tech' class='section-card'>"
                "<div class='section-header'>"
                "<h3>📈 技術面 K 線與動能指標（近半年日線）</h3>"
                "<div class='glossary-box'>"
                "💡 <b>技術指標速查</b>："
                "<b>17MA</b> 短線強弱支撐線 ｜ <b>45MA</b> 波段生命防守線 ｜ <b>117MA / 189MA</b> 中長期多空趨勢線 ｜ "
                "<b>KD (隨機指標)</b>：K 向上突破 D 為黃金交叉轉強（>80 高檔鈍化或超買、<20 超賣區）｜ "
                "<b>MACD</b>：DIF 快線突破 Signal 慢線且柱狀翻紅代表波段多頭動能啟動。可直接在 K 線圖上<b>拖曳滑鼠框選區間</b>自動計算區間最高/最低與高低均價。"
                "</div>"
                "</div>"
                f"{tech_fragment}"
                "</section>"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping embedded technical chart for %s: %s", stock_id, exc)

        generated_at = datetime.now(TAIPEI_TIMEZONE).strftime("%Y-%m-%d %H:%M")
        clean_ai_text, _ = self._split_ai_output(ai_text)
        tax_badge = self._taxonomy_badge_sync(stock_id, cached_only=True)
        tax_banner_html = (
            "<div style='width:100%;margin-top:4px'><span style='display:inline-block;"
            "background:#1e3a5f;color:#bfdbfe;border:1px solid #3b82f6;border-radius:999px;"
            f"padding:3px 12px;font-size:13px'>{html.escape(tax_badge)}</span></div>"
            if tax_badge
            else ""
        )

        fund_cards_html: list[str] = []
        for idx, (_, glossary_html, card_fig) in enumerate(chart_blocks):
            fig_html = card_fig.to_html(
                full_html=False,
                include_plotlyjs=False,
                config={"responsive": True, "displaylogo": False},
            )
            fund_cards_html.append(
                "<div class='chart-card'>"
                f"<div class='glossary-box'>{glossary_html}</div>"
                f"{fig_html}"
                "</div>"
            )

        html_parts = [
            "<!doctype html>",
            "<html lang='zh-Hant'><head><meta charset='utf-8' />",
            "<meta name='viewport' content='width=device-width, initial-scale=1' />",
            f"<title>{stock_id} 全方位投資戰情室（財報＋K線）</title>",
            "<script src='https://cdn.plot.ly/plotly-2.35.2.min.js' charset='utf-8'></script>",
            "<style>",
            "body{background:#0b0f14;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Microsoft JhengHei',sans-serif;margin:0;padding:0;line-height:1.6}",
            ".topbar{position:sticky;top:0;z-index:999;background:rgba(11,15,20,0.94);backdrop-filter:blur(8px);border-bottom:1px solid #243042;padding:12px 20px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px}",
            ".topbar h2{margin:0;font-size:20px;color:#f8fafc}",
            ".topbar small{color:#94a3b8;font-size:13px;font-weight:normal;margin-left:8px}",
            ".nav-links{display:flex;gap:8px;flex-wrap:wrap}",
            ".nav-btn{color:#e2e8f0;background:#1e293b;border:1px solid #334155;padding:6px 12px;border-radius:6px;text-decoration:none;font-size:13px;transition:all .15s}",
            ".nav-btn:hover{background:#3b82f6;border-color:#60a5fa;color:#fff}",
            ".container{max-width:1400px;margin:0 auto;padding:16px}",
            ".section-card{background:#111827;border:1px solid #243042;border-radius:10px;padding:16px;margin-bottom:20px;box-shadow:0 4px 12px rgba(0,0,0,0.25)}",
            ".section-card h3{margin:0 0 12px 0;color:#60a5fa;font-size:18px;border-left:4px solid #3b82f6;padding-left:10px}",
            ".ai-pre{white-space:pre-wrap;word-break:break-word;background:#0d131f;border:1px solid #1e293b;border-radius:8px;padding:14px;font-size:14.5px;color:#e2e8f0;margin:0;font-family:inherit}",
            ".chart-card{background:#111827;border:1px solid #243042;border-radius:10px;padding:14px;margin-bottom:18px}",
            ".glossary-box{background:rgba(30,41,59,0.65);border-left:3px solid #f59e0b;border-radius:6px;padding:8px 12px;margin-bottom:10px;font-size:13px;color:#cbd5e1}",
            ".glossary-box b{color:#fbbf24}",
            "</style>",
            "</head><body>",
            "<div class='topbar'>",
            f"<h2>📊 {stock_id} 全方位投資戰情室（基本面財報 ＋ 技術面 K 線）<small>更新：{generated_at}</small></h2>",
            tax_banner_html,
            "<div class='nav-links'>",
            "<a class='nav-btn' href='#sec-ai'>🤖 AI 深度診斷</a>",
            "<a class='nav-btn' href='#sec-tech'>📈 技術面 K 線與均線</a>",
            "<a class='nav-btn' href='#sec-fund'>📊 基本面六大指標圖表</a>",
            "</div>",
            "</div>",
            "<div class='container'>",
            "<section id='sec-ai' class='section-card'>",
            "<h3>🤖 AI 基本面與法說展望深度診斷</h3>",
            f"<div style='color:#93c5fd;margin:6px 0 12px 0'>分析模型：{html.escape(str(ai_text).splitlines()[0].replace('分析模型：', '').strip()) if str(ai_text).lstrip().startswith('分析模型：') else ''}</div>",
            "<pre class='ai-pre'>",
            self._escape_html(clean_ai_text),
            "</pre>",
            "</section>",
            tech_html_section,
            "<section id='sec-fund'>",
            "<h3 style='color:#60a5fa;font-size:18px;border-left:4px solid #3b82f6;padding-left:10px;margin:24px 0 14px 0'>📊 基本面六大核心趨勢圖（每張圖表上方皆可點擊圖例開關線條）</h3>",
            *fund_cards_html,
            "</section>",
            "</div>",
            "</body></html>",
        ]
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        file_path = _OUTPUT_DIR / f"{stock_id}_fund.html"
        file_path.write_text("\n".join(html_parts), encoding="utf-8")

        pages_url = upload_to_github(str(file_path), stock_id, filename_suffix="_fund")
        return pages_url

    _SUMMARY_MARKER = "===SUMMARY_JSON==="
    _SUMMARY_TEXT_FALLBACK_LIMIT = 2500

    def _split_ai_output(
        self, ai_text: str, *, latest_close: float | None = None
    ) -> tuple[str, dict | None]:
        """拆出 (人類可讀分析文字, 結構化摘要 dict)；解析失敗時 dict 為 None。

        解析成功的 dict 會經 sanitize_stage1_json 強制防呆校正。
        """
        cleaned = ai_text.strip()
        if self._SUMMARY_MARKER not in cleaned:
            return cleaned, None
        human, _, tail = cleaned.partition(self._SUMMARY_MARKER)
        match = re.search(r"\{.*\}", tail, flags=re.DOTALL)
        if match is None:
            return human.strip(), None
        try:
            parsed = json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            return human.strip(), None
        if not isinstance(parsed, dict):
            return human.strip(), None
        return human.strip(), sanitize_stage1_json(parsed, latest_close)

    def _summarize_ai(
        self, ai_text: str, *, model_version: str = "", latest_close: float | None = None
    ) -> str:
        """產生寫入 00_Watchlist_Master.ai_summary_json 的內容：優先為結構化 JSON，否則退回較長的純文字。

        SASD v3.5.1: 必須能在快取命中時仍顯示「分析模型」。
        - 若 AI 回傳了 SUMMARY_JSON，將 model_version 填入 JSON 內的 model_version 欄位。
        - 若回傳非 JSON 純文字，則直接把 "分析模型：..." 前綴寫入摘要文字。
        """
        human, parsed = self._split_ai_output(ai_text, latest_close=latest_close)
        if parsed is not None:
            if model_version:
                parsed["model_version"] = model_version
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))

        if model_version:
            return (f"分析模型：{model_version}\n" + human)[: self._SUMMARY_TEXT_FALLBACK_LIMIT]
        return human[: self._SUMMARY_TEXT_FALLBACK_LIMIT]

    def _parse_summary(self, summary: str) -> dict | None:
        try:
            parsed = json.loads(summary)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _render_summary_text(self, summary: str) -> str:
        """將 ai_summary_json 轉為 Discord 可讀文字；非 JSON（舊格式純文字）則原樣回傳。"""
        data = self._parse_summary(summary)
        if data is None:
            return summary
        guidance = data.get("guidance") if isinstance(data.get("guidance"), dict) else {}
        prefix = ""
        if data.get("model_version"):
            prefix = f"分析模型：{data.get('model_version')}\n"
        lines = [prefix + f"**{data.get('verdict', '')}**（體質評分 {data.get('score', 'N/A')}）"]
        tags = data.get("trend_tags") if isinstance(data.get("trend_tags"), dict) else {}
        tag_parts = [str(v) for v in (tags.get(k) for k in _TREND_TAG_KEYS) if v]
        if tag_parts:
            lines.append("趨勢標籤：" + "｜".join(tag_parts))
        for label, key in (("亮點", "highlights"), ("風險", "risks")):
            items = data.get(key) or []
            if isinstance(items, list) and items:
                lines.append(f"{label}：")
                lines.extend(f"- {item}" for item in items)
        for label, key in (
            ("ROE/FCF", "fcf_and_roe"),
            ("股利與籌碼", "chip_and_dividend"),
            ("技術面", "technical_view"),
        ):
            if data.get(key):
                lines.append(f"{label}：{data[key]}")
        if guidance:
            lines.append(
                f"法說展望【{guidance.get('verdict', '無資料')}】：{guidance.get('claims', '')}"
                f"｜佐證：{guidance.get('reality_check', '')}"
            )
        plan = data.get("trade_plan") if isinstance(data.get("trade_plan"), dict) else {}
        if any(plan.get(k) for k in _TRADE_PLAN_KEYS):
            lines.append(
                f"交易計畫：{plan.get('tactical_type', '')}｜進場：{plan.get('entry_trigger', '')}"
                f"｜目標1：{plan.get('target_price_1', '')}｜目標2：{plan.get('target_price_2', '')}"
                f"｜停損：{plan.get('stop_loss_price', '')}｜失效：{plan.get('invalidation', '')}"
            )
        return "\n".join(lines)

    @staticmethod
    def build_snapshot_trend_lines(
        wide_df: pd.DataFrame,
        rev_df: pd.DataFrame,
        bs_latest: dict,
        close: float | None,
        div_latest: dict | None = None,
    ) -> list[str]:
        """SASD v4.0 資料卡 [B] 跨期與估值錨點：PE/PB/近一年現金殖利率、毛利率/營益率 QoQ·YoY、近 3 月平均營收 YoY（含 Stale Data Guard）。"""

        def _f(value: object, suffix: str = "") -> str:
            number = _to_float(value)
            return "N/A" if number is None else f"{number:,.2f}{suffix}"

        def _delta(cur: object, prev: object) -> str:
            c, p = _to_float(cur), _to_float(prev)
            return "N/A" if c is None or p is None else f"{c - p:+.2f}%"

        lines: list[str] = []
        latest: dict = {}
        prev_q: dict = {}
        prev_y: dict = {}
        if wide_df is not None and not wide_df.empty:
            rows = wide_df.to_dict("records")
            latest = rows[-1]
            prev_q = rows[-2] if len(rows) >= 2 else {}
            match = re.fullmatch(r"(\d{4})Q([1-4])", str(latest.get("year_qtr", "")).strip())
            if match:
                target = f"{int(match.group(1)) - 1}Q{match.group(2)}"
                prev_y = next((r for r in rows if str(r.get("year_qtr", "")).strip() == target), {})

        ttm_eps = _to_float(latest.get("ttm_eps"))
        if close is not None and ttm_eps is not None and ttm_eps > 0:
            pe_text = f"{close / ttm_eps:,.2f} 倍（收盤 {close:,.2f} / TTM EPS {ttm_eps:,.2f}）"
        elif close is None:
            pe_text = "N/A（無最新收盤價）"
        else:
            pe_text = "N/A（TTM EPS <= 0 或缺漏）"
        parent_equity = _to_float(bs_latest.get("parent_equity")) if bs_latest else None
        capital_stock = _to_float(bs_latest.get("capital_stock")) if bs_latest else None
        if close is not None and parent_equity and capital_stock and parent_equity > 0 and capital_stock > 0:
            bps = parent_equity / (capital_stock / 10.0)
            pb_text = f"{close / bps:,.2f} 倍（每股淨值 {bps:,.2f}）"
        else:
            pb_text = "N/A"
        div_yield_part = ""
        if div_latest and close is not None and close > 0:
            cash_div = _to_float(div_latest.get("cash_dividend"))
            if cash_div is not None and cash_div >= 0:
                div_yield_part = f"｜近一年現金殖利率 {cash_div / close * 100.0:.2f}%（現金股利 {cash_div:.2f} 元）"
        lines.append(f"  - 估值錨點：本益比 PE {pe_text}｜股價淨值比 PB {pb_text}{div_yield_part}")

        if latest:
            lines.append(
                "  - 三率跨期變動（{q}）：毛利率 {g}（QoQ {gq}, YoY {gy}）｜營益率 {o}（QoQ {oq}, YoY {oy}）".format(
                    q=latest.get("year_qtr", "N/A"),
                    g=_f(latest.get("gross_margin_pct"), "%"),
                    gq=_delta(latest.get("gross_margin_pct"), prev_q.get("gross_margin_pct")),
                    gy=_delta(latest.get("gross_margin_pct"), prev_y.get("gross_margin_pct")),
                    o=_f(latest.get("op_margin_pct"), "%"),
                    oq=_delta(latest.get("op_margin_pct"), prev_q.get("op_margin_pct")),
                    oy=_delta(latest.get("op_margin_pct"), prev_y.get("op_margin_pct")),
                )
            )

        if rev_df is not None and not rev_df.empty and "yoy_pct" in rev_df.columns:
            latest_ym = rev_df["rev_ym"].iloc[-1] if "rev_ym" in rev_df.columns else None
            is_stale, stale_msg = check_revenue_staleness(latest_ym) if latest_ym is not None else (False, "")
            if is_stale:
                lines.append(
                    f"  - 營收動能：{stale_msg}（請優先採信季報 qtr_rev_yoy_pct 與 [C] 區趨勢標籤）"
                )
            else:
                yoys = [v for v in (_to_float(x) for x in rev_df["yoy_pct"].tail(3).tolist()) if v is not None]
                latest_yoy = _to_float(rev_df["yoy_pct"].iloc[-1])
                if yoys and latest_yoy is not None:
                    avg3 = sum(yoys) / len(yoys)
                    if latest_yoy > avg3 + 5:
                        trend = "加速成長"
                    elif latest_yoy < avg3 - 5:
                        trend = "高檔鈍化／動能趨緩"
                    else:
                        trend = "大致持平"
                    lines.append(
                        f"  - 營收動能：近 {len(yoys)} 個月平均 YoY {avg3:,.2f}%｜最新單月 YoY {latest_yoy:,.2f}%（{trend}）"
                    )
        return lines

    def build_quant_snapshot(self, stock_id: str, *, close: float | None = None) -> str:
        """由 Google Sheets 快取精確取出最新一期財務/領先指標/籌碼，供跨股決策資料卡 [B] 區使用（不經 AI）。

        close：最新收盤價（供計算 PE/PB）；未提供時估值欄位標註 N/A。
        """

        def _history(sheet_name: str) -> pd.DataFrame:
            frame = self.cache._read_stock_rows(self.cache._get_worksheet(sheet_name), stock_id)  # noqa: SLF001
            if frame.empty:
                return frame
            if "raw_date" in frame.columns:
                frame = frame.sort_values("raw_date")
            return frame.reset_index(drop=True)

        def _latest(sheet_name: str) -> dict:
            frame = _history(sheet_name)
            if frame.empty:
                return {}
            return frame.iloc[-1].to_dict()

        def _fmt(value: object, suffix: object = "", decimals: int = 2) -> str:
            if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
                return "N/A"
            dec = decimals
            suf = str(suffix) if suffix != "" else ""
            if isinstance(suffix, int):
                dec = suffix
                suf = ""
            try:
                number = float(value)
            except (TypeError, ValueError):
                return str(value)
            return f"{number:,.{dec}f}{suf}"

        wide_df = _history("05_AI_Metrics_Wide")
        rev_df = _history("01_Monthly_Revenue")
        wide = wide_df.iloc[-1].to_dict() if not wide_df.empty else {}
        revenue = rev_df.iloc[-1].to_dict() if not rev_df.empty else {}
        chip = _latest("07_Chip_Summary")
        bs_latest = _latest("03_Balance_Sheet")
        div_latest = _latest("06_Dividend_Policy")
        is_rev_stale, rev_stale_msg = check_revenue_staleness(revenue.get("rev_ym"))
        rev_stale_suffix = f"｜{rev_stale_msg}" if is_rev_stale else ""

        lines = [
            (
                f"  - 最新財報期別：{wide.get('year_qtr', 'N/A')}｜毛利率：{_fmt(wide.get('gross_margin_pct'), '%')}"
                f"｜營益率：{_fmt(wide.get('op_margin_pct'), '%')}｜TTM EPS：{_fmt(wide.get('ttm_eps'))}"
                f"｜ROE：{_fmt(wide.get('parent_roe_pct'), '%')}"
            ),
            (
                f"  - 領先與現金流：合約負債：{_fmt(wide.get('contract_liab_亿元'), ' 億')}"
                f"｜CapEx：{_fmt(wide.get('capex_亿元'), ' 億')}｜CCC：{_fmt(wide.get('ccc_days'), ' 天')}"
                f"（DIO {_fmt(wide.get('dio_days'), ' 天')}）｜單季 FCF：{_fmt(wide.get('fcf_亿元'), ' 億')}"
                f"｜OCF/NI：{_fmt(wide.get('ocf_to_ni_pct'), '%')}"
            ),
            (
                f"  - 償債：淨負債比 {_fmt(wide.get('net_debt_ratio_pct'), '%')}｜速動比 {_fmt(wide.get('quick_ratio_pct'), '%')}"
                f"｜利息保障倍數 {_fmt(wide.get('interest_coverage'))}"
            ),
            (
                f"  - 最新月營收（{revenue.get('rev_ym', 'N/A')}）：MoM {_fmt(revenue.get('mom_pct'), '%')}"
                f"｜YoY {_fmt(revenue.get('yoy_pct'), '%')}{rev_stale_suffix}"
            ),
            (
                f"  - 法人籌碼（{chip.get('trade_week', 'N/A')}）：外資持股 {_fmt(chip.get('foreign_holding_pct'), '%')}"
                f"（20日 {_fmt(chip.get('foreign_holding_chg_20d'), '%')}）｜外資 5日/20日 {_fmt(chip.get('foreign_net_buy_5d_lots'))}"
                f"/{_fmt(chip.get('foreign_net_buy_20d_lots'))} 張｜投信 5日/20日 {_fmt(chip.get('trust_net_buy_5d_lots'))}"
                f"/{_fmt(chip.get('trust_net_buy_20d_lots'))} 張｜三大法人 5日 {_fmt(chip.get('three_inst_net_5d_lots'))} 張"
            ),
            (
                f"  - 信用籌碼與維持率：{chip.get('chip_quadrant_tag', 'N/A')}｜"
                f"融資餘額 {_fmt(chip.get('margin_balance_lots'), 0)} 張（使用率 {_fmt(chip.get('margin_utilization_pct'), '%')}）｜"
                f"融資 5日/20日增減 {_fmt(chip.get('margin_chg_5d_lots'), 0)}/{_fmt(chip.get('margin_chg_20d_lots'), 0)} 張｜"
                f"估算融資維持率 {_fmt(chip.get('est_margin_maint_pct'), '%')}｜券資比 {_fmt(chip.get('short_margin_ratio_pct'), '%')}"
            ),
        ]
        lines.extend(self.build_snapshot_trend_lines(wide_df, rev_df, bs_latest, close, div_latest=div_latest))
        try:
            is_df = _history("02_Income_Statement")
            sem_block = self.build_ai_fundamental_prompt_block(
                stock_id,
                close=close,
                wide_df=wide_df,
                is_df=is_df,
                rev_df=rev_df,
            )
            if sem_block:
                lines.append("  - " + "\n    ".join(sem_block.splitlines()))
        except Exception:  # noqa: BLE001
            logger.debug("Phase 2 semantic block skipped for %s", stock_id, exc_info=True)
        return "\n".join(lines)

    def compute_cross_sectional_pe_map(self, close_map: Mapping[str, float]) -> dict[str, float]:
        """從快取表 `05_AI_Metrics_Wide` 最新 TTM EPS 與當日收盤價建立全池橫斷面 PE 對照表。"""
        if not close_map:
            return {}
        try:
            ws = self.cache._get_worksheet("05_AI_Metrics_Wide")  # noqa: SLF001
            records = self.cache._get_records_cached(ws)  # noqa: SLF001
        except Exception:  # noqa: BLE001
            return {}

        latest_by_sid: dict[str, tuple[str, float]] = {}
        for rec in records:
            sid = str(rec.get("stock_id") or "").strip()
            if not sid:
                continue
            ttm_eps = _to_float(rec.get("ttm_eps"))
            if ttm_eps is None or ttm_eps <= 0:
                continue
            raw_dt = str(rec.get("raw_date") or rec.get("year_qtr") or "")
            prev = latest_by_sid.get(sid)
            if prev is None or raw_dt >= prev[0]:
                latest_by_sid[sid] = (raw_dt, ttm_eps)

        pe_map: dict[str, float] = {}
        for sid, close_px in close_map.items():
            c_val = _to_float(close_px)
            if c_val is None or c_val <= 0:
                continue
            entry = latest_by_sid.get(str(sid).strip())
            if entry and entry[1] > 0:
                pe_map[str(sid).strip()] = round(c_val / entry[1], 4)
        return pe_map

    def compute_fundamental_features(
        self,
        stock_id: str,
        *,
        close: float | None = None,
        stock_name: str = "",
        sub_industry: str = "",
        sector_benchmark_pe: float | None = None,
        sector_pe_lookup: Mapping[str, float] | None = None,
        fallback_market_cap: float | None = None,
        wide_df: pd.DataFrame | None = None,
        is_df: pd.DataFrame | None = None,
        rev_df: pd.DataFrame | None = None,
        bs_df: pd.DataFrame | None = None,
    ) -> FundamentalFeatureSummary:
        """從快取表讀取財報、月營收與資產負債表，產出結構化 `FundamentalFeatureSummary`。"""
        if wide_df is None:
            wide_df = self.cache._read_stock_rows(self.cache._get_worksheet("05_AI_Metrics_Wide"), stock_id)  # noqa: SLF001
        if is_df is None:
            is_df = self.cache._read_stock_rows(self.cache._get_worksheet("02_Income_Statement"), stock_id)  # noqa: SLF001
        if rev_df is None:
            rev_df = self.cache._read_stock_rows(self.cache._get_worksheet("01_Monthly_Revenue"), stock_id)  # noqa: SLF001
        if bs_df is None:
            try:
                bs_df = self.cache._read_stock_rows(self.cache._get_worksheet("03_Balance_Sheet"), stock_id)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                bs_df = pd.DataFrame()

        combined_q: list[dict[str, Any]] = []
        if is_df is not None and not is_df.empty:
            is_map = {str(r.get("year_qtr", "")): dict(r) for r in is_df.to_dict("records")}
        else:
            is_map = {}

        if wide_df is not None and not wide_df.empty:
            for r in wide_df.to_dict("records"):
                qtr = str(r.get("year_qtr", ""))
                merged_row = {**is_map.get(qtr, {}), **dict(r)}
                combined_q.append(merged_row)
        elif is_map:
            combined_q = list(is_map.values())

        paid_in_capital_twd = 0.0
        if bs_df is not None and not bs_df.empty and "capital_stock" in bs_df.columns:
            sorted_bs = bs_df.sort_values("raw_date") if "raw_date" in bs_df.columns else bs_df
            cap_series = pd.to_numeric(sorted_bs["capital_stock"], errors="coerce").dropna()
            if not cap_series.empty:
                paid_in_capital_twd = float(cap_series.iloc[-1])

        if fallback_market_cap is None and paid_in_capital_twd > 0 and close is not None and close > 0:
            fallback_market_cap = (paid_in_capital_twd / 10.0) * float(close)

        tax_svc = None
        if not sub_industry or sector_benchmark_pe is None:
            try:
                from taxonomy_service import TaxonomyService

                tax_svc = TaxonomyService()
                if not sub_industry:
                    rec = tax_svc.get_cached(stock_id)
                    if rec:
                        sub_industry = str(rec.get("sub_industry") or "").strip()
                        if not stock_name:
                            stock_name = str(rec.get("stock_name") or "").strip()
            except Exception:  # noqa: BLE001
                tax_svc = None

        resolved_sub = sub_industry or "未分類"
        if sector_benchmark_pe is None:
            if tax_svc is not None and sector_pe_lookup:
                try:
                    sector_benchmark_pe = tax_svc.compute_sector_median_pe(resolved_sub, sector_pe_lookup)
                except Exception:  # noqa: BLE001
                    sector_benchmark_pe = 20.0
            else:
                sector_benchmark_pe = 20.0

        return compute_fundamental_features(
            stock_id=stock_id,
            stock_name=stock_name,
            sub_industry=resolved_sub,
            quarterly_statements=combined_q,
            monthly_revenues=rev_df,
            latest_close_price=close,
            fallback_market_cap=fallback_market_cap,
            sector_benchmark_pe=float(sector_benchmark_pe),
            paid_in_capital_twd=paid_in_capital_twd,
        )

    def build_ai_fundamental_prompt_block(
        self,
        stock_id: str,
        *,
        close: float | None = None,
        stock_name: str = "",
        sub_industry: str = "",
        sector_benchmark_pe: float | None = None,
        sector_pe_lookup: Mapping[str, float] | None = None,
        wide_df: pd.DataFrame | None = None,
        is_df: pd.DataFrame | None = None,
        rev_df: pd.DataFrame | None = None,
        bs_df: pd.DataFrame | None = None,
    ) -> str:
        """從快取表讀取財報與月營收，產出 SASD v5.4.0 Phase 2 高密度語意特徵區塊。"""
        return self.compute_fundamental_features(
            stock_id=stock_id,
            close=close,
            stock_name=stock_name,
            sub_industry=sub_industry,
            sector_benchmark_pe=sector_benchmark_pe,
            sector_pe_lookup=sector_pe_lookup,
            wide_df=wide_df,
            is_df=is_df,
            rev_df=rev_df,
            bs_df=bs_df,
        ).prompt_block

    def _escape_html(self, text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
