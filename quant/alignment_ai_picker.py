"""Daily AI stock selection pipeline: fundamental + earnings-outlook analysis of alignment_signals,
followed by a single cross-stock AI call that picks the best entry candidates.

Stages (SASD v3.2.0):
1. Load `{date}_alignment_signals.csv`, enrich with streak/new-entry state, cap candidate count.
2. Per stock: incremental FinMind/Sheets sync (FinMind-only rate limiting) + single-stock AI
   diagnosis (`ai_summary_json`, cached by data watermark).
3. One portfolio-level AI call (high-tier model, auto-fallback) over all dossiers.
4. Persist (local JSON/MD, `AI_Daily_Picks` sheet) and push to Discord.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

import sys

_ROOT = Path(__file__).resolve().parent.parent
for _sub in (_ROOT, _ROOT / "services", _ROOT / "quant", _ROOT / "taishin"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from ai_service import (
    DEFAULT_VERTEX_MODEL,
    ask_vertex_ai_with_meta,
    flush_bq_archives,
    get_vertex_batch_stage1_model,
    get_vertex_batch_stage2_model,
    get_vertex_batch_timeout_seconds,
    get_vertex_stage2_model,
    is_vertex_batch_enabled,
    run_vertex_batch_requests,
)
from finmind_stock_scanner import (
    DISCORD_API_BASE_URL,
    DISCORD_POST_PAUSE_SECONDS,
    REBOUND_WATCHLIST_SHEET_NAME,
    REQUEST_TIMEOUT_SECONDS,
    FinMindRequestLimiter,
    _atomic_write_csv,
    _cleanup_old_daily_reports,
    _extract_latest_volume_metrics,
    _ensure_tracker_signal_columns,
    _load_local_prior_signals,
    _rate_limit_finmind_http_requests,
    _today_in_taipei,
    add_moving_averages,
    build_rebound_watchlist_rows,
    build_signal_tracker_rows,
    fetch_or_load_market_regime,
    format_rebound_report,
    load_scan_config,
    post_rebound_report_to_discord,
    scan_rebound_from_prices,
    sync_rebound_watchlist_to_gsheet,
)
from fundamental_analyzer import FundamentalAnalyzer, build_hard_warnings
from fundamental_cache import FundamentalCacheError, FundamentalCacheManager
from fundamental_config import PROMPT_TEMPLATES

PICKS_SHEET_NAME = "AI_Daily_Picks"
FINMIND_HOSTS = ("finmindtrade.com",)
DEFAULT_MAX_CANDIDATES = 25
DEFAULT_MAX_TOP_PICKS = 5
DEFAULT_PORTFOLIO_MODEL = DEFAULT_VERTEX_MODEL
DEFAULT_CALL_INTERVAL_SECONDS = 13.0
DEFAULT_BATCH_POLL_SECONDS = 60.0
DISCORD_CHUNK_LIMIT = 1800
DISCORD_MAX_MESSAGE_LENGTH = 2000

_VERDICT_EMOJI = {"高度可信": "🟢", "待驗證": "🟡", "存疑": "🔴"}
_RANK_EMOJI = {1: "🥇", 2: "🥈", 3: "🥉"}


# --------------------------------------------------------------------------- #
# Stage 1: candidates
# --------------------------------------------------------------------------- #
def load_alignment_candidates(daily_dir: Path, target_date: date) -> pd.DataFrame | None:
    """Return the day's alignment signals, or None when the daily scanner job produced no file."""
    path = daily_dir / f"{target_date.isoformat()}_alignment_signals.csv"
    if not path.is_file():
        return None
    frame = pd.read_csv(path, dtype={"stock_id": str})
    if frame.empty:
        return frame
    frame["stock_id"] = frame["stock_id"].astype(str).str.strip()
    if "gap_percent" not in frame.columns and "MA17_MA45_gap_percent" in frame.columns:
        frame["gap_percent"] = frame["MA17_MA45_gap_percent"]
    return frame.drop_duplicates(subset=["stock_id"], keep="last").reset_index(drop=True)


def _backfill_volume_from_prices(
    frame: pd.DataFrame, daily_dir: Path, target_date: date
) -> pd.DataFrame:
    """若舊版 alignment_signals.csv 尚未帶入 vol_lots，自動從同日 prices.csv 回補成交量與均量。"""
    result = frame.copy()
    for col, default in (
        ("vol_lots", ""),
        ("vol_ma5_lots", ""),
        ("vol_ma20_lots", ""),
        ("vol_status", "-"),
    ):
        if col not in result.columns:
            result[col] = default

    has_vol = result["vol_lots"].astype(str).str.strip().replace({"nan": "", "None": ""}) != ""
    if has_vol.all():
        return result

    prices_path = daily_dir / f"{target_date.isoformat()}_prices.csv"
    if not prices_path.is_file():
        candidates = sorted(daily_dir.glob("*_prices.csv"))
        if candidates:
            prices_path = candidates[-1]
            try:
                date_str = prices_path.stem.split("_")[0]
                target_date = date.fromisoformat(date_str)
            except Exception:
                pass
    if not prices_path.is_file():
        return result
    try:
        prices_df = pd.read_csv(prices_path, dtype={"stock_id": str})
    except Exception:  # noqa: BLE001
        return result
    if prices_df.empty or "stock_id" not in prices_df.columns:
        return result

    prices_df["stock_id"] = prices_df["stock_id"].astype(str).str.strip()
    for idx, sid in result["stock_id"].items():
        if bool(has_vol.loc[idx]):
            continue
        sub = prices_df.loc[prices_df["stock_id"] == str(sid)].copy()
        if sub.empty:
            continue
        try:
            analyzed = add_moving_averages(sub)
            metrics = _extract_latest_volume_metrics(analyzed)
            for k, v in metrics.items():
                result.at[idx, k] = v
        except Exception:  # noqa: BLE001
            continue
    return result


def enrich_with_tracker(frame: pd.DataFrame, daily_dir: Path, target_date: date) -> pd.DataFrame:
    """Add is_new / streak / daily_status / volume metrics using the same tracker logic as MA_Signal_Tracker."""
    result = _backfill_volume_from_prices(frame, daily_dir, target_date)
    result["is_new"] = False
    result["streak"] = 1
    result["daily_status"] = ""
    if result.empty:
        return result
    try:
        prior = _load_local_prior_signals(daily_dir, target_date)
        tracker_source = _ensure_tracker_signal_columns(result)
        rows = build_signal_tracker_rows(
            tracker_source, existing_records=prior, as_of=target_date, replace_date=True
        )
    except Exception as error:  # noqa: BLE001
        print(f"[warn] 無法計算連續天數/新進狀態，使用預設值：{error}")
        return result

    today_iso = target_date.isoformat()
    info = {
        str(row["股票代號"]): row
        for row in rows
        if str(row.get("執行日期")) == today_iso and str(row.get("股票代號")) not in ("", "-")
    }
    for index, stock_id in result["stock_id"].items():
        row = info.get(str(stock_id))
        if row is None:
            continue
        result.at[index, "is_new"] = "🆕" in str(row.get("當日新進", ""))
        result.at[index, "streak"] = int(row.get("連續天數") or 1)
        result.at[index, "daily_status"] = str(row.get("每日執行狀況", ""))
    return result


def enrich_with_taxonomy(
    frame: pd.DataFrame,
    config: Mapping[str, object],
    target_date: date,
    *,
    task_type: str = "TAXONOMY",
) -> pd.DataFrame:
    """為候選股補齊 08_Stock_Taxonomy 的細分次產業 (sub_industry)、主被動元件屬性與同業標籤。"""
    result = frame.copy()
    if "industry_category" in result.columns:
        result["sub_industry"] = result["industry_category"]
    else:
        result["sub_industry"] = ""
    result["component_nature"] = ""
    result["sector_group"] = ""
    result["valuation_regime"] = ""
    result["taxonomy_badge"] = ""
    if result.empty:
        return result

    try:
        from taxonomy_service import (
            TaxonomyService,
            format_taxonomy_badge,
            load_registry_stock_map,
        )

        tax_path = Path(
            str(config.get("taxonomy_cache_path", "finmind_reports/.state/stock_taxonomy.json"))
        )
        tax_tab = str(config.get("taxonomy_gsheet_tab_name", "08_Stock_Taxonomy"))
        tax_svc = TaxonomyService(
            cache_path=tax_path,
            ttl_days=int(config.get("taxonomy_ttl_days", 90)),
            sheet_tab_name=tax_tab,
        )
        tax_map = tax_svc.load()

        # 建立 valid_stock_map (先用本地已快取 + 候選股名單)
        valid_map = load_registry_stock_map(
            Path(
                str(
                    config.get(
                        "universe_registry_path",
                        "finmind_reports/.state/stock_universe_registry.json",
                    )
                )
            )
        )
        valid_map.update({k: str(v.get("stock_name", k)) for k, v in tax_map.items()})
        for r in result.itertuples(index=False):
            valid_map[str(r.stock_id).strip()] = str(getattr(r, "stock_name", "")).strip()

        tax_svc.ensure_batch(
            result, valid_map, as_of=target_date, sync_sheet=True, task_type=task_type
        )
        for idx, sid in result["stock_id"].items():
            rec = tax_svc.get_cached(str(sid))
            if rec:
                result.at[idx, "sub_industry"] = (
                    str(rec.get("sub_industry") or result.at[idx, "sub_industry"]).strip()
                )
                result.at[idx, "sector_group"] = str(rec.get("sector_group", "")).strip()
                result.at[idx, "valuation_regime"] = str(rec.get("valuation_regime", "")).strip()
                result.at[idx, "component_nature"] = str(rec.get("component_nature", "")).strip()
                result.at[idx, "taxonomy_badge"] = format_taxonomy_badge(rec)
    except Exception as error:  # noqa: BLE001
        print(f"[warn] 無法載入細分產業知識庫，退回使用官方分類：{error}")
    return result


def prefilter_candidates(frame: pd.DataFrame, max_candidates: int) -> tuple[pd.DataFrame, int]:
    """Cap candidates: fresh/breakout first, then rising 17MA, then smallest 17/45 gap."""
    if len(frame) <= max_candidates:
        return frame.reset_index(drop=True), 0

    def trend_rank(trend: object) -> int:
        text = str(trend)
        if text.startswith(("📈", "↗️")):
            return 0
        if text.startswith(("📉", "↘️")):
            return 2
        return 1

    ranked = frame.copy()
    ranked["_priority"] = [
        0 if (bool(row.is_new) or "轉強突破" in str(row.daily_status)) else 1
        for row in ranked.itertuples(index=False)
    ]
    ranked["_trend"] = ranked["ma17_trend"].map(trend_rank)
    ranked["_gap"] = pd.to_numeric(ranked["gap_percent"], errors="coerce").abs().fillna(999.0)
    ranked = ranked.sort_values(["_priority", "_trend", "_gap", "stock_id"], kind="stable")
    selected = ranked.head(max_candidates).drop(columns=["_priority", "_trend", "_gap"])
    return selected.reset_index(drop=True), len(frame) - len(selected)


def _score_rebound_candidate(
    row: Mapping[str, object],
    *,
    cache: object | None = None,
    analyzer: object | None = None,
) -> float:
    """SASD v6.0 反彈菁英 100 分制預篩計分：技術共振 (25) + 籌碼底氣 (40) + 基本面硬實力 (35)。"""
    sid = str(row.get("stock_id", "")).strip()
    tech_score = 0.0
    k9 = _parse_optional_num(row.get("K9"))
    d9 = _parse_optional_num(row.get("D9"))
    osc = _parse_optional_num(row.get("MACD_OSC"))
    subtype = str(row.get("rebound_subtype") or "")
    pot_ret = _parse_optional_num(row.get("potential_return_pct")) or 0.0
    if k9 is not None and d9 is not None and k9 > d9 and k9 < 75.0:
        tech_score += 10.0
    elif k9 is not None and d9 is not None and k9 >= d9:
        tech_score += 5.0
    if osc is not None and osc >= -0.5:
        tech_score += 5.0
    if subtype == "W_BOTTOM_PULLBACK_SETUP":
        tech_score += 5.0
    elif subtype == "REBOUND_TOUCH_HOLD":
        tech_score += 3.0
    tech_score += min(5.0, max(0.0, pot_ret / 3.0))

    chip_score = 0.0
    fund_score = 0.0
    vol_lots = _parse_optional_num(row.get("vol_lots"))
    hard_metrics: dict[str, object] = {"vol_lots": vol_lots}

    if cache is not None:
        try:
            chip_df = cache._read_stock_rows(cache._get_worksheet("07_Chip_Summary"), sid)  # noqa: SLF001
            if chip_df is not None and not chip_df.empty:
                if "raw_date" in chip_df.columns:
                    chip_df = chip_df.sort_values("raw_date")
                c_row = chip_df.iloc[-1].to_dict()
                trust_5d = _parse_optional_num(c_row.get("trust_net_buy_5d_lots")) or 0.0
                tdcc_1000 = _parse_optional_num(c_row.get("tdcc_1000_lots_pct"))
                m_util = _parse_optional_num(c_row.get("margin_utilization_pct"))
                inst_5d = _parse_optional_num(c_row.get("three_inst_net_5d_lots"))
                m_chg_5d = _parse_optional_num(c_row.get("margin_chg_5d_lots"))
                est_mmr = _parse_optional_num(c_row.get("est_margin_maint_pct"))
                hard_metrics.update(
                    {
                        "three_inst_net_5d_lots": inst_5d,
                        "trust_net_buy_5d_lots": trust_5d,
                        "margin_chg_5d_lots": m_chg_5d,
                        "margin_utilization_pct": m_util,
                        "est_margin_maint_pct": est_mmr,
                        "tdcc_1000_lots_pct": tdcc_1000,
                    }
                )
                if trust_5d >= 1000:
                    chip_score += 18.0
                elif trust_5d >= 500:
                    chip_score += 12.0
                elif trust_5d > 0:
                    chip_score += 6.0
                if tdcc_1000 is not None and tdcc_1000 >= 70.0:
                    chip_score += 12.0
                elif tdcc_1000 is not None and tdcc_1000 >= 50.0:
                    chip_score += 6.0
                if m_util is not None and m_util < 6.0:
                    chip_score += 10.0
                elif m_util is not None and m_util < 15.0:
                    chip_score += 5.0
                elif (inst_5d or 0.0) > 0:
                    chip_score += 4.0
        except Exception:  # noqa: BLE001
            pass

        try:
            from fundamental_analyzer import _normalize_monthly_revenue_pcts

            wide_df = cache._read_stock_rows(cache._get_worksheet("05_AI_Metrics_Wide"), sid)  # noqa: SLF001
            rev_df = cache._read_stock_rows(cache._get_worksheet("01_Monthly_Revenue"), sid)  # noqa: SLF001
            if wide_df is not None and not wide_df.empty and "raw_date" in wide_df.columns:
                wide_df = wide_df.sort_values("raw_date")
            if rev_df is not None and not rev_df.empty:
                rev_df = _normalize_monthly_revenue_pcts(rev_df)
            w_row = wide_df.iloc[-1].to_dict() if (wide_df is not None and not wide_df.empty) else {}
            prev_w = wide_df.iloc[-2].to_dict() if (wide_df is not None and len(wide_df) >= 2) else {}
            hard_metrics["op_margin_pct"] = _parse_optional_num(w_row.get("op_margin_pct"))
            hard_metrics["contract_liab"] = _parse_optional_num(w_row.get("contract_liab_亿元"))
            rev_yoy = None
            if rev_df is not None and not rev_df.empty and "yoy_pct" in rev_df.columns:
                rev_yoy = _parse_optional_num(rev_df.iloc[-1].get("yoy_pct"))
            if rev_yoy is None:
                rev_yoy = _parse_optional_num(w_row.get("qtr_rev_yoy_pct"))
            if rev_yoy is not None and rev_yoy >= 30.0:
                fund_score += 12.0
            elif rev_yoy is not None and rev_yoy >= 15.0:
                fund_score += 8.0
            elif rev_yoy is not None and rev_yoy > 0:
                fund_score += 4.0

            gm_cur = _parse_optional_num(w_row.get("gross_margin_pct"))
            gm_prev = _parse_optional_num(prev_w.get("gross_margin_pct"))
            om_cur = _parse_optional_num(w_row.get("op_margin_pct"))
            om_prev = _parse_optional_num(prev_w.get("op_margin_pct"))
            if gm_cur is not None and gm_prev is not None and om_cur is not None and om_prev is not None:
                if gm_cur > gm_prev and om_cur > om_prev:
                    fund_score += 12.0
                elif om_cur > om_prev:
                    fund_score += 6.0

            fcf = _parse_optional_num(w_row.get("fcf_亿元"))
            ocf = _parse_optional_num(w_row.get("ocf_亿元"))
            capex = _parse_optional_num(w_row.get("capex_亿元"))
            ocf_to_ni = _parse_optional_num(w_row.get("ocf_to_ni_pct"))
            is_strategic_capex = (
                fcf is not None
                and fcf < 0
                and ocf is not None
                and ocf > 0
                and ocf_to_ni is not None
                and ocf_to_ni >= 90.0
                and capex is not None
                and abs(capex) >= 0.8 * ocf
                and (rev_yoy is not None and rev_yoy >= 15.0)
            )
            if is_strategic_capex or (fcf is not None and fcf > 0 and (ocf_to_ni or 0.0) >= 80.0):
                fund_score += 11.0
            elif ocf_to_ni is not None and ocf_to_ni >= 80.0:
                fund_score += 6.0
        except Exception:  # noqa: BLE001
            pass

    penalty = -50.0 if build_hard_warnings(hard_metrics) else 0.0
    return round(tech_score + chip_score + fund_score + penalty, 2)


def prefilter_rebound_candidates(
    frame: pd.DataFrame,
    *,
    cache: object | None = None,
    analyzer: object | None = None,
    max_candidates: int = 12,
) -> tuple[pd.DataFrame, int]:
    """SASD v6.0 反彈菁英預篩：依技術(25)+籌碼(40)+基本面(35) 100分制排序，保留 Top max_candidates 檔。"""
    if frame is None or frame.empty:
        return frame.reset_index(drop=True) if frame is not None else pd.DataFrame(), 0

    ranked = frame.copy()
    ranked["_rebound_score"] = [
        _score_rebound_candidate(row, cache=cache, analyzer=analyzer)
        for row in ranked.to_dict("records")
    ]
    ranked = ranked.sort_values(
        ["_rebound_score", "potential_return_pct", "stock_id"],
        ascending=[False, False, True],
        kind="stable",
    )
    if len(ranked) <= max_candidates:
        return ranked.rename(columns={"_rebound_score": "rebound_prefilter_score"}).reset_index(drop=True), 0
    selected = ranked.head(max_candidates).rename(columns={"_rebound_score": "rebound_prefilter_score"})
    return selected.reset_index(drop=True), len(ranked) - len(selected)


# --------------------------------------------------------------------------- #
# Stage 2: dossiers
# --------------------------------------------------------------------------- #
def _parse_optional_num(value: object) -> float | None:
    try:
        if value is None or pd.isna(value) or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(value: object, digits: int = 2) -> str:
    try:
        if value is None or pd.isna(value):
            return "N/A"
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_volume_line(row: Mapping[str, object]) -> str:
    vol = _num(row.get("vol_lots"), 0)
    ma5 = _num(row.get("vol_ma5_lots"), 0)
    ma20 = _num(row.get("vol_ma20_lots"), 0)
    status = str(row.get("vol_status", "") or "-").strip()
    if vol == "N/A":
        return "無成交量資料"
    return f"今日 {vol} 張｜5日均量 {ma5} 張｜20日均量 {ma20} 張｜狀態：{status}"


def build_dossier(index: int, row: Mapping[str, object], snapshot: str, summary: str) -> str:
    new_label = "🆕 是（新進榜）" if row.get("is_new") else "🔄 否（持續符合）"
    close_val = _parse_optional_num(row.get("close"))
    ma17_val = _parse_optional_num(row.get("MA17"))
    ma45_val = _parse_optional_num(row.get("MA45"))
    high_60d_val = _parse_optional_num(row.get("high_60d"))
    ma17_plus_12 = f"{ma17_val * 1.12:,.2f}" if ma17_val else "N/A"
    stop_minus_4 = f"{close_val * 0.96:,.2f}" if close_val else "N/A"

    tech_lines = [
        (
            f"  - 收盤價：{_num(row.get('close'))}｜3MA：{_num(row.get('MA3'))}｜17MA：{_num(row.get('MA17'))}"
            f"｜45MA：{_num(row.get('MA45'))}｜117MA：{_num(row.get('MA117'))}｜189MA：{_num(row.get('MA189'))}"
        )
    ]
    if row.get("support_ma") and high_60d_val is not None:
        tech_lines.append(
            f"  - 【型態 A：高點回跌回踩長均線反彈】近60日高點：{_num(row.get('high_60d'))}"
            f"｜回跌金額：{_num(row.get('drop_amount'))} 元（-{_num(row.get('drop_pct'))}%）"
            f"｜回踩支撐：{row.get('support_ma')}（{_num(row.get('support_price'))}，差距 {_num(row.get('support_gap_pct'))}%）"
            f"｜反彈至 17MA 目標：{_num(row.get('target_ma17'))}（空間 +{_num(row.get('potential_return_pct'))}%）"
            f"｜K線收盤強度：{_num(row.get('close_strength'))}"
        )
    else:
        tech_lines.append(
            f"  - 訊號類型：{row.get('signal', '')}（17MA 與 45MA 差距：{_num(row.get('gap_percent'))}%）"
            f"｜17MA 走勢：{row.get('ma17_trend', '')}"
        )
        tech_lines.append(
            f"  - 進榜狀態：{new_label}｜連續符合天數：{row.get('streak', 1)} 日｜動態：{row.get('daily_status', '')}"
        )

    tech_lines.append(f"  - 量能結構：{_fmt_volume_line(row)}")
    tax_badge = str(row.get("taxonomy_badge", "") or "").strip()
    val_regime = str(row.get("valuation_regime", "") or "").strip()
    if tax_badge:
        regime_suffix = f" ｜ ⚖️ 估值屬性：{val_regime}" if val_regime else ""
        tech_lines.insert(0, f"  - {tax_badge}{regime_suffix}")
    tech_lines.append(
        f"  - 法人測幅參考錨點：17MA={_num(ma17_val)}｜17MA正乖離極值(+12%)={ma17_plus_12}"
        f"｜45MA防守={_num(ma45_val)}｜收盤-4%停損={stop_minus_4}"
    )

    return "\n".join(
        [
            f"### 【候選標的 #{index}】{row['stock_id']} {row.get('stock_name', '')}"
            f"（{row.get('market', '')}｜{row.get('industry_category', '')}）",
            "- [A. 均線訊號、位階動態與量能結構]",
            *tech_lines,
            "- [B. 核心量化財務與領先指標（系統自 Google Sheets 精確提取）]",
            snapshot,
            "- [C. 單股 AI 財報診斷與法說展望交叉驗證 (ai_summary_json)]",
            f"  {summary}",
        ]
    )


def _supported_kwargs(fn: object, extra: Mapping[str, object]) -> dict[str, object]:
    """僅傳入目標函式簽章明確支援（或含 **kwargs）的選填參數，確保相容精簡版測試替身。"""
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(extra)
    return {k: v for k, v in extra.items() if k in params}


async def analyze_candidates(
    frame: pd.DataFrame,
    cache: FundamentalCacheManager,
    analyzer: FundamentalAnalyzer,
    *,
    force_ai: bool,
    publish_all: bool,
    call_interval: float,
    use_vertex_batch: bool = False,
    batch_model: str | None = None,
    poll_interval: float = 60.0,
    timeout_seconds: float | None = None,
    target_date: date | None = None,
    stage1_task_type: str = "STAGE1_DIAGNOSIS",
    online_task_type: str = "STAGE1_GROUNDING",
    taxonomy_task_type: str = "TAXONOMY",
) -> tuple[dict[str, dict[str, object]], list[tuple[str, str]]]:
    """Sync + diagnose each candidate; one stock failing never aborts the batch.

    SASD v5.3.0: with ``use_vertex_batch`` the cache-miss stocks are packed into a single
    Vertex AI Batch job (Phase A prepare -> Phase B batch -> Phase C finalize).
    """
    if use_vertex_batch:
        return await _analyze_candidates_batch(
            frame,
            cache,
            analyzer,
            force_ai=force_ai,
            publish_all=publish_all,
            batch_model=batch_model,
            poll_interval=poll_interval,
            timeout_seconds=timeout_seconds,
            target_date=target_date,
            stage1_task_type=stage1_task_type,
            taxonomy_task_type=taxonomy_task_type,
        )
    results: dict[str, dict[str, object]] = {}
    failures: list[tuple[str, str]] = []
    total = len(frame)
    for position, row in enumerate(frame.itertuples(index=False), start=1):
        stock_id = str(row.stock_id)
        label = f"{stock_id} {getattr(row, 'stock_name', '')}"
        try:
            await asyncio.to_thread(cache.check_and_sync_stock, stock_id, False)
            report = await analyzer.run_full_diagnosis(
                stock_id,
                force_ai=force_ai,
                publish_war_room=publish_all,
                **_supported_kwargs(
                    analyzer.run_full_diagnosis,
                    {"task_type": online_task_type, "taxonomy_task_type": taxonomy_task_type},
                ),
            )
            snapshot = await asyncio.to_thread(
                analyzer.build_quant_snapshot,
                stock_id,
                close=_parse_optional_num(getattr(row, "close", None)),
            )
        except FundamentalCacheError as error:
            failures.append((stock_id, f"財報資料不足：{error}"))
            print(f"[{position}/{total}] {label} ✗ {error}")
            continue
        except Exception as error:  # noqa: BLE001
            failures.append((stock_id, f"分析失敗：{type(error).__name__}: {error}"))
            print(f"[{position}/{total}] {label} ✗ {type(error).__name__}: {error}")
            continue

        results[stock_id] = {
            "report": report,
            "snapshot": snapshot,
            "summary": report.summary_json or report.ai_text,
        }
        print(f"[{position}/{total}] {label} ✓ cached={report.cached}")
        if not report.cached and position < total:
            await asyncio.sleep(call_interval)  # stay under the free-tier RPM
    return results, failures


