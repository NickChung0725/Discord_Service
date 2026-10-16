"""Fundamental analyzer for /stock_ai.

Responsibilities:
- Slice sliding windows for AI and charts
- Build markdown context from cached Sheets data (using in-memory cache)
- Call Gemini via ai_weather.ask_gemini
- Render Plotly war-room HTML and publish to GitHub Pages under /charts
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests

from ai_weather import ask_gemini
from finmind_stock_scanner import (
    MOVING_AVERAGE_PERIODS,
    classify_latest_signal,
    describe_ma17_trend,
)
from github_uploader import upload_to_github
from fundamental_cache import FundamentalCacheManager
from fundamental_config import PROMPT_TEMPLATES


TAIPEI_TIMEZONE = timezone(timedelta(hours=8))
_OUTPUT_DIR = Path(__file__).resolve().parent / "generated_charts"
_DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent / "finmind_reports"


@dataclass
class DiagnosisReport:
    stock_id: str
    ai_text: str
    war_room_url: str | None
    cached: bool
    cache_period: str | None


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
            if not title or not link:
                continue
            items.append(
                {
                    "title": title,
                    "link": link,
                    "pubDate": pub_date,
                    "source": source,
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

        if not items:
            return "（本次沒有抓到相關新聞）"

        lines = ["\n■ 即時新聞 (Google News RSS)"]
        for it in items:
            meta = ""
            if it.get("source") or it.get("pubDate"):
                meta = f" — {it.get('source', '').strip()} {it.get('pubDate', '').strip()}".strip()
            lines.append(f"- [{it['title']}]({it['link']}){meta}")
        return "\n".join(lines)

    def build_markdown_context(self, stock_id: str) -> str:
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
        parts.append(PROMPT_TEMPLATES["system_rules"])
        parts.append("\n==================================================")
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
                parts.append(show.to_markdown(index=False))

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
                parts.append(show.to_markdown(index=False))

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
            parts.append(show.to_markdown(index=False))

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
                parts.append(show.to_markdown(index=False))

        # Table 5: monthly revenue momentum
        if not df_rev.empty:
            cols = ["rev_ym", "revenue", "mom_pct", "yoy_pct", "raw_date"]
            show = df_rev[[c for c in cols if c in df_rev.columns]].copy()
            parts.append("\n■ 表 5：近期月營收動能 (近 12 個月)")
            parts.append(show.to_markdown(index=False))

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
            parts.append(show.to_markdown(index=False))
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
            parts.append(show.to_markdown(index=False))

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
            signal_info = classify_latest_signal(stock_prices)
            if signal_info is not None:
                signal_status = f"✅ 符合強勢均線排列訊號（{signal_info['signal']}）"
            else:
                signal_status = "➖ 未同時滿足五線強勢排列條件"

            recent_5d = stock_prices.tail(5).copy()
            recent_5d["date"] = recent_5d["date"].dt.strftime("%Y-%m-%d")
            display_cols = ["date", "close", *(f"MA{p}" for p in MOVING_AVERAGE_PERIODS)]
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
                recent_5d[display_cols].to_markdown(index=False),
            ]
            return "\n".join(lines)
        except Exception:
            return ""

    async def run_full_diagnosis(self, stock_id: str, *, force_ai: bool = False) -> DiagnosisReport:
        watchlist = self.cache._get_worksheet("00_Watchlist_Master")  # noqa: SLF001
        records = self.cache._get_records_cached(watchlist)  # noqa: SLF001
        row = next(
            (r for r in records if str(r.get("stock_id", "")).strip() == stock_id),
            None,
        )

        cache_period = None
        if row:
            last_fin = str(row.get("last_fin_qtr", "")).strip()
            last_rev = str(row.get("last_rev_ym", "")).strip()
            max_div = str(row.get("max_div_raw_date", "")).strip()
            max_chip = str(row.get("max_chip_raw_date", "")).strip()
            ai_cache_day = str(row.get("ai_cache_day", "")).strip() or datetime.now(TAIPEI_TIMEZONE).date().isoformat()
            if last_fin and last_rev:
                cache_period = f"{last_fin}_{last_rev}_{max_div}_{max_chip}_{ai_cache_day}"

        if row and not force_ai and cache_period and str(row.get("ai_cached_period", "")).strip() == cache_period:
            ai_summary = str(row.get("ai_summary_json", "")).strip()
            war_room_url = str(row.get("war_room_url", "")).strip() or None
            return DiagnosisReport(
                stock_id=stock_id,
                ai_text=ai_summary or "（已快取但無摘要內容）",
                war_room_url=war_room_url,
                cached=True,
                cache_period=cache_period,
            )

        context = self.build_markdown_context(stock_id)
        prompt = (
            context
            + "\n\n==================================================\n"
            + "【任務】\n"
            + PROMPT_TEMPLATES["full_diagnosis"]
        )
        ai_text = await ask_gemini(prompt)

        war_room_url = await self.render_and_publish_war_room_html(stock_id, ai_text)

        summary = self._summarize_ai(ai_text)
        self.cache._update_watchlist_master(  # noqa: SLF001
            watchlist,
            stock_id,
            {
                "ai_cache_day": datetime.now(TAIPEI_TIMEZONE).date().isoformat(),
                "ai_cached_period": cache_period or "",
                "ai_summary_json": summary,
                "war_room_url": war_room_url,
            },
        )

        return DiagnosisReport(
            stock_id=stock_id,
            ai_text=summary,
            war_room_url=war_room_url,
            cached=False,
            cache_period=cache_period,
        )

    async def run_targeted_qa(self, stock_id: str, user_prompt: str) -> str:
        context = self.build_markdown_context(stock_id)
        prompt = (
            context
            + "\n\n==================================================\n"
            + "【使用者詢問問題】\n"
            + user_prompt.strip()
            + "\n\n"
            + PROMPT_TEMPLATES["targeted_qa"]
        )
        return await ask_gemini(prompt)

    async def render_and_publish_war_room_html(self, stock_id: str, ai_text: str) -> str:
        df_wide = self.cache._read_stock_rows(self.cache._get_worksheet("05_AI_Metrics_Wide"), stock_id)  # noqa: SLF001
        df_rev = self.cache._read_stock_rows(self.cache._get_worksheet("01_Monthly_Revenue"), stock_id)  # noqa: SLF001
        df_cf = self.cache._read_stock_rows(self.cache._get_worksheet("04_Cash_Flow"), stock_id)  # noqa: SLF001

        df_wide = self.slice_sliding_window(df_wide, self.chart_window_quarters)
        df_rev = self.slice_sliding_window(df_rev, self.chart_window_months)
        df_cf = self.slice_sliding_window(df_cf, self.chart_window_quarters)

        fig = make_subplots(
            rows=6,
            cols=1,
            shared_xaxes=False,
            vertical_spacing=0.07,
            subplot_titles=(
                "月營收與 YoY",
                "三率走勢 (毛利率/營益率/母公司淨利率)",
                "營業費用率與業外佔稅前淨利比",
                "CCC 現金轉換週期 (DIO/DSO/DPO/CCC)",
                "單季 EPS 與 TTM EPS",
                "OCF / CapEx / FCF + 獲利含金量(OCF/NI)",
            ),
        )

        if not df_rev.empty and {"rev_ym", "revenue"}.issubset(df_rev.columns):
            x = df_rev["rev_ym"].astype(str)
            y = pd.to_numeric(df_rev["revenue"], errors="coerce") / 1e8
            yoy = pd.to_numeric(df_rev.get("yoy_pct"), errors="coerce")
            fig.add_trace(go.Bar(x=x, y=y, name="月營收(億)"), row=1, col=1)
            if yoy is not None:
                fig.add_trace(go.Scatter(x=x, y=yoy, name="YoY(%)", yaxis="y2"), row=1, col=1)

        if not df_wide.empty:
            x = df_wide["year_qtr"].astype(str)
            for col, name in (
                ("gross_margin_pct", "毛利率(%)"),
                ("op_margin_pct", "營益率(%)"),
                ("parent_net_margin_pct", "母公司淨利率(%)"),
            ):
                if col in df_wide.columns:
                    fig.add_trace(
                        go.Scatter(x=x, y=pd.to_numeric(df_wide[col], errors="coerce"), name=name),
                        row=2,
                        col=1,
                    )

            for col, name in (
                ("op_expense_pct", "營業費用率(%)"),
                ("non_op_share_pct", "業外佔稅前比(%)"),
                ("parent_roe_pct", "母公司ROE(%)"),
            ):
                if col in df_wide.columns:
                    fig.add_trace(
                        go.Scatter(
                            x=x,
                            y=pd.to_numeric(df_wide[col], errors="coerce"),
                            name=name,
                        ),
                        row=3,
                        col=1,
                    )

            for col, name in (
                ("ccc_days", "CCC(天)"),
                ("dio_days", "DIO"),
                ("dso_days", "DSO"),
                ("dpo_days", "DPO"),
            ):
                if col in df_wide.columns:
                    fig.add_trace(
                        go.Scatter(
                            x=x,
                            y=pd.to_numeric(df_wide[col], errors="coerce"),
                            name=name,
                        ),
                        row=4,
                        col=1,
                    )

            if "eps" in df_wide.columns:
                fig.add_trace(
                    go.Bar(x=x, y=pd.to_numeric(df_wide["eps"], errors="coerce"), name="EPS"),
                    row=5,
                    col=1,
                )
            if "ttm_eps" in df_wide.columns:
                fig.add_trace(
                    go.Scatter(
                        x=x,
                        y=pd.to_numeric(df_wide["ttm_eps"], errors="coerce"),
                        name="TTM EPS",
                    ),
                    row=5,
                    col=1,
                )

            if "ocf_to_ni_pct" in df_wide.columns:
                fig.add_trace(
                    go.Scatter(
                        x=x,
                        y=pd.to_numeric(df_wide["ocf_to_ni_pct"], errors="coerce"),
                        name="OCF/NI(%)",
                    ),
                    row=6,
                    col=1,
                )

        if not df_cf.empty and "year_qtr" in df_cf.columns:
            x = df_cf["year_qtr"].astype(str)
            for col, name in (
                ("operating_cf", "OCF"),
                ("capex", "CapEx"),
                ("free_cf", "FCF"),
            ):
                if col in df_cf.columns:
                    fig.add_trace(
                        go.Bar(
                            x=x,
                            y=pd.to_numeric(df_cf[col], errors="coerce") / 1e8,
                            name=f"{name}(億)",
                        ),
                        row=6,
                        col=1,
                    )

        fig.update_layout(
            template="plotly_dark",
            height=1500,
            title=f"{stock_id} 基本面 AI 戰情室",
            legend={"orientation": "h", "y": -0.08},
            margin={"l": 40, "r": 30, "t": 80, "b": 80},
        )

        generated_at = datetime.now(TAIPEI_TIMEZONE).strftime("%Y-%m-%d %H:%M")
        html_parts = [
            "<!doctype html>",
            "<html><head><meta charset='utf-8' />",
            "<meta name='viewport' content='width=device-width, initial-scale=1' />",
            f"<title>{stock_id} Fundamental War Room</title>",
            "</head><body style='background:#0b0f14;color:#e6edf3;font-family:Arial,sans-serif'>",
            f"<h2 style='margin:16px'> {stock_id} 基本面 AI 戰情室 <small style='color:#9aa4ad'>({generated_at})</small></h2>",
            "<div style='margin:16px'>",
            "<h3>AI 摘要</h3>",
            "<pre style='white-space:pre-wrap;background:#111827;border:1px solid #243042;border-radius:8px;padding:12px'>",
            self._escape_html(ai_text),
            "</pre>",
            "</div>",
            fig.to_html(full_html=False, include_plotlyjs="cdn"),
            "</body></html>",
        ]
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        file_path = _OUTPUT_DIR / f"{stock_id}_fund.html"
        file_path.write_text("\n".join(html_parts), encoding="utf-8")

        pages_url = upload_to_github(str(file_path), stock_id, filename_suffix="_fund")
        return pages_url

    def _summarize_ai(self, ai_text: str) -> str:
        cleaned = ai_text.strip()
        return cleaned[:1500]

    def _escape_html(self, text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )