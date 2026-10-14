"""Fundamental analyzer for /stock_ai.

Responsibilities:
- Slice sliding windows for AI and charts
- Build markdown context from cached Sheets data
- Call Gemini via ai_weather.ask_gemini
- Render Plotly war-room HTML and publish to GitHub Pages under /charts

This module assumes fundamentals are already synced by FundamentalCacheManager.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ai_weather import ask_gemini
from github_uploader import upload_to_github
from fundamental_cache import FundamentalCacheManager, SyncResult
from fundamental_config import PROMPT_TEMPLATES


TAIPEI_TIMEZONE = timezone(timedelta(hours=8))
_OUTPUT_DIR = Path(__file__).resolve().parent / "generated_charts"


@dataclass
class DiagnosisReport:
    stock_id: str
    ai_text: str
    war_room_url: str | None
    cached: bool
    cache_period: str | None


class FundamentalAnalyzer:
    def __init__(self, cache: FundamentalCacheManager) -> None:
        self.cache = cache

        self.ai_window_quarters = int(os.getenv("AI_WINDOW_QUARTERS", "8") or "8")
        self.ai_window_months = int(os.getenv("AI_WINDOW_MONTHS", "12") or "12")
        self.chart_window_quarters = int(os.getenv("CHART_WINDOW_QUARTERS", "16") or "16")
        self.chart_window_months = int(os.getenv("CHART_WINDOW_MONTHS", "36") or "36")

    def slice_sliding_window(self, df_full: pd.DataFrame, window_size: int) -> pd.DataFrame:
        if df_full.empty:
            return df_full
        if "raw_date" in df_full.columns:
            df_full = df_full.sort_values("raw_date")
        return df_full.tail(window_size).reset_index(drop=True)

    def build_markdown_context(self, stock_id: str) -> str:
        # Read from Sheets each time (simple v1). Optimization: pass dfs from cache.
        ss = self.cache._spreadsheet  # noqa: SLF001
        df_wide = self.cache._read_stock_rows(ss.worksheet("05_AI_Metrics_Wide"), stock_id)  # noqa: SLF001
        df_cf = self.cache._read_stock_rows(ss.worksheet("04_Cash_Flow"), stock_id)  # noqa: SLF001
        df_rev = self.cache._read_stock_rows(ss.worksheet("01_Monthly_Revenue"), stock_id)  # noqa: SLF001

        df_wide = self.slice_sliding_window(df_wide, self.ai_window_quarters)
        df_cf = self.slice_sliding_window(df_cf, self.ai_window_quarters)
        df_rev = self.slice_sliding_window(df_rev, self.ai_window_months)

        parts: list[str] = []
        parts.append(PROMPT_TEMPLATES["system_rules"])
        parts.append("\n==================================================")
        parts.append(f"【標的：{stock_id} — Google Sheets 快取財報數據包】")

        if not df_wide.empty:
            cols = [
                "year_qtr",
                "revenue_亿元",
                "qtr_rev_yoy_pct",
                "gross_margin_pct",
                "op_margin_pct",
                "net_margin_pct",
                "eps",
                "ttm_eps",
                "roe_pct",
                "debt_ratio_pct",
            ]
            df_show = df_wide[[c for c in cols if c in df_wide.columns]].copy()
            parts.append("\n■ 表 1：近 8 季損益與核心財務比率 (資料來源：05_AI_Metrics_Wide)")
            parts.append(df_show.to_markdown(index=False))

        if not df_cf.empty:
            cols = [
                "year_qtr",
                "operating_cf",
                "capex",
                "free_cf",
                "investing_cf",
                "financing_cf",
            ]
            df_show = df_cf[[c for c in cols if c in df_cf.columns]].copy()
            parts.append("\n■ 表 2：近 8 季現金流量與資本支出結構 (資料來源：04_Cash_Flow)")
            parts.append(df_show.to_markdown(index=False))

        if not df_rev.empty:
            cols = [
                "rev_ym",
                "revenue",
                "mom_pct",
                "yoy_pct",
                "raw_date",
            ]
            df_show = df_rev[[c for c in cols if c in df_rev.columns]].copy()
            parts.append("\n■ 表 3：近 12 個月營收趨勢 (資料來源：01_Monthly_Revenue)")
            parts.append(df_show.to_markdown(index=False))

        return "\n".join(parts)

    async def run_full_diagnosis(self, stock_id: str, *, force_ai: bool = False) -> DiagnosisReport:
        # AI cache hit: reuse Watchlist_Master.ai_cached_period and ai_summary_json.
        ss = self.cache._spreadsheet  # noqa: SLF001
        watchlist = ss.worksheet("00_Watchlist_Master")
        records = watchlist.get_all_records()
        row = next(
            (r for r in records if str(r.get("stock_id", "")).strip() == stock_id),
            None,
        )

        cache_period = None
        if row:
            last_fin = str(row.get("last_fin_qtr", "")).strip()
            last_rev = str(row.get("last_rev_ym", "")).strip()
            if last_fin and last_rev:
                cache_period = f"{last_fin}_{last_rev}"

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

        # Render and publish war room.
        war_room_url = await self.render_and_publish_war_room_html(stock_id, ai_text)

        # Save cache summary back to Watchlist_Master.
        summary = self._summarize_ai(ai_text)
        self.cache._update_watchlist_master(  # noqa: SLF001
            watchlist,
            stock_id,
            {
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
        ss = self.cache._spreadsheet  # noqa: SLF001
        df_wide = self.cache._read_stock_rows(ss.worksheet("05_AI_Metrics_Wide"), stock_id)  # noqa: SLF001
        df_rev = self.cache._read_stock_rows(ss.worksheet("01_Monthly_Revenue"), stock_id)  # noqa: SLF001
        df_cf = self.cache._read_stock_rows(ss.worksheet("04_Cash_Flow"), stock_id)  # noqa: SLF001

        df_wide = self.slice_sliding_window(df_wide, self.chart_window_quarters)
        df_rev = self.slice_sliding_window(df_rev, self.chart_window_months)
        df_cf = self.slice_sliding_window(df_cf, self.chart_window_quarters)

        fig = make_subplots(
            rows=4,
            cols=1,
            shared_xaxes=False,
            vertical_spacing=0.08,
            subplot_titles=(
                "月營收與 YoY",
                "三率走勢",
                "EPS 與 TTM EPS",
                "OCF / CapEx / FCF",
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
                ("net_margin_pct", "淨利率(%)"),
            ):
                if col in df_wide.columns:
                    fig.add_trace(
                        go.Scatter(x=x, y=pd.to_numeric(df_wide[col], errors="coerce"), name=name),
                        row=2,
                        col=1,
                    )

            if "eps" in df_wide.columns:
                fig.add_trace(
                    go.Bar(x=x, y=pd.to_numeric(df_wide["eps"], errors="coerce"), name="EPS"),
                    row=3,
                    col=1,
                )
            if "ttm_eps" in df_wide.columns:
                fig.add_trace(
                    go.Scatter(
                        x=x,
                        y=pd.to_numeric(df_wide["ttm_eps"], errors="coerce"),
                        name="TTM EPS",
                    ),
                    row=3,
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
                        go.Bar(x=x, y=pd.to_numeric(df_cf[col], errors="coerce") / 1e8, name=f"{name}(億)"),
                        row=4,
                        col=1,
                    )

        fig.update_layout(
            template="plotly_dark",
            height=1100,
            title=f"{stock_id} 基本面 AI 戰情室",
            margin={"l": 40, "r": 30, "t": 80, "b": 40},
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

        # Publish to GitHub Pages under charts/.
        pages_url = upload_to_github(str(file_path), stock_id, filename_suffix="_fund")
        return pages_url

    def _summarize_ai(self, ai_text: str) -> str:
        # v1: store as plain text; keep within a reasonable size for Sheets cell.
        cleaned = ai_text.strip()
        return cleaned[:1500]

    def _escape_html(self, text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