async def _analyze_candidates_batch(
    frame: pd.DataFrame,
    cache: FundamentalCacheManager,
    analyzer: FundamentalAnalyzer,
    *,
    force_ai: bool,
    publish_all: bool,
    batch_model: str | None,
    poll_interval: float,
    target_date: date | None,
    timeout_seconds: float | None = None,
    stage1_task_type: str = "STAGE1_DIAGNOSIS",
    taxonomy_task_type: str = "TAXONOMY",
) -> tuple[dict[str, dict[str, object]], list[tuple[str, str]]]:
    results: dict[str, dict[str, object]] = {}
    failures: list[tuple[str, str]] = []
    pending_preps: dict[str, dict] = {}
    batch_prompts: dict[str, str] = {}
    closes: dict[str, float | None] = {}
    labels: dict[str, str] = {}
    total = len(frame)

    # Phase A: FinMind sync + cache check (only cache misses go into the batch JSONL)
    for position, row in enumerate(frame.itertuples(index=False), start=1):
        stock_id = str(row.stock_id)
        label = f"{stock_id} {getattr(row, 'stock_name', '')}"
        labels[stock_id] = label
        close = _parse_optional_num(getattr(row, "close", None))
        closes[stock_id] = close
        try:
            await asyncio.to_thread(cache.check_and_sync_stock, stock_id, False)
            prep = await analyzer.prepare_diagnosis_context(
                stock_id,
                force_ai=force_ai,
                publish_war_room=publish_all,
                **_supported_kwargs(
                    analyzer.prepare_diagnosis_context,
                    {"taxonomy_task_type": taxonomy_task_type},
                ),
            )
            if prep["cached"] is True:
                report = prep["report"]
                snapshot = await asyncio.to_thread(analyzer.build_quant_snapshot, stock_id, close=close)
                results[stock_id] = {
                    "report": report,
                    "snapshot": snapshot,
                    "summary": report.summary_json or report.ai_text,
                }
                print(f"[{position}/{total}] {label} ✓ cached=True")
            else:
                pending_preps[stock_id] = prep
                batch_prompts[stock_id] = prep["prompt"]
                print(f"[{position}/{total}] {label} → 納入 Vertex Batch")
        except FundamentalCacheError as error:
            failures.append((stock_id, f"財報資料不足：{error}"))
            print(f"[{position}/{total}] {label} ✗ {error}")
        except Exception as error:  # noqa: BLE001
            failures.append((stock_id, f"分析失敗：{type(error).__name__}: {error}"))
            print(f"[{position}/{total}] {label} ✗ {type(error).__name__}: {error}")

    # Phase B: submit one batch job and block-poll (skipped when everything was cached)
    batch_outputs: dict[str, object] = {}
    if batch_prompts:
        day = (target_date or date.today()).isoformat()
        try:
            batch_outputs = await run_vertex_batch_requests(
                batch_prompts,
                model=batch_model or get_vertex_batch_stage1_model(),
                job_prefix=f"stage1_diag_{day}",
                poll_interval_seconds=poll_interval,
                tag="stage1_batch",
                task_type=stage1_task_type,
                timeout_seconds=timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001
            for stock_id in pending_preps:
                failures.append((stock_id, f"Vertex Batch 失敗：{type(error).__name__}: {error}"))
            print(f"[vertex-batch] Stage 1 批次失敗：{type(error).__name__}: {error}")
            pending_preps = {}

    # Phase C: finalize each stock; a single bad response only fails that stock
    for stock_id, prep in pending_preps.items():
        ai_meta = batch_outputs.get(stock_id)
        if ai_meta is None:
            failures.append((stock_id, "Vertex Batch 未回傳該股結果"))
            print(f"{labels[stock_id]} ✗ Vertex Batch 未回傳該股結果")
            continue
        try:
            report = await analyzer.finalize_diagnosis_from_ai(
                stock_id, prep, ai_meta, publish_war_room=publish_all
            )
            snapshot = await asyncio.to_thread(
                analyzer.build_quant_snapshot, stock_id, close=closes.get(stock_id)
            )
        except Exception as error:  # noqa: BLE001
            failures.append((stock_id, f"分析失敗：{type(error).__name__}: {error}"))
            print(f"{labels[stock_id]} ✗ {type(error).__name__}: {error}")
            continue
        results[stock_id] = {
            "report": report,
            "snapshot": snapshot,
            "summary": report.summary_json or report.ai_text,
        }
        print(f"{labels[stock_id]} ✓ cached=False (vertex-batch)")
    return results, failures


# --------------------------------------------------------------------------- #
# Stage 3: portfolio decision
# --------------------------------------------------------------------------- #
def parse_portfolio_json(raw: str, valid_ids: set[str], max_top: int) -> dict | None:
    """Extract and sanitise the decision JSON; ids outside the candidate list are dropped."""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    start = text.find("{")
    if start < 0:
        return None

    def try_load(candidate: str) -> dict | None:
        try:
            res = json.loads(candidate)
            return res if isinstance(res, dict) else None
        except Exception:
            return None

    data = None

    # Step 1: Try exact slice between first { and last }
    end = text.rfind("}")
    if end > start:
        data = try_load(text[start : end + 1])

    # Step 2: Fix trailing hallucinated words before/around closing braces (e.g. `} questionnaires}`)
    if data is None:
        cleaned = re.sub(r'\}\s*[a-zA-Z_0-9\-]+\s*\}?$', '}]}', text)
        start_c = cleaned.find("{")
        end_c = cleaned.rfind("}")
        if start_c >= 0 and end_c > start_c:
            data = try_load(cleaned[start_c : end_c + 1])

    # Step 3: Remove trailing commas like `, }` or `, ]`
    if data is None and end > start:
        cleaned_commas = re.sub(r',\s*([\]\}])', r'\1', text[start : end + 1])
        data = try_load(cleaned_commas)

    # Step 4: Bracket balancing repair (find last complete dict entry and close open brackets)
    if data is None:
        matches = list(re.finditer(r'\}\s*\]?', text))
        for m in reversed(matches):
            cut = text[start : m.end()]
            open_sq = cut.count("[") - cut.count("]")
            open_cu = cut.count("{") - cut.count("}")
            repaired = cut + ("]" * max(0, open_sq)) + ("}" * max(0, open_cu))
            data = try_load(repaired)
            if data is not None:
                break

    if not isinstance(data, dict):
        return None

    def clean(items: object, limit: int) -> list[dict]:
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            stock_id = str(item.get("stock_id", "")).strip()
            if stock_id in valid_ids and stock_id not in seen:
                seen.add(stock_id)
                cleaned.append({**item, "stock_id": stock_id})
        return cleaned[:limit]

    top = clean(data.get("top_picks"), max_top)
    taken = {item["stock_id"] for item in top}
    watch = [i for i in clean(data.get("watchlist"), 5) if i["stock_id"] not in taken]
    taken |= {item["stock_id"] for item in watch}
    avoid = [i for i in clean(data.get("avoid"), len(valid_ids)) if i["stock_id"] not in taken]
    for rank, item in enumerate(top, start=1):
        item["rank"] = rank
    return {
        "market_light": str(data.get("market_light", "")).strip(),
        "market_comment": str(data.get("market_comment", "")).strip(),
        "top_picks": top,
        "watchlist": watch,
        "avoid": avoid,
    }


# --------------------------------------------------------------------------- #
# SASD v4.0 Stage 0 硬性攔截 / Stage 2 候選上限 / 同產業限額
# --------------------------------------------------------------------------- #
STAGE2_TRIM_THRESHOLD = 15  # 候選股 > 15 檔時才裁剪
STAGE2_MAX_CANDIDATES = 12  # 裁剪後交給 Stage 2 (ARGON) 的最大檔數
SECTOR_PICK_CAP = 2  # 同產業最多入選 top_picks 檔數
HARD_FILTER_TAG = "【Stage 0 硬性攔截】"


def hard_prefilter(
    frame: pd.DataFrame,
    metrics_lookup: Mapping[str, Mapping[str, object]],
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    """Stage 0 硬性排雷：`vol_lots < 300` 或 `(op_margin_pct < 0 且 contract_liab < 1.0 億)` 直接歸入 avoid。

    回傳 (保留的候選 frame, pre_filtered_avoid)。日量以 frame 的 `vol_lots` 為主，缺漏時改用 metrics_lookup；
    兩者皆無資料者不判定（不誤殺）。被攔截者不進入 Stage 2 Prompt。
    """
    if frame.empty:
        return frame.reset_index(drop=True), []
    kept_index: list[int] = []
    pre_filtered: list[dict[str, str]] = []
    for position, row in enumerate(frame.to_dict("records")):
        stock_id = str(row.get("stock_id", ""))
        metrics = dict(metrics_lookup.get(stock_id, {}))
        vol = _parse_optional_num(row.get("vol_lots"))
        if vol is not None:
            metrics["vol_lots"] = vol
        warnings = build_hard_warnings(metrics)
        if warnings:
            pre_filtered.append({"stock_id": stock_id, "reason": HARD_FILTER_TAG + "；".join(warnings)})
        else:
            kept_index.append(position)
    return frame.iloc[kept_index].reset_index(drop=True), pre_filtered


def _stage1_score(results: Mapping[str, Mapping[str, object]], stock_id: str) -> float:
    try:
        parsed = json.loads(str(results.get(stock_id, {}).get("summary", "")))
        return float(parsed.get("score", 0)) if isinstance(parsed, dict) else 0.0
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0.0


def _volume_ratio_from_row(row: Mapping[str, object]) -> float | None:
    """Try to parse volume ratio (today / 20MA) from vol_status like "... (4.52x...)"."""
    text = str(row.get("vol_status") or "").strip()
    if not text:
        return None
    match = re.search(r"\((\d+(?:\.\d+)?)x", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _is_overheated_candidate(row: Mapping[str, object]) -> bool:
    """Stage 2 前置 Python 排雷：天量過熱或已超漲逼近 17MA+12%。

    規則（與 Stage 1 & Stage 2 Prompt 統一門檻）：
    - 量比 > 4.0x（天量過熱）
    - 或 收盤價 > 17MA * 1.12（短線超漲）

    缺資料者不判定（避免誤殺）。
    """

    ratio = _volume_ratio_from_row(row)
    close_val = _parse_optional_num(row.get("close"))
    ma17_val = _parse_optional_num(row.get("MA17"))

    if ratio is not None and ratio > 4.0:
        return True
    if close_val is not None and ma17_val is not None and ma17_val > 0 and close_val > ma17_val * 1.12:
        return True
    return False


def _stage2_rank_score(row: Mapping[str, object], results: Mapping[str, Mapping[str, object]]) -> float:
    base = _stage1_score(results, str(row.get("stock_id", "")))
    penalty = 20.0 if _is_overheated_candidate(row) else 0.0
    return base - penalty


def _effective_sector_key(row: Mapping[str, object]) -> str:
    """優先回傳細分次產業 sub_industry（如『被動元件-MLCC』），若無才退回官方 industry_category。"""
    sub = str(row.get("sub_industry") or "").strip()
    if sub:
        return sub
    ind = str(row.get("industry_category") or "").strip()
    return ind or f"__NO_SECTOR__:{row.get('stock_id')}"


def limit_stage2_candidates(
    frame: pd.DataFrame,
    results: Mapping[str, Mapping[str, object]],
    *,
    threshold: int = STAGE2_TRIM_THRESHOLD,
    keep: int = STAGE2_MAX_CANDIDATES,
) -> pd.DataFrame:
    """候選股 > threshold 檔時，以 Python 排序後保留前 keep 檔。

    改良點（SASD v4.0）：
    1) 不只看 Stage 1 score：天量過熱(>4.5x) 或 已超漲(>17MA+12%) 先扣 20 分。
    2) 決賽名單產業多元：同產業最多取 3 檔進入 Stage 2 Prompt。

    同分維持原順序；缺資料者不判定（避免誤殺）。
    """

    if len(frame) <= threshold:
        return frame

    # 以 sub_industry（細分次產業，缺值退回 industry_category）做 3 檔上限；缺資料者視為獨立類別。
    rows = frame.to_dict("records")
    ranked_rows = [
        (i, row, _stage2_rank_score(row, results)) for i, row in enumerate(rows)
    ]
    ranked_rows.sort(key=lambda t: (-t[2], t[0]))

    picked_positions: list[int] = []
    sector_counts: dict[str, int] = {}
    for position, row, _score in ranked_rows:
        sector = _effective_sector_key(row)
        if sector_counts.get(sector, 0) >= 3:
            continue
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        picked_positions.append(position)
        if len(picked_positions) >= keep:
            break

    # 依挑選順序（已按 rank score 高→低）輸出，讓較高勝率候選優先進入 Stage 2 Prompt。
    return frame.iloc[picked_positions].reset_index(drop=True)


def enforce_sector_cap(
    decision: dict,
    frame: pd.DataFrame,
    *,
    cap: int = SECTOR_PICK_CAP,
) -> dict:
    """同產業 top_picks 最多 cap 檔；超額者（依 rank 後者）移入 watchlist 並重排 rank。"""
    industries = {
        str(r.get("stock_id")): _effective_sector_key(r)
        for r in frame.to_dict("records")
    }
    counts: dict[str, int] = {}
    kept: list[dict] = []
    demoted: list[dict] = []
    for pick in decision.get("top_picks", []):
        sector = industries.get(pick["stock_id"]) or str(pick.get("sector", "") or "").strip() or pick["stock_id"]
        if counts.get(sector, 0) >= cap:
            demoted.append(
                {
                    "stock_id": pick["stock_id"],
                    "reason": f"同產業名額限制（{sector} 已入選 {cap} 檔），移入觀察；待拉回或名額釋出再評估。",
                }
            )
            continue
        counts[sector] = counts.get(sector, 0) + 1
        kept.append(pick)
    for rank, pick in enumerate(kept, start=1):
        pick["rank"] = rank
    decision["top_picks"] = kept
    decision["watchlist"] = [*decision.get("watchlist", []), *demoted]
    return decision


def merge_pre_filtered_avoid(decision: dict, pre_filtered_avoid: list[dict[str, str]]) -> dict:
    """把 Stage 0 攔截名單併回最終 avoid（置於前段；AI 已列入其他分類者不重複）。"""
    taken = {i["stock_id"] for i in decision.get("top_picks", [])} | {
        i["stock_id"] for i in decision.get("watchlist", [])
    }
    existing = {i["stock_id"] for i in decision.get("avoid", [])}
    merged = [i for i in pre_filtered_avoid if i["stock_id"] not in taken and i["stock_id"] not in existing]
    decision["avoid"] = [*merged, *decision.get("avoid", [])]
    return decision


_PORTFOLIO_MA17_RULE = (
    "2. 均線斜率與量價結構 (35%)：17MA 必須上揚或走平；若訊號雖標示金叉但「17MA 走勢持續下彎（5日斜率 < 0）」，"
    "代表均線假金叉或動能衰退，強制扣減 15 分並優先移入 `watchlist`！"
)

_PORTFOLIO_REBOUND_RULE = (
    "2. 均線斜率與量價結構 (35%)：本批次為『型態 A：回踩 45MA/117MA/189MA 的雙劇本反彈（R1 觸線止穩 / R2 W底右腳預掛）』，"
    "17MA 下彎屬正常現象，嚴禁因 17MA 下彎扣分！請改為檢核：\n"
    "   (a) 支撐守穩：45MA/117MA/189MA 上揚支撐力道與 R1/R2 型態完整性（R2 允許今日收盤高於支撐均線 +2.5%~+12%，靜待拉回右腳買點）；\n"
    "   (b) 籌碼與基本面護城河：對齊 `🛡️土洋對作/投信鎖碼護盤`（投信逆勢大買+千張大戶鎖碼）與 `STRATEGIC_CAPEX_EXPANSION`（戰略擴產負FCF豁免）；\n"
    "   (c) 空間與風報比：從支撐預掛買點反彈至上方反壓（17MA/117MA/POC）之潛在空間 >= 5% 且風報比 >= 1.8。\n"
    "   【⚠️ 反彈 CIO 戰術沙盤推演（取代死板單一買價）】：\n"
    "   - 針對每檔 `top_picks`，請依據 `[D]` 提供的「三層結構錨點選單（積極區/穩健洗盤區/極限左腳區）」與大盤紅綠燈、前瞻本益比，在 `entry_zone_and_trigger` 給出具體分批建倉劇本（例：第一批於 189MA/整數關卡掛單 40%、若破線洗盤至 0.786 回檔區加碼 60%），嚴禁建議於高檔追價；\n"
    "   - 於 `bull_thesis` 必須先回答【賣壓歸因與錯殺辨識】（基本面惡化 vs 外資提款/法說避險結帳）；\n"
    "   - 於 `bear_case_and_invalidation` 須給出【事件/法說前後之 If-Then 應變預案】與無條件硬停損條件（破前波左腳低點）。"
)


def _apply_portfolio_prompt_overrides(prompt: str, *, selection_mode: str) -> str:
    """依選股模式動態調整 Stage 2 Prompt 的關鍵規則。

    - trend：均線多頭順勢金叉（預設）
    - rebound：型態 A 回踩長均線反彈（雙劇本 + 三層錨點分批決策）
    """

    mode = (selection_mode or "").strip().lower()
    if mode != "rebound":
        return prompt

    # 反彈模式不適用 17MA 下彎扣分條款，改用「長均線守穩 + 雙劇本 + 三層錨點 CIO 推演」。
    out = prompt
    if _PORTFOLIO_MA17_RULE in out:
        out = out.replace(_PORTFOLIO_MA17_RULE, _PORTFOLIO_REBOUND_RULE)
    else:
        out = out.replace(
            "【輸出規定】",
            f"【型態 A 反彈模式專屬審視與 CIO 沙盤推演規則】\n{_PORTFOLIO_REBOUND_RULE}\n\n【輸出規定】",
        )
    out = out.replace(
        "確保 `target_price_2 > target_price_1 > 最新收盤價`",
        "確保 `target_price_2 > target_price_1 > 建議預掛買進區間上限`（R2 預掛股之 TP1 為上方 17MA/117MA 反壓）",
    )
    return out


async def run_portfolio_selection(
    dossiers: list[str],
    *,
    max_top: int,
    model: str | None = None,
    market_regime: Mapping[str, object] | None = None,
    strategy_context: str = "",
    selection_mode: str = "trend",
    use_vertex_batch: bool = False,
    poll_interval: float = 60.0,
    task_type: str = "STAGE2_PORTFOLIO",
) -> object:
    market_light_str = (
        str(market_regime.get("summary") or market_regime.get("light") or "🟡黃燈")
        if market_regime
        else "🟡黃燈"
    )
    prompt = (
        PROMPT_TEMPLATES["portfolio_selection"]
        .replace("{max_top}", str(max_top))
        .replace("{market_light}", market_light_str)
        .replace("{MARKET_LIGHT_INFO}", market_light_str)
    )
    prompt = _apply_portfolio_prompt_overrides(prompt, selection_mode=selection_mode)

    if strategy_context:
        prompt += f"\n\n【今日選股策略補充定位】\n{strategy_context}"
    prompt += (
        "\n\n==================================================\n"
        + f"【今日候選資料卡（共 {len(dossiers)} 檔）】\n\n"
        + "\n\n".join(dossiers)
    )
    if use_vertex_batch:
        batch_res = await run_vertex_batch_requests(
            {"PORTFOLIO_STAGE2": prompt},
            model=model or get_vertex_batch_stage2_model(),
            job_prefix="stage2_portfolio",
            poll_interval_seconds=poll_interval,
            tag="portfolio_select_batch",
            task_type=task_type,
            stock_ids={"PORTFOLIO_STAGE2": "PORTFOLIO"},
        )
        meta = batch_res.get("PORTFOLIO_STAGE2")
        if meta is None:
            raise RuntimeError("Vertex Batch 未回傳 Stage 2 跨股總決策結果")
        return meta
    meta = await ask_vertex_ai_with_meta(
        prompt,
        model=model,
        tag="portfolio_select",
        task_type=task_type,
        stock_id="PORTFOLIO",
        custom_id=f"{task_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        blocking_bq_log=True,
    )
    return meta


# --------------------------------------------------------------------------- #
# SASD v5.4.0 Phase 1：AI 紅藍軍審計（Pass 1）與庫存曝險耦合
# --------------------------------------------------------------------------- #
AUDIT_BATCH_SIZE = 3
AUDIT_RR_GUARD_THRESHOLD = 1.8
AUDIT_NEWS_MAX_ITEMS = 3
NO_PICK_MESSAGE = "今日無符合高勝率標準之標的，建議空手觀望"
AUDIT_FINALIZER_CONTEXT = (
    "【Pass 1 紅藍軍審計已完成】資料卡 D 區之停損價、追價上限、TP1 與風報比皆由 Python 確定性計算，"
    "`target_price_1` 與 `stop_loss_price` 必須沿用，嚴禁自行心算或改寫；"
    "僅標示『僅限換股』者不得新增總部位。若無任何標的符合標準，`top_picks` 請輸出空陣列（Zero-Pick Veto）。"
)

AUDITOR_SYSTEM_PROMPT = """你是一位掌管部位風控的台股機構級「冷血首席風控審計官（Chief Risk Auditor）」。
你的核心職責不是幫使用者找理由買股票，而是無情地戳破主力誘多的假突破，保護本金並確保獲利因子（Profit Factor）大於 2.0。

【核心審計鐵律】
1. 有罪推定原則：所有通過 Python 初篩的股票，預設都有 50% 機率是隔日沖主力製造的假突破陷阱。你必須先在 `bear_case_flaws` 找出它最致命的破綻。
2. 新聞與量價對齊鐵律：絕對不可單憑新聞利多標題給出正面評價！若新聞標題看多，但 Python 標註為『⚠️ 爆量收黑/長上影線：疑似利多出貨』，必須判定為主力藉新聞掩護出貨，直接打入 `REJECT_TRAP`。
3. 算術與點位尊重鐵律：所有停損價、追價上限、上方 Volume POC 壓力區與預估風報比（R/R Ratio）皆由 Python 精確計算完畢。若 deterministic_rr_ratio < 1.8，請優先評估是否適合改採 STYLE_B_PULLBACK_LIMIT（拉回支撐低接以提高風報比）並評為 A_TIER_PULLBACK，勿盲目追高。
4. 消除逆向選擇之進場紀律：
   - 右側突破強勢股：嚴禁建議「死等拉回平盤才買」（會導致真飆股踏空、只買到假突破倒貨股），亦嚴禁 09:00 開盤盲目市價追高；必須採用 `BREAKOUT_0915_VWAP_OR_1315_CLOSE`（早盤冷卻 15 分鐘確認站穩均價線且低於追價上限試單 50%，或 13:15 尾盤確認站穩買進）。
   - Style B 回測股：必須採用 `STYLE_B_PULLBACK_LIMIT` 於支撐區間限價低接。
5. 庫存共伴曝險鐵律：若候選股被標記為 SECTOR_OR_CORR_FULL_SWAP_ONLY，代表使用者台新帳戶中已有同產業或高相關持股；若該股基本面與籌碼顯著優於現有弱勢庫存，請將戰術設為 PORTFOLIO_SWAP_ONLY 並給予 S_TIER_SNIPER 或 A_TIER_PULLBACK，否則才判定為 REJECT_TRAP。
6. 寧缺勿濫否決權（Zero-Pick Veto）：如果整批候選股都存在結構性瑕疵，請毫不猶豫將全部股票評為 `REJECT_TRAP`。全數淘汰是完全正確且受讚賞的專業決策。
7. 微結構與大盤氣候鐵律：若標註『⚠️ 處置與流動性凍結預警』嚴禁建議右側追高突破；若標註『⚠️ 融資套牢誘多』、『⚠️ 散戶接刀警報』或『⚠️ 個股孤狼假突破』，必須列為重大空方破綻並從嚴降級或打入 `REJECT_TRAP`；若標註『MARGIN_CALENDAR_NOTE（強制融券回補窗口）』則屬制度性買盤，不得誤判為基本面利多。"""


class StockAuditItem(BaseModel):
    """Pass 1 個股審計結果。

    ⚠️ 欄位定義順序即 Structured Output 的生成 Token 順序，`verdict_grade` 必須維持最後一個欄位。
    """

    stock_id: str = Field(description="股票代號，例如 '2330'")
    stock_name: str = Field(description="股票名稱")

    # Step 1: 強制先找破綻 (Bear Case First)
    bear_case_flaws: list[str] = Field(
        description="【空頭狙擊手審視】列出 2~3 個未來 5 日最可能導致停損的致命傷（如：上方 Volume POC 套牢壓近、新聞利多卻留長上影線、融資暴增且收黑、同族群孤掌難鳴等）。嚴禁寫無關痛癢的客套話。"
    )
    # Step 2: 檢視多頭催化劑 (Bull Case Second)
    bull_case_catalysts: list[str] = Field(
        description="【多頭催化劑驗證】列出支持上漲的實質依據（如：投信初買鎖碼、營收加速擴張、突破長線底部等）。"
    )
    # Step 3: 正反辯證與庫存衝突裁決
    conflict_resolution_reasoning: str = Field(
        description="【風控審計官綜合裁決】權衡多空雙方：多頭力道是否足以完全碾壓空頭破綻？若該股標記為 SECTOR_OR_CORR_FULL_SWAP_ONLY，說明是否具備替換現有庫存的絕對優勢。"
    )
    # Step 4: 進場戰術類型選擇 (禁止 AI 自創價格，僅選擇戰術)
    execution_strategy_type: Literal[
        "BREAKOUT_0915_VWAP_OR_1315_CLOSE",
        "STYLE_B_PULLBACK_LIMIT",
        "PORTFOLIO_SWAP_ONLY",
        "NO_TRADE",
    ] = Field(description="依據訊號型態與庫存曝險狀態，裁定唯一適用的實盤執行戰術")
    # Step 5: 邏輯失效條件
    thesis_invalidation_triggers: list[str] = Field(
        description="列出 2 個『即使股價未觸及硬停損價，一旦發生也必須隔日無條件砍倉』的量化條件（如：投信轉賣超 > 500 張、跌破突破紅K實體一半且量增）。"
    )
    # Step 6: 最終評級 (必須放在最後一個欄位！)
    verdict_grade: Literal["S_TIER_SNIPER", "A_TIER_PULLBACK", "REJECT_TRAP"] = Field(
        description="最終審計評級。寧缺勿濫，若無十足把握必須給出 REJECT_TRAP。"
    )


class BatchAuditResponse(BaseModel):
    audited_stocks: list[StockAuditItem]


def _exposure_field(exposure: object, name: str, default: object = None) -> object:
    if exposure is None:
        return default
    if isinstance(exposure, Mapping):
        return exposure.get(name, default)
    return getattr(exposure, name, default)


def format_swap_banner(exposure: object) -> str:
    holding = _exposure_field(exposure, "max_corr_holding_id") or _exposure_field(
        exposure, "weakest_holding_to_swap"
    )
    return f"🔄 僅限換股（需先減碼現有共伴持股：{holding or '同題材庫存'}）"


def _enforce_post_audit_guards(
    items: list[StockAuditItem],
    setups: Mapping[str, Mapping[str, object]],
    exposures: Mapping[str, object],
    *,
    context_by_stock: Mapping[str, Mapping[str, object]] | None = None,
    regime_quota: Mapping[str, object] | None = None,
) -> list[StockAuditItem]:
    """AI 紅藍軍審計後之輕量風控標註（SASD v5.9.0：不以死板單一指標硬砍 AI 選出之優質股，改為附加風險提示交由 Stage 2 總決策權衡）。

    1. 僅當庫存曝險為 SECTOR_OR_CORR_FULL_SWAP_ONLY 且非 REJECT_TRAP 時，標記為 PORTFOLIO_SWAP_ONLY（提醒換股操作）。
    2. 風報比偏低、處置預警、大盤紅黃燈、族群廣度或前瞻 PE 偏高時，將量化觀測附加於 conflict_resolution_reasoning 與 bear_case_flaws 供 Stage 2 CIO 綜合裁量，不強制覆寫為 REJECT_TRAP。
    """
    guarded: list[StockAuditItem] = []
    for item in items:
        update: dict[str, object] = {}
        setup = setups.get(item.stock_id)
        ctx = (context_by_stock or {}).get(item.stock_id) or {}
        grade = item.verdict_grade
        tactic = item.execution_strategy_type
        reasoning = item.conflict_resolution_reasoning
        flaws = list(item.bear_case_flaws)
        notes: list[str] = []

        if setup is not None and grade != "REJECT_TRAP":
            rr = setup.get("deterministic_rr_ratio")
            if rr is not None and float(rr) < AUDIT_RR_GUARD_THRESHOLD:
                notes.append(f"確定性風報比 {rr} 偏低(<{AUDIT_RR_GUARD_THRESHOLD})，宜等拉回支撐區低接")
            if setup.get("rebound_subtype") and tactic == "BREAKOUT_0915_VWAP_OR_1315_CLOSE":
                update["execution_strategy_type"] = "STYLE_B_PULLBACK_LIMIT"

        disp_tag = str(ctx.get("disposition_tag") or "").strip()
        if grade != "REJECT_TRAP" and disp_tag:
            notes.append("觸發注意/處置過熱預警，切忌早盤追高，宜採回測限價低接")
            if tactic == "BREAKOUT_0915_VWAP_OR_1315_CLOSE":
                update["execution_strategy_type"] = "STYLE_B_PULLBACK_LIMIT"

        if grade != "REJECT_TRAP" and regime_quota:
            r_code = str(regime_quota.get("regime_state") or regime_quota.get("regime") or "")
            if r_code == "RED_DEFENSIVE":
                notes.append("大盤處於紅燈防禦期，須確認基本面現金流與法人逆勢買盤特別強勁")
            min_breadth = float(regime_quota.get("min_sector_breadth_pct", 0.0) or 0.0)
            b_members = int(ctx.get("sector_member_count", 0) or 0)
            b_pct = ctx.get("sector_breadth_pct")
            if min_breadth > 0 and b_members >= 3 and b_pct is not None and float(b_pct) < min_breadth:
                notes.append(f"同細分產業站上17MA廣度僅 {b_pct}%，屬個股突圍需留意資金續航力")
            max_pe_ratio = float(regime_quota.get("max_allowed_sector_pe_ratio", 999.0) or 999.0)
            pe_ratio = ctx.get("sector_pe_ratio")
            if pe_ratio is not None and float(pe_ratio) > max_pe_ratio:
                notes.append(f"前瞻PE相對同業比值 {pe_ratio}x 高於常態({max_pe_ratio}x)，需高成長動能支撐")

        if grade != "REJECT_TRAP":
            status = _exposure_field(exposures.get(item.stock_id), "exposure_status")
            if status == "SECTOR_OR_CORR_FULL_SWAP_ONLY":
                update["execution_strategy_type"] = "PORTFOLIO_SWAP_ONLY"
                notes.append("台新庫存已有同產業或高相關持股，建議採汰弱留強換股操作")

        if notes:
            note_str = "｜[量化風控備註] " + "；".join(notes)
            update["conflict_resolution_reasoning"] = f"{reasoning}{note_str}"
            if len(flaws) < 4:
                flaws.append("；".join(notes))
                update["bear_case_flaws"] = flaws

        guarded.append(item.model_copy(update=update) if update else item)
    return guarded


def enforce_regime_quotas(
    decision: dict,
    audit_records: Mapping[str, Mapping[str, object]],
    regime_quota: Mapping[str, object] | None,
    *,
    frame: pd.DataFrame | None = None,
) -> dict:
    """依雙指數大盤風控路由器（RegimeQuotaConfig）嚴格限制右側突破與回測低接之當日入選檔數上限。"""
    if not decision or not regime_quota:
        return decision
    max_rb = int(regime_quota.get("max_right_breakout_picks", 99))
    max_sb = int(regime_quota.get("max_style_b_pullback_picks", 99))
    row_map = _row_lookup(frame) if frame is not None and not frame.empty else {}

    kept: list[dict] = []
    demoted: list[dict] = []
    rb_count = 0
    sb_count = 0
    red_exempt_used = False
    for pick in decision.get("top_picks", []):
        sid = str(pick.get("stock_id", ""))
        rec = audit_records.get(sid) or pick.get("audit") or {}
        tactic = str(rec.get("Execution_Tactic") or "")
        sig_text = str((row_map.get(sid) or {}).get("signal") or "")
        is_rb = tactic == "BREAKOUT_0915_VWAP_OR_1315_CLOSE" or (
            not tactic and ("突破" in sig_text or "站上" in sig_text) and "回踩" not in sig_text
        )
        if is_rb:
            pick_score = float(_parse_optional_num(pick.get("score")) or 0.0)
            if rb_count < max_rb:
                rb_count += 1
                kept.append(pick)
            elif max_rb == 0 and not red_exempt_used and pick_score >= 85.0:
                # SASD v5.9.1: 對齊 Stage 2 授權，紅燈時豁免保留 1 檔高分抗跌真龍頭 (score >= 85)
                red_exempt_used = True
                rb_count += 1
                kept.append(pick)
            else:
                demoted.append(
                    {
                        "stock_id": sid,
                        "reason": f"【大盤氣候配額調控】當日右側突破上限 {max_rb} 檔已滿，移列觀察名單。",
                    }
                )
        else:
            if sb_count < max_sb:
                sb_count += 1
                kept.append(pick)
            else:
                demoted.append(
                    {
                        "stock_id": sid,
                        "reason": f"【大盤氣候配額調控】當日回測低接上限 {max_sb} 檔已滿，移列觀察名單。",
                    }
                )

    for idx, p in enumerate(kept, start=1):
        p["rank"] = idx
    decision["top_picks"] = kept
    if demoted:
        existing_w = {str(w.get("stock_id")) for w in decision.get("watchlist", [])}
        for d_item in demoted:
            if d_item["stock_id"] not in existing_w:
                decision.setdefault("watchlist", []).append(d_item)
                existing_w.add(d_item["stock_id"])
    return decision


def select_audit_survivors(items: list[StockAuditItem]) -> list[StockAuditItem]:
    """剔除 REJECT_TRAP 與 NO_TRADE；允許回傳空清單（Zero-Pick Veto）。"""
    return [
        i for i in items if i.verdict_grade != "REJECT_TRAP" and i.execution_strategy_type != "NO_TRADE"
    ]


def build_audit_record(
    item: StockAuditItem,
    setup: Mapping[str, object] | None,
    exposure: object,
    *,
    microstructure_block: str = "",
    news_items: list[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """合併 Python 確定性點位、微結構、新聞量價反應與 AI 審計結論（完整傳遞至 Stage 2 總決策）。"""
    setup = setup or {}
    zone_low, zone_high = setup.get("ideal_buy_zone_low"), setup.get("ideal_buy_zone_high")
    ref = setup.get("entry_reference_price")
    if zone_low is not None and zone_high is not None and zone_low != zone_high:
        ideal = f"{zone_low}~{zone_high}"
    else:
        ideal = "" if ref is None else str(ref)
    swap = _exposure_field(exposure, "exposure_status") == "SECTOR_OR_CORR_FULL_SWAP_ONLY"
    compact_news = [
        f"[{n.get('pub_date', '')[:10]}] {n.get('title', '')} ➔ {n.get('reaction_tag', '')}"
        for n in (news_items or [])[:3]
    ]
    return {
        "Audit_Grade": item.verdict_grade,
        "Execution_Tactic": item.execution_strategy_type,
        "Rebound_Subtype": setup.get("rebound_subtype", ""),
        "Anchor_Tiers": setup.get("anchor_tiers"),
        "Ideal_Buy_Or_Ref": ideal,
        "Chase_Limit_Price": setup.get("chase_limit_price"),
        "Hard_Stop_Loss": setup.get("hard_stop_loss"),
        "TP1_Target_Or_POC": setup.get("tp1_price"),
        "Deterministic_RR": setup.get("deterministic_rr_ratio"),
        "Bear_Case_Warning": item.bear_case_flaws[0] if item.bear_case_flaws else "",
        "Bear_Case_Flaws_All": list(item.bear_case_flaws),
        "Bull_Case_Catalysts": list(item.bull_case_catalysts),
        "Auditor_Reasoning": item.conflict_resolution_reasoning,
        "Invalidation_Triggers": list(item.thesis_invalidation_triggers),
        "Exposure_Status": _exposure_field(exposure, "exposure_status", "OPEN"),
        "Swap_Banner": format_swap_banner(exposure) if swap else "",
        "Microstructure_Summary": microstructure_block,
        "Recent_News_Reactions": compact_news,
    }


def build_audit_block(
    row: Mapping[str, object],
    setup: Mapping[str, object] | None,
    exposure: object,
    news: list[Mapping[str, object]],
    fundamental_block: str = "",
    microstructure_block: str = "",
) -> str:
    """組裝單檔候選股的審計資料卡（Python 點位 + 庫存曝險 + 新聞量價反應 + Phase 2 基本面 + Phase 3 微結構）。"""
    lines = [f"### 【審計標的】{row.get('stock_id')} {row.get('stock_name', '')}"]
    if fundamental_block:
        lines.append(fundamental_block)
    if microstructure_block:
        lines.append(microstructure_block)
    if setup:
        lines.append(
            f"- 訊號型態：{setup['signal_type']}｜收盤 {setup['close_t']}｜ATR14 {setup['atr_14']}"
            f"｜60日量 POC {setup.get('volume_poc_60d')}"
        )
        lines.append(
            f"- 確定性點位：參考建倉 {setup['entry_reference_price']}｜低接區 "
            f"{setup['ideal_buy_zone_low']}~{setup['ideal_buy_zone_high']}｜追價上限 {setup['chase_limit_price']}"
            f"｜硬停損 {setup['hard_stop_loss']}｜TP1 {setup['tp1_price']}（{setup['tp1_source']}）"
            f"｜deterministic_rr_ratio {setup['deterministic_rr_ratio']}"
            + (f"｜⚠️ {setup['rr_warning']}" if setup.get("rr_warning") else "")
        )
        anchor_tiers = setup.get("anchor_tiers")
        if isinstance(anchor_tiers, Mapping) and anchor_tiers:
            lines.append(
                "- 三層結構錨點選單（供 CIO 依基本面與大盤決定分批建倉）："
                f"第一道防線(積極) {anchor_tiers.get('tier1_aggressive_support', '')}｜"
                f"第二道防線(穩健洗盤) {anchor_tiers.get('tier2_steady_washout', '')}｜"
                f"第三道防線(極限左腳) {anchor_tiers.get('tier3_extreme_left_foot', '')}"
            )
        if setup.get("rebound_subtype"):
            lines.append(
                f"- 【型態 A 反彈審計準則（{setup.get('rebound_subtype')}）】：本標的為長均線反彈/W底右腳低接（非右側追突破），"
                "execution_strategy_type 請優先選用 STYLE_B_PULLBACK_LIMIT；若具備『STRATEGIC_CAPEX_EXPANSION』"
                "或『🛡️土洋對作/投信鎖碼護盤』，屬機構鎖碼與擴產利多，勿因負 FCF 或外資短線調節誤判為 REJECT_TRAP。"
            )
    else:
        lines.append("- 確定性點位：K 線資料不足，無法計算（請從嚴審計）。")
    status = _exposure_field(exposure, "exposure_status", "OPEN")
    lines.append(
        f"- 庫存曝險：{status}｜同產業持股 {list(_exposure_field(exposure, 'same_sector_holdings', []) or [])}"
        f"｜最高相關持股 {_exposure_field(exposure, 'max_corr_holding_id')}"
        f"（60日相關係數 {_exposure_field(exposure, 'max_corr_value', 0.0)}）"
        f"｜建議比較之弱勢庫存 {_exposure_field(exposure, 'weakest_holding_to_swap')}"
    )
    if status == "SECTOR_OR_CORR_FULL_SWAP_ONLY":
        lines.append(
            "- 禁止直接新增總部位！僅當此候選股明顯強於現有庫存中最弱勢的同質股 "
            f"`{_exposure_field(exposure, 'weakest_holding_to_swap')}` 時，才可列為『汰弱留強換股建議』，否則直接淘汰。"
        )
    if news:
        lines.append("- 近期新聞與量價反應：")
        for n in news:
            lines.append(
                f"  - [{n.get('pub_date', '')}] {n.get('title', '')}｜{n.get('reaction_tag', '')}"
                f"（反應日 {n.get('reaction_date', '')}，漲跌 {n.get('day_return_pct', 'N/A')}%，"
                f"量比 {n.get('vol_ratio_5d', 'N/A')}，上影線 {n.get('upper_shadow_ratio', 'N/A')}）"
                f"\n    摘要：{n.get('summary', '')}"
            )
    else:
        lines.append("- 近期新聞：無")
    return "\n".join(lines)


def _extract_json_text(raw: str) -> str:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    if fenced:
        return fenced.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start : end + 1] if start != -1 and end > start else raw


def parse_audit_response(raw: str) -> BatchAuditResponse | None:
    try:
        return BatchAuditResponse.model_validate_json(_extract_json_text(raw or ""))
    except (ValidationError, ValueError):
        return None


def build_audit_prompt(blocks: list[str]) -> str:
    schema = json.dumps(BatchAuditResponse.model_json_schema(), ensure_ascii=False)
    return (
        AUDITOR_SYSTEM_PROMPT
        + "\n\n請嚴格輸出符合下列 JSON Schema 的單一 JSON 物件（可包在 ```json 區塊內），"
        "且每個物件的欄位必須依 Schema 定義順序輸出（先 bear_case_flaws，最後才是 verdict_grade）。"
        "每一檔候選股都必須回覆，不可遺漏：\n"
        + schema
        + "\n\n==================================================\n【待審計候選股】\n\n"
        + "\n\n".join(blocks)
    )


async def _audit_batch(
    batch: list[tuple[str, str]],
    model: str | None,
    *,
    task_type: str = "RED_BLUE_AUDIT",
) -> tuple[list[StockAuditItem], list[str]]:
    ids = [sid for sid, _ in batch]
    ids_set = set(ids)
    # 避免多個數字代號以逗號串接時被 BigQuery/Pandas 誤判為千分位大整數 (如 203,428,822,885)
    stock_ids_str = "|".join(sorted(ids))
    try:
        meta = await ask_vertex_ai_with_meta(
            build_audit_prompt([block for _, block in batch]),
            model=model,
            tag="red_blue_audit",
            response_schema=BatchAuditResponse,
            task_type=task_type,
            stock_id=stock_ids_str,
            custom_id=f"AUDIT_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{'_'.join(sorted(ids))[:30]}",
            blocking_bq_log=True,
        )
    except Exception as error:  # noqa: BLE001
        print(f"[warn] 紅藍軍審計批次 {sorted(ids)} 呼叫失敗：{type(error).__name__}: {error}")
        return [], sorted(ids)
    parsed = parse_audit_response(str(getattr(meta, "text", "") or ""))
    if parsed is None:
        print(f"[warn] 紅藍軍審計批次 {sorted(ids)} 輸出無法解析。")
        return [], sorted(ids)
    items = [i for i in parsed.audited_stocks if i.stock_id in ids_set]
    missing = sorted(ids_set - {i.stock_id for i in items})
    return items, missing


async def run_red_blue_audit(
    blocks: list[tuple[str, str]],
    *,
    model: str | None = None,
    batch_size: int = AUDIT_BATCH_SIZE,
    use_vertex_batch: bool = False,
    poll_interval: float = DEFAULT_BATCH_POLL_SECONDS,
    target_date: date | None = None,
    task_type: str = "RED_BLUE_AUDIT",
) -> tuple[list[StockAuditItem], list[str]]:
    """Pass 1 紅藍軍審計：
    - 當 `use_vertex_batch=True` 時，採 Vertex AI Batch API（預設模型 `get_vertex_batch_stage1_model()` 即 `gemini-3.8-flash`），
      每檔候選股獨立一筆 Batch Request（`custom_id` 為 `stock_id`），享 50% Batch 折扣且 BigQuery `stock_id` 欄位精確為單一股票代號。
    - 當 `use_vertex_batch=False` 時，維持每 `batch_size` 檔以 `asyncio.gather` 即時並行呼叫。
    """
    if not blocks:
        return [], []

    resolved_model = model or get_vertex_batch_stage1_model()

    if use_vertex_batch:
        day_str = (target_date or date.today()).isoformat()
        batch_prompts: dict[str, str] = {
            sid: build_audit_prompt([block]) for sid, block in blocks
        }
        stock_id_map: dict[str, str] = {sid: sid for sid, _ in blocks}
        try:
            batch_outputs = await run_vertex_batch_requests(
                batch_prompts,
                model=resolved_model,
                job_prefix=f"pass1_audit_{day_str}",
                poll_interval_seconds=poll_interval,
                tag="red_blue_audit_batch",
                task_type=task_type,
                stock_ids=stock_id_map,
            )
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 紅藍軍 Vertex Batch 呼叫失敗，自動退回即時模式：{type(error).__name__}: {error}")
            batch_outputs = {}

        if batch_outputs:
            items: list[StockAuditItem] = []
            missing: list[str] = []
            for sid, _ in blocks:
                ai_meta = batch_outputs.get(sid)
                if ai_meta is None:
                    missing.append(sid)
                    continue
                parsed = parse_audit_response(str(getattr(ai_meta, "text", "") or ""))
                if parsed is None or not parsed.audited_stocks:
                    missing.append(sid)
                    continue
                matched = next((it for it in parsed.audited_stocks if str(it.stock_id).strip() == sid), parsed.audited_stocks[0])
                if str(matched.stock_id).strip() != sid:
                    matched = matched.model_copy(update={"stock_id": sid})
                items.append(matched)
            return items, missing

    batches = [blocks[i : i + batch_size] for i in range(0, len(blocks), batch_size)]
    outcomes = await asyncio.gather(
        *(_audit_batch(b, resolved_model, task_type=task_type) for b in batches)
    )
    items = []
    missing = []
    for batch_items, batch_missing in outcomes:
        items.extend(batch_items)
        missing.extend(batch_missing)
    return items, missing


def _filter_recent_rss_news(
    raw_news: list[Mapping[str, object]],
    stock_prices: pd.DataFrame | None,
    *,
    trading_days: int = 5,
    target_date: date | None = None,
) -> list[dict[str, object]]:
    """過濾保留近 5 個交易日內的即時 RSS 新聞（若無日期或無法解析則保留以維持相容）。"""
    if not raw_news:
        return []
    from datetime import timedelta
    from email.utils import parsedate_to_datetime

    cutoff: date | None = None
    if stock_prices is not None and not stock_prices.empty and "date" in stock_prices.columns:
        parsed_dates: list[date] = []
        for raw_dt in stock_prices["date"].dropna().astype(str).unique():
            try:
                parsed_dates.append(date.fromisoformat(raw_dt[:10]))
            except ValueError:
                continue
        if parsed_dates:
            parsed_dates.sort()
            idx = max(0, len(parsed_dates) - max(1, trading_days))
            cutoff = parsed_dates[idx]
    if cutoff is None and target_date is not None:
        cutoff = target_date - timedelta(days=9)
    if cutoff is None:
        return [dict(it) for it in raw_news]

    filtered: list[dict[str, object]] = []
    for it in raw_news:
        pub_raw = str(it.get("pubDate") or it.get("pub_date") or "").strip()
        if not pub_raw:
            filtered.append(dict(it))
            continue
        pub_d: date | None = None
        try:
            pub_d = parsedate_to_datetime(pub_raw).date()
        except Exception:  # noqa: BLE001
            try:
                pub_d = date.fromisoformat(pub_raw[:10])
            except ValueError:
                pub_d = None
        if pub_d is None or pub_d >= cutoff:
            filtered.append(dict(it))
    return filtered


def _resolve_audit_db_path(config: Mapping[str, object], output_root: Path) -> Path:
    raw = str(config.get("signal_audit_db_path", "data/signal_audit.db") or "data/signal_audit.db").strip()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    if str(output_root) and output_root != Path("finmind_reports") and raw == "data/signal_audit.db":
        return output_root / ".state" / "signal_audit.db"
    return _ROOT / candidate


def _taxonomy_sector_resolver(config: Mapping[str, object]):
    try:
        from taxonomy_service import TaxonomyService

        service = TaxonomyService(
            cache_path=Path(str(config.get("taxonomy_cache_path", "finmind_reports/.state/stock_taxonomy.json"))),
            ttl_days=int(config.get("taxonomy_ttl_days", 90)),
            sheet_tab_name=str(config.get("taxonomy_gsheet_tab_name", "08_Stock_Taxonomy")),
        )
        service.load()

        def resolve(stock_id: str) -> str:
            record = service.get_cached(str(stock_id)) or {}
            return str(record.get("sub_industry") or "").strip()

        return resolve
    except Exception as error:  # noqa: BLE001
        print(f"[warn] 無法載入細分產業知識庫，庫存同產業比對將略過：{error}")
        return None


def _load_prices_for_audit(daily_dir: Path, target_date: date) -> pd.DataFrame | None:
    path = daily_dir / f"{target_date.isoformat()}_prices.csv"
    if not path.is_file():
        found = sorted(daily_dir.glob("*_prices.csv"))
        if not found:
            return None
        path = found[-1]
    frame = pd.read_csv(path, dtype={"stock_id": str})
    frame["stock_id"] = frame["stock_id"].astype(str).str.strip()
    return frame


async def run_audit_stage(
    analyzed: pd.DataFrame,
    config: Mapping[str, object],
    *,
    target_date: date,
    daily_dir: Path,
    analyzer: object | None = None,
    model: str | None = None,
    market_regime: Mapping[str, object] | None = None,
    dry_run: bool = False,
    stage1_results: Mapping[str, Mapping[str, object]] | None = None,
    use_vertex_batch: bool = False,
    poll_interval: float = DEFAULT_BATCH_POLL_SECONDS,
    selection_mode: str = "trend",
    task_type: str = "RED_BLUE_AUDIT",
) -> tuple[pd.DataFrame, dict[str, dict[str, object]], list[dict[str, str]]]:
    """Pass 1 紅藍軍審計：除息補償 → 點位 → 處置/三層籌碼/族群廣度 → 庫存曝險 → 新聞量價對齊 → AI 審計 → 硬攔截 → 訊號留痕。

    回傳 (倖存候選 frame, {stock_id: audit_record}, 被淘汰清單 [{stock_id, reason}])。
    """
    from finmind_stock_scanner import (
        MARGIN_CALENDAR_NOTE,
        align_news_with_price_action,
        classify_daily_margin_efficiency,
        compensate_ex_dividend_gaps,
        compute_rebound_trade_setup,
        evaluate_disposition_risk,
        evaluate_weekly_tdcc_anchor,
        extract_ex_dividend_dates,
        fetch_news_excerpt,
        fetch_or_cache_tdcc_holding,
        compute_deterministic_trade_setup,
        is_mandatory_margin_covering_window,
    )
    from macro_context import quota_from_market_regime
    from taxonomy_service import TaxonomyService, build_sector_metrics_from_prices, compute_sector_breadth
    from taishin_portfolio_service import evaluate_candidate_exposure, load_portfolio_exposure_inputs

    prices = _load_prices_for_audit(daily_dir, target_date)
    holdings, holding_prices = await asyncio.to_thread(
        load_portfolio_exposure_inputs, daily_dir, _taxonomy_sector_resolver(config)
    )

    regime_quota_dict: dict[str, object] | None = None
    if market_regime:
        rq = market_regime.get("regime_quota")
        if isinstance(rq, Mapping):
            regime_quota_dict = dict(rq)
        elif bool(_cfg(config, "enable_dual_index_regime_router", False)):
            regime_quota_dict = quota_from_market_regime(market_regime).to_dict()

    universe_metrics: dict[str, dict[str, object]] = {}
    close_map: dict[str, float] = {}
    if prices is not None and not prices.empty:
        universe_metrics = build_sector_metrics_from_prices(prices)
        close_map = {sid: float(m["close"]) for sid, m in universe_metrics.items() if float(m.get("close", 0)) > 0}

    sector_pe_lookup: dict[str, float] = {}
    if analyzer is not None and hasattr(analyzer, "compute_cross_sectional_pe_map") and close_map:
        try:
            sector_pe_lookup = await asyncio.to_thread(analyzer.compute_cross_sectional_pe_map, close_map)
        except Exception:  # noqa: BLE001
            sector_pe_lookup = {}

    tax_map: dict[str, dict[str, object]] = {}
    try:
        tax_svc = TaxonomyService(
            cache_path=Path(str(config.get("taxonomy_cache_path", "finmind_reports/.state/stock_taxonomy.json"))),
            ttl_days=int(config.get("taxonomy_ttl_days", 90)),
            sheet_tab_name=str(config.get("taxonomy_gsheet_tab_name", "08_Stock_Taxonomy")),
        )
        tax_map = tax_svc.load()
    except Exception:  # noqa: BLE001
        tax_map = {}

    enable_ex_div = bool(_cfg(config, "enable_ex_dividend_compensation", True))
    cache_obj = getattr(analyzer, "cache", None) if analyzer is not None else None
    loader = getattr(cache_obj, "_finmind", None) if cache_obj is not None else None

    setups: dict[str, dict[str, object]] = {}
    exposures: dict[str, object] = {}
    audit_contexts: dict[str, dict[str, object]] = {}
    blocks: list[tuple[str, str]] = []
    rows = analyzed.to_dict("records")
    for row in rows:
        sid = str(row["stock_id"])
        sub_ind = _effective_sector_key(row)
        raw_stock_prices = prices.loc[prices["stock_id"] == sid].copy() if prices is not None else None
        has_prices = raw_stock_prices is not None and not raw_stock_prices.empty

        # 讀取快取股利表（供除權息跳空補償與強制融券回補日曆雷達共用）
        div_df: pd.DataFrame | None = None
        if cache_obj is not None:
            try:
                div_df = await asyncio.to_thread(
                    cache_obj._read_stock_rows,  # noqa: SLF001
                    cache_obj._get_worksheet("06_Dividend_Policy"),  # noqa: SLF001
                    sid,
                )
            except Exception:  # noqa: BLE001
                div_df = None

        # Phase 2 基本面語意特徵與安全流通股數 / 實收資本額
        fund_block = ""
        safe_shares: float | None = None
        paid_in_cap_twd: float = 0.0
        sector_pe_ratio: float | None = None
        if analyzer is not None and hasattr(analyzer, "compute_fundamental_features"):
            try:
                feat_summary = await asyncio.to_thread(
                    analyzer.compute_fundamental_features,
                    sid,
                    close=_parse_optional_num(row.get("close")),
                    stock_name=str(row.get("stock_name", "") or ""),
                    sub_industry=sub_ind,
                    sector_pe_lookup=sector_pe_lookup,
                )
                fund_block = feat_summary.prompt_block
                safe_shares = feat_summary.safe_weighted_shares
                paid_in_cap_twd = float(getattr(feat_summary, "paid_in_capital_twd", 0.0) or 0.0)
                val_ratio = feat_summary.forward_valuation.get("sector_pe_ratio")
                sector_pe_ratio = float(val_ratio) if val_ratio is not None else None
            except Exception as error:  # noqa: BLE001
                print(f"[warn] {sid} 基本面語意特徵計算失敗（略過）：{error}")
        elif analyzer is not None and hasattr(analyzer, "build_ai_fundamental_prompt_block"):
            try:
                fund_block = await asyncio.to_thread(
                    analyzer.build_ai_fundamental_prompt_block,
                    sid,
                    close=_parse_optional_num(row.get("close")),
                    stock_name=str(row.get("stock_name", "") or ""),
                    sub_industry=sub_ind,
                )
            except Exception as error:  # noqa: BLE001
                print(f"[warn] {sid} 基本面語意特徵計算失敗（略過）：{error}")

        # 強制注入 Stage 1 已算妥之量化財務快照與單股財報診斷結論，徹底消除 Pass 1 盲審（並去除重複之基本面與前瞻估值審計摘要）
        s1_entry = (stage1_results or {}).get(sid) or {}
        s1_snap = str(s1_entry.get("snapshot") or "").strip()
        if s1_snap and fund_block and "【基本面與前瞻估值審計摘要" in s1_snap:
            s1_snap = s1_snap.split("【基本面與前瞻估值審計摘要", 1)[0].rstrip(" -\n")
        s1_sum = str(s1_entry.get("summary") or "").strip()
        s1_extra_lines: list[str] = []
        if s1_snap:
            s1_extra_lines.append("【Stage 1 核心財務、估值與籌碼實測快照】\n" + s1_snap)
        if s1_sum:
            s1_extra_lines.append("【Stage 1 單股財報審計結論 (ai_summary_json)】\n  " + s1_sum[:900])
        if s1_extra_lines:
            fund_block = (fund_block + "\n" + "\n".join(s1_extra_lines)).strip()

        # Phase 3.1: 除權息跳空補償（僅用於技術點位/POC/ATR 計算；處置風險維持真實原始價格與週轉率）
        prices_for_setup = raw_stock_prices
        if has_prices and enable_ex_div and div_df is not None and not div_df.empty:
            try:
                prices_for_setup = compensate_ex_dividend_gaps(raw_stock_prices, div_df)
            except Exception:  # noqa: BLE001
                prices_for_setup = raw_stock_prices

        if has_prices and (selection_mode or "").strip().lower() == "rebound":
            setup = compute_rebound_trade_setup(
                prices_for_setup,
                support_ma=str(row.get("support_ma") or "") or None,
                support_price=_parse_optional_num(row.get("support_price")),
                rebound_subtype=str(row.get("rebound_subtype") or "") or None,
            )
        else:
            setup = compute_deterministic_trade_setup(prices_for_setup) if has_prices else None
        if setup:
            setups[sid] = setup

        # Phase 3.1: 處置股與流動性凍結預警
        disposition_tag = ""
        if has_prices:
            try:
                disposition_tag = evaluate_disposition_risk(raw_stock_prices, shares_outstanding=safe_shares)
            except Exception:  # noqa: BLE001
                disposition_tag = ""

        # Phase 3.3: 同族群資金廣度共振
        breadth_info: dict[str, object] = {}
        if universe_metrics:
            try:
                breadth_info = compute_sector_breadth(
                    sub_ind,
                    universe_metrics,
                    taxonomy_records=tax_map,
                    target_stock_id=sid,
                )
            except Exception:  # noqa: BLE001
                breadth_info = {}

        # Phase 3.2: 三層式籌碼微結構引擎（日曆回補雷達 + 週頻集保大戶錨點 + 日頻融資K棒效率）
        chip_notes: list[str] = []
        try:
            ex_dates = extract_ex_dividend_dates(div_df)
            if is_mandatory_margin_covering_window(target_date, ex_dates):
                chip_notes.append(MARGIN_CALENDAR_NOTE)
        except Exception:  # noqa: BLE001
            pass

        if paid_in_cap_twd > 0 and (loader is not None or not dry_run):
            try:
                tdcc_df = await asyncio.to_thread(
                    fetch_or_cache_tdcc_holding,
                    sid,
                    as_of=target_date,
                    cache_path=daily_dir.parent / ".state" / "tdcc_holding_cache.json",
                    loader=loader if not dry_run else None,
                )
                tdcc_info = evaluate_weekly_tdcc_anchor(tdcc_df, paid_in_cap_twd)
                if isinstance(tdcc_info, Mapping) and tdcc_info.get("summary"):
                    chip_notes.append(str(tdcc_info["summary"]))
            except Exception:  # noqa: BLE001
                pass

        if has_prices and cache_obj is not None:
            try:
                chip_df = await asyncio.to_thread(
                    cache_obj._read_stock_rows,  # noqa: SLF001
                    cache_obj._get_worksheet("07_Chip_Summary"),  # noqa: SLF001
                    sid,
                )
                if chip_df is not None and not chip_df.empty:
                    chip_latest = chip_df.iloc[-1].to_dict()
                    sorted_bars = raw_stock_prices.sort_values("date")
                    last_bar = sorted_bars.iloc[-1]
                    b_open = float(last_bar.get("open", last_bar.get("close", 0.0)) or 0.0)
                    b_high = float(last_bar.get("max", last_bar.get("high", b_open)) or b_open)
                    b_low = float(last_bar.get("min", last_bar.get("low", b_open)) or b_open)
                    b_close = float(last_bar.get("close", 0.0) or 0.0)
                    prev_close_p = (
                        float(sorted_bars.iloc[-2]["close"])
                        if len(sorted_bars) >= 2
                        else (b_open if b_open > 0 else max(b_close, 1.0))
                    )
                    c_10d_ago = (
                        float(sorted_bars.iloc[-11]["close"])
                        if len(sorted_bars) >= 11
                        else float(sorted_bars.iloc[0]["close"])
                    )
                    ret_10d = ((b_close - c_10d_ago) / c_10d_ago * 100.0) if c_10d_ago > 0 else 0.0
                    if "Trading_Volume" in last_bar and pd.notna(last_bar.get("Trading_Volume")):
                        b_vol_lots = float(last_bar.get("Trading_Volume", 0.0) or 0.0) / 1000.0
                    else:
                        b_vol_lots = float(last_bar.get("vol_lots", 0.0) or 0.0)
                    est_daily_m_chg = float(chip_latest.get("margin_chg_5d_lots", 0.0) or 0.0) / 5.0
                    est_daily_inst = float(chip_latest.get("three_inst_net_5d_lots", 0.0) or 0.0) / 5.0
                    trust_5d = float(chip_latest.get("trust_net_buy_5d_lots", 0.0) or 0.0)
                    trust_hold_pct = (
                        max(
                            0.0,
                            float(chip_latest.get("trust_net_buy_20d_lots", 0.0) or 0.0)
                            * 1000.0
                            / float(safe_shares)
                            * 100.0,
                        )
                        if (safe_shares and safe_shares > 0)
                        else 0.0
                    )
                    eff_res = classify_daily_margin_efficiency(
                        open_p=b_open,
                        high_p=b_high,
                        low_p=b_low,
                        close_p=b_close,
                        prev_close_p=max(prev_close_p, 0.01),
                        volume_lots=b_vol_lots,
                        margin_net_change_lots=est_daily_m_chg,
                        inst_net_buy_lots=est_daily_inst,
                        trust_holding_pct=trust_hold_pct,
                        trust_3d_net_buy_lots=trust_5d * 0.6,
                        return_10d_pct=ret_10d,
                    )
                    if eff_res.margin_pattern != "CHIP_NEUTRAL":
                        chip_notes.append(eff_res.semantic_tag)
            except Exception:  # noqa: BLE001
                pass

        micro_lines: list[str] = []
        if regime_quota_dict:
            r_label = regime_quota_dict.get("regime_state") or regime_quota_dict.get("regime") or ""
            r_banner = regime_quota_dict.get("regime_summary_tag") or regime_quota_dict.get("banner") or ""
            micro_lines.append(
                f"- 大盤雙指數氣候：{r_label}（{r_banner or '請優先篩選基本面與籌碼頂尖且具安全墊之標的'}）"
            )
        if disposition_tag:
            micro_lines.append(f"- 處置流動性雷達：{disposition_tag}")
        if breadth_info and int(breadth_info.get("member_count", 0) or 0) > 0:
            b_cnt = int(breadth_info.get("member_count", 0) or 0)
            b_pct_val = float(breadth_info.get("breadth_pct", 0.0) or 0.0)
            pr_val = breadth_info.get("return_percentile")
            pr_text = f"PR{pr_val}" if pr_val is not None else "樣本不足"
            b_tag = (
                "🔥 族群齊漲共振"
                if b_pct_val >= 60.0
                else ("⚠️ 孤軍深入" if b_cnt >= 3 and b_pct_val < 35.0 else "➖ 族群中性")
            )
            micro_lines.append(
                f"- 同族群資金廣度（{sub_ind or '未分類'}，共 {b_cnt} 檔）："
                f"站上 17MA 比例 {b_pct_val}%｜個股日報酬同業 {pr_text}｜{b_tag}"
            )
        if chip_notes:
            micro_lines.append("- 三層籌碼微結構：" + " ｜ ".join(chip_notes))
        microstructure_block = "\n".join(micro_lines)

        audit_contexts[sid] = {
            "disposition_tag": disposition_tag,
            "sector_breadth_pct": breadth_info.get("breadth_pct"),
            "sector_member_count": breadth_info.get("member_count", 0),
            "return_percentile": breadth_info.get("return_percentile"),
            "sector_pe_ratio": sector_pe_ratio,
            "sub_industry": sub_ind,
            "stock_name": str(row.get("stock_name", "") or ""),
            "close": _parse_optional_num(row.get("close")),
        }

        exposure = None
        if holdings and has_prices:
            exposure = evaluate_candidate_exposure(
                sid,
                sub_ind,
                holdings,
                candidate_prices=raw_stock_prices,
                holding_prices=holding_prices,
            )
            exposures[sid] = exposure
        news: list[dict[str, object]] = []
        if analyzer is not None and has_prices:
            try:
                from fundamental_analyzer import filter_and_clean_rss
                raw_news = await asyncio.to_thread(analyzer._fetch_yahoo_stock_rss, sid)  # noqa: SLF001
                cleaned_news = filter_and_clean_rss(raw_news, is_guidance=False, max_age_days=14, sort_by_date=True)
                if not cleaned_news:
                    q_str = f"{sid} {row.get('stock_name', '')} (MoneyDJ OR 鉅亨網 OR 中央社 OR 經濟日報 OR 工商時報) when:14d"
                    raw_gnews = await asyncio.to_thread(analyzer._fetch_google_news_rss, q_str)  # noqa: SLF001
                    cleaned_news = filter_and_clean_rss(raw_gnews, is_guidance=False, max_age_days=14, sort_by_date=True)
                recent_news = _filter_recent_rss_news(
                    cleaned_news,
                    raw_stock_prices,
                    trading_days=5,
                    target_date=target_date,
                )
                news = await asyncio.to_thread(
                    align_news_with_price_action,
                    recent_news[:AUDIT_NEWS_MAX_ITEMS],
                    raw_stock_prices,
                    excerpt_fetcher=lambda url: fetch_news_excerpt(url, timeout=3.0),
                )
            except Exception as error:  # noqa: BLE001
                print(f"[warn] {sid} 新聞量價對齊失敗（略過）：{error}")
        audit_contexts[sid]["microstructure_block"] = microstructure_block
        audit_contexts[sid]["news"] = news
        blocks.append((sid, build_audit_block(row, setup, exposure, news, fund_block, microstructure_block)))

    audit_model = model or get_vertex_batch_stage1_model()
    items, missing = await run_red_blue_audit(
        blocks,
        model=audit_model,
        use_vertex_batch=use_vertex_batch,
        poll_interval=poll_interval,
        target_date=target_date,
        task_type=task_type,
    )
    guarded = _enforce_post_audit_guards(
        items,
        setups,
        exposures,
        context_by_stock=audit_contexts,
        regime_quota=regime_quota_dict,
    )

    # Phase 3.5: 訊號留痕與勝率歸因資料庫（記錄所有 S_TIER / A_TIER / REJECT_TRAP，以利 False Negative 驗證）
    if bool(_cfg(config, "enable_signal_audit", False)) and not dry_run and guarded:
        try:
            from signal_audit_service import SignalAuditStore

            store = SignalAuditStore(_resolve_audit_db_path(config, daily_dir.parent))
            light_txt = str((market_regime or {}).get("light") or "")
            fallback_regime = (
                "GREEN_BULL"
                if "🟢" in light_txt
                else ("RED_DEFENSIVE" if "🔴" in light_txt else "YELLOW_ROTATION")
            )
            regime_str = str(
                (regime_quota_dict or {}).get("regime_state")
                or (regime_quota_dict or {}).get("regime")
                or fallback_regime
            )
            for g_item in guarded:
                g_setup = setups.get(g_item.stock_id) or {}
                g_ctx = audit_contexts.get(g_item.stock_id) or {}
                close_val = (
                    _parse_optional_num(g_setup.get("close_t"))
                    or _parse_optional_num(g_ctx.get("close"))
                    or 0.0
                )
                if close_val <= 0:
                    continue
                chase_val = _parse_optional_num(g_setup.get("chase_limit_price")) or round(close_val * 1.03, 2)
                stop_val = _parse_optional_num(g_setup.get("hard_stop_loss")) or round(close_val * 0.93, 2)
                tp1_val = _parse_optional_num(g_setup.get("tp1_price")) or round(close_val * 1.10, 2)
                sig_type_val = str(g_setup.get("signal_type") or "STYLE_B_PULLBACK")
                store.record_signal(
                    {
                        "signal_date": target_date.isoformat(),
                        "stock_id": g_item.stock_id,
                        "stock_name": str(g_ctx.get("stock_name") or g_item.stock_name or g_item.stock_id),
                        "sector_name": str(g_ctx.get("sub_industry") or ""),
                        "regime_state": regime_str,
                        "scanner_signal_type": sig_type_val,
                        "ai_verdict_grade": g_item.verdict_grade,
                        "execution_strategy_type": g_item.execution_strategy_type,
                        "close_t": close_val,
                        "ideal_buy_zone_low": _parse_optional_num(g_setup.get("ideal_buy_zone_low")),
                        "ideal_buy_zone_high": _parse_optional_num(g_setup.get("ideal_buy_zone_high")),
                        "chase_limit_price": chase_val,
                        "hard_stop_loss": stop_val,
                        "tp1_price": tp1_val,
                        "deterministic_rr_ratio": _parse_optional_num(g_setup.get("deterministic_rr_ratio")) or 0.0,
                    }
                )
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 寫入 SignalAuditStore 訊號留痕失敗（略過）：{error}")

    survivors = select_audit_survivors(guarded)
    survivor_ids = {i.stock_id for i in survivors}

    records = {
        i.stock_id: build_audit_record(
            i,
            setups.get(i.stock_id),
            exposures.get(i.stock_id),
            microstructure_block=str((audit_contexts.get(i.stock_id) or {}).get("microstructure_block") or ""),
            news_items=list((audit_contexts.get(i.stock_id) or {}).get("news") or []),
        )
        for i in survivors
    }
    rejected: list[dict[str, str]] = []
    for item in guarded:
        if item.stock_id in survivor_ids:
            continue
        flaw = item.bear_case_flaws[0] if item.bear_case_flaws else item.conflict_resolution_reasoning
        rejected.append({"stock_id": item.stock_id, "reason": f"【紅藍軍審計 {item.verdict_grade}】{flaw}"[:120]})
    for sid in missing:
        rejected.append({"stock_id": sid, "reason": "【紅藍軍審計】審計無有效回覆，為保護本金暫不入選。"})
    kept = analyzed.loc[analyzed["stock_id"].astype(str).isin(survivor_ids)].reset_index(drop=True)
    return kept, records, rejected


def apply_audit_to_decision(
    decision: dict,
    audit_records: Mapping[str, Mapping[str, object]],
    *,
    selection_mode: str = "trend",
) -> dict:
    """將 Python 確定性點位與審計結論併入 top_picks 與 watchlist，並防止 TP1 >= TP2 價格倒掛。"""
    try:
        from quant.fundamental_analyzer import _first_price
    except ImportError:
        from fundamental_analyzer import _first_price  # type: ignore[no-redef]

    is_rebound = (selection_mode or "").strip().lower() == "rebound"
    for pick in decision.get("top_picks", []):
        record = audit_records.get(str(pick.get("stock_id")))
        if not record:
            continue
        pick["audit"] = dict(record)
        py_tp1 = _parse_optional_num(record.get("TP1_Target_Or_POC"))
        py_sl = _parse_optional_num(record.get("Hard_Stop_Loss"))
        ai_tp1 = _first_price(pick.get("target_price_1"))
        ai_tp2 = _first_price(pick.get("target_price_2"))
        ai_sl = _first_price(pick.get("stop_loss_price"))

        if py_tp1 is not None and py_tp1 > 0 and (not is_rebound or ai_tp1 is None):
            pick["target_price_1"] = f"{py_tp1:.2f}（Python 確定性點位）"
            # 防倒掛：若原 AI 設定之 TP2 <= Python TP1，自動將 TP2 向上推升至少 8%
            if ai_tp2 is not None and ai_tp2 <= py_tp1:
                bumped_tp2 = round(py_tp1 * 1.08, 2)
                pick["target_price_2"] = f"{bumped_tp2:.2f}（依 Python TP1 自動順延 +8% 波段目標）"
        if py_sl is not None and py_sl > 0 and (not is_rebound or ai_sl is None):
            pick["stop_loss_price"] = f"{py_sl:.2f}（Python 確定性硬停損）"

    for w_item in decision.get("watchlist", []):
        record = audit_records.get(str(w_item.get("stock_id")))
        if record and "audit" not in w_item:
            w_item["audit"] = dict(record)
    return decision


def format_audit_lines(pick: Mapping[str, object]) -> list[str]:
    audit = pick.get("audit")
    if not isinstance(audit, Mapping):
        return []
    lines = [
        f"審計：{audit.get('Audit_Grade')}｜戰術：{audit.get('Execution_Tactic')}"
        + (
            f"｜建議資金權重 {audit.get('Allocation_Weight_Pct')}%"
            if audit.get("Allocation_Weight_Pct") not in (None, "")
            else f"｜風報比 {audit.get('Deterministic_RR')}"
        )
        + f"｜追價上限 {audit.get('Chase_Limit_Price')}"
        + f"｜建倉參考 {audit.get('Ideal_Buy_Or_Ref')}"
    ]
    if audit.get("Swap_Banner"):
        lines.insert(0, str(audit["Swap_Banner"]))
    t1 = str(audit.get("Tier1_Entry_Plan") or "").strip()
    t2 = str(audit.get("Tier2_Entry_Plan") or "").strip()
    t3 = str(audit.get("Tier3_Entry_Plan") or "").strip()
    if t1 or t2 or t3:
        if t1:
            lines.append(f"🎯 {t1}")
        if t2:
            lines.append(f"🛡️ {t2}")
        if t3:
            lines.append(f"💎 {t3}")
    if audit.get("Bear_Case_Warning"):
        lines.append(f"頭號風險：{audit['Bear_Case_Warning']}")
    triggers = audit.get("Invalidation_Triggers") or []
    if triggers:
        lines.append("失效砍倉：" + "；".join(str(t) for t in triggers))
    return lines


# --------------------------------------------------------------------------- #
# SASD v6.1：三階段專職分工（AI#2 跨股績優股選拔 ➔ AI#3 戰略低接點位與雙層籌碼分配）
# --------------------------------------------------------------------------- #
_QUALITY_LENS_TREND = (
    "1. 【21:30 順勢強勢股視角 — 成長動能真實性驗證】：本批候選股處於均線多頭排列，請優先檢驗股價轉強背後是否有『實質本業三率擴張、前瞻 EPS 爆發與訂單能見度』支撐，剔除無業績支撐之純題材炒作股。\n"
    "2. 【選股與擇時解耦】：完全豁免今日短線正乖離偏大、現價風報比偏低或大盤紅黃燈，只要公司基本面與未來性頂尖，必須選入 `quality_picks`，交由下一關 AI #3 計算回測均線與大量支撐區之三階低接掛單價！"
)

_QUALITY_LENS_REBOUND = (
    "1. 【22:00 型態 A 反彈視角 — 急跌賣壓歸因（錯殺 vs 基本面崩壞）】：本批候選股剛從高點急跌 10~30% 回踩長均線（45/117/189MA），請優先辨識是否為『大盤系統性修正或外資短線提款造成之流動性錯殺（且投信或千張大戶逆勢鎖碼）』，剔除掉單或毛利崩跌之價值陷阱。\n"
    "2. 【豁免 17MA 下彎與戰略擴產負 FCF】：回踩長均線之反彈股 17MA 必為下彎，且高成長股投入 CapEx 擴產（`STRATEGIC_CAPEX_EXPANSION`）屬良性資本支出，只要公司基本面與未來性頂尖，必須選入 `quality_picks`，交由下一關 AI #3 規劃長均線右腳與破線洗盤三階低接價！"
)


def parse_quality_selection_json(raw: str, valid_ids: set[str], max_top: int) -> dict | None:
    """解析 AI #2 (`quality_selection`) 回傳之結構化 JSON。"""
    text = _extract_json_text((raw or "").strip())
    data: dict | None = None
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            data = loaded
    except json.JSONDecodeError:
        repaired = re.sub(r",(\s*[\]}])", r"\1", text)
        try:
            loaded = json.loads(repaired)
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = None
    if data is None:
        return None

    seen: set[str] = set()
    qp_out: list[dict] = []
    raw_qp = data.get("quality_picks") if isinstance(data.get("quality_picks"), list) else data.get("top_picks", [])
    if isinstance(raw_qp, list):
        for item in raw_qp:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("stock_id", "")).strip()
            if not sid or sid not in valid_ids or sid in seen:
                continue
            seen.add(sid)
            qp_out.append(
                {
                    "rank": len(qp_out) + 1,
                    "stock_id": sid,
                    "stock_name": str(item.get("stock_name", "")).strip(),
                    "sector": str(item.get("sector", "")).strip(),
                    "quality_score": _parse_optional_num(item.get("quality_score") or item.get("score")) or 80,
                    "quality_grade": str(item.get("quality_grade") or "S_TIER_CORE").strip(),
                    "outlook_verdict": str(item.get("outlook_verdict") or "高度可信").strip(),
                    "fundamental_moat_and_catalysts": str(
                        item.get("fundamental_moat_and_catalysts") or item.get("bull_thesis") or ""
                    ).strip(),
                    "chip_and_institutional_backing": str(
                        item.get("chip_and_institutional_backing") or ""
                    ).strip(),
                    "fundamental_invalidation": str(
                        item.get("fundamental_invalidation") or item.get("bear_case_and_invalidation") or ""
                    ).strip(),
                }
            )
            if len(qp_out) >= max_top:
                break

    def _clean_sub(items_obj: object) -> list[dict]:
        out_list: list[dict] = []
        if not isinstance(items_obj, list):
            return out_list
        for it in items_obj:
            if not isinstance(it, dict):
                continue
            sid = str(it.get("stock_id", "")).strip()
            if not sid or sid not in valid_ids or sid in seen:
                continue
            seen.add(sid)
            out_list.append(
                {
                    "stock_id": sid,
                    "stock_name": str(it.get("stock_name", "")).strip(),
                    "reason": str(it.get("reason", "")).strip(),
                }
            )
        return out_list

    return {
        "selection_summary": str(data.get("selection_summary") or data.get("market_comment") or "").strip(),
        "quality_picks": qp_out,
        "watchlist": _clean_sub(data.get("watchlist")),
        "avoid": _clean_sub(data.get("avoid")),
    }


async def run_quality_selection(
    dossiers: list[str],
    *,
    max_top: int,
    model: str | None = None,
    selection_mode: str = "trend",
    use_vertex_batch: bool = True,
    poll_interval: float = DEFAULT_BATCH_POLL_SECONDS,
    timeout_seconds: float | None = None,
    task_type: str = "AI_PICK_QUALITY_SELECT",
) -> object:
    """SASD v6.1 AI #2：跨股績優股選拔委員會（選股與擇時解耦，挑出 Top 3~5 真績優股）。"""
    lens = _QUALITY_LENS_REBOUND if (selection_mode or "").strip().lower() == "rebound" else _QUALITY_LENS_TREND
    prompt = (
        PROMPT_TEMPLATES["quality_selection"]
        .replace("{max_top}", str(max_top))
        .replace("{MODE_SPECIFIC_LENS}", lens)
        + "\n\n==================================================\n"
        + f"【今日候選基本面與籌碼資料卡（共 {len(dossiers)} 檔）】\n\n"
        + "\n\n".join(dossiers)
    )
    resolved_model = model or (get_vertex_batch_stage2_model() if use_vertex_batch else get_vertex_stage2_model())
    if use_vertex_batch:
        batch_res = await run_vertex_batch_requests(
            {"QUALITY_SELECT_STAGE2": prompt},
            model=resolved_model,
            job_prefix="stage2_quality_select",
            poll_interval_seconds=poll_interval,
            tag="quality_select_batch",
            task_type=task_type,
            stock_ids={"QUALITY_SELECT_STAGE2": "PORTFOLIO"},
            timeout_seconds=timeout_seconds,
        )
        meta = batch_res.get("QUALITY_SELECT_STAGE2")
        if meta is None:
            raise RuntimeError("Vertex Batch 未回傳 AI #2 績優股選拔結果")
        return meta
    return await ask_vertex_ai_with_meta(
        prompt,
        model=resolved_model,
        tag="quality_select",
        task_type=task_type,
        stock_id="PORTFOLIO",
        custom_id=f"{task_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        blocking_bq_log=True,
    )


def parse_strategic_layout_json(raw: str, valid_ids: set[str], max_top: int) -> dict | None:
    """解析 AI #3 (`strategic_layout_trend` / `strategic_layout_rebound`) 回傳之結構化 JSON。"""
    text = _extract_json_text((raw or "").strip())
    data: dict | None = None
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            data = loaded
    except json.JSONDecodeError:
        repaired = re.sub(r",(\s*[\]}])", r"\1", text)
        try:
            loaded = json.loads(repaired)
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = None
    if data is None:
        return None

    raw_picks = data.get("strategic_picks") if isinstance(data.get("strategic_picks"), list) else data.get("top_picks", [])
    seen: set[str] = set()
    picks_out: list[dict] = []
    if isinstance(raw_picks, list):
        for item in raw_picks:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("stock_id", "")).strip()
            if not sid or sid not in valid_ids or sid in seen:
                continue
            seen.add(sid)
            picks_out.append(
                {
                    "rank": len(picks_out) + 1,
                    "stock_id": sid,
                    "stock_name": str(item.get("stock_name", "")).strip(),
                    "sector": str(item.get("sector", "")).strip(),
                    "score": _parse_optional_num(item.get("score")) or 85,
                    "allocation_weight_pct": _parse_optional_num(item.get("allocation_weight_pct")) or round(100.0 / max(1, len(valid_ids)), 0),
                    "tactical_type": str(item.get("tactical_type") or "🎯 績優股三階回檔低接").strip(),
                    "current_tech_and_chip_diagnosis": str(
                        item.get("current_tech_and_chip_diagnosis") or ""
                    ).strip(),
                    "tier1_entry": str(item.get("tier1_entry") or "").strip(),
                    "tier2_entry": str(item.get("tier2_entry") or "").strip(),
                    "tier3_entry": str(item.get("tier3_entry") or "").strip(),
                    "weighted_avg_entry_price": str(item.get("weighted_avg_entry_price") or "").strip(),
                    "chase_limit_price": str(item.get("chase_limit_price") or "").strip(),
                    "target_price_1": str(item.get("target_price_1") or "").strip(),
                    "target_price_2": str(item.get("target_price_2") or "").strip(),
                    "stop_loss_price": str(item.get("stop_loss_price") or "").strip(),
                    "invalidation_rule": str(item.get("invalidation_rule") or "").strip(),
                }
            )
            if len(picks_out) >= max_top:
                break

    exp_pct = _parse_optional_num(data.get("recommended_total_exposure_pct"))
    return {
        "market_light": str(data.get("market_light", "")).strip(),
        "recommended_total_exposure_pct": int(exp_pct) if exp_pct is not None else None,
        "market_comment": str(data.get("market_comment", "")).strip(),
        "strategic_picks": picks_out,
    }


async def run_strategic_entry_planner(
    strategic_blocks: list[str],
    *,
    model: str | None = None,
    market_regime: Mapping[str, object] | None = None,
    macro_block: str = "",
    market_daily_block: str = "",
    selection_mode: str = "trend",
    use_vertex_batch: bool = True,
    poll_interval: float = DEFAULT_BATCH_POLL_SECONDS,
    timeout_seconds: float | None = None,
    task_type: str = "AI_PICK_STRATEGIC_LAYOUT",
) -> object:
    """SASD v6.1 AI #3：專職針對入選之 Top 3~5 績優股，結合 20日逐日價量均線指標籌碼表、大量支撐定位器、台股與美股多指數，規劃三階回檔低接點位與雙層資金配置。"""
    template_key = (
        "strategic_layout_rebound"
        if (selection_mode or "").strip().lower() == "rebound"
        else "strategic_layout_trend"
    )
    market_light_str = (
        str(market_regime.get("summary") or market_regime.get("light") or "🟡黃燈")
        if market_regime
        else "🟡黃燈"
    )
    prompt = (
        PROMPT_TEMPLATES[template_key]
        + "\n\n==================================================\n"
        + "【一、隔夜美股與國際總經環境（費半 SOX / 那指 IXIC / 標普 GSPC / 台積電 ADR / NVDA / VIX / 台幣匯率）】\n"
        + (macro_block.strip() or "（無國際盤快取資料）")
        + "\n\n==================================================\n"
        + "【二、台股大盤雙指數氣候與近 10 交易日價量、均線及大盤整體融資趨勢】\n"
        + f"- 大盤即時狀態：{market_light_str}\n"
        + (market_daily_block.strip() or "")
        + "\n\n==================================================\n"
        + f"【三、入選 Top 績優股（共 {len(strategic_blocks)} 檔）— 20日逐日價量均線指標籌碼合一表 ＋ 三大大量支撐定位器】\n\n"
        + "\n\n---\n\n".join(strategic_blocks)
    )
    resolved_model = model or (get_vertex_batch_stage2_model() if use_vertex_batch else get_vertex_stage2_model())
    if use_vertex_batch:
        batch_res = await run_vertex_batch_requests(
            {"STRATEGIC_LAYOUT_STAGE3": prompt},
            model=resolved_model,
            job_prefix="stage3_strategic_layout",
            poll_interval_seconds=poll_interval,
            tag="strategic_layout_batch",
            task_type=task_type,
            stock_ids={"STRATEGIC_LAYOUT_STAGE3": "PORTFOLIO"},
            timeout_seconds=timeout_seconds,
        )
        meta = batch_res.get("STRATEGIC_LAYOUT_STAGE3")
        if meta is None:
            raise RuntimeError("Vertex Batch 未回傳 AI #3 戰略進場布局結果")
        return meta
    return await ask_vertex_ai_with_meta(
        prompt,
        model=resolved_model,
        tag="strategic_layout",
        task_type=task_type,
        stock_id="PORTFOLIO",
        custom_id=f"{task_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        blocking_bq_log=True,
    )


async def execute_v61_quality_and_strategic_pipeline(
    analyzed: pd.DataFrame,
    config: Mapping[str, object],
    *,
    target_date: date,
    daily_dir: Path,
    cache: FundamentalCacheManager,
    analyzer: FundamentalAnalyzer,
    stage1_results: Mapping[str, Mapping[str, object]],
    market_regime: Mapping[str, object] | None,
    max_top: int,
    model: str | None,
    use_vertex_batch: bool,
    poll_interval: float,
    timeout_seconds: float | None = None,
    selection_mode: str = "trend",
    dry_run: bool = False,
) -> tuple[dict | None, dict[str, dict[str, object]], str, str]:
    """SASD v6.1 雙階段跨股決策管線：
    1. AI #2 (`run_quality_selection`)：從 Stage 1 合格候選股中選出 Top 3~5 檔真績優股（選股與擇時解耦）。
    2. Python 戰略武器庫組裝：為入選之 Top 3~5 檔提取 20日逐日價量均線指標籌碼合一表、三大大量支撐定位器、台股大盤10日趨勢與美股七大指標。
    3. AI #3 (`run_strategic_entry_planner`)：規劃雙層資金配置（總倉位% + 各股權重%）與三階金字塔回檔低接掛單（30%/40%/30%）。
    """
    from finmind_stock_scanner import (
        align_news_with_price_action,
        build_market_daily_trend_block,
        build_strategic_tech_and_chip_package,
        compensate_ex_dividend_gaps,
        fetch_news_excerpt,
    )
    from taishin.macro_context import fetch_global_macro_context
    from taxonomy_service import build_sector_metrics_from_prices
    from taishin_portfolio_service import evaluate_candidate_exposure, load_portfolio_exposure_inputs

    is_rebound = (selection_mode or "").strip().lower() == "rebound"
    q_task_type = "REBOUND_QUALITY_SELECT" if is_rebound else "AI_PICK_QUALITY_SELECT"
    s_task_type = "REBOUND_STRATEGIC_LAYOUT" if is_rebound else "AI_PICK_STRATEGIC_LAYOUT"

    prices = _load_prices_for_audit(daily_dir, target_date)
    holdings, holding_prices = await asyncio.to_thread(
        load_portfolio_exposure_inputs, daily_dir, _taxonomy_sector_resolver(config)
    )
    close_map: dict[str, float] = {}
    if prices is not None and not prices.empty:
        u_metrics = build_sector_metrics_from_prices(prices)
        close_map = {sid: float(m["close"]) for sid, m in u_metrics.items() if float(m.get("close", 0)) > 0}

    sector_pe_lookup: dict[str, float] = {}
    if hasattr(analyzer, "compute_cross_sectional_pe_map") and close_map:
        try:
            sector_pe_lookup = await asyncio.to_thread(analyzer.compute_cross_sectional_pe_map, close_map)
        except Exception:  # noqa: BLE001
            sector_pe_lookup = {}

    # Step 1: 組裝 AI #2 績優股選拔資料卡（去重、附加近期新聞反應）
    quality_dossiers: list[str] = []
    for idx, row in enumerate(analyzed.to_dict("records"), start=1):
        sid = str(row["stock_id"])
        raw_stock_prices = (
            prices.loc[prices["stock_id"] == sid] if prices is not None and not prices.empty else pd.DataFrame()
        )
        news_list: list[dict[str, object]] = []
        if not raw_stock_prices.empty:
            try:
                from fundamental_analyzer import filter_and_clean_rss

                raw_news = await asyncio.to_thread(analyzer._fetch_yahoo_stock_rss, sid)  # noqa: SLF001
                cleaned_news = filter_and_clean_rss(raw_news, is_guidance=False, max_age_days=14, sort_by_date=True)
                recent_news = _filter_recent_rss_news(
                    cleaned_news, raw_stock_prices, trading_days=5, target_date=target_date
                )
                news_list = await asyncio.to_thread(
                    align_news_with_price_action,
                    recent_news[:AUDIT_NEWS_MAX_ITEMS],
                    raw_stock_prices,
                    excerpt_fetcher=lambda url: fetch_news_excerpt(url, timeout=3.0),
                )
            except Exception:  # noqa: BLE001
                news_list = []

        snap_txt = str(stage1_results.get(sid, {}).get("snapshot") or "").strip()
        sum_txt = str(stage1_results.get(sid, {}).get("summary") or "").strip()
        dossier = build_dossier(idx, row, snap_txt, sum_txt)
        if news_list:
            n_lines = [
                f"  - [{n.get('pub_date', '')[:16]}] {n.get('title', '')} ➔ {n.get('reaction_tag', '')}"
                for n in news_list
            ]
            dossier += "\n- [D. 近期重大新聞與市場量價真實反應]\n" + "\n".join(n_lines)
        quality_dossiers.append(dossier)

    valid_ids = set(analyzed["stock_id"].astype(str))
    q_meta = await run_quality_selection(
        quality_dossiers,
        max_top=max_top,
        model=model,
        selection_mode=selection_mode,
        use_vertex_batch=use_vertex_batch,
        poll_interval=poll_interval,
        timeout_seconds=timeout_seconds,
        task_type=q_task_type,
    )
    q_raw = str(getattr(q_meta, "text", "") or "")
    q_model_ver = str(getattr(q_meta, "model_version", "") or "")
    q_parsed = parse_quality_selection_json(q_raw, valid_ids, max_top)
    if q_parsed is None:
        return None, {}, q_raw, q_model_ver

    # 同產業最多 2 檔控管
    temp_dec = {
        "top_picks": q_parsed["quality_picks"],
        "watchlist": q_parsed["watchlist"],
        "avoid": q_parsed["avoid"],
    }
    temp_dec = enforce_sector_cap(temp_dec, analyzed)
    quality_picks = temp_dec["top_picks"]
    watchlist_items = temp_dec["watchlist"]
    avoid_items = temp_dec["avoid"]

    if not quality_picks:
        empty_decision = {
            "market_light": str((market_regime or {}).get("light", "")),
            "market_comment": q_parsed.get("selection_summary") or (NO_PICK_MESSAGE + "（AI #2 績優股選拔無合格標的）。"),
            "top_picks": [],
            "watchlist": watchlist_items,
            "avoid": avoid_items,
        }
        if bool(_cfg(config, "enable_signal_audit", False)) and not dry_run:
            _record_v62_decision_signals(
                config=config,
                output_root=daily_dir.parent,
                target_date=target_date,
                strategy_id="rebound_2200" if is_rebound else "alignment_2130",
                market_regime=market_regime,
                top_picks=[],
                watchlist=watchlist_items,
                avoid=avoid_items,
                row_map=_row_lookup(analyzed),
                close_map=close_map,
                tech_packages={},
                audit_records={},
            )
        return empty_decision, {}, q_raw, q_model_ver

    # Step 2: 為入選之 Top 3~5 績優股組裝「20日逐日價量均線指標籌碼大表 ＋ 三大大量支撐定位器 ＋ 台美股總經」
    tw_latest_date_str = ""
    if prices is not None and not prices.empty and "date" in prices.columns:
        try:
            tw_latest_date_str = str(pd.to_datetime(prices["date"], errors="coerce").dropna().max().strftime("%Y-%m-%d"))
        except Exception:  # noqa: BLE001
            tw_latest_date_str = ""
    try:
        macro_ctx = await asyncio.to_thread(
            fetch_global_macro_context,
            context_mode="post_market",
            tw_latest_date=tw_latest_date_str or None,
        )
        macro_block = str(macro_ctx.get("prompt_block") or "")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 抓取隔夜美股總經環境失敗（略過）：{exc}")
        macro_block = ""

    benchmark_id = str(_cfg(config, "market_benchmark_id", "0050"))
    market_daily_block = build_market_daily_trend_block(
        prices,
        benchmark_id=benchmark_id,
        margin_history_path=daily_dir.parent / ".state" / "market_margin_history.json",
        days=10,
    )
    bench_prices_df: pd.DataFrame | None = None
    if prices is not None and not prices.empty and "stock_id" in prices.columns:
        for b_cand in ("TAIEX", benchmark_id, "0050"):
            b_sub = prices.loc[prices["stock_id"].astype(str) == str(b_cand)].copy()
            if not b_sub.empty:
                bench_prices_df = b_sub
                break

    from finmind_stock_scanner import fetch_or_cache_tdcc_holding

    tdcc_cache_path = daily_dir.parent / ".state" / "tdcc_holding_cache.json"
    finmind_loader = getattr(cache, "_finmind", None)

    row_map = _row_lookup(analyzed)
    strategic_blocks: list[str] = []
    tech_packages: dict[str, dict[str, object]] = {}
    exposures_by_sid: dict[str, object] = {}
    enable_ex_div = bool(_cfg(config, "enable_ex_dividend_compensation", True))

    for q_item in quality_picks:
        sid = str(q_item["stock_id"])
        row = row_map.get(sid, {})
        sname = str(row.get("stock_name") or q_item.get("stock_name") or "")
        sub_ind = str(row.get("sub_industry") or row.get("industry_category") or "")

        raw_stock_prices = (
            prices.loc[prices["stock_id"] == sid].copy()
            if prices is not None and not prices.empty
            else pd.DataFrame()
        )
        div_df: pd.DataFrame | None = None
        if hasattr(analyzer, "cache"):
            try:
                div_df = await asyncio.to_thread(
                    analyzer.cache._read_stock_rows,  # noqa: SLF001
                    analyzer.cache._get_worksheet("06_Dividend_Policy"),  # noqa: SLF001
                    sid,
                )
            except Exception:  # noqa: BLE001
                div_df = None

        prices_for_tech = raw_stock_prices
        if not raw_stock_prices.empty and enable_ex_div and div_df is not None and not div_df.empty:
            try:
                prices_for_tech = compensate_ex_dividend_gaps(raw_stock_prices, div_df)
            except Exception:  # noqa: BLE001
                prices_for_tech = raw_stock_prices

        daily_chip_df = await asyncio.to_thread(cache.get_daily_chip_history, sid, 60)

        feat_obj = None
        if hasattr(analyzer, "compute_fundamental_features"):
            try:
                feat_obj = await asyncio.to_thread(
                    analyzer.compute_fundamental_features,
                    sid,
                    close=_parse_optional_num(row.get("close")),
                    stock_name=sname,
                    sub_industry=sub_ind,
                    sector_pe_lookup=sector_pe_lookup,
                )
            except Exception:  # noqa: BLE001
                feat_obj = None
        fwd_val = getattr(feat_obj, "forward_valuation", {}) if feat_obj is not None else {}
        fwd_eps = _parse_optional_num(fwd_val.get("implied_core_forward_eps")) if isinstance(fwd_val, Mapping) else None
        peer_pe = _parse_optional_num(fwd_val.get("sector_median_pe")) if isinstance(fwd_val, Mapping) else None
        cap_twd: float | None = None
        if feat_obj is not None:
            cs_val = getattr(feat_obj, "capital_stock", None)
            if cs_val is None and isinstance(fwd_val, Mapping):
                cs_val = fwd_val.get("capital_stock")
            cap_num = _parse_optional_num(cs_val)
            if cap_num and cap_num > 0:
                cap_twd = cap_num * 1e6 if cap_num < 1_000_000 else cap_num

        tdcc_df: pd.DataFrame | None = None
        try:
            tdcc_df = await asyncio.to_thread(
                fetch_or_cache_tdcc_holding,
                sid,
                as_of=target_date,
                cache_path=tdcc_cache_path,
                loader=finmind_loader,
            )
        except Exception:  # noqa: BLE001
            tdcc_df = None

        ann_div: float | None = None
        if div_df is not None and not div_df.empty and "cash_dividend" in div_df.columns:
            div_vals = pd.to_numeric(div_df["cash_dividend"], errors="coerce").dropna()
            if not div_vals.empty:
                ann_div = float(div_vals.iloc[-1])

        pkg = build_strategic_tech_and_chip_package(
            sid,
            sname,
            prices_for_tech,
            daily_chip_df=daily_chip_df,
            benchmark_prices=bench_prices_df,
            tdcc_holding_df=tdcc_df,
            paid_in_capital_twd=cap_twd,
            forward_eps=fwd_eps,
            peer_median_pe=peer_pe,
            annual_cash_dividend=ann_div,
            selection_mode=selection_mode,
        )
        tech_packages[sid] = pkg

        exposure = evaluate_candidate_exposure(
            sid,
            sub_ind,
            holdings,
            candidate_prices=raw_stock_prices,
            holding_prices=holding_prices,
        )
        exposures_by_sid[sid] = exposure
        exp_status = _exposure_field(exposure, "exposure_status", "OPEN")

        block_lines = [
            f"### 🏆 【入選績優股 #{q_item['rank']}】{sid} {sname}（{sub_ind}｜AI#2 體質評級：{q_item['quality_grade']}｜體質分：{q_item['quality_score']}｜法說展望：{q_item['outlook_verdict']}）",
            f"- **AI #2 績優選拔核心理由**：{q_item['fundamental_moat_and_catalysts']}",
            f"- **AI #2 法人與大戶底氣**：{q_item['chip_and_institutional_backing']}",
            f"- **台新帳戶庫存共伴曝險**：{exp_status}（同產業持股：{list(_exposure_field(exposure, 'same_sector_holdings', []) or [])}｜最高相關持股：{_exposure_field(exposure, 'max_corr_holding_id')} 相關係數 {_exposure_field(exposure, 'max_corr_value', 0.0)}）",
            str(pkg.get("prompt_block") or ""),
        ]
        strategic_blocks.append("\n".join(block_lines))

    # Step 3: 呼叫 AI #3 (`run_strategic_entry_planner`) 產出雙層資金權重與三階回檔低接點位
    s_meta = await run_strategic_entry_planner(
        strategic_blocks,
        model=model,
        market_regime=market_regime,
        macro_block=macro_block,
        market_daily_block=market_daily_block,
        selection_mode=selection_mode,
        use_vertex_batch=use_vertex_batch,
        poll_interval=poll_interval,
        timeout_seconds=timeout_seconds,
        task_type=s_task_type,
    )
    s_raw = str(getattr(s_meta, "text", "") or "")
    s_model_ver = str(getattr(s_meta, "model_version", "") or q_model_ver)
    qp_ids = {str(it["stock_id"]) for it in quality_picks}
    s_parsed = parse_strategic_layout_json(s_raw, qp_ids, max_top)

    q_by_sid = {str(it["stock_id"]): it for it in quality_picks}
    s_by_sid = (
        {str(it["stock_id"]): it for it in s_parsed.get("strategic_picks", [])}
        if isinstance(s_parsed, dict)
        else {}
    )

    top_picks_final: list[dict] = []
    audit_records: dict[str, dict[str, object]] = {}
    default_w = round(100.0 / max(1, len(quality_picks)), 0)

    for rank_idx, q_item in enumerate(quality_picks, start=1):
        sid = str(q_item["stock_id"])
        s_item = s_by_sid.get(sid) or {}
        pkg = tech_packages.get(sid) or {}
        exposure = exposures_by_sid.get(sid)
        swap = _exposure_field(exposure, "exposure_status") == "SECTOR_OR_CORR_FULL_SWAP_ONLY"

        w_pct = s_item.get("allocation_weight_pct")
        if w_pct in (None, ""):
            w_pct = default_w
        t1_plan = str(s_item.get("tier1_entry") or f"【第1批 30% 試單】{pkg.get('tier1_default', '')}").strip()
        t2_plan = str(s_item.get("tier2_entry") or f"【第2批 40% 核心】{pkg.get('tier2_default', '')}").strip()
        t3_plan = str(s_item.get("tier3_entry") or f"【第3批 30% 黃金坑】{pkg.get('tier3_default', '')}").strip()
        avg_entry = str(s_item.get("weighted_avg_entry_price") or pkg.get("close_t") or "").strip()
        chase_lim = s_item.get("chase_limit_price") or pkg.get("chase_limit_price") or ""
        tp1_str = str(s_item.get("target_price_1") or f"{pkg.get('tp1_price', '')}（Python 確定性目標）").strip()
        tp2_str = str(s_item.get("target_price_2") or "").strip()
        sl_str = str(s_item.get("stop_loss_price") or f"{pkg.get('hard_stop_loss', '')}（Python 結構硬停損）").strip()
        tech_diag = str(s_item.get("current_tech_and_chip_diagnosis") or "").strip()
        inv_rule = str(s_item.get("invalidation_rule") or q_item.get("fundamental_invalidation") or "").strip()

        ideal_summary = f"預估均價 {avg_entry}（權重 {w_pct}%）" if avg_entry else f"權重 {w_pct}% 三階低接"
        tactic_code = "PORTFOLIO_SWAP_ONLY" if swap else "STRATEGIC_3TIER_PULLBACK"

        from signal_audit_service import parse_plan_price

        close_ref = _parse_optional_num(pkg.get("close_t")) or _parse_optional_num(row_map.get(sid, {}).get("close"))
        t1_fallback = _parse_optional_num(pkg.get("tier1_price")) or parse_plan_price(
            pkg.get("tier1_default"), close_ref=close_ref
        )
        t2_fallback = _parse_optional_num(pkg.get("tier2_price")) or parse_plan_price(
            pkg.get("tier2_default"), close_ref=close_ref
        )
        t3_fallback = _parse_optional_num(pkg.get("tier3_price")) or parse_plan_price(
            pkg.get("tier3_default"), close_ref=close_ref
        )
        t1_num = parse_plan_price(t1_plan, close_ref=close_ref, fallback=t1_fallback)
        t2_num = parse_plan_price(t2_plan, close_ref=close_ref, fallback=t2_fallback)
        t3_num = parse_plan_price(t3_plan, close_ref=close_ref, fallback=t3_fallback)
        # 最後一道數值單調性防呆：確保三階掛單嚴格遞減 (t1 > t2 > t3) 且不倒掛
        valid_tiers = [v for v in (t1_num, t2_num, t3_num) if v is not None and v > 0]
        if len(valid_tiers) == 3 and not (t1_num > t2_num > t3_num):  # type: ignore[operator]
            desc_tiers = sorted(valid_tiers, reverse=True)
            t1_num, t2_num, t3_num = desc_tiers[0], desc_tiers[1], desc_tiers[2]
            if t2_num >= t1_num and t2_fallback and t2_fallback < t1_num:
                t2_num = t2_fallback
            if t3_num >= t2_num and t3_fallback and t3_fallback < t2_num:
                t3_num = t3_fallback
        tp1_num = parse_plan_price(tp1_str, close_ref=close_ref, fallback=_parse_optional_num(pkg.get("tp1_price")))
        tp2_num = parse_plan_price(tp2_str, close_ref=close_ref, fallback=None)
        if tp1_num is not None and tp2_num is not None and tp2_num <= tp1_num:
            tp2_num = None
        sl_num = parse_plan_price(
            sl_str, close_ref=close_ref, fallback=_parse_optional_num(pkg.get("hard_stop_loss")), pick="low"
        )
        chase_num = parse_plan_price(
            chase_lim, close_ref=close_ref, fallback=_parse_optional_num(pkg.get("chase_limit_price"))
        )
        if t1_num and t2_num and t3_num:
            entry_for_rr = round(0.3 * t1_num + 0.4 * t2_num + 0.3 * t3_num, 2)
        else:
            entry_for_rr = t1_num or close_ref or 0.0
        rr_val: float | str = ""
        if tp1_num and sl_num and entry_for_rr > sl_num:
            rr_val = round((tp1_num - entry_for_rr) / (entry_for_rr - sl_num), 2)

        ai_action = str(s_item.get("action") or "").strip().upper()
        is_wait_observe = (
            ai_action == "WAIT_OBSERVE"
            or (isinstance(w_pct, (int, float)) and float(w_pct) <= 0.0)
            or (isinstance(rr_val, float) and rr_val < 1.5)
        )
        if is_wait_observe:
            w_pct = 0
            if not swap:
                tactic_code = "WAIT_OBSERVE"
            ideal_summary = f"🛡️ 保留現金觀望（權重 0%｜R/R {rr_val if rr_val != '' else 'N/A'}）"

        audit_rec = {
            "Audit_Grade": str(q_item.get("quality_grade") or "S_TIER_CORE"),
            "Execution_Tactic": tactic_code,
            "Allocation_Weight_Pct": w_pct,
            "Tier1_Entry_Plan": t1_plan,
            "Tier2_Entry_Plan": t2_plan,
            "Tier3_Entry_Plan": t3_plan,
            "Tier1_Entry_Price": t1_num,
            "Tier2_Entry_Price": t2_num,
            "Tier3_Entry_Price": t3_num,
            "Ideal_Buy_Or_Ref": ideal_summary,
            "Chase_Limit_Price": chase_num if chase_num is not None else chase_lim,
            "Hard_Stop_Loss": sl_num if sl_num is not None else (pkg.get("hard_stop_loss") or sl_str),
            "TP1_Target_Or_POC": tp1_num if tp1_num is not None else (pkg.get("tp1_price") or tp1_str),
            "TP2_Target_Price": tp2_num,
            "Deterministic_RR": rr_val,
            "Bear_Case_Warning": tech_diag,
            "Invalidation_Triggers": [inv_rule] if inv_rule else [],
            "Exposure_Status": _exposure_field(exposure, "exposure_status", "OPEN"),
            "Swap_Banner": format_swap_banner(exposure) if swap else "",
        }
        audit_records[sid] = audit_rec

        bull_combined = q_item.get("fundamental_moat_and_catalysts", "")
        if q_item.get("chip_and_institutional_backing"):
            bull_combined += f"｜籌碼底氣：{q_item['chip_and_institutional_backing']}"
        if tech_diag:
            bull_combined += f"｜技術與支撐定位：{tech_diag}"

        entry_combined = f"資金權重 {w_pct}%｜預估加權成本 {avg_entry}｜嚴禁追高上限 {chase_lim}"
        top_picks_final.append(
            {
                "rank": rank_idx,
                "stock_id": sid,
                "stock_name": str(q_item.get("stock_name") or ""),
                "sector": str(q_item.get("sector") or ""),
                "score": int(_parse_optional_num(s_item.get("score") or q_item.get("quality_score")) or 85),
                "tactical_type": str(s_item.get("tactical_type") or "🎯 績優股三階回檔低接"),
                "entry_zone_and_trigger": entry_combined,
                "target_price_1": tp1_str,
                "target_price_2": tp2_str,
                "stop_loss_price": sl_str,
                "outlook_verdict": str(q_item.get("outlook_verdict") or "高度可信"),
                "bull_thesis": bull_combined,
                "bear_case_and_invalidation": inv_rule,
                "audit": audit_rec,
            }
        )

    rec_exp_pct = s_parsed.get("recommended_total_exposure_pct") if isinstance(s_parsed, dict) else None
    m_comment = (
        str(s_parsed.get("market_comment") or "")
        if isinstance(s_parsed, dict)
        else ""
    ) or str(q_parsed.get("selection_summary") or "")
    if rec_exp_pct is not None and f"{rec_exp_pct}%" not in m_comment:
        m_comment = f"【建議總持倉水位：{rec_exp_pct}%】{m_comment}"

    final_decision = {
        "market_light": (
            str(s_parsed.get("market_light") or "") if isinstance(s_parsed, dict) else ""
        )
        or str((market_regime or {}).get("light", "🟡黃燈")),
        "recommended_total_exposure_pct": rec_exp_pct,
        "market_comment": m_comment,
        "top_picks": top_picks_final,
        "watchlist": watchlist_items,
        "avoid": avoid_items,
    }

    # 訊號留痕寫入 signal_audit.db（SASD v6.2.0：修復鍵名並同步寫入 Top Picks / Watchlist / Avoid）
    if bool(_cfg(config, "enable_signal_audit", False)) and not dry_run:
        _record_v62_decision_signals(
            config=config,
            output_root=daily_dir.parent,
            target_date=target_date,
            strategy_id="rebound_2200" if is_rebound else "alignment_2130",
            market_regime=market_regime,
            top_picks=top_picks_final,
            watchlist=watchlist_items,
            avoid=avoid_items,
            row_map=row_map,
            close_map=close_map,
            tech_packages=tech_packages,
            audit_records=audit_records,
        )

    return final_decision, audit_records, s_raw or q_raw, s_model_ver


def _resolve_regime_state_code(market_regime: Mapping[str, object] | None) -> str:
    if isinstance(market_regime, Mapping):
        rq = market_regime.get("regime_quota")
        if isinstance(rq, Mapping):
            code = str(rq.get("regime_state") or rq.get("regime") or "").strip()
            if code:
                return code
        light_txt = str(market_regime.get("light") or "")
        if "🟢" in light_txt:
            return "GREEN_BULL"
        if "🔴" in light_txt:
            return "RED_DEFENSIVE"
    return "YELLOW_ROTATION"


def _record_v62_decision_signals(
    *,
    config: Mapping[str, object],
    output_root: Path,
    target_date: date,
    strategy_id: str,
    market_regime: Mapping[str, object] | None,
    top_picks: Sequence[Mapping[str, object]],
    watchlist: Sequence[Mapping[str, object]],
    avoid: Sequence[Mapping[str, object]],
    row_map: Mapping[str, Mapping[str, object]],
    close_map: Mapping[str, float] | None = None,
    tech_packages: Mapping[str, Mapping[str, object]] | None = None,
    audit_records: Mapping[str, Mapping[str, object]] | None = None,
) -> int:
    """將 Top Picks / Watchlist / Avoid 留痕寫入 `signal_audit.db`（SASD v6.2.0）。"""
    try:
        from signal_audit_service import (
            CATEGORY_AVOID,
            CATEGORY_TOP_PICK,
            CATEGORY_WATCHLIST,
            GRADE_REJECT_TRAP,
            SIGNAL_RIGHT_BREAKOUT,
            SIGNAL_STYLE_B_PULLBACK,
            SignalAuditStore,
            parse_plan_price,
        )

        store = SignalAuditStore(_resolve_audit_db_path(config, output_root))
        regime_str = _resolve_regime_state_code(market_regime)
        sig_date_str = target_date.isoformat()
        pkgs = tech_packages or {}
        audits = audit_records or {}
        closes = close_map or {}
        recorded = 0
        seen_sids: set[str] = set()

        def _lookup_close(sid: str) -> float:
            pkg_c = _parse_optional_num((pkgs.get(sid) or {}).get("close_t"))
            if pkg_c and pkg_c > 0:
                return float(pkg_c)
            row_c = _parse_optional_num((row_map.get(sid) or {}).get("close"))
            if row_c and row_c > 0:
                return float(row_c)
            map_c = _parse_optional_num(closes.get(sid))
            return float(map_c) if (map_c and map_c > 0) else 0.0

        def _lookup_name_sector(sid: str, item: Mapping[str, object]) -> tuple[str, str]:
            row = row_map.get(sid) or {}
            sname = str(item.get("stock_name") or row.get("stock_name") or sid).strip()
            sector = str(
                item.get("sector")
                or row.get("sub_industry")
                or row.get("industry_category")
                or ""
            ).strip()
            return sname, sector

        for pick_it in top_picks:
            sid = str(pick_it.get("stock_id") or "").strip()
            if not sid:
                continue
            close_v = _lookup_close(sid)
            if close_v <= 0:
                continue
            pkg = pkgs.get(sid) or {}
            aud = audits.get(sid) or (pick_it.get("audit") if isinstance(pick_it.get("audit"), Mapping) else {}) or {}
            sname, sector = _lookup_name_sector(sid, pick_it)

            t1_p = _parse_optional_num(aud.get("Tier1_Entry_Price")) or parse_plan_price(
                aud.get("Tier1_Entry_Plan"),
                close_ref=close_v,
                fallback=_parse_optional_num(pkg.get("tier1_price"))
                or parse_plan_price(pkg.get("tier1_default"), close_ref=close_v)
                or round(close_v * 0.985, 2),
            )
            t2_p = _parse_optional_num(aud.get("Tier2_Entry_Price")) or parse_plan_price(
                aud.get("Tier2_Entry_Plan"),
                close_ref=close_v,
                fallback=_parse_optional_num(pkg.get("tier2_price"))
                or parse_plan_price(pkg.get("tier2_default"), close_ref=close_v),
            )
            t3_p = _parse_optional_num(aud.get("Tier3_Entry_Price")) or parse_plan_price(
                aud.get("Tier3_Entry_Plan"),
                close_ref=close_v,
                fallback=_parse_optional_num(pkg.get("tier3_price"))
                or parse_plan_price(pkg.get("tier3_default"), close_ref=close_v),
            )
            valid_tiers = [p for p in (t1_p, t2_p, t3_p) if p is not None and p > 0]
            zone_low = min(valid_tiers) if valid_tiers else None
            zone_high = max(valid_tiers) if valid_tiers else None

            chase_p = parse_plan_price(
                aud.get("Chase_Limit_Price"),
                close_ref=close_v,
                fallback=_parse_optional_num(pkg.get("chase_limit_price")) or close_v,
            ) or close_v
            sl_p = parse_plan_price(
                aud.get("Hard_Stop_Loss") or pick_it.get("stop_loss_price"),
                close_ref=close_v,
                fallback=_parse_optional_num(pkg.get("hard_stop_loss")) or round(close_v * 0.92, 2),
                pick="low",
            ) or round(close_v * 0.92, 2)
            tp1_p = parse_plan_price(
                aud.get("TP1_Target_Or_POC") or pick_it.get("target_price_1"),
                close_ref=close_v,
                fallback=_parse_optional_num(pkg.get("tp1_price")) or round(close_v * 1.10, 2),
            ) or round(close_v * 1.10, 2)
            tp2_p = _parse_optional_num(aud.get("TP2_Target_Price")) or parse_plan_price(
                pick_it.get("target_price_2"), close_ref=close_v, fallback=None
            )
            if tp2_p is not None and tp2_p <= tp1_p:
                tp2_p = None

            entry_ref = t1_p or close_v
            rr_num = _parse_optional_num(aud.get("Deterministic_RR"))
            if rr_num is None and entry_ref > sl_p:
                rr_num = round((tp1_p - entry_ref) / (entry_ref - sl_p), 2)

            store.record_signal(
                {
                    "signal_date": sig_date_str,
                    "strategy_id": strategy_id,
                    "pick_category": CATEGORY_TOP_PICK,
                    "stock_id": sid,
                    "stock_name": sname,
                    "sector_name": sector,
                    "regime_state": regime_str,
                    "scanner_signal_type": SIGNAL_STYLE_B_PULLBACK,
                    "ai_verdict_grade": str(aud.get("Audit_Grade") or "S_TIER_CORE"),
                    "execution_strategy_type": str(aud.get("Execution_Tactic") or "STRATEGIC_3TIER_PULLBACK"),
                    "close_t": close_v,
                    "tier1_entry_price": t1_p,
                    "tier2_entry_price": t2_p,
                    "tier3_entry_price": t3_p,
                    "ideal_buy_zone_low": zone_low,
                    "ideal_buy_zone_high": zone_high,
                    "chase_limit_price": chase_p,
                    "hard_stop_loss": sl_p,
                    "tp1_price": tp1_p,
                    "tp2_price": tp2_p,
                    "deterministic_rr_ratio": rr_num or 0.0,
                }
            )
            seen_sids.add(sid)
            recorded += 1

        for w_it in watchlist:
            sid = str(w_it.get("stock_id") or "").strip()
            if not sid or sid in seen_sids:
                continue
            close_v = _lookup_close(sid)
            if close_v <= 0:
                continue
            sname, sector = _lookup_name_sector(sid, w_it)
            row = row_map.get(sid) or {}
            ma17_v = _parse_optional_num(row.get("MA17"))
            buy_hi = round(ma17_v, 2) if (ma17_v and 0 < ma17_v < close_v) else round(close_v * 0.97, 2)
            store.record_signal(
                {
                    "signal_date": sig_date_str,
                    "strategy_id": strategy_id,
                    "pick_category": CATEGORY_WATCHLIST,
                    "stock_id": sid,
                    "stock_name": sname,
                    "sector_name": sector,
                    "regime_state": regime_str,
                    "scanner_signal_type": SIGNAL_STYLE_B_PULLBACK,
                    "ai_verdict_grade": "WATCHLIST",
                    "execution_strategy_type": "WATCHLIST_WAIT",
                    "close_t": close_v,
                    "ideal_buy_zone_low": round(buy_hi * 0.98, 2),
                    "ideal_buy_zone_high": buy_hi,
                    "chase_limit_price": round(close_v, 2),
                    "hard_stop_loss": round(close_v * 0.92, 2),
                    "tp1_price": round(close_v * 1.10, 2),
                    "deterministic_rr_ratio": 0.0,
                }
            )
            seen_sids.add(sid)
            recorded += 1

        for a_it in avoid:
            sid = str(a_it.get("stock_id") or "").strip()
            if not sid or sid in seen_sids:
                continue
            close_v = _lookup_close(sid)
            if close_v <= 0:
                continue
            sname, sector = _lookup_name_sector(sid, a_it)
            store.record_signal(
                {
                    "signal_date": sig_date_str,
                    "strategy_id": strategy_id,
                    "pick_category": CATEGORY_AVOID,
                    "stock_id": sid,
                    "stock_name": sname,
                    "sector_name": sector,
                    "regime_state": regime_str,
                    "scanner_signal_type": SIGNAL_RIGHT_BREAKOUT,
                    "ai_verdict_grade": GRADE_REJECT_TRAP,
                    "execution_strategy_type": "NO_TRADE",
                    "close_t": close_v,
                    "chase_limit_price": round(close_v * 1.03, 2),
                    "hard_stop_loss": round(close_v * 0.92, 2),
                    "tp1_price": round(close_v * 1.08, 2),
                    "deterministic_rr_ratio": 0.0,
                }
            )
            seen_sids.add(sid)
            recorded += 1

        return recorded
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 訊號留痕寫入失敗（略過）：{exc}")
        return 0


def wantgoo_chart_url(stock_id: str) -> str:
    """回傳玩股網個股技術線圖直達連結（上市與上櫃網址一致）。"""
    return f"https://www.wantgoo.com/stock/{str(stock_id).strip()}/technical-chart"


def _extract_trade_levels_for_chart(
    decision: Mapping[str, object] | None,
    audit_records: Mapping[str, Mapping[str, object]] | None,
    stock_id: str,
) -> dict[str, float] | None:
    """提取個股之 TP1 / TP2 / 低接支撐區 / 硬停損數值，供 K 線圖疊加水平標線。"""
    if not decision:
        return None
    top_picks = decision.get("top_picks")
    if not isinstance(top_picks, Sequence):
        return None
    target_pick = next(
        (p for p in top_picks if isinstance(p, Mapping) and str(p.get("stock_id")) == str(stock_id)),
        None,
    )
    if target_pick is None:
        return None

    from signal_audit_service import parse_plan_price

    aud = (audit_records or {}).get(str(stock_id)) or (
        target_pick.get("audit") if isinstance(target_pick.get("audit"), Mapping) else {}
    ) or {}
    levels: dict[str, float] = {}
    tp1 = _parse_optional_num(aud.get("TP1_Target_Or_POC")) or parse_plan_price(target_pick.get("target_price_1"))
    tp2 = _parse_optional_num(aud.get("TP2_Target_Price")) or parse_plan_price(target_pick.get("target_price_2"))
    sl = _parse_optional_num(aud.get("Hard_Stop_Loss")) or parse_plan_price(
        target_pick.get("stop_loss_price"), pick="low"
    )
    tiers = [
        p
        for p in (
            _parse_optional_num(aud.get("Tier1_Entry_Price")) or parse_plan_price(aud.get("Tier1_Entry_Plan")),
            _parse_optional_num(aud.get("Tier2_Entry_Price")) or parse_plan_price(aud.get("Tier2_Entry_Plan")),
            _parse_optional_num(aud.get("Tier3_Entry_Price")) or parse_plan_price(aud.get("Tier3_Entry_Plan")),
        )
        if p is not None and p > 0
    ]
    if tp1 and tp1 > 0:
        levels["tp1"] = float(tp1)
    if tp2 and tp2 > 0 and (not tp1 or tp2 > tp1):
        levels["tp2"] = float(tp2)
    if sl and sl > 0:
        levels["stop_loss"] = float(sl)
    if tiers:
        levels["buy_zone_low"] = float(min(tiers))
        levels["buy_zone_high"] = float(max(tiers))
    return levels or None


# --------------------------------------------------------------------------- #
# Stage 4: report, persistence, Discord
# --------------------------------------------------------------------------- #
def chart_url(stock_id: str) -> str | None:
    owner_repo = (os.getenv("GITHUB_REPO", "") or "").strip().split("/")
    if len(owner_repo) != 2 or not all(owner_repo):
        return None
    owner, repo = owner_repo
    if repo.lower() == f"{owner}.github.io".lower():
        return f"https://{owner}.github.io/charts/{stock_id}.html"
    return f"https://{owner}.github.io/{repo}/charts/{stock_id}.html"


def _links(stock_id: str, war_room_url: str | None) -> str:
    parts = [f"[📈 玩股網技術線圖]({wantgoo_chart_url(stock_id)})"]
    if war_room_url:
        parts.append(f"[📊 全方位戰情室（含財報+K線）]({war_room_url})")
    else:
        chart = chart_url(stock_id)
        if chart:
            parts.append(f"[📈 K 線圖]({chart})")
    return " ｜ ".join(parts)


def _row_lookup(frame: pd.DataFrame) -> dict[str, dict[str, object]]:
    return {str(r["stock_id"]): r for r in frame.to_dict("records")}


def format_compact_pick_notification(
    target_date: date,
    decision: dict | None,
    frame: pd.DataFrame,
    *,
    total_signals: int,
    analyzed_count: int = 0,
    audit_records: Mapping[str, Mapping[str, object]] | None = None,
    market_regime: Mapping[str, object] | None = None,
    title_label: str = "AI 嚴選進場決策快報",
    weekly_summary_md: str = "",
) -> list[str]:
    """精簡單則 Discord 作戰通知卡片（完整 AI 理由、觀察/避開名單與歷史對答案導流至戰情總部）。"""
    from github_uploader import get_github_pages_base_url
    from signal_audit_service import parse_plan_price

    rows = _row_lookup(frame)
    portal_base = get_github_pages_base_url()
    top_picks = list((decision or {}).get("top_picks") or [])
    watchlist = list((decision or {}).get("watchlist") or [])
    avoid = list((decision or {}).get("avoid") or [])
    audits = audit_records or {}

    lines: list[str] = []
    if portal_base:
        lines.append(f"🌐 [開啟今日量化戰情總部]({portal_base}/?date={target_date.isoformat()})")
    lines.append(
        f"🎯 **【{title_label}】{target_date.isoformat()}**"
        f"｜候選 {total_signals} ➔ 深度 {analyzed_count} ➔ 嚴選 **{len(top_picks)} 檔**"
    )

    regime_parts: list[str] = []
    if market_regime and market_regime.get("light"):
        light_s = str(market_regime.get("light")).strip()
        status_s = str(market_regime.get("status") or "").split("（")[0].strip()
        regime_parts.append(f"{light_s} {status_s}".strip())
    rec_exp = (decision or {}).get("recommended_total_exposure_pct")
    if rec_exp is not None and rec_exp != "":
        regime_parts.append(f"建議總持倉 {rec_exp}%")
    if regime_parts:
        lines.append("🚦 " + " ｜ ".join(regime_parts))

    if decision is None:
        lines.append("⚠️ AI 輸出解析異常，請至戰情總部或本地日誌檢視。")
    elif not top_picks:
        lines.append(f"🛡️ **今日防禦空手觀望（0 檔嚴選）**｜觀察 {len(watchlist)} 檔・避開 {len(avoid)} 檔（詳見戰情總部）")
    else:
        for idx, pick in enumerate(top_picks, start=1):
            sid = str(pick.get("stock_id") or "")
            row = rows.get(sid, {})
            sname = str(pick.get("stock_name") or row.get("stock_name") or "").strip()
            rank_emoji = _RANK_EMOJI.get(pick.get("rank", idx), "🏅")
            score = pick.get("score", "N/A")
            aud = audits.get(sid) or (pick.get("audit") if isinstance(pick.get("audit"), Mapping) else {}) or {}
            w_pct = aud.get("Allocation_Weight_Pct")
            w_part = f"｜權重 {w_pct}%" if w_pct not in (None, "") else ""

            t1 = _parse_optional_num(aud.get("Tier1_Entry_Price")) or parse_plan_price(aud.get("Tier1_Entry_Plan"))
            t2 = _parse_optional_num(aud.get("Tier2_Entry_Price")) or parse_plan_price(aud.get("Tier2_Entry_Plan"))
            t3 = _parse_optional_num(aud.get("Tier3_Entry_Price")) or parse_plan_price(aud.get("Tier3_Entry_Plan"))
            tp1 = _parse_optional_num(aud.get("TP1_Target_Or_POC")) or parse_plan_price(pick.get("target_price_1"))
            sl = _parse_optional_num(aud.get("Hard_Stop_Loss")) or parse_plan_price(pick.get("stop_loss_price"), pick="low")

            tiers_txt = " / ".join(f"{p:.1f}" for p in (t1, t2, t3) if p is not None)
            plan_parts: list[str] = []
            if tiers_txt:
                plan_parts.append(f"低接 `{tiers_txt}`")
            if tp1 is not None:
                plan_parts.append(f"TP1 `{tp1:.1f}`")
            if sl is not None:
                plan_parts.append(f"停損 `{sl:.1f}`")
            plan_parts.append(f"[📈玩股網]({wantgoo_chart_url(sid)})")

            lines.append(f"{rank_emoji} **#{pick.get('rank', idx)} `{sid}` {sname}**（分 {score}{w_part}）")
            lines.append("   └ " + "｜".join(plan_parts))

        lines.append(
            f"👀 次佳觀察 {len(watchlist)} 檔 ｜ ⚠️ 建議避開 {len(avoid)} 檔"
            "（完整理由、財報戰情室與歷史對答案請點上方總部）"
        )

    out = ["\n".join(lines)[:DISCORD_CHUNK_LIMIT]]
    if weekly_summary_md:
        out.append(weekly_summary_md[:DISCORD_CHUNK_LIMIT])
    return out


def format_pick_report(
    target_date: date,
    decision: dict | None,
    raw_text: str,
    frame: pd.DataFrame,
    results: Mapping[str, Mapping[str, object]],
    failures: list[tuple[str, str]],
    *,
    total_signals: int,
    skipped: int,
    model: str,
    model_version: str = "",
    market_regime: Mapping[str, object] | None = None,
    title_label: str = "AI 嚴選進場決策報告",
) -> list[str]:
    """Render Discord messages (each <= DISCORD_CHUNK_LIMIT chars)."""
    from github_uploader import get_github_pages_base_url

    rows = _row_lookup(frame)
    top_count = len(decision["top_picks"]) if decision else 0
    portal_base = get_github_pages_base_url()
    portal_line = (
        f"🌐 [開啟今日量化戰情總部]({portal_base}/?date={target_date.isoformat()})\n"
        if portal_base
        else ""
    )
    heading = (
        f"{portal_line}"
        f"🎯 【{title_label}】{target_date.isoformat()}"
        f"\n候選訊號股 {total_signals} 檔 ➔ 深度分析 {len(results)} 檔 ➔ 嚴選 {top_count} 檔"
        f"｜決策模型：Vertex AI ({model})"
        + (f"｜分析模型：{model_version}" if model_version else "")
    )
    if market_regime and market_regime.get("summary"):
        heading += f"\n🚦 大盤紅綠燈：{market_regime['summary']}"
        if "🔴" in str(market_regime.get("light", "")):
            heading += "\n🛡️ **空頭防禦警示**：目前大盤跌破 45MA 且下彎，請縮小部位、嚴格過濾基本面與現金流並收緊停損！"
    blocks: list[str] = []

    def name_of(stock_id: str) -> str:
        row = rows.get(stock_id, {})
        return f"{stock_id} {row.get('stock_name', '')}".strip()

    if decision is None:
        blocks.append("⚠️ AI 輸出無法解析為結構化格式，以下為原始回覆：\n" + raw_text.strip()[:3000])
    else:
        if decision["market_comment"]:
            blocks.append(f"📝 {decision['market_comment']}")
        if not decision["top_picks"]:
            blocks.append(f"🏆 {NO_PICK_MESSAGE}（寧缺勿濫）。")
        for pick in decision["top_picks"]:
            stock_id = pick["stock_id"]
            row = rows.get(stock_id, {})
            report = results.get(stock_id, {}).get("report")
            verdict = str(pick.get("outlook_verdict", "無資料"))
            emoji = _VERDICT_EMOJI.get(verdict, "⚪")
            vol_str = _fmt_volume_line(row)
            vol_line = [f"量能：{vol_str}"] if vol_str != "無成交量資料" else []

            tactical_type = str(pick.get("tactical_type", "")).strip()
            tp1 = str(pick.get("target_price_1", "")).strip()
            tp2 = str(pick.get("target_price_2", "")).strip()
            sl = str(pick.get("stop_loss_price", "")).strip()
            tactical_lines: list[str] = []
            if tactical_type:
                tactical_lines.append(f"定位：{tactical_type}")
            if tp1 or tp2 or sl:
                parts_tp = []
                if tp1:
                    parts_tp.append(f"第一目標價 {tp1}")
                if tp2:
                    parts_tp.append(f"第二目標價 {tp2}")
                if sl:
                    parts_tp.append(f"嚴格停損 {sl}")
                tactical_lines.append("測幅：" + "｜".join(parts_tp))

            entry_trigger = str(pick.get("entry_zone_and_trigger", "")).strip()
            entry_lines = [f"進場觸發：{entry_trigger}"] if entry_trigger else []
            tax_line = [str(row["taxonomy_badge"])] if row.get("taxonomy_badge") else []

            if row.get("support_ma") and row.get("high_60d") is not None:
                price_line = (
                    f"收盤 {_num(row.get('close'))}｜60日高 {_num(row.get('high_60d'))}"
                    f"（跌 {_num(row.get('drop_amount'))}元/-{_num(row.get('drop_pct'))}%）"
                    f"｜支撐 {row.get('support_ma')} {_num(row.get('support_price'))}"
                    f"｜17MA目標 {_num(row.get('target_ma17'))}（+{_num(row.get('potential_return_pct'))}%）"
                )
            else:
                price_line = (
                    f"收盤 {_num(row.get('close'))}｜17MA {_num(row.get('MA17'))}（短線支撐）"
                    f"｜45MA {_num(row.get('MA45'))}（波段防守）｜{row.get('signal', '')}"
                    f"｜連 {row.get('streak', 1)} 日"
                )

            blocks.append(
                "\n".join(
                    [
                        f"{_RANK_EMOJI.get(pick['rank'], '🏅')} #{pick['rank']} {name_of(stock_id)}"
                        f"｜推薦分 {pick.get('score', 'N/A')}｜法說展望 {emoji}{verdict}",
                        price_line,
                        *tax_line,
                        *vol_line,
                        *tactical_lines,
                        *entry_lines,
                        *format_audit_lines(pick),
                        f"理由（藍軍）：{pick.get('bull_thesis') or pick.get('thesis', '')}",
                        f"風險與失效（紅軍）：{pick.get('bear_case_and_invalidation') or pick.get('risk', '')}",
                        _links(stock_id, getattr(report, "war_room_url", None)),
                    ]
                ).rstrip()
            )
        if decision["watchlist"]:
            lines = ["👀 次佳觀察名單"]
            lines += [f"- {name_of(i['stock_id'])}：{i.get('reason', '')}" for i in decision["watchlist"]]
            blocks.append("\n".join(lines))
        if decision["avoid"]:
            lines = ["⚠️ 訊號背離／建議避開（切勿單看均線追高）"]
            lines += [f"- {name_of(i['stock_id'])}：{i.get('reason', '')}" for i in decision["avoid"]]
            blocks.append("\n".join(lines))

    footer = []
    if skipped:
        footer.append(f"ℹ️ 另有 {skipped} 檔因超過候選上限未進入深度分析。")
    if failures:
        footer.append(
            "❗ 分析失敗／財報不足：" + "、".join(f"{sid}（{reason[:30]}）" for sid, reason in failures)
        )
    if footer:
        blocks.append("\n".join(footer))
    blocks.append("本報告僅為資料整理與 AI 輔助評選，非投資建議。")

    chunks: list[str] = []
    continuation_heading = heading + "\n（續）"
    current = heading

    def _flush_and_reset() -> None:
        nonlocal current
        chunks.append(current)
        current = continuation_heading

    for block in blocks:
        if len(current) + len(block) + 2 <= DISCORD_CHUNK_LIMIT:
            current += "\n\n" + block
            continue
        if current not in (heading, continuation_heading):
            _flush_and_reset()
        if len(current) + len(block) + 2 <= DISCORD_CHUNK_LIMIT:
            current += "\n\n" + block
            continue
        # Block exceeds remaining chunk space even on a fresh chunk; split by lines.
        sep = "\n\n"
        budget = max(1, DISCORD_CHUNK_LIMIT - len(continuation_heading) - 2)
        for line in block.splitlines():
            pieces = _split_text_to_limit(line, budget) if len(line) > budget else [line]
            for piece in pieces:
                if len(current) + len(sep) + len(piece) > DISCORD_CHUNK_LIMIT and current not in (
                    heading,
                    continuation_heading,
                ):
                    _flush_and_reset()
                    sep = "\n\n"
                current += sep + piece
                sep = "\n"
    chunks.append(current)
    return chunks


def _split_text_to_limit(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most ``limit`` characters, preferring line boundaries."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                parts.append(current.rstrip("\n"))
                current = ""
            stripped = line.rstrip("\n")
            for idx in range(0, len(stripped), limit):
                parts.append(stripped[idx : idx + limit])
            continue
        if len(current) + len(line) > limit:
            parts.append(current.rstrip("\n"))
            current = line
        else:
            current += line
    if current:
        parts.append(current.rstrip("\n"))
    return [part for part in parts if part] or [text[:limit]]


def _trim_reply_continuation(content: str, *, marker: str = "（續）", scan_lines: int = 50) -> str:
    """Trim repeated heading for continuation chunks.

    The scheduled reports may build continuation chunks as "<heading>\n（續）\n...".
    When we are replying to the first message via message_reference, the heading is
    already visible in the reply preview, so we strip everything up to and
    including the marker line.
    """

    lines = content.splitlines()
    for idx, line in enumerate(lines[:scan_lines]):
        if line.strip() == marker:
            trimmed = "\n".join(lines[idx + 1 :]).lstrip("\n")
            return trimmed or content
    return content


def post_messages_to_discord(messages: list[str], channel_id: str, bot_token: str) -> int:
    if not channel_id.isdigit():
        raise ValueError("stock_channel_id 必須是 Discord 頻道 ID 數字。")
    if not bot_token.strip():
        raise ValueError("DISCORD_TOKEN (or BOT_TOKEN) is required to post the report.")
    sent = 0
    first_message_id: str | None = None
    for index, content in enumerate(messages):
        content_to_send = content
        if index > 0 and first_message_id:
            content_to_send = _trim_reply_continuation(content)
        parts = _split_text_to_limit(content_to_send, DISCORD_MAX_MESSAGE_LENGTH)

        for part_idx, part in enumerate(parts):
            payload_dict: dict[str, Any] = {
                "content": part,
                "flags": 4,
            }
            if sent > 0 and first_message_id:
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
                    "User-Agent": "DiscordService (alignment AI picker)",
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
                        if sent == 0 and isinstance(resp_data, dict) and resp_data.get("id"):
                            first_message_id = str(resp_data["id"])
                    except Exception:
                        pass
            except HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Discord rejected the report (HTTP {error.code}): {body}") from error
            except URLError as error:
                raise RuntimeError(f"Failed to reach Discord: {error.reason}") from error
            sent += 1
            if part_idx + 1 < len(parts) or index + 1 < len(messages):
                time.sleep(DISCORD_POST_PAUSE_SECONDS)
    return sent


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_pick_rows(
    target_date: date,
    decision: dict,
    frame: pd.DataFrame,
    results: Mapping[str, Mapping[str, object]],
    audit_records: Mapping[str, Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Rows for AI_Daily_Picks (including SASD v5.4.0 Phase 1 deterministic audit columns)."""
    rows = _row_lookup(frame)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output: list[dict[str, object]] = []

    def _stage1_summary_dict(stock_id: str) -> dict[str, object]:
        summary = str(results.get(stock_id, {}).get("summary", ""))
        try:
            parsed = json.loads(summary)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}

    def fundamental_score(stock_id: str) -> object:
        return _stage1_summary_dict(stock_id).get("score", "")

    def entry_thesis(item: Mapping[str, object]) -> str:
        bull = str(item.get("bull_thesis") or item.get("thesis") or item.get("reason") or "")
        entry = str(item.get("entry_zone_and_trigger", "") or "").strip()
        return f"【進場】{entry}｜{bull}" if entry else bull

    def build(category: str, rank: object, item: Mapping[str, object]) -> dict[str, object]:
        stock_id = str(item["stock_id"])
        row = rows.get(stock_id, {})
        report = results.get(stock_id, {}).get("report")
        s1_dict = _stage1_summary_dict(stock_id)
        s1_plan = s1_dict.get("trade_plan") if isinstance(s1_dict.get("trade_plan"), Mapping) else {}
        is_actionable = category in ("TOP_PICK", "WATCHLIST")
        audit_map: Mapping[str, object] = (
            item.get("audit")
            if isinstance(item.get("audit"), Mapping)
            else ((audit_records or {}).get(stock_id) or {})
        )

        tactical_type = (
            str(item.get("tactical_type") or (s1_plan.get("tactical_type") if is_actionable else "") or "").strip()
        )
        tp1 = str(item.get("target_price_1") or (s1_plan.get("target_price_1") if is_actionable else "") or "").strip()
        tp2 = str(item.get("target_price_2") or (s1_plan.get("target_price_2") if is_actionable else "") or "").strip()
        sl = str(item.get("stop_loss_price") or (s1_plan.get("stop_loss_price") if is_actionable else "") or "").strip()
        risk_val = (
            str(
                item.get("bear_case_and_invalidation")
                or item.get("risk")
                or (s1_plan.get("invalidation") if category == "WATCHLIST" else "")
                or ""
            ).strip()
        )
        raw_triggers = audit_map.get("Invalidation_Triggers", "")
        if isinstance(raw_triggers, (list, tuple)):
            triggers_str = "；".join(str(t) for t in raw_triggers if str(t).strip())
        else:
            triggers_str = str(raw_triggers or "").strip()

        def _audit_cell(key: str) -> object:
            val = audit_map.get(key, "")
            return "" if val is None else val

        return {
            "pk_key": f"{target_date.isoformat()}_{stock_id}",
            "scan_date": target_date.isoformat(),
            "rank": rank,
            "category": category,
            "stock_id": stock_id,
            "stock_name": row.get("stock_name", ""),
            "industry_category": row.get("industry_category", ""),
            "close_at_pick": row.get("close", ""),
            "ma17_at_pick": row.get("MA17", ""),
            "ma45_at_pick": row.get("MA45", ""),
            "signal_type": row.get("signal", ""),
            "ma17_trend": row.get("ma17_trend", ""),
            "streak_days": row.get("streak", ""),
            "fundamental_score": fundamental_score(stock_id),
            "final_pick_score": item.get("score", ""),
            "outlook_verdict": item.get("outlook_verdict") or (
                s1_dict.get("guidance", {}).get("verdict", "")
                if isinstance(s1_dict.get("guidance"), Mapping) and category == "WATCHLIST"
                else ""
            ),
            "entry_thesis": entry_thesis(item),
            "risk_note": risk_val,
            "war_room_url": getattr(report, "war_room_url", "") or "",
            "updated_at": now,
            "tactical_type": tactical_type,
            "target_price_1": tp1,
            "target_price_2": tp2,
            "stop_loss_price": sl,
            "Audit_Grade": _audit_cell("Audit_Grade"),
            "Execution_Tactic": _audit_cell("Execution_Tactic"),
            "Ideal_Buy_Or_Ref": _audit_cell("Ideal_Buy_Or_Ref"),
            "Chase_Limit_Price": _audit_cell("Chase_Limit_Price"),
            "Hard_Stop_Loss": _audit_cell("Hard_Stop_Loss"),
            "TP1_Target_Or_POC": _audit_cell("TP1_Target_Or_POC"),
            "Deterministic_RR": _audit_cell("Deterministic_RR"),
            "Bear_Case_Warning": _audit_cell("Bear_Case_Warning"),
            "Invalidation_Triggers": triggers_str,
            "Allocation_Weight_Pct": _audit_cell("Allocation_Weight_Pct"),
            "Tier1_Entry_Plan": _audit_cell("Tier1_Entry_Plan"),
            "Tier2_Entry_Plan": _audit_cell("Tier2_Entry_Plan"),
            "Tier3_Entry_Plan": _audit_cell("Tier3_Entry_Plan"),
        }

    for pick in decision["top_picks"]:
        output.append(build("TOP_PICK", pick["rank"], pick))
    for index, item in enumerate(decision["watchlist"], start=1):
        output.append(build("WATCHLIST", f"W{index}", item))
    for item in decision["avoid"]:
        output.append(build("AVOID", "", item))
    return output


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _cfg(config: Mapping[str, object], key: str, default: object) -> object:
    value = config.get(key, default)
    return default if value is None else value


def resolve_batch_settings(
    config: Mapping[str, object], use_batch: bool | None
) -> tuple[bool, bool, float]:
    """Return (stage1_batch, stage2_batch, poll_interval). Priority: CLI > scanner json > env."""
    if use_batch is not None:
        stage1 = bool(use_batch)
        stage2 = bool(use_batch)
    else:
        cfg1 = config.get("ai_picker_use_vertex_batch")
        cfg2 = config.get("ai_picker_stage2_use_vertex_batch")
        stage1 = is_vertex_batch_enabled(None if cfg1 is None else bool(cfg1))
        stage2 = is_vertex_batch_enabled(None if cfg2 is None else bool(cfg2))
    try:
        env_poll = float(os.getenv("VERTEX_BATCH_POLL_SECONDS") or DEFAULT_BATCH_POLL_SECONDS)
    except ValueError:
        env_poll = DEFAULT_BATCH_POLL_SECONDS
    poll = float(_cfg(config, "vertex_batch_poll_interval_seconds", env_poll))
    return stage1, stage2, poll


def resolve_batch_timeout(config: Mapping[str, object]) -> float:
    """Return Vertex AI Batch timeout in seconds. Priority: scanner json > env > 7200s (2h)."""
    cfg_val = config.get("vertex_batch_timeout_seconds")
    if cfg_val is not None:
        try:
            parsed = float(cfg_val)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return get_vertex_batch_timeout_seconds()


async def run_pipeline(
    config: Mapping[str, object],
    *,
    target_date: date,
    force_ai: bool = False,
    dry_run: bool = False,
    use_batch: bool | None = None,
) -> int:
    output_root = Path(str(config["output_directory"]))
    daily_dir = output_root / "daily"
    max_candidates = int(_cfg(config, "ai_picker_max_candidates", DEFAULT_MAX_CANDIDATES))
    max_top = int(_cfg(config, "ai_picker_max_top_picks", DEFAULT_MAX_TOP_PICKS))
    stage1_batch, stage2_batch, batch_poll = resolve_batch_settings(config, use_batch)
    batch_timeout = resolve_batch_timeout(config)
    portfolio_model = get_vertex_batch_stage2_model() if stage2_batch else get_vertex_stage2_model()
    call_interval = float(_cfg(config, "ai_picker_call_interval_seconds", DEFAULT_CALL_INTERVAL_SECONDS))
    publish_all = bool(_cfg(config, "publish_war_room_for_all_candidates", True))
    post_discord = bool(_cfg(config, "post_ai_picks_to_discord", True)) and not dry_run
    save_gsheet = bool(_cfg(config, "save_ai_picks_to_gsheet", True)) and not dry_run
    channel_id = str(config.get("stock_channel_id", ""))
    bot_token = (os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()

    candidates = load_alignment_candidates(daily_dir, target_date)
    if candidates is None:
        print(f"[skip] 找不到 {target_date.isoformat()}_alignment_signals.csv（休市或盤後掃描任務未產出）。")
        return 0
    if candidates.empty:
        message = f"📭 今日 ({target_date.isoformat()}) 無符合強勢均線排列之個股，AI 財報選股略過。"
        print(message)
        if post_discord:
            post_messages_to_discord([message], channel_id, bot_token)
        return 0

    total_signals = len(candidates)
    enriched = enrich_with_tracker(candidates, daily_dir, target_date)
    selected, skipped = prefilter_candidates(enriched, max_candidates)
    selected = enrich_with_taxonomy(selected, config, target_date, task_type="AI_PICK_TAXONOMY")
    print(f"候選訊號股 {total_signals} 檔，進入深度分析 {len(selected)} 檔（略過 {skipped} 檔）。")

    # Stage 0 硬性攔截（日量 < 300 張）：先於 Stage 1 剔除，不消耗 Token；財務類硬傷於財報同步後再判斷。
    selected_all = selected
    selected, pre_filtered_avoid = hard_prefilter(selected_all, {})
    if pre_filtered_avoid:
        print(f"Stage 0 硬性攔截（日量）{len(pre_filtered_avoid)} 檔：{[i['stock_id'] for i in pre_filtered_avoid]}")

    limiter = FinMindRequestLimiter(
        output_root / ".state" / "finmind_request_history.sqlite3",
        int(config["max_requests_per_hour"]),
        float(config["min_request_interval_seconds"]),
    )
    cache = FundamentalCacheManager()
    cache.enable_batch_mode()
    analyzer = FundamentalAnalyzer(cache)

    with _rate_limit_finmind_http_requests(limiter, only_hosts=FINMIND_HOSTS):
        market_regime = fetch_or_load_market_regime(
            daily_directory=daily_dir,
            as_of=target_date,
            loader=getattr(cache, "_finmind", None),
            benchmark_id=str(_cfg(config, "market_benchmark_id", "0050")),
            enable_dual_index_router=bool(_cfg(config, "enable_dual_index_regime_router", False)),
        )
        results, failures = await analyze_candidates(
            selected,
            cache,
            analyzer,
            force_ai=force_ai,
            publish_all=publish_all,
            call_interval=call_interval,
            use_vertex_batch=stage1_batch,
            poll_interval=batch_poll,
            timeout_seconds=batch_timeout,
            target_date=target_date,
            stage1_task_type="AI_PICK_STAGE1_DIAGNOSIS",
            online_task_type="AI_PICK_STAGE1_GROUNDING",
            taxonomy_task_type="AI_PICK_TAXONOMY",
        )

    analyzed = selected.loc[selected["stock_id"].isin(results.keys())].reset_index(drop=True)
    if analyzed.empty and not pre_filtered_avoid:
        message = f"❌ {target_date.isoformat()} AI 選股失敗：所有候選股的財報分析皆失敗。"
        print(message)
        if post_discord:
            post_messages_to_discord([message], channel_id, bot_token)
        return 1

    # Stage 0 硬性攔截（財務：本業虧損且合約負債 < 1 億）＋ Stage 2 候選上限（> 15 檔取 Stage 1 評分前 12 檔）
    metrics_lookup = {
        str(sid): await asyncio.to_thread(analyzer.get_hard_screen_metrics, str(sid)) for sid in results
    }
    analyzed, financial_avoid = hard_prefilter(analyzed, metrics_lookup)
    pre_filtered_avoid = [*pre_filtered_avoid, *financial_avoid]
    before_trim = len(analyzed)
    analyzed = limit_stage2_candidates(analyzed, results)
    if len(analyzed) < before_trim:
        print(f"Stage 2 候選 {before_trim} 檔 > {STAGE2_TRIM_THRESHOLD}，依 Stage 1 評分保留前 {len(analyzed)} 檔。")

    # SASD v6.1 三階段專職分工：AI #2 跨股績優股選拔 (QUALITY_SELECT) ➔ AI #3 戰略回檔低接與雙層籌碼分配 (STRATEGIC_LAYOUT)
    audit_records: dict[str, dict[str, object]] = {}
    raw_text = ""
    portfolio_model_version = ""
    decision: dict | None = None
    if not analyzed.empty:
        try:
            decision, audit_records, raw_text, portfolio_model_version = (
                await execute_v61_quality_and_strategic_pipeline(
                    analyzed,
                    config,
                    target_date=target_date,
                    daily_dir=daily_dir,
                    cache=cache,
                    analyzer=analyzer,
                    stage1_results=results,
                    market_regime=market_regime,
                    max_top=max_top,
                    model=portfolio_model,
                    use_vertex_batch=stage2_batch,
                    poll_interval=batch_poll,
                    timeout_seconds=batch_timeout,
                    selection_mode="trend",
                    dry_run=dry_run,
                )
            )
            print(
                f"SASD v6.1 績優股選拔與戰略低接布局完成：入選 Top {len((decision or {}).get('top_picks', []))} 檔。"
            )
        except Exception as error:  # noqa: BLE001
            message = (
                f"❌ {target_date.isoformat()} AI 績優股選拔與戰略布局失敗"
                f"（Vertex AI 模型 `{portfolio_model}` 呼叫異常，為確保決策品質未降級至免費版）："
                f"{type(error).__name__}: {error}"
            )
            print(message)
            if post_discord:
                post_messages_to_discord([message], channel_id, bot_token)
            return 1
    else:
        decision = {
            "market_light": str((market_regime or {}).get("light", "")),
            "market_comment": "今日候選股皆被 Stage 0 硬性排雷攔截，無符合條件之進場標的。",
            "top_picks": [],
            "watchlist": [],
            "avoid": [],
        }
    if decision is not None:
        decision = merge_pre_filtered_avoid(decision, pre_filtered_avoid)
    if bool(_cfg(config, "enable_signal_audit", False)) and not dry_run and pre_filtered_avoid:
        _record_v62_decision_signals(
            config=config,
            output_root=output_root,
            target_date=target_date,
            strategy_id="alignment_2130",
            market_regime=market_regime,
            top_picks=[],
            watchlist=[],
            avoid=pre_filtered_avoid,
            row_map=_row_lookup(selected_all),
        )

    # 只要有經過 AI 分析，即確保所有完成深度分析的標的皆已生成戰情室 HTML 並發布至 GitHub，
    # 且 war_room_url 確實寫入 00_Watchlist_Master（供後續寫入 AI_Daily_Picks 與 Discord 連結使用）。
    if results:
        watchlist_sheet = cache._get_worksheet("00_Watchlist_Master")
        for sid, res_data in results.items():
            report = res_data.get("report")
            if report is None:
                continue
            trade_levels = _extract_trade_levels_for_chart(decision, audit_records, sid)
            need_publish = (not bool(getattr(report, "war_room_url", None))) or (
                bool(trade_levels) and not dry_run
            )
            if need_publish:
                try:
                    if trade_levels:
                        try:
                            report.war_room_url = await analyzer.render_and_publish_war_room_html(
                                sid, getattr(report, "ai_text", ""), trade_levels=trade_levels
                            )
                        except TypeError:
                            report.war_room_url = await analyzer.render_and_publish_war_room_html(
                                sid, getattr(report, "ai_text", "")
                            )
                    else:
                        report.war_room_url = await analyzer.render_and_publish_war_room_html(
                            sid, getattr(report, "ai_text", "")
                        )
                except Exception as error:  # noqa: BLE001
                    print(f"[warn] {sid} 戰情室發布失敗：{error}")

            if getattr(report, "war_room_url", None):
                try:
                    cache._update_watchlist_master(
                        watchlist_sheet,
                        sid,
                        {"war_room_url": report.war_room_url},
                    )
                except Exception as error:  # noqa: BLE001
                    print(f"[warn] 更新 {sid} war_room_url 到 00_Watchlist_Master 失敗：{error}")

    messages = format_pick_report(
        target_date,
        decision,
        raw_text,
        selected_all,
        results,
        failures,
        total_signals=total_signals,
        skipped=skipped,
        model=portfolio_model,
        model_version=portfolio_model_version,
        market_regime=market_regime,
    )

    # Phase 3.5: 週五盤後勝率歸因與 False Negative 檢討報告 + Signal_Audit_Log 同步
    audit_store = None
    weekly_summary_md = ""
    if bool(_cfg(config, "enable_signal_audit", False)):
        try:
            from signal_audit_service import SignalAuditStore, format_weekly_summary_markdown

            audit_store = SignalAuditStore(_resolve_audit_db_path(config, output_root))
            if target_date.weekday() == 4:
                weekly_summary = audit_store.build_weekly_summary()
                if (weekly_summary.get("settled_trades", 0) or 0) > 0 or (
                    weekly_summary.get("false_negative_rate") is not None
                ):
                    weekly_summary_md = format_weekly_summary_markdown(
                        weekly_summary, as_of_str=target_date.isoformat()
                    )[:DISCORD_CHUNK_LIMIT]
                    messages.append(weekly_summary_md)
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 產生週五訊號勝率歸因摘要失敗（略過）：{error}")

    if dry_run:
        print("\n\n".join(messages))
        return 0

    payload = {
        "date": target_date.isoformat(),
        "portfolio_model": portfolio_model,
        "portfolio_model_version": portfolio_model_version,
        "total_signals": total_signals,
        "analyzed": len(results),
        "skipped": skipped,
        "failures": [{"stock_id": sid, "reason": reason} for sid, reason in failures],
        "decision": decision,
        "audit": audit_records,
        "raw_response": raw_text if decision is None else None,
        "candidates": {
            sid: {"summary": data["summary"], "snapshot": data["snapshot"]} for sid, data in results.items()
        },
    }
    daily_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        daily_dir / f"{target_date.isoformat()}_ai_recommendations.json",
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
    )
    _atomic_write_text(
        daily_dir / f"{target_date.isoformat()}_ai_recommendations.md",
        "\n\n".join(messages),
    )

    if save_gsheet and decision is not None:
        try:
            cache.ensure_workbook_schema()
            picks = pd.DataFrame(
                build_pick_rows(target_date, decision, selected_all, results, audit_records=audit_records)
            )
            if not picks.empty:
                worksheet = cache._get_worksheet(PICKS_SHEET_NAME)  # noqa: SLF001
                cache._upsert_delta(worksheet, picks, key_col="pk_key")  # noqa: SLF001
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 寫入 {PICKS_SHEET_NAME} 失敗：{error}")

    if save_gsheet and audit_store is not None:
        try:
            audit_tab = str(_cfg(config, "signal_audit_gsheet_tab_name", "Signal_Audit_Log"))
            synced_audit = audit_store.sync_to_gsheet(tab_name=audit_tab)
            if synced_audit > 0:
                print(f"已同步 {synced_audit} 筆訊號留痕至 Google Sheets [{audit_tab}]。")
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 同步訊號留痕至 Google Sheets 失敗：{error}")

    # 先發布戰情總部網頁再發 Discord，確保點開通知連結即見最新資料
    if bool(_cfg(config, "enable_portal_publish", False)):
        try:
            from portal_publisher import publish_portal_data

            publish_portal_data(
                config,
                as_of=target_date,
                output_root=output_root,
                commit_message=f"Update quant portal 21:30 alignment ({target_date.isoformat()})",
            )
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 21:30 戰情總部發布失敗（略過）：{error}")

    if post_discord:
        discord_msgs = (
            format_compact_pick_notification(
                target_date,
                decision,
                selected_all,
                total_signals=total_signals,
                analyzed_count=len(results),
                audit_records=audit_records,
                market_regime=market_regime,
                weekly_summary_md=weekly_summary_md,
            )
            if bool(_cfg(config, "discord_compact_mode", False))
            else messages
        )
        sent = post_messages_to_discord(discord_msgs, channel_id, bot_token)
        print(f"已推播 AI 嚴選報告（{sent} 則訊息）。")

    _cleanup_old_daily_reports(daily_dir, target_date, int(config["local_retention_days"]))
    flush_bq_archives()
    return 0


def run_from_cli(
    *,
    config_path: Path,
    target_date: str | None = None,
    force_ai: bool = False,
    dry_run: bool = False,
    use_batch: bool | None = None,
) -> int:
    config = load_scan_config(config_path)
    parsed_date = date.fromisoformat(target_date) if target_date else _today_in_taipei()
    return asyncio.run(
        run_pipeline(
            config,
            target_date=parsed_date,
            force_ai=force_ai,
            dry_run=dry_run,
            use_batch=use_batch,
        )
    )


async def run_rebound_pipeline(
    config: Mapping[str, object],
    *,
    target_date: date,
    output_dir: Path | None = None,
    run_ai: bool | None = None,
    force_ai: bool = False,
    dry_run: bool = False,
    skip_gsheet: bool = False,
    skip_discord: bool = False,
    use_batch: bool | None = None,
) -> int:
    """型態 A（高點急跌回踩長均線反彈）獨立量化掃描、Rebound_Watchlist 同步與彈性 AI 嚴選流程。"""
    output_root = output_dir or Path(str(config["output_directory"]))
    daily_dir = output_root / "daily"
    prices_path = daily_dir / f"{target_date.isoformat()}_prices.csv"
    if not prices_path.is_file():
        print(f"[skip] 找不到 {prices_path}（休市或盤後掃描任務未產出）。")
        return 0

    prices = pd.read_csv(prices_path, dtype={"stock_id": str})
    rebound_df, rebound_failures_df = scan_rebound_from_prices(prices)
    total_rebounds = len(rebound_df)
    scan_fail_count = len(rebound_failures_df)

    limiter = FinMindRequestLimiter(
        output_root / ".state" / "finmind_request_history.sqlite3",
        int(config["max_requests_per_hour"]),
        float(config["min_request_interval_seconds"]),
    )
    benchmark_id = str(_cfg(config, "market_benchmark_id", "0050"))

    # 取得大盤紅綠燈（優先讀本地快取或 prices.csv，若無且有 FINMIND_API_TOKEN 則查 0050）
    from FinMind.data import DataLoader

    api_token = os.getenv("FINMIND_API_TOKEN", "").strip()
    loader = DataLoader(token=api_token) if (api_token and not dry_run) else None
    with _rate_limit_finmind_http_requests(limiter, only_hosts=FINMIND_HOSTS):
        market_regime = fetch_or_load_market_regime(
            daily_directory=daily_dir if not dry_run else None,
            as_of=target_date,
            loader=loader,
            benchmark_id=benchmark_id,
        )

    market_light = str(market_regime.get("light", "🟡黃燈"))
    print(
        f"[rebound] {target_date.isoformat()} 型態 A 反彈掃描完成：符合條件 {total_rebounds} 檔"
        f"（失敗 {scan_fail_count} 檔）｜大盤環境：{market_regime.get('summary', market_light)}"
    )
    if not rebound_df.empty:
        try:
            rebound_df = enrich_with_taxonomy(
                rebound_df, config, target_date, task_type="REBOUND_TAXONOMY"
            )
        except Exception:
            logger.warning('[rebound] 細分產業補齊失敗', exc_info=True)
        preview_cols = [
            c
            for c in [
                "stock_id",
                "stock_name",
                "close",
                "high_60d",
                "drop_amount",
                "drop_pct",
                "support_ma",
                "support_price",
                "target_ma17",
                "potential_return_pct",
                "close_strength",
                "vol_status",
            ]
            if c in rebound_df.columns
        ]
        print(rebound_df[preview_cols].to_string(index=False))

    if not dry_run:
        daily_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_csv(
            rebound_df, daily_dir / f"{target_date.isoformat()}_rebound_signals.csv"
        )
        _atomic_write_csv(
            rebound_failures_df,
            daily_dir / f"{target_date.isoformat()}_rebound_failures.csv",
        )

    # 彈性決策點：決定是否啟動 AI 深度診斷
    # 1. CLI 明確指定 --run-ai 或 --no-ai 時優先遵循
    # 2. 否則讀取 finmind_scanner.json 的 rebound_auto_run_ai（第一階段預設 False，先觀察標的數量）
    #    若 rebound_auto_run_ai 為 True，則檢查標的數量是否落在合理區間 [min_count, max_count]（預設 3 ~ 15 檔）
    min_ai_count = int(_cfg(config, "rebound_ai_min_candidates", 1))
    auto_ai_cfg = bool(_cfg(config, "rebound_auto_run_ai", False))
    if run_ai is not None:
        should_run_ai = bool(run_ai) and total_rebounds > 0
    else:
        # SASD v6.0：由 prefilter_rebound_candidates 負責裁剪至 Top 12，不再因候選數 > 15 而跳過 AI
        should_run_ai = auto_ai_cfg and (total_rebounds >= min_ai_count)

    post_discord = (
        bool(_cfg(config, "post_rebound_to_discord", True))
        and not dry_run
        and not skip_discord
    )
    save_gsheet = (
        bool(_cfg(config, "sync_rebound_to_gsheet", True))
        and not dry_run
        and not skip_gsheet
    )
    channel_id = str(config.get("stock_channel_id", ""))
    bot_token = (os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()

    ai_results: dict[str, dict[str, object]] = {}
    audit_records: dict[str, dict[str, object]] = {}
    ai_failures: list[tuple[str, str]] = []
    ai_messages: list[str] = []

    if should_run_ai and not rebound_df.empty:
        max_candidates = int(_cfg(config, "rebound_prefilter_top_n", 12))
        max_top = int(_cfg(config, "ai_picker_max_top_picks", DEFAULT_MAX_TOP_PICKS))
        stage1_batch, stage2_batch, batch_poll = resolve_batch_settings(config, use_batch)
        batch_timeout = resolve_batch_timeout(config)
        portfolio_model = get_vertex_batch_stage2_model() if stage2_batch else get_vertex_stage2_model()
        call_interval = float(_cfg(config, "ai_picker_call_interval_seconds", DEFAULT_CALL_INTERVAL_SECONDS))
        publish_all = bool(_cfg(config, "publish_war_room_for_all_candidates", True))

        cache = FundamentalCacheManager()
        cache.enable_batch_mode()
        analyzer = FundamentalAnalyzer(cache)

        enriched_rebound = enrich_with_taxonomy(
            rebound_df, config, target_date, task_type="REBOUND_TAXONOMY"
        )
        vol_filtered, pre_filtered_avoid = hard_prefilter(enriched_rebound, {})
        selected, skipped = prefilter_rebound_candidates(
            vol_filtered,
            cache=cache,
            analyzer=analyzer,
            max_candidates=max_candidates,
        )
        if skipped > 0:
            print(f"[rebound] Python 100分制菁英預篩：保留前 {len(selected)} 檔進入 AI 審計（篩除 {skipped} 檔）。")

        with _rate_limit_finmind_http_requests(limiter, only_hosts=FINMIND_HOSTS):
            ai_results, ai_failures = await analyze_candidates(
                selected,
                cache,
                analyzer,
                force_ai=force_ai,
                publish_all=publish_all,
                call_interval=call_interval,
                use_vertex_batch=stage1_batch,
                poll_interval=batch_poll,
                timeout_seconds=batch_timeout,
                target_date=target_date,
                stage1_task_type="REBOUND_STAGE1_DIAGNOSIS",
                online_task_type="REBOUND_STAGE1_GROUNDING",
                taxonomy_task_type="REBOUND_TAXONOMY",
            )

        analyzed = selected.loc[selected["stock_id"].isin(ai_results.keys())].reset_index(drop=True)
        if not analyzed.empty:
            metrics_lookup = {
                str(sid): await asyncio.to_thread(analyzer.get_hard_screen_metrics, str(sid))
                for sid in ai_results
            }
            analyzed, financial_avoid = hard_prefilter(analyzed, metrics_lookup)
            pre_filtered_avoid = [*pre_filtered_avoid, *financial_avoid]

        if not analyzed.empty or pre_filtered_avoid:
            raw_text = ""
            portfolio_model_version = ""
            decision: dict | None = None
            if not analyzed.empty:
                try:
                    decision, audit_records, raw_text, portfolio_model_version = (
                        await execute_v61_quality_and_strategic_pipeline(
                            analyzed,
                            config,
                            target_date=target_date,
                            daily_dir=daily_dir,
                            cache=cache,
                            analyzer=analyzer,
                            stage1_results=ai_results,
                            market_regime=market_regime,
                            max_top=max_top,
                            model=portfolio_model,
                            use_vertex_batch=stage2_batch,
                            poll_interval=batch_poll,
                            timeout_seconds=batch_timeout,
                            selection_mode="rebound",
                            dry_run=dry_run,
                        )
                    )
                    print(
                        f"[rebound] SASD v6.1 績優股選拔與戰略低接布局完成：入選 Top {len((decision or {}).get('top_picks', []))} 檔。"
                    )
                except Exception as error:  # noqa: BLE001
                    print(f"[warn] [rebound] AI #2/#3 執行異常（保留量化清單與戰情室發布）：{type(error).__name__}: {error}")
                    decision = None
                    raw_text = f"AI #2/#3 呼叫異常（{type(error).__name__}: {error}）"
            else:
                decision = {
                    "market_light": str((market_regime or {}).get("light", "")),
                    "market_comment": "今日型態 A 候選股皆被 Stage 0 硬性排雷攔截，建議觀望。",
                    "top_picks": [],
                    "watchlist": [],
                    "avoid": [],
                }
            if decision is not None:
                decision = merge_pre_filtered_avoid(decision, pre_filtered_avoid)
            if bool(_cfg(config, "enable_signal_audit", False)) and not dry_run and pre_filtered_avoid:
                _record_v62_decision_signals(
                    config=config,
                    output_root=output_root,
                    target_date=target_date,
                    strategy_id="rebound_2200",
                    market_regime=market_regime,
                    top_picks=[],
                    watchlist=[],
                    avoid=pre_filtered_avoid,
                    row_map=_row_lookup(selected),
                )

            # 只要有經過 AI 分析，即確保所有反彈標的皆生成戰情室 HTML 並發布至 GitHub，且 war_room_url 寫入 00_Watchlist_Master
            if ai_results:
                watchlist_sheet = cache._get_worksheet("00_Watchlist_Master")
                for sid, res_data in ai_results.items():
                    report = res_data.get("report")
                    if report is None:
                        continue
                    trade_levels = _extract_trade_levels_for_chart(decision, audit_records, sid)
                    need_publish = (not bool(getattr(report, "war_room_url", None))) or (
                        bool(trade_levels) and not dry_run
                    )
                    if need_publish:
                        try:
                            if trade_levels:
                                try:
                                    report.war_room_url = await analyzer.render_and_publish_war_room_html(
                                        sid, getattr(report, "ai_text", ""), trade_levels=trade_levels
                                    )
                                except TypeError:
                                    report.war_room_url = await analyzer.render_and_publish_war_room_html(
                                        sid, getattr(report, "ai_text", "")
                                    )
                            else:
                                report.war_room_url = await analyzer.render_and_publish_war_room_html(
                                    sid, getattr(report, "ai_text", "")
                                )
                        except Exception as error:  # noqa: BLE001
                            print(f"[warn] {sid} 戰情室發布失敗：{error}")
                    if getattr(report, "war_room_url", None):
                        try:
                            cache._update_watchlist_master(
                                watchlist_sheet,
                                sid,
                                {"war_room_url": report.war_room_url},
                            )
                        except Exception as error:  # noqa: BLE001
                            print(f"[warn] 更新 {sid} war_room_url 失敗：{error}")

            ai_messages = format_pick_report(
                target_date,
                decision,
                raw_text,
                analyzed,
                ai_results,
                ai_failures,
                total_signals=total_rebounds,
                skipped=skipped,
                model=portfolio_model,
                model_version=portfolio_model_version,
                market_regime=market_regime,
                title_label="型態 A 反彈 AI 嚴選決策報告",
            )
            if not dry_run:
                payload = {
                    "date": target_date.isoformat(),
                    "market_regime": market_regime,
                    "portfolio_model": portfolio_model,
                    "portfolio_model_version": portfolio_model_version,
                    "total_signals": total_rebounds,
                    "analyzed": len(ai_results),
                    "skipped": skipped,
                    "failures": [{"stock_id": sid, "reason": r} for sid, r in ai_failures],
                    "decision": decision,
                    "audit": audit_records,
                }
                _atomic_write_text(
                    daily_dir / f"{target_date.isoformat()}_rebound_ai_recommendations.json",
                    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                )
                _atomic_write_text(
                    daily_dir / f"{target_date.isoformat()}_rebound_ai_recommendations.md",
                    "\n\n".join(ai_messages),
                )

    if save_gsheet:
        try:
            tab_name = str(_cfg(config, "rebound_gsheet_tab_name", REBOUND_WATCHLIST_SHEET_NAME))
            synced_count = sync_rebound_watchlist_to_gsheet(
                rebound_df,
                as_of=target_date,
                market_light=market_light,
                ai_results=ai_results if ai_results else None,
                audit_records=audit_records if audit_records else None,
                tab_name=tab_name,
                replace_date=True,
            )
            print(f"[rebound] 已同步 {synced_count} 筆反彈標的至 Google Sheets [{tab_name}]。")
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 寫入 {REBOUND_WATCHLIST_SHEET_NAME} 失敗：{error}")

    scan_messages = format_rebound_report(
        rebound_df,
        as_of=target_date,
        failed_count=scan_fail_count,
        market_regime=market_regime,
        ai_executed=bool(ai_messages),
    )

    if dry_run:
        print("\n\n".join(scan_messages))
        if ai_messages:
            print("\n\n" + "\n\n".join(ai_messages))
        return 0

    # 先發布戰情總部網頁再發 Discord，確保點開通知連結即見最新資料
    if bool(_cfg(config, "enable_portal_publish", False)):
        try:
            from portal_publisher import publish_portal_data

            publish_portal_data(
                config,
                as_of=target_date,
                output_root=output_root,
                commit_message=f"Update quant portal 22:00 rebound ({target_date.isoformat()})",
            )
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 22:00 戰情總部發布失敗（略過）：{error}")

    if post_discord:
        compact_mode = bool(_cfg(config, "discord_compact_mode", False))
        rebound_channel_id = str(
            config.get("rebound_channel_id")
            or config.get("alignment_channel_id")
            or channel_id
            or os.getenv("ALIGNMENT_SIGNALS_CHANNEL_ID")
            or os.getenv("STOCK_CHANNEL_ID")
            or ""
        ).strip()
        sent_scan = post_rebound_report_to_discord(
            rebound_df,
            channel_id=rebound_channel_id,
            bot_token=bot_token,
            as_of=target_date,
            failed_count=scan_fail_count,
            market_regime=market_regime,
            ai_executed=bool(ai_messages),
            compact=compact_mode,
        )
        print(f"[rebound] 已推播型態 A 反彈清單快報（{sent_scan} 則訊息）。")
        if ai_messages:
            discord_ai_msgs = (
                format_compact_pick_notification(
                    target_date,
                    decision,
                    analyzed,
                    total_signals=total_rebounds,
                    analyzed_count=len(ai_results),
                    audit_records=audit_records,
                    market_regime=market_regime,
                    title_label="型態 A 反彈 AI 嚴選快報",
                )
                if compact_mode
                else ai_messages
            )
            sent_ai = post_messages_to_discord(discord_ai_msgs, channel_id, bot_token)
            print(f"[rebound] 已推播型態 A 反彈 AI 嚴選報告（{sent_ai} 則訊息）。")

    _cleanup_old_daily_reports(daily_dir, target_date, int(config["local_retention_days"]))
    flush_bq_archives()
    return 0


def run_rebound_from_cli(
    *,
    config_path: Path,
    target_date: str | None = None,
    output_dir: Path | None = None,
    run_ai: bool | None = None,
    force_ai: bool = False,
    dry_run: bool = False,
    skip_gsheet: bool = False,
    skip_discord: bool = False,
    use_batch: bool | None = None,
) -> int:
    config = load_scan_config(config_path)
    parsed_date = date.fromisoformat(target_date) if target_date else _today_in_taipei()
    return asyncio.run(
        run_rebound_pipeline(
            config,
            target_date=parsed_date,
            output_dir=output_dir,
            run_ai=run_ai,
            force_ai=force_ai,
            dry_run=dry_run,
            skip_gsheet=skip_gsheet,
            skip_discord=skip_discord,
            use_batch=use_batch,
        )
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    load_dotenv()
    parser = argparse.ArgumentParser(description="alignment_signals 財報分析與 AI 嚴選進場標的")
    parser.add_argument("--config", type=Path, default=Path("finmind_scanner.json"))
    parser.add_argument("--date", help="指定分析日期 YYYY-MM-DD（預設今日）")
    parser.add_argument("--force-ai", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch", dest="batch", action="store_true", default=None, help="強制使用 Vertex AI Batch")
    parser.add_argument("--no-batch", dest="batch", action="store_false", help="強制使用即時 API")
    args = parser.parse_args(argv)
    return run_from_cli(
        config_path=args.config,
        target_date=args.date,
        force_ai=args.force_ai,
        dry_run=args.dry_run,
        use_batch=args.batch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
