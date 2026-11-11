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
from collections.abc import Mapping
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

        tax_svc.ensure_batch(result, valid_map, as_of=target_date, sync_sheet=True)
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
    target_date: date | None = None,
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
            target_date=target_date,
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
                stock_id, force_ai=force_ai, publish_war_room=publish_all
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
                stock_id, force_ai=force_ai, publish_war_room=publish_all
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
                task_type="STAGE1_DIAGNOSIS",
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
    "2. 均線斜率與量價結構 (35%)：本批次為『回踩 45MA/117MA 的均值回歸反彈』，"
    "17MA 多數下彎屬正常現象，不因 17MA 下彎扣分。改請檢查："
    "(a) 45MA/117MA 支撐守穩；(b) K 線收盤強度 close_strength >= 0.55；"
    "(c) 反彈至 17MA 空間 potential_return_pct >= 5%。並且 `target_price_1` 優先設為「17MA 反壓」。"
)


def _apply_portfolio_prompt_overrides(prompt: str, *, selection_mode: str) -> str:
    """依選股模式動態調整 Stage 2 Prompt 的關鍵規則。

    - trend：均線多頭順勢金叉（預設）
    - rebound：型態 A 回踩長均線反彈
    """

    mode = (selection_mode or "").strip().lower()
    if mode != "rebound":
        return prompt

    # 反彈模式不適用 17MA 下彎扣分條款，改用「長均線守穩 + 收盤強度 + 反彈空間」檢核。
    if _PORTFOLIO_MA17_RULE in prompt:
        return prompt.replace(_PORTFOLIO_MA17_RULE, _PORTFOLIO_REBOUND_RULE)
    return prompt


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
            task_type="STAGE2_PORTFOLIO",
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
        task_type="STAGE2_PORTFOLIO",
        stock_id="PORTFOLIO",
        custom_id=f"STAGE2_PORTFOLIO_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
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
3. 算術與點位尊重鐵律：所有停損價、追價上限、上方 Volume POC 壓力區與預估風報比（R/R Ratio）皆由 Python 精確計算完畢。若 Python 計算之 `deterministic_rr_ratio < 2.0`，原則上不得評為 `S_TIER_SNIPER`。
4. 消除逆向選擇之進場紀律：
   - 右側突破強勢股：嚴禁建議「死等拉回平盤才買」（會導致真飆股踏空、只買到假突破倒貨股），亦嚴禁 09:00 開盤盲目市價追高；必須採用 `BREAKOUT_0915_VWAP_OR_1315_CLOSE`（早盤冷卻 15 分鐘確認站穩均價線且低於追價上限試單 50%，或 13:15 尾盤確認站穩買進）。
   - Style B 回測股：必須採用 `STYLE_B_PULLBACK_LIMIT` 於支撐區間限價低接。
