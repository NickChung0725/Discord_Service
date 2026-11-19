"""量化戰情總部資料打包、對答案組裝與 GitHub Pages 單次發布模組 (`services/portal_publisher.py`).

SASD v6.2.0 Step 3：
- 本地為唯一事實來源（Single Source of Truth）：一律從 `data/signal_audit.db`、
  `finmind_reports/daily/*`、`finmind_reports/.state/stock_taxonomy.json` 與
  `finmind_reports/.state/manual_stock_ai_queries.json` 全量重建網站資料包。
- 併發防護：以 `finmind_reports/.state/portal_publish.lock` 跨行程互斥鎖搭配
  `github_uploader.sync_portal_batch()` 的 HTTP 422 衝突重試。
- 30 交易日熱資料保留：僅保留最近 30 個交易日的 `data/daily/YYYY-MM-DD.json`，
  遠端過期每日 JSON 自動刪除；`charts/{stock_id}_fund.html` 永久保留不刪除。
- 發布例外絕不中斷 19:00 / 21:30 / 22:00 / `/stock_ai` 主流程。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
for _sub in (_ROOT, _ROOT / "services", _ROOT / "quant", _ROOT / "taishin"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from github_uploader import get_github_pages_base_url, sync_portal_batch
from signal_audit_service import (
    CATEGORY_AVOID,
    CATEGORY_TOP_PICK,
    CATEGORY_WATCHLIST,
    DEFAULT_AUDIT_DB_PATH,
    FILL_PENDING,
    FILL_PULLBACK,
    FILL_SKIPPED,
    FILL_T1,
    FILL_UNFILLED,
    GRADE_REJECT_TRAP,
    OUTCOME_PENDING,
    OUTCOME_STOP,
    OUTCOME_TIME_EXIT,
    OUTCOME_TP1,
    OUTCOME_TP2,
    OUTCOME_UNFILLED,
    STRATEGY_ALIGNMENT_2130,
    STRATEGY_REBOUND_2200,
    SignalAuditStore,
    parse_plan_price,
)

logger = logging.getLogger(__name__)

TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
DEFAULT_RETENTION_TRADING_DAYS = 30
MANUAL_QUERIES_FILENAME = "manual_stock_ai_queries.json"
LOCK_FILENAME = "portal_publish.lock"
WEB_INDEX_REL_PATH = Path("web") / "index.html"

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_")
_MANUAL_QUERY_STRATEGY_ID = "manual_stock_ai"
_MANUAL_QUERY_CATEGORY = "MANUAL_QUERY"

DEFAULT_STRATEGIES_CATALOG: list[dict[str, str]] = [
    {
        "id": STRATEGY_ALIGNMENT_2130,
        "tab_name": "🔥 21:30 右側順勢強勢股",
        "description": "五線多頭排列 ＋ 基本面績優選拔 ＋ 三階拉回低接",
    },
    {
        "id": STRATEGY_REBOUND_2200,
        "tab_name": "🛡️ 22:00 型態A長均線反彈",
        "description": "高點回跌 ≥10% 回踩 45/117/189MA 支撐 ＋ 錯殺歸因低接",
    },
]

_STRATEGY_META_BY_ID: dict[str, dict[str, str]] = {
    item["id"]: dict(item) for item in DEFAULT_STRATEGIES_CATALOG
}


def wantgoo_url(stock_id: str) -> str:
    return f"https://www.wantgoo.com/stock/{str(stock_id).strip()}/technical-chart"


def war_room_rel_path(stock_id: str) -> str:
    return f"charts/{str(stock_id).strip()}_fund.html"


def _stock_links(stock_id: str) -> dict[str, str]:
    """每檔股票統一僅配置玩股網（唯一外部看盤）＋ 戰情室相對路徑。"""
    return {
        "wantgoo": wantgoo_url(stock_id),
        "war_room": war_room_rel_path(stock_id),
    }


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num == num else None


def _safe_int(value: Any, default: int = 0) -> int:
    num = _safe_float(value)
    return int(round(num)) if num is not None else default


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse JSON %s: %s", path, exc)
        return None


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


@contextmanager
def _portal_publish_lock(lock_path: Path, *, timeout_seconds: float = 120.0) -> Iterator[None]:
    """跨平台檔案鎖（Linux `fcntl.flock` / Windows `msvcrt.locking`），防止 21:30 與 22:00 併發寫入。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")
    acquired = False
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            if fh.tell() == 0 and lock_path.stat().st_size == 0:
                fh.write("0")
                fh.flush()
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"等待戰情總部發布鎖逾時：{lock_path}")
                    time.sleep(0.25)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"等待戰情總部發布鎖逾時：{lock_path}")
                    time.sleep(0.25)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _load_manual_queries(state_dir: Path, *, retention_calendar_days: int = 45) -> dict[str, dict[str, Any]]:
    path = state_dir / MANUAL_QUERIES_FILENAME
    raw = _read_json_file(path) or {}
    cutoff = (datetime.now(TAIPEI_TIMEZONE).date() - timedelta(days=retention_calendar_days)).isoformat()
    cleaned: dict[str, dict[str, Any]] = {}
    for sid, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        queries = [
            q
            for q in (entry.get("queries") or [])
            if isinstance(q, dict) and str(q.get("date") or "") >= cutoff
        ]
        if not queries:
            continue
        cleaned[str(sid)] = {
            "stock_name": str(entry.get("stock_name") or sid),
            "sector": str(entry.get("sector") or ""),
            "war_room_url": str(entry.get("war_room_url") or war_room_rel_path(sid)),
            "queries": queries,
        }
    return cleaned


def record_manual_stock_ai_query(
    stock_id: str,
    *,
    stock_name: str = "",
    sector: str = "",
    score: int | float | None = None,
    war_room_url: str | None = None,
    query_date: date | str | None = None,
    output_root: Path | str = Path("finmind_reports"),
) -> None:
    """將 `/stock_ai` 單股查詢紀錄寫入本地索引，供戰情總部 `stock_search_index` 收錄。"""
    sid = str(stock_id).strip()
    if not sid:
        return
    state_dir = Path(output_root) / ".state"
    lock_path = state_dir / LOCK_FILENAME
    dt_str = (
        query_date.isoformat()
        if isinstance(query_date, date)
        else (str(query_date)[:10] if query_date else datetime.now(TAIPEI_TIMEZONE).date().isoformat())
    )
    score_val = _safe_int(score) if _safe_float(score) is not None else None
    try:
        with _portal_publish_lock(lock_path, timeout_seconds=10.0):
            records = _load_manual_queries(state_dir)
            entry = records.get(sid) or {
                "stock_name": stock_name or sid,
                "sector": sector or "",
                "war_room_url": war_room_rel_path(sid),
                "queries": [],
            }
            if stock_name:
                entry["stock_name"] = stock_name
            if sector:
                entry["sector"] = sector
            if war_room_url:
                entry["war_room_url"] = war_room_rel_path(sid)

            existing = [q for q in entry.get("queries", []) if q.get("date") != dt_str]
            existing.append({"date": dt_str, "score": score_val})
            existing.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
            entry["queries"] = existing[:30]
            records[sid] = entry
            _atomic_write_json(state_dir / MANUAL_QUERIES_FILENAME, records)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record manual /stock_ai query for %s: %s", sid, exc)


