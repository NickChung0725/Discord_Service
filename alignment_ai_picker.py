"""22:00 pipeline: fundamental + earnings-outlook analysis of the day's alignment_signals,
followed by a single cross-stock AI call that picks the best entry candidates.

Stages (SASD v3.2.0):
1. Load `{date}_alignment_signals.csv`, enrich with streak/new-entry state, cap candidate count.
2. Per stock: incremental FinMind/Sheets sync (FinMind-only rate limiting) + single-stock AI
   diagnosis (`ai_summary_json`, cached by data watermark).
3. One portfolio-level AI call (high-tier model, auto-fallback) over all dossiers.
4. Persist (local JSON/MD, `08_AI_Daily_Picks` sheet) and push to Discord.
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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
from dotenv import load_dotenv

from ai_weather import DEFAULT_GEMINI_MODEL, ask_gemini
from finmind_stock_scanner import (
    DISCORD_API_BASE_URL,
    DISCORD_POST_PAUSE_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    FinMindRequestLimiter,
    _cleanup_old_daily_reports,
    _extract_latest_volume_metrics,
    _load_local_prior_signals,
    _rate_limit_finmind_http_requests,
    _today_in_taipei,
    add_moving_averages,
    build_signal_tracker_rows,
    load_scan_config,
)
from fundamental_analyzer import FundamentalAnalyzer
from fundamental_cache import FundamentalCacheError, FundamentalCacheManager
from fundamental_config import PROMPT_TEMPLATES

PICKS_SHEET_NAME = "08_AI_Daily_Picks"
FINMIND_HOSTS = ("finmindtrade.com",)
DEFAULT_MAX_CANDIDATES = 25
DEFAULT_MAX_TOP_PICKS = 5
DEFAULT_PORTFOLIO_MODEL = "gemini-3.8-flash"
DEFAULT_CALL_INTERVAL_SECONDS = 4.5
DISCORD_CHUNK_LIMIT = 1800

_VERDICT_EMOJI = {"高度可信": "🟢", "待驗證": "🟡", "存疑": "🔴"}
_RANK_EMOJI = {1: "🥇", 2: "🥈", 3: "🥉"}


# --------------------------------------------------------------------------- #
# Stage 1: candidates
# --------------------------------------------------------------------------- #
def load_alignment_candidates(daily_dir: Path, target_date: date) -> pd.DataFrame | None:
    """Return the day's alignment signals, or None when the 19:00 job produced no file."""
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
        rows = build_signal_tracker_rows(
            result, existing_records=prior, as_of=target_date, replace_date=True
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
    return "\n".join(
        [
            f"### 【候選標的 #{index}】{row['stock_id']} {row.get('stock_name', '')}"
            f"（{row.get('market', '')}｜{row.get('industry_category', '')}）",
            "- [A. 均線訊號、位階動態與量能結構]",
            (
                f"  - 收盤價：{_num(row.get('close'))}｜3MA：{_num(row.get('MA3'))}｜17MA：{_num(row.get('MA17'))}"
                f"｜45MA：{_num(row.get('MA45'))}｜117MA：{_num(row.get('MA117'))}｜189MA：{_num(row.get('MA189'))}"
            ),
            (
                f"  - 訊號類型：{row.get('signal', '')}（17MA 與 45MA 差距：{_num(row.get('gap_percent'))}%）"
                f"｜17MA 走勢：{row.get('ma17_trend', '')}"
            ),
            f"  - 量能結構：{_fmt_volume_line(row)}",
            f"  - 進榜狀態：{new_label}｜連續符合天數：{row.get('streak', 1)} 日｜動態：{row.get('daily_status', '')}",
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
) -> tuple[dict[str, dict[str, object]], list[tuple[str, str]]]:
    """Sync + diagnose each candidate; one stock failing never aborts the batch."""
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
            snapshot = await asyncio.to_thread(analyzer.build_quant_snapshot, stock_id)
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


# --------------------------------------------------------------------------- #
# Stage 3: portfolio decision
# --------------------------------------------------------------------------- #
def parse_portfolio_json(raw: str, valid_ids: set[str], max_top: int) -> dict | None:
    """Extract and sanitise the decision JSON; ids outside the candidate list are dropped."""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
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
        "market_comment": str(data.get("market_comment", "")).strip(),
        "top_picks": top,
        "watchlist": watch,
        "avoid": avoid,
    }


async def run_portfolio_selection(
    dossiers: list[str], *, max_top: int, model: str, fallback_model: str
) -> str:
    prompt = (
        PROMPT_TEMPLATES["portfolio_selection"].replace("{max_top}", str(max_top))
        + "\n\n==================================================\n"
        + f"【今日候選資料卡（共 {len(dossiers)} 檔）】\n\n"
        + "\n\n".join(dossiers)
    )
    return await ask_gemini(prompt, model=model, fallback_model=fallback_model)


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
) -> list[str]:
    """Render Discord messages (each <= DISCORD_CHUNK_LIMIT chars)."""
    rows = _row_lookup(frame)
    top_count = len(decision["top_picks"]) if decision else 0
    heading = (
        f"🎯 【22:00 AI 嚴選進場決策報告】{target_date.isoformat()}"
        f"\n候選訊號股 {total_signals} 檔 ➔ 深度分析 {len(results)} 檔 ➔ 嚴選 {top_count} 檔"
        f"｜決策模型：{model}（額度不足時自動降級）"
    )
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
            blocks.append("🏆 今日沒有達標的進場標的（寧缺勿濫）。")
        for pick in decision["top_picks"]:
            stock_id = pick["stock_id"]
            row = rows.get(stock_id, {})
            report = results.get(stock_id, {}).get("report")
            verdict = str(pick.get("outlook_verdict", "無資料"))
            emoji = _VERDICT_EMOJI.get(verdict, "⚪")
            vol_str = _fmt_volume_line(row)
            vol_line = [f"量能：{vol_str}"] if vol_str != "無成交量資料" else []
            blocks.append(
                "\n".join(
                    [
                        f"{_RANK_EMOJI.get(pick['rank'], '🏅')} #{pick['rank']} {name_of(stock_id)}"
                        f"｜推薦分 {pick.get('score', 'N/A')}｜法說展望 {emoji}{verdict}",
                        f"收盤 {_num(row.get('close'))}｜17MA {_num(row.get('MA17'))}（短線支撐）"
                        f"｜45MA {_num(row.get('MA45'))}（波段防守）｜{row.get('signal', '')}"
                        f"｜連 {row.get('streak', 1)} 日",
                        *vol_line,
                        f"理由：{pick.get('thesis', '')}",
                        f"風險：{pick.get('risk', '')}",
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


def post_messages_to_discord(messages: list[str], channel_id: str, bot_token: str) -> int:
    if not channel_id.isdigit():
        raise ValueError("stock_channel_id 必須是 Discord 頻道 ID 數字。")
    if not bot_token.strip():
        raise ValueError("DISCORD_TOKEN (or BOT_TOKEN) is required to post the report.")
    sent = 0
    for index, content in enumerate(messages):
        request = Request(
            f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages",
            data=json.dumps({"content": content, "flags": 4}, ensure_ascii=False).encode("utf-8"),
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
) -> list[dict[str, object]]:
    """Rows for 08_AI_Daily_Picks."""
    rows = _row_lookup(frame)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output: list[dict[str, object]] = []

    def fundamental_score(stock_id: str) -> object:
        summary = str(results.get(stock_id, {}).get("summary", ""))
        try:
            parsed = json.loads(summary)
            return parsed.get("score", "") if isinstance(parsed, dict) else ""
        except (json.JSONDecodeError, ValueError):
            return ""

    def build(category: str, rank: object, item: Mapping[str, object]) -> dict[str, object]:
        stock_id = str(item["stock_id"])
        row = rows.get(stock_id, {})
        report = results.get(stock_id, {}).get("report")
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
            "outlook_verdict": item.get("outlook_verdict", ""),
            "entry_thesis": item.get("thesis", item.get("reason", "")),
            "risk_note": item.get("risk", ""),
            "war_room_url": getattr(report, "war_room_url", "") or "",
            "updated_at": now,
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


async def run_pipeline(
    config: Mapping[str, object],
    *,
    target_date: date,
    force_ai: bool = False,
    dry_run: bool = False,
) -> int:
    output_root = Path(str(config["output_directory"]))
    daily_dir = output_root / "daily"
    max_candidates = int(_cfg(config, "ai_picker_max_candidates", DEFAULT_MAX_CANDIDATES))
    max_top = int(_cfg(config, "ai_picker_max_top_picks", DEFAULT_MAX_TOP_PICKS))
    portfolio_model = str(_cfg(config, "ai_picker_portfolio_model", DEFAULT_PORTFOLIO_MODEL))
    fallback_model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    call_interval = float(_cfg(config, "ai_picker_call_interval_seconds", DEFAULT_CALL_INTERVAL_SECONDS))
    publish_all = bool(_cfg(config, "publish_war_room_for_all_candidates", False))
    post_discord = bool(_cfg(config, "post_ai_picks_to_discord", True)) and not dry_run
    save_gsheet = bool(_cfg(config, "save_ai_picks_to_gsheet", True)) and not dry_run
    channel_id = str(config.get("stock_channel_id", ""))
    bot_token = (os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()

    candidates = load_alignment_candidates(daily_dir, target_date)
    if candidates is None:
        print(f"[skip] 找不到 {target_date.isoformat()}_alignment_signals.csv（休市或 19:00 任務未產出）。")
        return 0
    if candidates.empty:
        message = f"📭 今日 ({target_date.isoformat()}) 無符合強勢均線排列之個股，22:00 AI 財報選股略過。"
        print(message)
        if post_discord:
            post_messages_to_discord([message], channel_id, bot_token)
        return 0

    total_signals = len(candidates)
    enriched = enrich_with_tracker(candidates, daily_dir, target_date)
    selected, skipped = prefilter_candidates(enriched, max_candidates)
    print(f"候選訊號股 {total_signals} 檔，進入深度分析 {len(selected)} 檔（略過 {skipped} 檔）。")

    limiter = FinMindRequestLimiter(
        output_root / ".state" / "finmind_request_history.sqlite3",
        int(config["max_requests_per_hour"]),
        float(config["min_request_interval_seconds"]),
    )
    cache = FundamentalCacheManager()
    cache.enable_batch_mode()
    analyzer = FundamentalAnalyzer(cache)

    with _rate_limit_finmind_http_requests(limiter, only_hosts=FINMIND_HOSTS):
        results, failures = await analyze_candidates(
            selected,
            cache,
            analyzer,
            force_ai=force_ai,
            publish_all=publish_all,
            call_interval=call_interval,
        )

    analyzed = selected.loc[selected["stock_id"].isin(results.keys())].reset_index(drop=True)
    if analyzed.empty:
        message = f"❌ {target_date.isoformat()} 22:00 AI 選股失敗：所有候選股的財報分析皆失敗。"
        print(message)
        if post_discord:
            post_messages_to_discord([message], channel_id, bot_token)
        return 1

    dossiers = [
        build_dossier(i, row, str(results[str(row["stock_id"])]["snapshot"]), str(results[str(row["stock_id"])]["summary"]))
        for i, row in enumerate(analyzed.to_dict("records"), start=1)
    ]
    raw_text = await run_portfolio_selection(
        dossiers, max_top=max_top, model=portfolio_model, fallback_model=fallback_model
    )
    decision = parse_portfolio_json(raw_text, set(results.keys()), max_top)

    # Publish war-room pages only for final picks that do not have one yet.
    if decision and not publish_all:
        for pick in decision["top_picks"]:
            report = results[pick["stock_id"]]["report"]
            if report.war_room_url:
                continue
            try:
                report.war_room_url = await analyzer.render_and_publish_war_room_html(
                    pick["stock_id"], report.ai_text
                )
            except Exception as error:  # noqa: BLE001
                print(f"[warn] {pick['stock_id']} 戰情室發布失敗：{error}")

    messages = format_pick_report(
        target_date,
        decision,
        raw_text,
        analyzed,
        results,
        failures,
        total_signals=total_signals,
        skipped=skipped,
        model=portfolio_model,
    )

    if dry_run:
        print("\n\n".join(messages))
        return 0

    payload = {
        "date": target_date.isoformat(),
        "portfolio_model": portfolio_model,
        "total_signals": total_signals,
        "analyzed": len(results),
        "skipped": skipped,
        "failures": [{"stock_id": sid, "reason": reason} for sid, reason in failures],
        "decision": decision,
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
            picks = pd.DataFrame(build_pick_rows(target_date, decision, analyzed, results))
            if not picks.empty:
                worksheet = cache._get_worksheet(PICKS_SHEET_NAME)  # noqa: SLF001
                cache._upsert_delta(worksheet, picks, key_col="pk_key")  # noqa: SLF001
        except Exception as error:  # noqa: BLE001
            print(f"[warn] 寫入 {PICKS_SHEET_NAME} 失敗：{error}")

    if post_discord:
        sent = post_messages_to_discord(messages, channel_id, bot_token)
        print(f"已推播 AI 嚴選報告（{sent} 則訊息）。")

    _cleanup_old_daily_reports(daily_dir, target_date, int(config["local_retention_days"]))
    return 0


def run_from_cli(
    *,
    config_path: Path,
    target_date: str | None = None,
    force_ai: bool = False,
    dry_run: bool = False,
) -> int:
    config = load_scan_config(config_path)
    parsed_date = date.fromisoformat(target_date) if target_date else _today_in_taipei()
    return asyncio.run(
        run_pipeline(config, target_date=parsed_date, force_ai=force_ai, dry_run=dry_run)
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    load_dotenv()
    parser = argparse.ArgumentParser(description="22:00 alignment_signals 財報分析與 AI 嚴選進場標的")
    parser.add_argument("--config", type=Path, default=Path("finmind_scanner.json"))
    parser.add_argument("--date", help="指定分析日期 YYYY-MM-DD（預設今日）")
    parser.add_argument("--force-ai", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return run_from_cli(
        config_path=args.config,
        target_date=args.date,
        force_ai=args.force_ai,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