5. 庫存共伴曝險鐵律：若候選股被標記為 `SECTOR_OR_CORR_FULL_SWAP_ONLY`（代表使用者台新帳戶中已有同產業或 60 日報酬相關係數 >= 0.80 的高度共伴持股），除非該股基本面與籌碼強度遠勝現有庫存，否則一律判定為 `REJECT_TRAP`。
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
    """AI 回傳後的 Python 確定性攔截，防止 LLM 偶發性違規（Phase 1 + Phase 3 微結構與大盤風控）。

    1. 風報比 < 1.8（或無法計算）卻給 S_TIER：降級 A_TIER_PULLBACK；右側突破型直接 REJECT_TRAP。
    2. 處置風險預警標的非空時：禁止右側突破追高（RIGHT_BREAKOUT -> REJECT_TRAP；其他改 STYLE_B_PULLBACK_LIMIT）。
    3. 大盤雙指數風控路由器（RegimeQuotaConfig）：紅燈全面禁止右側突破、黃燈右側突破須 PR85+、族群廣度與相對 PE 上限硬攔截。
    4. 庫存曝險為 SECTOR_OR_CORR_FULL_SWAP_ONLY 且非 REJECT_TRAP：戰術強制改為 PORTFOLIO_SWAP_ONLY。
    """
    guarded: list[StockAuditItem] = []
    for item in items:
        update: dict[str, object] = {}
        setup = setups.get(item.stock_id)
        ctx = (context_by_stock or {}).get(item.stock_id) or {}
        grade = item.verdict_grade
        tactic = item.execution_strategy_type
        reasoning = item.conflict_resolution_reasoning
        sig_type = str((setup or {}).get("signal_type") or "")

        if setup is not None and grade == "S_TIER_SNIPER":
            rr = setup.get("deterministic_rr_ratio")
            if rr is None or float(rr) < AUDIT_RR_GUARD_THRESHOLD:
                if sig_type == "RIGHT_BREAKOUT":
                    grade = "REJECT_TRAP"
                    tactic = "NO_TRADE"
                else:
                    grade = "A_TIER_PULLBACK"
                update["verdict_grade"] = grade
                update["execution_strategy_type"] = tactic
                reasoning = (
                    f"{reasoning}｜[Python 攔截] 風報比 {rr} < "
                    f"{AUDIT_RR_GUARD_THRESHOLD}，S 級評等已被降級為 {grade}。"
                )
                update["conflict_resolution_reasoning"] = reasoning

        # Phase 3.1: 處置風險預警禁止右側突破追高
        disp_tag = str(ctx.get("disposition_tag") or "").strip()
        if grade != "REJECT_TRAP" and disp_tag:
            if sig_type == "RIGHT_BREAKOUT":
                grade = "REJECT_TRAP"
                tactic = "NO_TRADE"
                reasoning = f"{reasoning}｜[Python 處置攔截] 觸發處置流動性預警，禁止右側突破追高。"
                update["verdict_grade"] = grade
                update["execution_strategy_type"] = tactic
                update["conflict_resolution_reasoning"] = reasoning
            elif tactic == "BREAKOUT_0915_VWAP_OR_1315_CLOSE":
                grade = "A_TIER_PULLBACK"
                tactic = "STYLE_B_PULLBACK_LIMIT"
                reasoning = f"{reasoning}｜[Python 處置降級] 觸發處置流動性預警，強制改為回測限價低接。"
                update["verdict_grade"] = grade
                update["execution_strategy_type"] = tactic
                update["conflict_resolution_reasoning"] = reasoning

        # Phase 3.3 & 3.4: 雙指數大盤風控路由器、族群廣度與前瞻估值上限攔截
        if grade != "REJECT_TRAP" and regime_quota:
            r_code = str(regime_quota.get("regime_state") or regime_quota.get("regime") or "")
            max_rb = int(regime_quota.get("max_right_breakout_picks", 99) or 0)
            is_rb = sig_type == "RIGHT_BREAKOUT" or tactic == "BREAKOUT_0915_VWAP_OR_1315_CLOSE"
            if is_rb and (r_code == "RED_DEFENSIVE" or max_rb <= 0):
                grade = "REJECT_TRAP"
                tactic = "NO_TRADE"
                reasoning = f"{reasoning}｜[Python 大盤風控攔截] 紅燈防禦期全面禁止右側突破追價。"
                update["verdict_grade"] = grade
                update["execution_strategy_type"] = tactic
                update["conflict_resolution_reasoning"] = reasoning
            elif is_rb and r_code == "YELLOW_ROTATION":
                ret_pr = ctx.get("return_percentile")
                if ret_pr is not None and float(ret_pr) < 85.0:
                    grade = "REJECT_TRAP"
                    tactic = "NO_TRADE"
                    reasoning = (
                        f"{reasoning}｜[Python 黃燈強度攔截] 黃燈震盪期右側突破須達同業強弱 PR85（目前 PR{ret_pr}）。"
                    )
                    update["verdict_grade"] = grade
                    update["execution_strategy_type"] = tactic
                    update["conflict_resolution_reasoning"] = reasoning

            min_breadth = float(regime_quota.get("min_sector_breadth_pct", 0.0) or 0.0)
            b_members = int(ctx.get("sector_member_count", 0) or 0)
            b_pct = ctx.get("sector_breadth_pct")
            if grade != "REJECT_TRAP" and min_breadth > 0 and b_members >= 3 and b_pct is not None:
                if float(b_pct) < min_breadth:
                    grade = "REJECT_TRAP"
                    tactic = "NO_TRADE"
                    reasoning = (
                        f"{reasoning}｜[Python 族群廣度攔截] 同細分產業站上 17MA 比例 {b_pct}% < 門檻 {min_breadth}%。"
                    )
                    update["verdict_grade"] = grade
                    update["execution_strategy_type"] = tactic
                    update["conflict_resolution_reasoning"] = reasoning

            max_pe_ratio = float(regime_quota.get("max_allowed_sector_pe_ratio", 999.0) or 999.0)
            pe_ratio = ctx.get("sector_pe_ratio")
            if grade != "REJECT_TRAP" and pe_ratio is not None and float(pe_ratio) > max_pe_ratio:
                grade = "REJECT_TRAP"
                tactic = "NO_TRADE"
                reasoning = (
                    f"{reasoning}｜[Python 估值上限攔截] 前瞻 PE 相對同業比值 {pe_ratio}x > 當前氣候上限 {max_pe_ratio}x。"
                )
                update["verdict_grade"] = grade
                update["execution_strategy_type"] = tactic
                update["conflict_resolution_reasoning"] = reasoning

        if grade != "REJECT_TRAP":
            status = _exposure_field(exposures.get(item.stock_id), "exposure_status")
            if status == "SECTOR_OR_CORR_FULL_SWAP_ONLY":
                update["execution_strategy_type"] = "PORTFOLIO_SWAP_ONLY"
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
    for pick in decision.get("top_picks", []):
        sid = str(pick.get("stock_id", ""))
        rec = audit_records.get(sid) or pick.get("audit") or {}
        tactic = str(rec.get("Execution_Tactic") or "")
        sig_text = str((row_map.get(sid) or {}).get("signal") or "")
        is_rb = tactic == "BREAKOUT_0915_VWAP_OR_1315_CLOSE" or (
            not tactic and ("突破" in sig_text or "站上" in sig_text) and "回踩" not in sig_text
        )
        if is_rb:
            if rb_count < max_rb:
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
) -> dict[str, object]:
    """合併 Python 確定性點位與 AI 審計結論（對應 05_Position_Thesis_Memory 擴充欄位語意）。"""
    setup = setup or {}
    zone_low, zone_high = setup.get("ideal_buy_zone_low"), setup.get("ideal_buy_zone_high")
    ref = setup.get("entry_reference_price")
    if zone_low is not None and zone_high is not None and zone_low != zone_high:
        ideal = f"{zone_low}~{zone_high}"
    else:
        ideal = "" if ref is None else str(ref)
    swap = _exposure_field(exposure, "exposure_status") == "SECTOR_OR_CORR_FULL_SWAP_ONLY"
    return {
        "Audit_Grade": item.verdict_grade,
        "Execution_Tactic": item.execution_strategy_type,
        "Ideal_Buy_Or_Ref": ideal,
        "Chase_Limit_Price": setup.get("chase_limit_price"),
        "Hard_Stop_Loss": setup.get("hard_stop_loss"),
        "TP1_Target_Or_POC": setup.get("tp1_price"),
        "Deterministic_RR": setup.get("deterministic_rr_ratio"),
        "Bear_Case_Warning": item.bear_case_flaws[0] if item.bear_case_flaws else "",
        "Invalidation_Triggers": list(item.thesis_invalidation_triggers),
        "Exposure_Status": _exposure_field(exposure, "exposure_status", "OPEN"),
        "Swap_Banner": format_swap_banner(exposure) if swap else "",
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
    batch: list[tuple[str, str]], model: str | None
) -> tuple[list[StockAuditItem], list[str]]:
    ids = {sid for sid, _ in batch}
    stock_ids_str = ",".join(sorted(ids))
    try:
        meta = await ask_vertex_ai_with_meta(
            build_audit_prompt([block for _, block in batch]),
            model=model,
            tag="red_blue_audit",
            response_schema=BatchAuditResponse,
            task_type="RED_BLUE_AUDIT",
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
    items = [i for i in parsed.audited_stocks if i.stock_id in ids]
    missing = sorted(ids - {i.stock_id for i in items})
    return items, missing


async def run_red_blue_audit(
    blocks: list[tuple[str, str]],
    *,
    model: str | None = None,
    batch_size: int = AUDIT_BATCH_SIZE,
) -> tuple[list[StockAuditItem], list[str]]:
    """Pass 1：每 batch_size 檔為一批以 asyncio.gather 並行審計。回傳 (審計結果, 未取得審計結果之代號)。"""
    batches = [blocks[i : i + batch_size] for i in range(0, len(blocks), batch_size)]
    outcomes = await asyncio.gather(*(_audit_batch(b, model) for b in batches))
    items: list[StockAuditItem] = []
    missing: list[str] = []
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
) -> tuple[pd.DataFrame, dict[str, dict[str, object]], list[dict[str, str]]]:
    """Pass 1 紅藍軍審計：除息補償 → 點位 → 處置/三層籌碼/族群廣度 → 庫存曝險 → 新聞量價對齊 → AI 審計 → 硬攔截 → 訊號留痕。

    回傳 (倖存候選 frame, {stock_id: audit_record}, 被淘汰清單 [{stock_id, reason}])。
    """
    from finmind_stock_scanner import (
        MARGIN_CALENDAR_NOTE,
        align_news_with_price_action,
        classify_daily_margin_efficiency,
        compensate_ex_dividend_gaps,
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

        # Phase 3.1: 除權息跳空補償（僅用於技術點位/POC/ATR 計算；處置風險維持真實原始價格與週轉率）
        prices_for_setup = raw_stock_prices
        if has_prices and enable_ex_div and div_df is not None and not div_df.empty:
            try:
                prices_for_setup = compensate_ex_dividend_gaps(raw_stock_prices, div_df)
            except Exception:  # noqa: BLE001
                prices_for_setup = raw_stock_prices

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
                f"- 大盤雙指數風控：{r_label}｜右側突破配額上限 "
                f"{regime_quota_dict.get('max_right_breakout_picks')} 檔｜回測低接上限 "
                f"{regime_quota_dict.get('max_style_b_pullback_picks')} 檔"
                + (f"｜{r_banner}" if r_banner else "")
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
                raw_news = await asyncio.to_thread(
                    analyzer._fetch_google_news_rss,  # noqa: SLF001
                    f"{sid} {row.get('stock_name', '')} 台股".strip(),
                )
                recent_news = _filter_recent_rss_news(
                    raw_news,
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
        blocks.append((sid, build_audit_block(row, setup, exposure, news, fund_block, microstructure_block)))

    items, missing = await run_red_blue_audit(blocks, model=model)
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
        i.stock_id: build_audit_record(i, setups.get(i.stock_id), exposures.get(i.stock_id)) for i in survivors
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


def apply_audit_to_decision(decision: dict, audit_records: Mapping[str, Mapping[str, object]]) -> dict:
    """將 Python 確定性點位與審計結論併入 top_picks 與 watchlist；TP1 / 停損一律沿用 Python 計算值。"""
    for pick in decision.get("top_picks", []):
        record = audit_records.get(str(pick.get("stock_id")))
        if not record:
            continue
        pick["audit"] = dict(record)
        if record.get("TP1_Target_Or_POC") is not None:
            pick["target_price_1"] = f"{record['TP1_Target_Or_POC']}（Python 確定性點位）"
        if record.get("Hard_Stop_Loss") is not None:
            pick["stop_loss_price"] = f"{record['Hard_Stop_Loss']}（Python 確定性硬停損）"
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
        f"｜風報比 {audit.get('Deterministic_RR')}｜追價上限 {audit.get('Chase_Limit_Price')}"
        f"｜建倉參考 {audit.get('Ideal_Buy_Or_Ref')}"
    ]
    if audit.get("Swap_Banner"):
        lines.insert(0, str(audit["Swap_Banner"]))
    if audit.get("Bear_Case_Warning"):
        lines.append(f"頭號風險：{audit['Bear_Case_Warning']}")
    triggers = audit.get("Invalidation_Triggers") or []
    if triggers:
        lines.append("失效砍倉：" + "；".join(str(t) for t in triggers))
    return lines


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
    if war_room_url:
        return f"[📊 全方位戰情室（含財報+K線）]({war_room_url})"
    chart = chart_url(stock_id)
    if chart:
        return f"[📈 K 線圖]({chart})"
    return ""


def _row_lookup(frame: pd.DataFrame) -> dict[str, dict[str, object]]:
    return {str(r["stock_id"]): r for r in frame.to_dict("records")}


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
    rows = _row_lookup(frame)
    top_count = len(decision["top_picks"]) if decision else 0
    heading = (
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
    current = heading
    for block in blocks:
        if len(current) + len(block) + 2 > DISCORD_CHUNK_LIMIT and current != heading:
            chunks.append(current)
            current = heading + "\n（續）"
        current += "\n\n" + block
    chunks.append(current)
    return chunks


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

        payload_dict: dict[str, Any] = {
            "content": content_to_send,
            "flags": 4,
        }
        if index > 0 and first_message_id:
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
                    if index == 0 and isinstance(resp_data, dict) and resp_data.get("id"):
                        first_message_id = str(resp_data["id"])
                except Exception:
                    pass
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Discord rejected the report (HTTP {error.code}): {body}") from error
        except URLError as error:
            raise RuntimeError(f"Failed to reach Discord: {error.reason}") from error
        sent += 1
        if index + 1 < len(messages):
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
    selected = enrich_with_taxonomy(selected, config, target_date)
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
            target_date=target_date,
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

    # SASD v5.4.0 Pass 1：紅藍軍審計（確定性點位 + 庫存曝險 + 新聞量價對齊），允許全數淘汰。
    audit_records: dict[str, dict[str, object]] = {}
    audit_all_rejected = False
    if bool(_cfg(config, "ai_picker_red_blue_audit", True)) and not analyzed.empty:
        before_audit = analyzed
        try:
            analyzed, audit_records, audit_rejected = await run_audit_stage(
                analyzed,
                config,
                target_date=target_date,
                daily_dir=daily_dir,
                analyzer=analyzer,
                model=portfolio_model,
                market_regime=market_regime,
                dry_run=dry_run,
            )
            pre_filtered_avoid = [*pre_filtered_avoid, *audit_rejected]
            audit_all_rejected = analyzed.empty
            print(f"Pass 1 紅藍軍審計：通過 {len(analyzed)} 檔、淘汰 {len(audit_rejected)} 檔。")
        except Exception as error:  # noqa: BLE001
            analyzed = before_audit
            audit_records = {}
            print(f"[warn] 紅藍軍審計失敗，退回未審計流程：{type(error).__name__}: {error}")

    dossiers = []
    for i, row in enumerate(analyzed.to_dict("records"), start=1):
        sid = str(row["stock_id"])
        dossier = build_dossier(i, row, str(results[sid]["snapshot"]), str(results[sid]["summary"]))
        if sid in audit_records:
            dossier += "\n- [D. 紅藍軍審計與 Python 確定性點位]\n  " + json.dumps(
                audit_records[sid], ensure_ascii=False, default=str
            )
        dossiers.append(dossier)
    portfolio_meta = None
    raw_text = ""
    if dossiers:
        try:
            portfolio_meta = await run_portfolio_selection(
                dossiers,
                max_top=max_top,
                model=portfolio_model,
                market_regime=market_regime,
                selection_mode="trend",
                strategy_context=AUDIT_FINALIZER_CONTEXT if audit_records else "",
                use_vertex_batch=stage2_batch,
                poll_interval=batch_poll,
            )
            raw_text = str(getattr(portfolio_meta, "text", "") or "")
        except Exception as error:  # noqa: BLE001
            message = (
                f"❌ {target_date.isoformat()} AI 彙總選股失敗"
                f"（Vertex AI 模型 `{portfolio_model}` 呼叫異常，為確保決策品質未降級至免費版）："
                f"{type(error).__name__}: {error}"
            )
            print(message)
            if post_discord:
                post_messages_to_discord([message], channel_id, bot_token)
            return 1
        decision = parse_portfolio_json(raw_text, set(analyzed["stock_id"].astype(str)), max_top)
    else:
        decision = {
            "market_light": str((market_regime or {}).get("light", "")),
            "market_comment": (
                NO_PICK_MESSAGE + "（Pass 1 紅藍軍審計全數淘汰）。"
                if audit_all_rejected
                else "今日候選股皆被 Stage 0 硬性排雷攔截，無符合條件之進場標的。"
            ),
            "top_picks": [],
            "watchlist": [],
            "avoid": [],
        }
    if decision is not None:
        decision = enforce_sector_cap(decision, analyzed)
        decision = apply_audit_to_decision(decision, audit_records)
        if bool(_cfg(config, "enable_dual_index_regime_router", False)):
            rq_map = (market_regime or {}).get("regime_quota")
            if isinstance(rq_map, Mapping):
                decision = enforce_regime_quotas(decision, audit_records, rq_map, frame=selected_all)
        decision = merge_pre_filtered_avoid(decision, pre_filtered_avoid)
    portfolio_model_version = str(getattr(portfolio_meta, "model_version", "") or "")

    # 只要有經過 AI 分析，即確保所有完成深度分析的標的皆已生成戰情室 HTML 並發布至 GitHub，
    # 且 war_room_url 確實寫入 00_Watchlist_Master（供後續寫入 AI_Daily_Picks 與 Discord 連結使用）。
    if results:
        watchlist_sheet = cache._get_worksheet("00_Watchlist_Master")
        for sid, res_data in results.items():
            report = res_data.get("report")
            if report is None:
                continue
            need_publish = not bool(getattr(report, "war_room_url", None))
            if need_publish:
                try:
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

    # Phase 3.5: 週五盤後勝率歸因與 False Negative 檢討報告 + 09_Signal_Audit_Log 同步
    audit_store = None
    if bool(_cfg(config, "enable_signal_audit", False)):
        try:
            from signal_audit_service import SignalAuditStore, format_weekly_summary_markdown

            audit_store = SignalAuditStore(_resolve_audit_db_path(config, output_root))
            if target_date.weekday() == 4:
                weekly_summary = audit_store.build_weekly_summary()
                if (weekly_summary.get("settled_trades", 0) or 0) > 0 or (
                    weekly_summary.get("false_negative_rate") is not None
                ):
                    messages.append(
                        format_weekly_summary_markdown(weekly_summary, as_of_str=target_date.isoformat())[
                            :DISCORD_CHUNK_LIMIT
                        ]
                    )
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
            audit_tab = str(_cfg(config, "signal_audit_gsheet_tab_name", "09_Signal_Audit_Log"))
            synced_audit = audit_store.sync_to_gsheet(tab_name=audit_tab)
            if synced_audit > 0:
                print(f"已同步 {synced_audit} 筆訊號留痕至 Google Sheets [{audit_tab}]。")
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 同步訊號留痕至 Google Sheets 失敗：{error}")

    if post_discord:
        sent = post_messages_to_discord(messages, channel_id, bot_token)
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
            rebound_df = enrich_with_taxonomy(rebound_df, config, target_date)
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
    min_ai_count = int(_cfg(config, "rebound_ai_min_candidates", 3))
    max_ai_count = int(_cfg(config, "rebound_ai_max_candidates", 15))
    auto_ai_cfg = bool(_cfg(config, "rebound_auto_run_ai", False))
    if run_ai is not None:
        should_run_ai = bool(run_ai) and total_rebounds > 0
    else:
        should_run_ai = (
            auto_ai_cfg and (min_ai_count <= total_rebounds <= max_ai_count)
        )

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
    ai_failures: list[tuple[str, str]] = []
    ai_messages: list[str] = []

    if should_run_ai and not rebound_df.empty:
        max_candidates = int(_cfg(config, "ai_picker_max_candidates", DEFAULT_MAX_CANDIDATES))
        max_top = int(_cfg(config, "ai_picker_max_top_picks", DEFAULT_MAX_TOP_PICKS))
        stage1_batch, stage2_batch, batch_poll = resolve_batch_settings(config, use_batch)
        portfolio_model = get_vertex_batch_stage2_model() if stage2_batch else get_vertex_stage2_model()
        call_interval = float(_cfg(config, "ai_picker_call_interval_seconds", DEFAULT_CALL_INTERVAL_SECONDS))
        publish_all = bool(_cfg(config, "publish_war_room_for_all_candidates", True))

        selected = rebound_df.head(max_candidates).reset_index(drop=True)
        skipped = max(0, len(rebound_df) - len(selected))
        selected = enrich_with_taxonomy(selected, config, target_date)

        cache = FundamentalCacheManager()
        cache.enable_batch_mode()
        analyzer = FundamentalAnalyzer(cache)

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
                target_date=target_date,
            )

        analyzed = selected.loc[selected["stock_id"].isin(ai_results.keys())].reset_index(drop=True)
        if not analyzed.empty:
            dossiers = [
                build_dossier(
                    i,
                    row,
                    str(ai_results[str(row["stock_id"])]["snapshot"]),
                    str(ai_results[str(row["stock_id"])]["summary"]),
                )
                for i, row in enumerate(analyzed.to_dict("records"), start=1)
            ]
            rebound_context = (
                "本批次為【型態 A：高點急跌回踩長均線（45MA/117MA）反彈清單】。"
                "請特別針對「均值回歸反彈空間（目標 17MA 與 +12% 正乖離）」、「長均線支撐防守力道」、"
                "「法人近 5 日是否趁急跌逢低承接（或連續大賣需降級排雷）」及「自由現金流 FCF 品質」進行嚴選。"
            )
            portfolio_meta = await run_portfolio_selection(
                dossiers,
                max_top=max_top,
                model=portfolio_model,
                market_regime=market_regime,
                strategy_context=rebound_context,
                selection_mode="rebound",
                use_vertex_batch=stage2_batch,
                poll_interval=batch_poll,
            )
            raw_text = str(getattr(portfolio_meta, "text", "") or "")
            decision = parse_portfolio_json(raw_text, set(ai_results.keys()), max_top)
            portfolio_model_version = str(getattr(portfolio_meta, "model_version", "") or "")

            # 只要有經過 AI 分析，即確保所有反彈標的皆生成戰情室 HTML 並發布至 GitHub，且 war_room_url 寫入 00_Watchlist_Master
            if ai_results:
                watchlist_sheet = cache._get_worksheet("00_Watchlist_Master")
                for sid, res_data in ai_results.items():
                    report = res_data.get("report")
                    if report is None:
                        continue
                    need_publish = not bool(getattr(report, "war_room_url", None))
                    if need_publish:
                        try:
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

    if post_discord:
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
        )
        print(f"[rebound] 已推播型態 A 反彈清單快報（{sent_scan} 則訊息）。")
        if ai_messages:
            sent_ai = post_messages_to_discord(ai_messages, channel_id, bot_token)
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