def _load_taxonomy_lookup(state_dir: Path, config: Mapping[str, object] | None = None) -> dict[str, dict[str, str]]:
    tax_path_raw = str((config or {}).get("taxonomy_cache_path") or (state_dir / "stock_taxonomy.json"))
    tax_path = Path(tax_path_raw)
    if not tax_path.is_file():
        tax_path = state_dir / "stock_taxonomy.json"
    raw = _read_json_file(tax_path) or {}
    lookup: dict[str, dict[str, str]] = {}
    for sid, val in raw.items():
        if not isinstance(val, dict):
            continue
        lookup[str(sid)] = {
            "stock_name": str(val.get("stock_name") or ""),
            "sector": str(val.get("sub_industry") or val.get("official_industry") or ""),
        }
    return lookup


def _load_csv_rows_by_stock(csv_path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not csv_path.is_file():
        return [], {}
    try:
        df = pd.read_csv(csv_path, dtype={"stock_id": str})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read CSV %s: %s", csv_path, exc)
        return [], {}
    if df.empty or "stock_id" not in df.columns:
        return [], {}
    rows = df.where(pd.notna(df), None).to_dict("records")
    by_sid = {str(r["stock_id"]).strip(): r for r in rows if r.get("stock_id")}
    return rows, by_sid


def _build_actual_verification(
    audit_row: Mapping[str, Any] | None,
    *,
    category: str,
    close_at_pick: float | None = None,
    tier1_price: float | None = None,
) -> dict[str, Any]:
    """將 `signal_audit_log` 單列轉為前端好讀的對答案物件與白話徽章。"""
    if not audit_row:
        return {"is_pending": True, "trading_days_elapsed": 0}

    bars_elapsed = _safe_int(audit_row.get("bars_elapsed"), 0)
    if bars_elapsed <= 0:
        return {"is_pending": True, "trading_days_elapsed": 0}

    fill_status = str(audit_row.get("entry_fill_status") or FILL_PENDING)
    outcome_status = str(audit_row.get("outcome_status_10d") or OUTCOME_PENDING)
    filled_tiers = str(audit_row.get("filled_tiers_text") or "").strip()
    eff_entry = _safe_float(audit_row.get("effective_entry_price"))
    period_low = _safe_float(audit_row.get("period_low_price"))
    period_high = _safe_float(audit_row.get("period_high_price"))
    latest_close = _safe_float(audit_row.get("latest_close_price"))
    net_ret_10d = _safe_float(audit_row.get("net_return_10d_pct"))
    mfe_pct = _safe_float(audit_row.get("mfe_10d_pct"))
    mae_pct = _safe_float(audit_row.get("mae_10d_pct"))
    is_whipsawed = bool(audit_row.get("is_whipsawed"))
    is_fn_miss = bool(audit_row.get("is_false_negative_miss"))
    close_ref = close_at_pick or _safe_float(audit_row.get("close_t"))
    t1_ref = tier1_price or _safe_float(audit_row.get("tier1_entry_price")) or _safe_float(
        audit_row.get("ideal_buy_zone_high")
    )

    # 1. 買點命中徽章
    if fill_status == FILL_PULLBACK:
        tier_label = filled_tiers or "低接區間"
        eff_txt = f"均價 {eff_entry:.1f}" if eff_entry is not None else ""
        entry_badge = f"✅ 命中{tier_label}" + (f" ({eff_txt})" if eff_txt else "")
    elif fill_status == FILL_T1:
        eff_txt = f"均價 {eff_entry:.1f}" if eff_entry is not None else ""
        entry_badge = "✅ T+1 進場成交" + (f" ({eff_txt})" if eff_txt else "")
    elif fill_status == FILL_UNFILLED:
        if period_low is not None and t1_ref is not None and t1_ref > 0 and period_low > t1_ref:
            gap_pct = (period_low - t1_ref) / t1_ref * 100.0
            entry_badge = f"🏃‍♂️ 差 {gap_pct:.1f}% 未到價踏空"
        else:
            entry_badge = "🏃‍♂️ 5 日內未到價踏空"
    elif fill_status == FILL_SKIPPED:
        entry_badge = "⏭️ 開高逾追價上限略過"
    else:
        low_txt = f"，期間最低 {period_low:.1f}" if period_low is not None else ""
        entry_badge = f"⏳ 掛單觀察中 (第 {bars_elapsed} 日{low_txt})"

    # 2. 目標與停損結果徽章
    cat_upper = category.upper()
    if cat_upper == CATEGORY_AVOID:
        chg_pct = (
            round((latest_close - close_ref) / close_ref * 100.0, 2)
            if (latest_close is not None and close_ref is not None and close_ref > 0)
            else None
        )
        if is_fn_miss:
            mfe_txt = f"最高 +{mfe_pct:.1f}%" if mfe_pct is not None else "大漲 >8%"
            outcome_badge = f"🚨 錯殺飆股 ({mfe_txt})"
        elif chg_pct is not None and chg_pct < 0:
            outcome_badge = f"🛡️ 成功避開下跌 ({chg_pct:+.1f}%)"
        elif chg_pct is not None:
            outcome_badge = f"➖ 觀察走勢 ({chg_pct:+.1f}%)"
        else:
            outcome_badge = f"⏳ 追蹤中 (第 {bars_elapsed} 日)"
    elif outcome_status == OUTCOME_TP2:
        hi_txt = f"最高 {period_high:.1f}，" if period_high is not None else ""
        ret_txt = f"TP1 鎖利 {net_ret_10d:+.2f}%" if net_ret_10d is not None else ""
        outcome_badge = f"🚀 達標 TP2 ({hi_txt}{ret_txt})".rstrip("，)") + ")" if (hi_txt or ret_txt) else "🚀 達標 TP2"
    elif outcome_status == OUTCOME_TP1:
        hi_txt = f"最高 {period_high:.1f}，" if period_high is not None else ""
        ret_txt = f"報酬 {net_ret_10d:+.2f}%" if net_ret_10d is not None else ""
        outcome_badge = f"🏆 達標 TP1 ({hi_txt}{ret_txt})" if (hi_txt or ret_txt) else "🏆 達標 TP1"
    elif outcome_status == OUTCOME_STOP and is_whipsawed:
        outcome_badge = "⚡ 洗盤穿價：破停損後反彈達標"
    elif outcome_status == OUTCOME_STOP:
        ret_txt = f" (報酬 {net_ret_10d:+.2f}%)" if net_ret_10d is not None else ""
        outcome_badge = f"🛑 觸及硬停損{ret_txt}"
    elif outcome_status == OUTCOME_TIME_EXIT:
        ret_txt = f" (報酬 {net_ret_10d:+.2f}%)" if net_ret_10d is not None else ""
        outcome_badge = f"⏱️ 10 日到期結算{ret_txt}"
    elif outcome_status == OUTCOME_UNFILLED:
        if period_high is not None and close_ref is not None and close_ref > 0:
            up_pct = (period_high - close_ref) / close_ref * 100.0
            outcome_badge = f"🏃‍♂️ 未成交 (期間最高 {up_pct:+.1f}%)"
        else:
            outcome_badge = "🏃‍♂️ 未成交"
    else:
        cur_txt = f"，現價 {latest_close:.1f}" if latest_close is not None else ""
        outcome_badge = f"⏳ 持有觀察中 (第 {bars_elapsed} 日{cur_txt})"

    # 3. 抱單舒適度徽章 (MFE / MAE)
    pain_badge = ""
    if mae_pct is not None and fill_status in (FILL_PULLBACK, FILL_T1):
        if mae_pct >= -2.0:
            pain_badge = f"🟢 買點極準 (最大套牢僅 {mae_pct:.1f}%)"
        else:
            mfe_part = f" / 最高浮盈 {mfe_pct:+.1f}%" if mfe_pct is not None else ""
            pain_badge = f"📉 最大套牢 {mae_pct:.1f}%{mfe_part}"

    payload: dict[str, Any] = {
        "is_pending": False,
        "is_settled": outcome_status != OUTCOME_PENDING,
        "trading_days_elapsed": bars_elapsed,
        "latest_close": latest_close,
        "period_low": period_low,
        "period_high": period_high,
        "effective_entry_price": eff_entry,
        "entry_fill_status": fill_status,
        "filled_tiers_text": filled_tiers,
        "outcome_status": outcome_status,
        "net_return_10d_pct": round(net_ret_10d, 2) if net_ret_10d is not None else None,
        "entry_result_badge": entry_badge,
        "outcome_badge": outcome_badge,
        "mfe_pct": round(mfe_pct, 2) if mfe_pct is not None else None,
        "mae_pct": round(mae_pct, 2) if mae_pct is not None else None,
        "pain_badge": pain_badge,
        "is_whipsawed": is_whipsawed,
        "is_false_negative_miss": is_fn_miss,
    }
    return payload


def _extract_market_overview(
    market_regime: Mapping[str, Any] | None,
    *,
    recommended_exposure_pct: int | float | None = None,
    us_market_text: str = "",
) -> dict[str, Any]:
    regime = market_regime or {}
    light = str(regime.get("light") or "⚪ 尚無大盤資料")
    status = str(regime.get("status") or "").strip()
    if status and status not in light:
        short_status = status.split("（")[0].strip()
        light_display = f"{light} ({short_status})" if short_status else light
    else:
        light_display = light

    exp_val = recommended_exposure_pct
    if exp_val is None:
        raw_exp = regime.get("recommended_max_exposure_pct")
        if isinstance(raw_exp, (int, float)):
            exp_val = int(round(raw_exp))
        elif isinstance(raw_exp, str):
            nums = re.findall(r"\d+", raw_exp)
            if nums:
                exp_val = int(nums[-1])

    return {
        "light": light_display,
        "regime_state": str((regime.get("regime_quota") or {}).get("regime_state") or ""),
        "recommended_exposure_pct": exp_val,
        "summary": str(regime.get("summary") or status or ""),
        "us_market": us_market_text or str(regime.get("us_market") or ""),
    }


def _build_strategy_from_rec_json(
    strategy_id: str,
    rec_payload: Mapping[str, Any],
    *,
    csv_by_sid: Mapping[str, Mapping[str, Any]],
    audit_by_key: Mapping[tuple[str, str, str], Mapping[str, Any]],
    taxonomy_lookup: Mapping[str, Mapping[str, str]],
    signal_date: str,
) -> dict[str, Any]:
    meta = _STRATEGY_META_BY_ID.get(strategy_id) or {
        "id": strategy_id,
        "tab_name": strategy_id,
        "description": "",
    }
    decision = rec_payload.get("decision")
    if not isinstance(decision, Mapping):
        decision = {"top_picks": [], "watchlist": [], "avoid": [], "market_comment": ""}
    audit_map = rec_payload.get("audit") if isinstance(rec_payload.get("audit"), Mapping) else {}

    def _name_sector(sid: str, item: Mapping[str, Any]) -> tuple[str, str]:
        csv_row = csv_by_sid.get(sid) or {}
        tax_row = taxonomy_lookup.get(sid) or {}
        audit_row = audit_by_key.get((signal_date, strategy_id, sid)) or {}
        sname = str(
            item.get("stock_name")
            or csv_row.get("stock_name")
            or audit_row.get("stock_name")
            or tax_row.get("stock_name")
            or sid
        ).strip()
        sector = str(
            item.get("sector")
            or csv_row.get("sub_industry")
            or audit_row.get("sector_name")
            or tax_row.get("sector")
            or csv_row.get("industry_category")
            or ""
        ).strip()
        return sname, sector

    top_picks_out: list[dict[str, Any]] = []
    for idx, pick in enumerate(decision.get("top_picks") or [], start=1):
        if not isinstance(pick, Mapping):
            continue
        sid = str(pick.get("stock_id") or "").strip()
        if not sid:
            continue
        sname, sector = _name_sector(sid, pick)
        csv_row = csv_by_sid.get(sid) or {}
        audit_rec = (
            pick.get("audit")
            if isinstance(pick.get("audit"), Mapping)
            else (audit_map.get(sid) if isinstance(audit_map.get(sid), Mapping) else {})
        ) or {}
        db_row = audit_by_key.get((signal_date, strategy_id, sid))

        close_at_pick = (
            _safe_float((db_row or {}).get("close_t"))
            or _safe_float(csv_row.get("close"))
        )
        t1_text = str(audit_rec.get("Tier1_Entry_Plan") or "").strip()
        t2_text = str(audit_rec.get("Tier2_Entry_Plan") or "").strip()
        t3_text = str(audit_rec.get("Tier3_Entry_Plan") or "").strip()

        t1_price = (
            _safe_float((db_row or {}).get("tier1_entry_price"))
            or _safe_float(audit_rec.get("Tier1_Entry_Price"))
            or parse_plan_price(t1_text, close_ref=close_at_pick)
        )
        t2_price = (
            _safe_float((db_row or {}).get("tier2_entry_price"))
            or _safe_float(audit_rec.get("Tier2_Entry_Price"))
            or parse_plan_price(t2_text, close_ref=close_at_pick)
        )
        t3_price = (
            _safe_float((db_row or {}).get("tier3_entry_price"))
            or _safe_float(audit_rec.get("Tier3_Entry_Price"))
            or parse_plan_price(t3_text, close_ref=close_at_pick)
        )
        chase_limit = (
            _safe_float((db_row or {}).get("chase_limit_price"))
            or parse_plan_price(audit_rec.get("Chase_Limit_Price"), close_ref=close_at_pick)
            or close_at_pick
        )
        tp1 = (
            _safe_float((db_row or {}).get("tp1_price"))
            or parse_plan_price(audit_rec.get("TP1_Target_Or_POC"), close_ref=close_at_pick)
            or parse_plan_price(pick.get("target_price_1"), close_ref=close_at_pick)
        )
        tp2 = (
            _safe_float((db_row or {}).get("tp2_price"))
            or _safe_float(audit_rec.get("TP2_Target_Price"))
            or parse_plan_price(pick.get("target_price_2"), close_ref=close_at_pick)
        )
        if tp1 is not None and tp2 is not None and tp2 <= tp1:
            tp2 = None
        stop_loss = (
            _safe_float((db_row or {}).get("hard_stop_loss"))
            or parse_plan_price(audit_rec.get("Hard_Stop_Loss"), close_ref=close_at_pick, pick="low")
            or parse_plan_price(pick.get("stop_loss_price"), close_ref=close_at_pick, pick="low")
        )
        tiers_weighted = [
            (w, p)
            for w, p in ((0.30, t1_price), (0.40, t2_price), (0.30, t3_price))
            if p is not None and p > 0
        ]
        weighted_avg_entry = (
            round(sum(w * p for w, p in tiers_weighted) / sum(w for w, _ in tiers_weighted), 2)
            if tiers_weighted
            else close_at_pick
        )

        verdict = str(pick.get("outlook_verdict") or "無資料")
        top_picks_out.append(
            {
                "rank": _safe_int(pick.get("rank"), idx),
                "stock_id": sid,
                "stock_name": sname,
                "sector": sector,
                "score": _safe_int(pick.get("score"), 0),
                "outlook_verdict": verdict,
                "allocation_weight_pct": _safe_float(audit_rec.get("Allocation_Weight_Pct")),
                "tactical_type": str(pick.get("tactical_type") or ""),
                "links": _stock_links(sid),
                "predicted_plan": {
                    "close_at_pick": close_at_pick,
                    "chase_limit": chase_limit,
                    "weighted_avg_entry": weighted_avg_entry,
                    "tier1": {"price": t1_price, "text": t1_text},
                    "tier2": {"price": t2_price, "text": t2_text},
                    "tier3": {"price": t3_price, "text": t3_text},
                    "tp1": tp1,
                    "tp2": tp2,
                    "stop_loss": stop_loss,
                },
                "actual_verification": _build_actual_verification(
                    db_row,
                    category=CATEGORY_TOP_PICK,
                    close_at_pick=close_at_pick,
                    tier1_price=t1_price,
                ),
                "details": {
                    "bull_thesis": str(pick.get("bull_thesis") or pick.get("thesis") or ""),
                    "invalidation_rule": str(pick.get("bear_case_and_invalidation") or pick.get("risk") or ""),
                },
            }
        )

    watchlist_out: list[dict[str, Any]] = []
    for item in decision.get("watchlist") or []:
        if not isinstance(item, Mapping):
            continue
        sid = str(item.get("stock_id") or "").strip()
        if not sid:
            continue
        sname, sector = _name_sector(sid, item)
        csv_row = csv_by_sid.get(sid) or {}
        db_row = audit_by_key.get((signal_date, strategy_id, sid))
        close_at_pick = _safe_float((db_row or {}).get("close_t")) or _safe_float(csv_row.get("close"))
        watchlist_out.append(
            {
                "stock_id": sid,
                "stock_name": sname,
                "sector": sector,
                "reason": str(item.get("reason") or ""),
                "close_at_pick": close_at_pick,
                "actual_verification": _build_actual_verification(
                    db_row,
                    category=CATEGORY_WATCHLIST,
                    close_at_pick=close_at_pick,
                ),
                "links": _stock_links(sid),
            }
        )

    avoid_out: list[dict[str, Any]] = []
    for item in decision.get("avoid") or []:
        if not isinstance(item, Mapping):
            continue
        sid = str(item.get("stock_id") or "").strip()
        if not sid:
            continue
        sname, sector = _name_sector(sid, item)
        csv_row = csv_by_sid.get(sid) or {}
        db_row = audit_by_key.get((signal_date, strategy_id, sid))
        close_at_pick = _safe_float((db_row or {}).get("close_t")) or _safe_float(csv_row.get("close"))
        avoid_out.append(
            {
                "stock_id": sid,
                "stock_name": sname,
                "sector": sector,
                "reason": str(item.get("reason") or ""),
                "close_at_pick": close_at_pick,
                "actual_verification": _build_actual_verification(
                    db_row,
                    category=CATEGORY_AVOID,
                    close_at_pick=close_at_pick,
                ),
                "links": _stock_links(sid),
            }
        )

    has_fn_miss = any(
        bool((a.get("actual_verification") or {}).get("is_false_negative_miss"))
        for a in avoid_out
    )

    return {
        "id": strategy_id,
        "tab_name": meta["tab_name"],
        "status": "READY",
        "status_badge": "",
        "total_candidates": _safe_int(rec_payload.get("total_signals"), len(csv_by_sid)),
        "analyzed_count": _safe_int(rec_payload.get("analyzed"), 0),
        "ai_model": str(rec_payload.get("portfolio_model_version") or rec_payload.get("portfolio_model") or ""),
        "strategy_comment": str(decision.get("market_comment") or ""),
        "recommended_exposure_pct": decision.get("recommended_total_exposure_pct"),
        "has_false_negative_alert": has_fn_miss,
        "top_picks": top_picks_out,
        "watchlist": watchlist_out,
        "avoid": avoid_out,
        "pending_candidates": [],
    }


def _build_pending_strategy_from_csv(
    strategy_id: str,
    csv_rows: Sequence[Mapping[str, Any]],
    *,
    taxonomy_lookup: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """19:00 剛掃完（或 22:00 尚未跑 AI）時，先呈現初選清單並標註等待 AI 審計。"""
    meta = _STRATEGY_META_BY_ID.get(strategy_id) or {
        "id": strategy_id,
        "tab_name": strategy_id,
        "description": "",
    }
    wait_label = "⏳ 等待 21:30 AI 審計" if strategy_id == STRATEGY_ALIGNMENT_2130 else "⏳ 等待 22:00 AI 審計"
    pending_list: list[dict[str, Any]] = []
    for row in csv_rows:
        sid = str(row.get("stock_id") or "").strip()
        if not sid:
            continue
        tax = taxonomy_lookup.get(sid) or {}
        sname = str(row.get("stock_name") or tax.get("stock_name") or sid).strip()
        sector = str(row.get("sub_industry") or tax.get("sector") or row.get("industry_category") or "").strip()
        pending_list.append(
            {
                "stock_id": sid,
                "stock_name": sname,
                "sector": sector,
                "close": _safe_float(row.get("close")),
                "signal": str(row.get("signal") or row.get("rebound_subtype") or ""),
                "vol_status": str(row.get("vol_status") or ""),
                "links": _stock_links(sid),
            }
        )
    return {
        "id": strategy_id,
        "tab_name": meta["tab_name"],
        "status": "PENDING_AI",
        "status_badge": wait_label,
        "total_candidates": len(pending_list),
        "analyzed_count": 0,
        "ai_model": "",
        "strategy_comment": f"{wait_label}：以下為盤後量化初選清單，AI 深度財報與三階低接點位將於排程完成後自動更新。",
        "recommended_exposure_pct": None,
        "has_false_negative_alert": False,
        "top_picks": [],
        "watchlist": [],
        "avoid": [],
        "pending_candidates": pending_list,
    }


def _build_strategy_from_audit_only(
    strategy_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """若本地舊日 recommendations.json 已清理，直接由 `signal_audit.db` 重建對答案清單。"""
    meta = _STRATEGY_META_BY_ID.get(strategy_id) or {
        "id": strategy_id,
        "tab_name": strategy_id,
        "description": "",
    }
    top_picks: list[dict[str, Any]] = []
    watchlist: list[dict[str, Any]] = []
    avoid: list[dict[str, Any]] = []

    for r in rows:
        sid = str(r.get("stock_id") or "").strip()
        if not sid:
            continue
        cat = str(r.get("pick_category") or "").upper()
        if not cat:
            cat = CATEGORY_AVOID if r.get("ai_verdict_grade") == GRADE_REJECT_TRAP else CATEGORY_TOP_PICK
        close_t = _safe_float(r.get("close_t"))
        t1 = _safe_float(r.get("tier1_entry_price")) or _safe_float(r.get("ideal_buy_zone_high"))
        t2 = _safe_float(r.get("tier2_entry_price"))
        t3 = _safe_float(r.get("tier3_entry_price"))

        if cat == CATEGORY_AVOID:
            avoid.append(
                {
                    "stock_id": sid,
                    "stock_name": str(r.get("stock_name") or sid),
                    "sector": str(r.get("sector_name") or ""),
                    "reason": f"【{r.get('ai_verdict_grade', 'REJECT_TRAP')}】系統審計排除",
                    "close_at_pick": close_t,
                    "actual_verification": _build_actual_verification(r, category=CATEGORY_AVOID, close_at_pick=close_t),
                    "links": _stock_links(sid),
                }
            )
        elif cat == CATEGORY_WATCHLIST:
            watchlist.append(
                {
                    "stock_id": sid,
                    "stock_name": str(r.get("stock_name") or sid),
                    "sector": str(r.get("sector_name") or ""),
                    "reason": "次佳觀察追蹤標的",
                    "close_at_pick": close_t,
                    "actual_verification": _build_actual_verification(r, category=CATEGORY_WATCHLIST, close_at_pick=close_t),
                    "links": _stock_links(sid),
                }
            )
        else:
            tiers_weighted = [
                (w, p) for w, p in ((0.30, t1), (0.40, t2), (0.30, t3)) if p is not None and p > 0
            ]
            w_avg = (
                round(sum(w * p for w, p in tiers_weighted) / sum(w for w, _ in tiers_weighted), 2)
                if tiers_weighted
                else close_t
            )
            top_picks.append(
                {
                    "rank": len(top_picks) + 1,
                    "stock_id": sid,
                    "stock_name": str(r.get("stock_name") or sid),
                    "sector": str(r.get("sector_name") or ""),
                    "score": 0,
                    "outlook_verdict": str(r.get("ai_verdict_grade") or ""),
                    "allocation_weight_pct": None,
                    "tactical_type": str(r.get("execution_strategy_type") or ""),
                    "links": _stock_links(sid),
                    "predicted_plan": {
                        "close_at_pick": close_t,
                        "chase_limit": _safe_float(r.get("chase_limit_price")),
                        "weighted_avg_entry": w_avg,
                        "tier1": {"price": t1, "text": f"第1批 ({t1})" if t1 else ""},
                        "tier2": {"price": t2, "text": f"第2批 ({t2})" if t2 else ""},
                        "tier3": {"price": t3, "text": f"第3批 ({t3})" if t3 else ""},
                        "tp1": _safe_float(r.get("tp1_price")),
                        "tp2": _safe_float(r.get("tp2_price")),
                        "stop_loss": _safe_float(r.get("hard_stop_loss")),
                    },
                    "actual_verification": _build_actual_verification(
                        r, category=CATEGORY_TOP_PICK, close_at_pick=close_t, tier1_price=t1
                    ),
                    "details": {"bull_thesis": "", "invalidation_rule": ""},
                }
            )

    return {
        "id": strategy_id,
        "tab_name": meta["tab_name"],
        "status": "READY",
        "status_badge": "",
        "total_candidates": len(rows),
        "analyzed_count": len(rows),
        "ai_model": "",
        "strategy_comment": "",
        "recommended_exposure_pct": None,
        "has_false_negative_alert": any(
            bool((a.get("actual_verification") or {}).get("is_false_negative_miss")) for a in avoid
        ),
        "top_picks": top_picks,
        "watchlist": watchlist,
        "avoid": avoid,
        "pending_candidates": [],
    }


def _compute_stats_for_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def _is_avoid(r: Mapping[str, Any]) -> bool:
        return r.get("ai_verdict_grade") == GRADE_REJECT_TRAP or str(r.get("pick_category") or "").upper() == CATEGORY_AVOID

    def _is_watchlist(r: Mapping[str, Any]) -> bool:
        return str(r.get("pick_category") or "").upper() == CATEGORY_WATCHLIST

    settled = [
        r
        for r in rows
        if not _is_avoid(r)
        and not _is_watchlist(r)
        and r.get("entry_fill_status") in (FILL_T1, FILL_PULLBACK)
        and r.get("outcome_status_10d") in (OUTCOME_TP1, OUTCOME_TP2, OUTCOME_STOP, OUTCOME_TIME_EXIT)
        and _safe_float(r.get("net_return_10d_pct")) is not None
    ]
    returns = [float(r["net_return_10d_pct"]) for r in settled]
    wins = sum(1 for v in returns if v > 0)
    win_rate_pct = round(wins / len(returns) * 100.0, 1) if returns else None
    gains = sum(v for v in returns if v > 0)
    losses = abs(sum(v for v in returns if v < 0))
    if losses > 0:
        pf: float | None = round(gains / losses, 2)
    elif gains > 0:
        pf = 99.0
    else:
        pf = None

    rejects = [r for r in rows if _is_avoid(r) and r.get("outcome_status_10d") != OUTCOME_PENDING]
    fn_rate_pct = (
        round(sum(1 for r in rejects if r.get("is_false_negative_miss")) / len(rejects) * 100.0, 1)
        if rejects
        else None
    )
    whipsaw_count = sum(1 for r in settled if r.get("is_whipsawed"))
    return {
        "settled_trades": len(settled),
        "win_rate_pct": win_rate_pct,
        "profit_factor": pf,
        "false_negative_rate_pct": fn_rate_pct,
        "whipsaw_count": whipsaw_count,
    }


def _build_audit_summary_payload(
    all_audit_rows: Sequence[Mapping[str, Any]],
    *,
    updated_at: str,
) -> dict[str, Any]:
    by_week: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_month: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_strategy: dict[str, list[Mapping[str, Any]]] = defaultdict(list)

    for r in all_audit_rows:
        dt_str = str(r.get("signal_date") or "")[:10]
        if len(dt_str) == 10:
            try:
                d_obj = date.fromisoformat(dt_str)
                iso_y, iso_w, _ = d_obj.isocalendar()
                by_week[f"{iso_y}-W{iso_w:02d}"].append(r)
                by_month[dt_str[:7]].append(r)
            except ValueError:
                pass
        strat = str(r.get("strategy_id") or STRATEGY_ALIGNMENT_2130)
        by_strategy[strat].append(r)

    return {
        "updated_at": updated_at,
        "overall": _compute_stats_for_rows(all_audit_rows),
        "by_strategy": {
            sid: _compute_stats_for_rows(s_rows) for sid, s_rows in sorted(by_strategy.items())
        },
        "weekly": [
            {"period": wk, **_compute_stats_for_rows(w_rows)}
            for wk, w_rows in sorted(by_week.items(), reverse=True)
        ],
        "monthly": [
            {"period": mo, **_compute_stats_for_rows(m_rows)}
            for mo, m_rows in sorted(by_month.items(), reverse=True)
        ],
    }


def _discover_available_dates(
    daily_dir: Path,
    audit_rows: Sequence[Mapping[str, Any]],
    *,
    retention_trading_days: int,
) -> list[str]:
    dates_set: set[str] = set()
    if daily_dir.is_dir():
        for f in daily_dir.iterdir():
            if not f.is_file():
                continue
            name = f.name
            if not name.endswith(
                (
                    "_market_regime.json",
                    "_alignment_signals.csv",
                    "_ai_recommendations.json",
                    "_rebound_signals.csv",
                    "_rebound_ai_recommendations.json",
                )
            ):
                continue
            m = _DATE_PREFIX_RE.match(name)
            if m:
                dates_set.add(m.group(1))
    for r in audit_rows:
        dt_str = str(r.get("signal_date") or "")[:10]
        if len(dt_str) == 10:
            dates_set.add(dt_str)
    sorted_dates = sorted(dates_set, reverse=True)
    return sorted_dates[: max(1, int(retention_trading_days))]


def build_portal_bundle(
    config: Mapping[str, object] | None = None,
    *,
    as_of: date | None = None,
    output_root: Path | None = None,
    audit_db_path: Path | None = None,
    web_index_path: Path | None = None,
    retention_trading_days: int | None = None,
    us_market_summary: str = "",
) -> dict[str, Any]:
    """從本地檔案與 `signal_audit.db` 全量重建戰情總部 JSON 與靜態資產。"""
    cfg = config or {}
    root = output_root or Path(str(cfg.get("output_directory") or "finmind_reports"))
    daily_dir = root / "daily"
    state_dir = root / ".state"
    db_path = audit_db_path or Path(str(cfg.get("signal_audit_db_path") or DEFAULT_AUDIT_DB_PATH))
    retention = int(
        retention_trading_days
        if retention_trading_days is not None
        else (cfg.get("portal_retention_trading_days") or DEFAULT_RETENTION_TRADING_DAYS)
    )
    now_tw = datetime.now(TAIPEI_TIMEZONE)
    updated_at = now_tw.strftime("%Y-%m-%d %H:%M:%S")
    verified_date_str = (as_of or now_tw.date()).isoformat()

    all_audit_rows: list[dict[str, Any]] = []
    if db_path.is_file():
        all_audit_rows = SignalAuditStore(db_path).fetch_all_rows()

    audit_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    audit_by_date_strat: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in all_audit_rows:
        dt_s = str(row.get("signal_date") or "")[:10]
        st_id = str(row.get("strategy_id") or STRATEGY_ALIGNMENT_2130)
        sid = str(row.get("stock_id") or "").strip()
        if dt_s and sid:
            audit_by_key[(dt_s, st_id, sid)] = row
            audit_by_date_strat[(dt_s, st_id)].append(row)

    keep_dates = _discover_available_dates(
        daily_dir, all_audit_rows, retention_trading_days=retention
    )
    taxonomy_lookup = _load_taxonomy_lookup(state_dir, cfg)
    manual_queries = _load_manual_queries(state_dir)

    files_to_publish: dict[str, bytes] = {}
    daily_payloads: dict[str, dict[str, Any]] = {}
    stock_search_index: dict[str, dict[str, Any]] = {}
    discovered_strategy_ids: list[str] = [item["id"] for item in DEFAULT_STRATEGIES_CATALOG]

    def _add_search_appearance(
        sid: str,
        *,
        stock_name: str,
        sector: str,
        dt_s: str,
        strategy_id: str,
        category: str,
        rank: int | None = None,
        score: int | None = None,
    ) -> None:
        entry = stock_search_index.get(sid)
        if entry is None:
            entry = {
                "stock_name": stock_name or sid,
                "sector": sector or "",
                "wantgoo_url": wantgoo_url(sid),
                "war_room_url": war_room_rel_path(sid),
                "appearances": [],
            }
            stock_search_index[sid] = entry
        else:
            if stock_name and entry["stock_name"] == sid:
                entry["stock_name"] = stock_name
            if sector and not entry["sector"]:
                entry["sector"] = sector
        entry["appearances"].append(
            {
                "date": dt_s,
                "strategy_id": strategy_id,
                "category": category,
                "rank": rank,
                "score": score,
            }
        )

    for dt_s in keep_dates:
        regime_data = _read_json_file(daily_dir / f"{dt_s}_market_regime.json")
        align_csv_rows, align_csv_by_sid = _load_csv_rows_by_stock(daily_dir / f"{dt_s}_alignment_signals.csv")
        rebound_csv_rows, rebound_csv_by_sid = _load_csv_rows_by_stock(daily_dir / f"{dt_s}_rebound_signals.csv")
        align_rec = _read_json_file(daily_dir / f"{dt_s}_ai_recommendations.json")
        rebound_rec = _read_json_file(daily_dir / f"{dt_s}_rebound_ai_recommendations.json")

        strategies_for_day: list[dict[str, Any]] = []
        rec_exposure_override: int | float | None = None

        # Strategy 1: alignment_2130
        if align_rec is not None and isinstance(align_rec.get("decision"), Mapping):
            st_obj = _build_strategy_from_rec_json(
                STRATEGY_ALIGNMENT_2130,
                align_rec,
                csv_by_sid=align_csv_by_sid,
                audit_by_key=audit_by_key,
                taxonomy_lookup=taxonomy_lookup,
                signal_date=dt_s,
            )
            strategies_for_day.append(st_obj)
            if st_obj.get("recommended_exposure_pct") is not None:
                rec_exposure_override = st_obj["recommended_exposure_pct"]
            if regime_data is None and isinstance(align_rec.get("market_regime"), Mapping):
                regime_data = dict(align_rec["market_regime"])
        elif align_csv_rows:
            strategies_for_day.append(
                _build_pending_strategy_from_csv(
                    STRATEGY_ALIGNMENT_2130, align_csv_rows, taxonomy_lookup=taxonomy_lookup
                )
            )
        elif audit_by_date_strat.get((dt_s, STRATEGY_ALIGNMENT_2130)):
            strategies_for_day.append(
                _build_strategy_from_audit_only(
                    STRATEGY_ALIGNMENT_2130, audit_by_date_strat[(dt_s, STRATEGY_ALIGNMENT_2130)]
                )
            )

        # Strategy 2: rebound_2200
        if rebound_rec is not None and isinstance(rebound_rec.get("decision"), Mapping):
            st_obj = _build_strategy_from_rec_json(
                STRATEGY_REBOUND_2200,
                rebound_rec,
                csv_by_sid=rebound_csv_by_sid,
                audit_by_key=audit_by_key,
                taxonomy_lookup=taxonomy_lookup,
                signal_date=dt_s,
            )
            strategies_for_day.append(st_obj)
            if rec_exposure_override is None and st_obj.get("recommended_exposure_pct") is not None:
                rec_exposure_override = st_obj["recommended_exposure_pct"]
            if regime_data is None and isinstance(rebound_rec.get("market_regime"), Mapping):
                regime_data = dict(rebound_rec["market_regime"])
        elif rebound_csv_rows:
            strategies_for_day.append(
                _build_pending_strategy_from_csv(
                    STRATEGY_REBOUND_2200, rebound_csv_rows, taxonomy_lookup=taxonomy_lookup
                )
            )
        elif audit_by_date_strat.get((dt_s, STRATEGY_REBOUND_2200)):
            strategies_for_day.append(
                _build_strategy_from_audit_only(
                    STRATEGY_REBOUND_2200, audit_by_date_strat[(dt_s, STRATEGY_REBOUND_2200)]
                )
            )

        # 未來擴充策略：若 signal_audit.db 有其他 strategy_id，自動呈現新分頁
        handled_ids = {s["id"] for s in strategies_for_day}
        for (row_dt, row_strat), s_rows in sorted(audit_by_date_strat.items()):
            if row_dt == dt_s and row_strat not in handled_ids:
                strategies_for_day.append(_build_strategy_from_audit_only(row_strat, s_rows))
                if row_strat not in discovered_strategy_ids:
                    discovered_strategy_ids.append(row_strat)

        # 更新個股 30 日搜尋索引
        for st_obj in strategies_for_day:
            st_id = str(st_obj["id"])
            for p in st_obj.get("top_picks") or []:
                _add_search_appearance(
                    str(p["stock_id"]),
                    stock_name=str(p.get("stock_name") or ""),
                    sector=str(p.get("sector") or ""),
                    dt_s=dt_s,
                    strategy_id=st_id,
                    category=CATEGORY_TOP_PICK,
                    rank=p.get("rank"),
                    score=p.get("score"),
                )
            for w in st_obj.get("watchlist") or []:
                _add_search_appearance(
                    str(w["stock_id"]),
                    stock_name=str(w.get("stock_name") or ""),
                    sector=str(w.get("sector") or ""),
                    dt_s=dt_s,
                    strategy_id=st_id,
                    category=CATEGORY_WATCHLIST,
                )
            for a in st_obj.get("avoid") or []:
                _add_search_appearance(
                    str(a["stock_id"]),
                    stock_name=str(a.get("stock_name") or ""),
                    sector=str(a.get("sector") or ""),
                    dt_s=dt_s,
                    strategy_id=st_id,
                    category=CATEGORY_AVOID,
                )
            for c in st_obj.get("pending_candidates") or []:
                _add_search_appearance(
                    str(c["stock_id"]),
                    stock_name=str(c.get("stock_name") or ""),
                    sector=str(c.get("sector") or ""),
                    dt_s=dt_s,
                    strategy_id=st_id,
                    category="PENDING_SCAN",
                )

        day_payload = {
            "date": dt_s,
            "last_verified_date": verified_date_str,
            "market_overview": _extract_market_overview(
                regime_data,
                recommended_exposure_pct=rec_exposure_override,
                us_market_text=us_market_summary if dt_s == keep_dates[0] else "",
            ),
            "strategies": strategies_for_day,
        }
        daily_payloads[dt_s] = day_payload
        files_to_publish[f"data/daily/{dt_s}.json"] = json.dumps(
            day_payload, ensure_ascii=False, indent=2, default=str
        ).encode("utf-8")

    # 併入近 30 天 `/stock_ai` 手動查詢紀錄
    for sid, m_entry in manual_queries.items():
        tax = taxonomy_lookup.get(sid) or {}
        sname = str(m_entry.get("stock_name") or tax.get("stock_name") or sid)
        sector = str(m_entry.get("sector") or tax.get("sector") or "")
        for q in m_entry.get("queries") or []:
            _add_search_appearance(
                sid,
                stock_name=sname,
                sector=sector,
                dt_s=str(q.get("date") or ""),
                strategy_id=_MANUAL_QUERY_STRATEGY_ID,
                category=_MANUAL_QUERY_CATEGORY,
                rank=None,
                score=q.get("score"),
            )

    for entry in stock_search_index.values():
        entry["appearances"].sort(key=lambda x: str(x.get("date") or ""), reverse=True)

    keep_dates_set = set(keep_dates)
    rolling_rows = [r for r in all_audit_rows if str(r.get("signal_date") or "")[:10] in keep_dates_set]
    rolling_stats = _compute_stats_for_rows(rolling_rows)

    strategies_catalog = [
        _STRATEGY_META_BY_ID.get(sid, {"id": sid, "tab_name": sid, "description": ""})
        for sid in discovered_strategy_ids
    ]

    manifest_payload = {
        "updated_at": updated_at,
        "available_dates": keep_dates,
        "strategies_catalog": strategies_catalog,
        "rolling_30d_summary": {
            "settled_trades": rolling_stats["settled_trades"],
            "win_rate_pct": rolling_stats["win_rate_pct"],
            "profit_factor": rolling_stats["profit_factor"],
            "false_negative_rate_pct": rolling_stats["false_negative_rate_pct"],
        },
        "stock_search_index": dict(sorted(stock_search_index.items())),
    }
    audit_summary_payload = _build_audit_summary_payload(all_audit_rows, updated_at=updated_at)

    files_to_publish["data/manifest.json"] = json.dumps(
        manifest_payload, ensure_ascii=False, indent=2, default=str
    ).encode("utf-8")
    files_to_publish["data/audit_summary.json"] = json.dumps(
        audit_summary_payload, ensure_ascii=False, indent=2, default=str
    ).encode("utf-8")

    # 若 web/index.html 存在，自動納入批次同步（未變更時由 Blob SHA 比對自動略過）
    resolved_index = web_index_path or (_ROOT / WEB_INDEX_REL_PATH)
    if resolved_index.is_file():
        files_to_publish["index.html"] = resolved_index.read_bytes()

    return {
        "keep_dates": keep_dates,
        "manifest": manifest_payload,
        "audit_summary": audit_summary_payload,
        "daily_payloads": daily_payloads,
        "files": files_to_publish,
    }


def _write_local_portal_mirror(portal_dir: Path, files: Mapping[str, bytes], keep_dates: set[str]) -> None:
    portal_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        target = portal_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, target)
    local_daily_dir = portal_dir / "data" / "daily"
    if local_daily_dir.is_dir():
        for f in local_daily_dir.glob("*.json"):
            if f.stem not in keep_dates:
                try:
                    f.unlink()
                except OSError:
                    pass


def publish_portal_data(
    config: Mapping[str, object] | None = None,
    *,
    as_of: date | None = None,
    output_root: Path | None = None,
    audit_db_path: Path | None = None,
    web_index_path: Path | None = None,
    retention_trading_days: int | None = None,
    us_market_summary: str = "",
    commit_message: str | None = None,
    dry_run: bool = False,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """以本地檔案鎖保護，全量重建戰情總部 JSON 並透過 GitHub Git Trees API 單次發布。"""
    cfg = config or {}
    if not bool(cfg.get("enable_portal_publish", True)):
        return {"skipped": True, "reason": "enable_portal_publish=false"}

    root = output_root or Path(str(cfg.get("output_directory") or "finmind_reports"))
    lock_path = root / ".state" / LOCK_FILENAME
    portal_mirror_dir = root / "portal"
    now_str = (as_of or datetime.now(TAIPEI_TIMEZONE).date()).isoformat()
    resolved_msg = commit_message or f"Update quant portal data ({now_str})"

    try:
        with _portal_publish_lock(lock_path):
            bundle = build_portal_bundle(
                cfg,
                as_of=as_of,
                output_root=root,
                audit_db_path=audit_db_path,
                web_index_path=web_index_path,
                retention_trading_days=retention_trading_days,
                us_market_summary=us_market_summary,
            )
            keep_dates_set = set(bundle["keep_dates"])
            _write_local_portal_mirror(portal_mirror_dir, bundle["files"], keep_dates_set)

            if dry_run:
                return {
                    "dry_run": True,
                    "dates_count": len(bundle["keep_dates"]),
                    "files_count": len(bundle["files"]),
                    "mirror_dir": str(portal_mirror_dir),
                    "portal_url": get_github_pages_base_url(),
                }

            sync_res = sync_portal_batch(
                bundle["files"],
                keep_daily_dates=keep_dates_set,
                commit_message=resolved_msg,
            )
            return {
                "dry_run": False,
                "dates_count": len(bundle["keep_dates"]),
                "mirror_dir": str(portal_mirror_dir),
                **sync_res,
            }
    except Exception as exc:  # noqa: BLE001
        if raise_on_error:
            raise
        logger.warning("戰情總部發布失敗（不影響主流程）：%s", exc)
        print(f"[warn] 戰情總部發布失敗（不影響主流程）：{exc}")
        return {"error": str(exc)}
