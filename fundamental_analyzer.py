"""Fundamental analyzer for /stock_ai.

Responsibilities:
- Slice sliding windows for AI and charts
- Build markdown context from cached Sheets data (using in-memory cache)
- Call Gemini via ai_service.ask_gemini_with_meta / ask_gemini_diagnosis_with_meta
- Render Plotly war-room HTML and publish to GitHub Pages under /charts
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import difflib
import html
import json
import logging
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests

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
from fundamental_config import PROMPT_TEMPLATES


TAIPEI_TIMEZONE = timezone(timedelta(hours=8))
_OUTPUT_DIR = Path(__file__).resolve().parent / "generated_charts"
_DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent / "finmind_reports"

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

# Stage 0 硬性排雷門檻（/stock_ai 軟性警示；22:00 排程硬性攔截）
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

    metrics 鍵：vol_lots、op_margin_pct、contract_liab（億）。
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
    match = _PRICE_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def sanitize_stage1_json(summary_json: dict, latest_close: float | None = None) -> dict:
    """解析 SUMMARY_JSON 後的硬校正，避免小模型違規輸出污染快取。

    - 法說無實質內容 → claims="無"、verdict="無資料"。
    - highlights/risks 至少 1 筆、至多 4 筆。
    - 補齊 trend_tags / trade_plan 結構；目標價不高於最新收盤價時加註警示。
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
    if latest_close and latest_close > 0:
        for key in ("target_price_1", "target_price_2"):
            price = _first_price(plan.get(key))
            text = str(plan.get(key, ""))
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

        lines = [title_block, "以下為媒體對管理層說法的報導摘要；請只引用本區內容，不得自行補充展望。"]
        for it in relevant:
            meta = f" — {it.get('source', '').strip()} {it.get('pubDate', '').strip()}".rstrip()
            lines.append(f"- {it['title']}{meta}")
            description = it.get("description", "").strip()
            if description and description != it["title"]:
                lines.append(f"  摘要：{description[:200]}")
        return "\n".join(lines)

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

        parts: list[str] = []
        parts.append(f"【標的：{stock_id} — Google Sheets 法人級深度財報與籌碼數據包】")

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
            parts.append("\n■ 表 1：損益結構、營業費用率與本業/業外歸因 (近 8 季)")
            if not show.empty:
                parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 2: CCC + contract liabilities + financial assets
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

            base = pd.DataFrame()
            if not df_wide.empty and "year_qtr" in df_wide.columns:
                base = df_wide[[c for c in cols if c in df_wide.columns]].copy()

            if not df_bs.empty and "year_qtr" in df_bs.columns:
                bs_part = df_bs[[c for c in cols if c in df_bs.columns]].copy()
                base = bs_part if base.empty else base.merge(bs_part, on="year_qtr", how="left")

            show = base[[c for c in cols if c in base.columns]].copy() if not base.empty else pd.DataFrame()
            parts.append("\n■ 表 2：營運週轉效率 (CCC)、合約負債與金融資產配置 (近 8 季)")
            if not show.empty:
                parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 3: cash flow breakdown
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
            parts.append("\n■ 表 3：單季現金流量拆解、營運資金變動與含金量 (近 8 季)")
            parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 4: leverage and solvency
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
            parts.append("\n■ 表 4：財務槓桿、有息負債結構與償債能力 (近 8 季)")
            if not show.empty:
                parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 5: monthly revenue momentum
        if not df_rev.empty:
            cols = ["rev_ym", "revenue", "mom_pct", "yoy_pct", "raw_date"]
            show = df_rev[[c for c in cols if c in df_rev.columns]].copy()
            parts.append("\n■ 表 5：近期月營收動能 (近 12 個月)")
            parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 6: dividend + chip summary
        parts.append("\n■ 表 6：歷年股利政策與近期外資/投信/自營商法人籌碼動向")
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
            ]
            show = df_chip[[c for c in cols if c in df_chip.columns]].copy()
            parts.append("\n- 【法人籌碼 (最新週)】")
            parts.append(show.to_markdown(index=False, floatfmt=".2f"))

        # Table 7: Optional latest prices.csv moving averages (if available locally)
        ma_context = self.build_latest_ma_context(stock_id)
        if ma_context:
            parts.append(ma_context)

        # News injection (using cached Watchlist_Master)
        stock_name = ""
        try:
            watchlist = self.cache._get_worksheet("00_Watchlist_Master")  # noqa: SLF001
            records = self.cache._get_records_cached(watchlist)  # noqa: SLF001
            row = next((r for r in records if str(r.get("stock_id", "")).strip() == stock_id), None)
            if row:
                stock_name = str(row.get("stock_name", "")).strip()
        except Exception:
            stock_name = ""

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
            if (
                ma17_val is not None
                and ma45_val is not None
                and not pd.isna(ma17_val)
                and not pd.isna(ma45_val)
                and float(ma17_val) > 0
            ):
                gap_pct = (float(ma17_val) - float(ma45_val)) / float(ma17_val) * 100.0
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
                f"- 17MA與45MA差距：{gap_str}｜17MA近期走勢：{ma17_trend}｜均線訊號：{signal_status}",
            ]
            if vol_line:
                lines.append(vol_line)
            lines.append(recent_5d[display_cols].to_markdown(index=False, floatfmt=".2f"))
            return "\n".join(lines)
        except Exception:
            return ""

    def get_hard_screen_metrics(self, stock_id: str) -> dict:
        """Stage 0：取最新收盤/日量（本地 prices.csv）與最近一季營益率、合約負債（億）。缺資料者為 None。"""
        metrics: dict = {"close": None, "vol_lots": None, "op_margin_pct": None, "contract_liab": None}
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

    async def run_full_diagnosis(
        self,
        stock_id: str,
        *,
        force_ai: bool = False,
        publish_war_room: bool = True,
        master: bool = False,
    ) -> DiagnosisReport:
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

            return DiagnosisReport(
                stock_id=stock_id,
                ai_text=cached_text,
                war_room_url=war_room_url,
                cached=True,
                cache_period=cache_period,
                summary_json=cached_summary,
                model_version=str(row.get("ai_model_version", "")).strip() if row else "",
            )

        context = self.build_markdown_context(stock_id, include_outlook=True)
        # Stage 0 軟性警示：不擋股票，僅注入「系統硬傷警示」交由 AI 診斷。
        hard_metrics = self.get_hard_screen_metrics(stock_id)
        hard_warnings = build_hard_warnings(hard_metrics)
        if hard_warnings:
            context += "\n\n🚨 系統硬傷警示（Python 預篩，請於 Step 5~7 列入風險並反映於評分與操作定位）：\n" + "\n".join(
                f"- {w}" for w in hard_warnings
            )
        latest_close = hard_metrics.get("close")
        prompt = PROMPT_TEMPLATES["full_diagnosis"].replace("{DATA_PACKAGE_PLACEHOLDER}", context)
        meta = await ask_gemini_diagnosis_with_meta(prompt, tag=f"stock_diag:{stock_id}", master=master)
        raw_ai_text = meta.text
        human_text, _parsed = self._split_ai_output(raw_ai_text, latest_close=latest_close)
        human_text = f"分析模型：{meta.model_version}\n" + human_text

        war_room_url = None
        if publish_war_room:
            war_room_url = await self.render_and_publish_war_room_html(stock_id, human_text)
        elif row:
            war_room_url = str(row.get("war_room_url", "")).strip() or None

        summary = self._summarize_ai(raw_ai_text, model_version=meta.model_version, latest_close=latest_close)
        self.cache._update_watchlist_master(  # noqa: SLF001
            watchlist,
            stock_id,
            {
                "ai_cache_day": datetime.now(TAIPEI_TIMEZONE).date().isoformat(),
                "ai_cached_period": cache_period or "",
                "ai_summary_json": summary,
                "ai_model_version": meta.model_version,
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
            model_version=meta.model_version,
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
        meta = await ask_gemini_with_meta(prompt, tag=f"stock_qa:{stock_id}")
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
    ) -> list[str]:
        """SASD v4.0 資料卡 [B] 跨期與估值錨點：PE/PB、毛利率/營益率 QoQ·YoY、近 3 月平均營收 YoY。"""

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
        lines.append(f"  - 估值錨點：本益比 PE {pe_text}｜股價淨值比 PB {pb_text}")

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

        def _fmt(value: object, suffix: str = "") -> str:
            if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
                return "N/A"
            try:
                number = float(value)
            except (TypeError, ValueError):
                return str(value)
            return f"{number:,.2f}{suffix}"

        wide_df = _history("05_AI_Metrics_Wide")
        rev_df = _history("01_Monthly_Revenue")
        wide = wide_df.iloc[-1].to_dict() if not wide_df.empty else {}
        revenue = rev_df.iloc[-1].to_dict() if not rev_df.empty else {}
        chip = _latest("07_Chip_Summary")
        bs_latest = _latest("03_Balance_Sheet")

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
                f"｜YoY {_fmt(revenue.get('yoy_pct'), '%')}"
            ),
            (
                f"  - 法人籌碼（{chip.get('trade_week', 'N/A')}）：外資持股 {_fmt(chip.get('foreign_holding_pct'), '%')}"
                f"（20日 {_fmt(chip.get('foreign_holding_chg_20d'), '%')}）｜外資 5日/20日 {_fmt(chip.get('foreign_net_buy_5d_lots'))}"
                f"/{_fmt(chip.get('foreign_net_buy_20d_lots'))} 張｜投信 5日/20日 {_fmt(chip.get('trust_net_buy_5d_lots'))}"
                f"/{_fmt(chip.get('trust_net_buy_20d_lots'))} 張｜三大法人 5日 {_fmt(chip.get('three_inst_net_5d_lots'))} 張"
            ),
        ]
        lines.extend(self.build_snapshot_trend_lines(wide_df, rev_df, bs_latest, close))
        return "\n".join(lines)

    def _escape_html(self, text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )