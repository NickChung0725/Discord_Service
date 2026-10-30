"""全市場盤後快照預篩器 (MarketUniversePrefilter) 與 細分產業同業知識庫服務 (TaxonomyService)。"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import gspread
import pandas as pd
import requests

from ai_service import ask_gemini_with_meta
from fundamental_config import SHEET_SCHEMAS

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
FOUR_DIGIT_RE = re.compile(r"^\d{4}$")

VALID_SECTOR_GROUPS = (
    "半導體",
    "電子硬體與零組件",
    "軟體、網通與資安",
    "傳產製造與原物料",
    "生技醫療與綠能",
    "金融、航運與內需服務",
)

VALID_COMPONENT_NATURES = (
    "主動元件",
    "被動元件",
    "機構與模組周邊",
    "系統與終端設備",
    "非電子/不適用",
)

VALID_SUPPLY_CHAIN_ROLES = (
    "上游(IP/材料/晶圓/原物料)",
    "中游(製造/封測/零組件/加工)",
    "下游(品牌/系統組裝/通路/終端服務)",
    "垂直整合(IDM/一條龍)",
)

TAXONOMY_SHEET_NAME = "08_Stock_Taxonomy"
TAXONOMY_HEADERS = list(SHEET_SCHEMAS[TAXONOMY_SHEET_NAME])


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _parse_int_clean(val: object) -> int:
    if val is None:
        return 0
    text = str(val).replace(",", "").strip()
    if not text or text in ("--", "-", "N/A"):
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _parse_float_clean(val: object) -> float:
    if val is None:
        return 0.0
    text = str(val).replace(",", "").strip()
    if not text or text in ("--", "-", "N/A"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# Part 1: TWSE / TPEx 全市場當日收盤快照與雙軌流動性預篩器
# --------------------------------------------------------------------------- #
def fetch_twse_tpex_daily_snapshot(timeout: int = 15) -> dict[str, dict[str, float]]:
    """從證交所與櫃買中心 OpenAPI 抓取當日全市場收盤快照（0 FinMind 額度，耗時約 2 秒）。

    回傳格式:
    {
        "2330": {
            "vol_lots": 35200.0,
            "turnover_twd": 38000000000.0,
            "close": 1080.0,
            "market": "上市",
        },
        ...
    }
    """
    snapshot: dict[str, dict[str, float]] = {}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DiscordStockAssistant/5.0)"}

    # 1. 上市 (TWSE) 全市場當日收盤表
    twse_url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX_ALL"
    try:
        resp = requests.get(twse_url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        for row in resp.json():
            code = str(row.get("Code", "")).strip()
            if not FOUR_DIGIT_RE.match(code):
                continue
            shares = _parse_int_clean(row.get("TradeVolume"))
            turnover = _parse_float_clean(row.get("TradeValue"))
            close_price = _parse_float_clean(row.get("ClosingPrice"))
            if turnover <= 0 and shares > 0 and close_price > 0:
                turnover = shares * close_price
            snapshot[code] = {
                "vol_lots": round(shares / 1000.0, 2),
                "turnover_twd": turnover,
                "close": close_price,
                "market": "上市",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("TWSE OpenAPI 快照抓取失敗: %s", exc)

    # 2. 上櫃 (TPEx) 全市場當日收盤表
    tpex_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    try:
        resp = requests.get(tpex_url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        for row in resp.json():
            code = str(row.get("SecuritiesCompanyCode", "")).strip()
            if not FOUR_DIGIT_RE.match(code):
                continue
            shares = _parse_int_clean(row.get("TradingShares"))
            turnover = _parse_float_clean(row.get("TransactionAmount"))
            close_price = _parse_float_clean(row.get("Close"))
            if turnover <= 0 and shares > 0 and close_price > 0:
                turnover = shares * close_price
            snapshot[code] = {
                "vol_lots": round(shares / 1000.0, 2),
                "turnover_twd": turnover,
                "close": close_price,
                "market": "上櫃",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("TPEx OpenAPI 快照抓取失敗: %s", exc)

    return snapshot


def load_universe_registry(registry_path: Path) -> dict[str, Any]:
    if registry_path.is_file():
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("stocks"), dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    return {"updated_at": "", "last_snapshot_date": "", "stocks": {}}


def update_registry_with_daily_snapshot(
    registry_path: Path,
    stock_info: pd.DataFrame,
    snapshot: Mapping[str, Mapping[str, float]],
    as_of: date,
    delisted_threshold: int = 5,
) -> dict[str, Any]:
    """用當日 TWSE/TPEx 快照更新本地 20 日滾動成交量與下市偵測計數器。"""
    reg = load_universe_registry(registry_path)
    stocks_map: dict[str, dict[str, Any]] = reg.setdefault("stocks", {})
    today_iso = as_of.isoformat()
    is_new_day = reg.get("last_snapshot_date") != today_iso

    # 若快照總檔數 < 800，代表當日休市或官網 API 異常，不遞增 missing_count
    snapshot_valid = len(snapshot) >= 800
    if not snapshot_valid:
        return reg

    info_df = stock_info.drop_duplicates(subset=["stock_id"])
    for row in info_df.itertuples(index=False):
        sid = str(row.stock_id).strip()
        if not FOUR_DIGIT_RE.match(sid):
            continue
        entry = stocks_map.setdefault(
            sid,
            {
                "stock_name": str(row.stock_name).strip(),
                "industry_category": str(row.industry_category).strip(),
                "status": "ACTIVE",
                "missing_count": 0,
                "vol_history_20d": [],
                "vol_ma20_lots": 0.0,
                "latest_vol_lots": 0.0,
                "latest_turnover_twd": 0.0,
                "last_seen": "",
            },
        )
        entry["stock_name"] = str(row.stock_name).strip()
        entry["industry_category"] = str(row.industry_category).strip()

        snap_item = snapshot.get(sid)
        if snap_item is not None and snap_item.get("close", 0.0) > 0:
            vol_lots = float(snap_item.get("vol_lots", 0.0))
            turnover = float(snap_item.get("turnover_twd", 0.0))
            entry["status"] = "ACTIVE"
            entry["missing_count"] = 0
            entry["latest_vol_lots"] = vol_lots
            entry["latest_turnover_twd"] = turnover
            entry["last_seen"] = today_iso
            hist: list[float] = list(entry.get("vol_history_20d") or [])
            if is_new_day or not hist:
                hist.append(vol_lots)
            else:
                hist[-1] = vol_lots
            hist = hist[-20:]
            entry["vol_history_20d"] = hist
            entry["vol_ma20_lots"] = round(sum(hist) / len(hist), 1) if hist else vol_lots
        else:
            if is_new_day and entry.get("status") != "DELISTED":
                entry["missing_count"] = int(entry.get("missing_count", 0)) + 1
                if entry["missing_count"] >= delisted_threshold:
                    entry["status"] = "DELISTED"
                    entry["reason"] = f"連續 {entry['missing_count']} 個交易日無收盤報價（自動判定下市/終止買賣）"

    reg["updated_at"] = datetime.now(TAIPEI_TZ).isoformat()
    reg["last_snapshot_date"] = today_iso
    _atomic_write_json(registry_path, reg)
    return reg


DEFAULT_REGISTRY_PATH = Path("finmind_reports/.state/stock_universe_registry.json")


def load_registry_stock_map(
    registry_path: Path = DEFAULT_REGISTRY_PATH,
) -> dict[str, str]:
    """從 stock_universe_registry.json 取得 {stock_id: stock_name}，供同業代號防幻覺校驗（無網路消耗）。"""
    stocks = load_universe_registry(registry_path).get("stocks", {})
    return {
        str(sid): str(info.get("stock_name", sid))
        for sid, info in stocks.items()
        if isinstance(info, Mapping) and info.get("status") != "DELISTED"
    }


def passes_dual_track_liquidity(
    stock_id: str,
    snapshot: Mapping[str, Mapping[str, float]],
    registry_stocks: Mapping[str, Mapping[str, Any]],
    *,
    min_lots: int = 1000,
    min_turnover_twd: float = 100_000_000.0,
    mode: str = "today_or_ma20_or_turnover",
) -> bool:
    """雙軌流動性守門員：排除已下市與低量殭屍股，同時保護中高價績優股與回踩量縮股。"""
    reg_info = registry_stocks.get(stock_id, {})
    if reg_info.get("status") == "DELISTED":
        return False

    snap_valid = len(snapshot) >= 800
    snap_item = snapshot.get(stock_id)

    if snap_valid:
        if snap_item is None or snap_item.get("close", 0.0) <= 0:
            # 當日無收盤價（已下市或暫停交易）
            return False
        today_vol = float(snap_item.get("vol_lots", 0.0))
        today_turnover = float(snap_item.get("turnover_twd", 0.0))
    else:
        # 當日官方快照連線失敗時，Fallback 使用本地 Registry 快取
        if reg_info.get("status") == "DORMANT":
            return False
        if not reg_info:
            return True
        today_vol = float(reg_info.get("latest_vol_lots", 0.0))
        today_turnover = float(reg_info.get("latest_turnover_twd", 0.0))

    ma20_vol = float(reg_info.get("vol_ma20_lots", 0.0))

    if mode == "today":
        return today_vol >= min_lots
    if mode == "today_or_ma20":
        return today_vol >= min_lots or ma20_vol >= min_lots
    # 預設 "today_or_ma20_or_turnover"
    return (
        today_vol >= min_lots
        or ma20_vol >= min_lots
        or today_turnover >= min_turnover_twd
    )


# --------------------------------------------------------------------------- #
# Part 2: Yahoo 台股業務簡介抓取 + Gemini Lite 細分產業與同業知識庫
# --------------------------------------------------------------------------- #
def fetch_yahoo_company_profile(stock_id: str, timeout: int = 8) -> str:
    """從 Yahoo 奇摩股市抓取公司『主要經營業務』簡述（250字內），作為 Gemini Lite 的 RAG 依據。"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for suffix in (".TW", ".TWO"):
        url = f"https://tw.stock.yahoo.com/quote/{stock_id}{suffix}/profile"
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                continue
            text = html.unescape(resp.text)
            # 嘗試擷取 meta description 或「主要經營業務」段落
            meta_match = re.search(
                r'<meta\s+name="description"\s+content="([^"]+)"', text
            )
            desc_parts: list[str] = []
            if meta_match:
                desc_parts.append(meta_match.group(1).strip())
            biz_match = re.search(
                r"主要經營業務[</\w\s\"=-]*?>([^<]{10,300})<", text
            )
            if biz_match:
                desc_parts.append(biz_match.group(1).strip())
            combined = "｜".join(desc_parts).strip()
            if combined:
                return combined[:250]
        except Exception:  # noqa: BLE001
            continue
    return ""


def build_taxonomy_prompt(
    stock_id: str,
    stock_name: str,
    official_industry: str,
    business_summary: str = "",
) -> str:
    biz_line = f"\n- 公開主要業務與產品摘要：{business_summary}" if business_summary else ""
    return f"""你是一位精通台灣上市櫃供應鏈的資深產業研究員。請針對台股「{stock_id} {stock_name}」進行細分產業與同業競爭對手定義。
- 證交所官方分類：{official_industry}{biz_line}

請嚴格輸出單一 JSON 物件（不要加上 Markdown 語法），必須包含以下欄位：
{{
  "sector_group": "請從 {list(VALID_SECTOR_GROUPS)} 擇一",
  "sub_industry": "具體細分次產業名稱（15字內，例如：被動元件-MLCC與晶片電阻、IC設計-電源管理IC、PCB-ABF載板、貨櫃航運、重電設備與變壓器）",
  "component_nature": "請嚴格從 {list(VALID_COMPONENT_NATURES)} 擇一。判準：IC/二極體/MOSFET/雷射等需外部電源主動控制訊號者選『主動元件』；MLCC電容/電阻/電感/石英元件等選『被動元件』；PCB/散熱/機殼/連接器/電源供應器選『機構與模組周邊』；整機/伺服器/設備廠選『系統與終端設備』；傳產/航運/金融/生技/純軟體等非電子硬體選『非電子/不適用』",
  "supply_chain_role": "請從 {list(VALID_SUPPLY_CHAIN_ROLES)} 擇一",
  "core_products": "主力產品或核心業務（以頓號分隔，35字內）",
  "theme_tags": ["核心題材標籤1", "核心題材標籤2"],
  "peers": ["同業4碼代號1 同業股名1", "同業4碼代號2 同業股名2", "同業4碼代號3 同業股名3"]
}}
注意：peers 必須為台灣上市櫃真實存在的 2~4 家最直接競爭同業（4碼數字代號+空白+股名）。"""


def sanitize_taxonomy_record(
    raw_data: Mapping[str, Any],
    *,
    stock_id: str,
    stock_name: str,
    official_industry: str,
    valid_stock_map: Mapping[str, str],
    as_of: date | None = None,
) -> dict[str, Any]:
    """對 AI 回傳的分類結果執行 Enum 防呆與真實台股代號防幻覺清洗。"""
    today_iso = (as_of or datetime.now(TAIPEI_TZ).date()).isoformat()

    sector_group = str(raw_data.get("sector_group", "")).strip()
    if sector_group not in VALID_SECTOR_GROUPS:
        if "半導體" in official_industry:
            sector_group = "半導體"
        elif any(k in official_industry for k in ("電子", "電腦", "光電", "通信")):
            sector_group = "電子硬體與零組件"
        elif any(k in official_industry for k in ("軟體", "資訊", "數位")):
            sector_group = "軟體、網通與資安"
        elif any(k in official_industry for k in ("生技", "綠能", "環保")):
            sector_group = "生技醫療與綠能"
        elif any(k in official_industry for k in ("金融", "航運", "貿易", "居家", "觀光")):
            sector_group = "金融、航運與內需服務"
        else:
            sector_group = "傳產製造與原物料"

    component_nature = str(raw_data.get("component_nature", "")).strip()
    if component_nature not in VALID_COMPONENT_NATURES:
        if "半導體" in official_industry:
            component_nature = "主動元件"
        elif any(k in official_industry for k in ("電子", "電腦", "光電", "通信")):
            component_nature = "機構與模組周邊"
        else:
            component_nature = "非電子/不適用"

    supply_chain_role = str(raw_data.get("supply_chain_role", "")).strip()
    if supply_chain_role not in VALID_SUPPLY_CHAIN_ROLES:
        supply_chain_role = "中游(製造/封測/零組件/加工)"

    sub_industry = str(raw_data.get("sub_industry", "")).strip() or official_industry
    core_products = str(raw_data.get("core_products", "")).strip()[:60]

    raw_tags = raw_data.get("theme_tags")
    if isinstance(raw_tags, str):
        try:
            raw_tags = json.loads(raw_tags)
        except Exception:  # noqa: BLE001
            raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    theme_tags = [str(t).strip() for t in (raw_tags or []) if str(t).strip()][:3]

    # 防幻覺校驗 peers：必須存在於 valid_stock_map 且不可為自己
    raw_peers = raw_data.get("peers")
    if isinstance(raw_peers, str):
        try:
            raw_peers = json.loads(raw_peers)
        except Exception:  # noqa: BLE001
            raw_peers = [p.strip() for p in raw_peers.split(",") if p.strip()]
    verified_peers: list[str] = []
    seen_codes: set[str] = {stock_id}
    for item in raw_peers or []:
        match = re.search(r"\b(\d{4})\b", str(item))
        if not match:
            continue
        code = match.group(1)
        if code not in seen_codes and code in valid_stock_map:
            seen_codes.add(code)
            verified_peers.append(f"{code} {valid_stock_map[code]}")
        if len(verified_peers) >= 4:
            break

    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "official_industry": official_industry,
        "sector_group": sector_group,
        "sub_industry": sub_industry,
        "component_nature": component_nature,
        "supply_chain_role": supply_chain_role,
        "core_products": core_products,
        "theme_tags": json.dumps(theme_tags, ensure_ascii=False),
        "peers": json.dumps(verified_peers, ensure_ascii=False),
        "updated_at": today_iso,
    }


def format_taxonomy_badge(rec: Mapping[str, Any] | None) -> str:
    """產生供 Discord 報表與戰情室嵌入的單行標籤字串。"""
    if not rec:
        return ""
    nature = str(rec.get("component_nature", "")).strip()
    sub_ind = str(rec.get("sub_industry", "")).strip()
    products = str(rec.get("core_products", "")).strip()
    peers_raw = rec.get("peers", "[]")
    if isinstance(peers_raw, str):
        try:
            peers_list = json.loads(peers_raw)
        except Exception:  # noqa: BLE001
            peers_list = []
    elif isinstance(peers_raw, list):
        peers_list = peers_raw
    else:
        peers_list = []

    nature_tag = f"【{nature}】" if nature and nature != "非電子/不適用" else ""
    prod_part = f"（{products}）" if products else ""
    peers_str = "、".join(str(p) for p in peers_list[:4]) if peers_list else "無直接對標"
    return f"🏷️ 細分產業：{nature_tag}{sub_ind}{prod_part} ｜ 🔗 相似同業：{peers_str}"


class TaxonomyService:
    """管理本地 stock_taxonomy.json 與 Google Sheets [08_Stock_Taxonomy] 雙向快取與 AI 生成。"""

    def __init__(
        self,
        cache_path: Path = Path("finmind_reports/.state/stock_taxonomy.json"),
        ttl_days: int = 90,
        sheet_tab_name: str = TAXONOMY_SHEET_NAME,
    ) -> None:
        self.cache_path = cache_path
        self.ttl_days = ttl_days
        self.sheet_tab_name = sheet_tab_name
        self._records: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def load(self) -> dict[str, dict[str, Any]]:
        if self._loaded:
            return self._records
        if self.cache_path.is_file():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._records = data
                    self._loaded = True
                    return self._records
            except Exception:  # noqa: BLE001
                logger.warning("Failed to load local taxonomy JSON, trying Google Sheets")
        # Fallback: 從 Google Sheets 08_Stock_Taxonomy 拉取重建
        self._pull_from_gsheet()
        self._loaded = True
        return self._records

    def save_local(self) -> None:
        _atomic_write_json(self.cache_path, self._records)

    def _is_fresh(self, rec: Mapping[str, Any], as_of: date) -> bool:
        updated_str = str(rec.get("updated_at", "")).strip()
        if not updated_str or not rec.get("sub_industry"):
            return False
        try:
            updated_date = date.fromisoformat(updated_str[:10])
            return (as_of - updated_date).days <= self.ttl_days
        except ValueError:
            return False

    def get_cached(self, stock_id: str) -> dict[str, Any] | None:
        self.load()
        return self._records.get(str(stock_id).strip())

    def get_or_create_sync(
        self,
        stock_id: str,
        stock_name: str = "",
        official_industry: str = "",
        valid_stock_map: Mapping[str, str] | None = None,
        as_of: date | None = None,
        sync_sheet: bool = False,
    ) -> dict[str, Any]:
        """同步取得單股細分產業與同業資訊（若快取未命中則呼叫免費版 Gemini Lite）。"""
        self.load()
        sid = str(stock_id).strip()
        today = as_of or datetime.now(TAIPEI_TZ).date()
        existing = self._records.get(sid)
        if existing and self._is_fresh(existing, today):
            return existing

        s_name = stock_name or (existing or {}).get("stock_name", "") or (valid_stock_map or {}).get(sid, sid)
        off_ind = official_industry or (existing or {}).get("official_industry", "其他")
        biz_desc = fetch_yahoo_company_profile(sid)
        prompt = build_taxonomy_prompt(sid, s_name, off_ind, biz_desc)

        model_name = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest").strip() or "gemini-flash-lite-latest"
        raw_dict: dict[str, Any] = {}
        try:
            meta = asyncio.run(
                ask_gemini_with_meta(prompt, model=model_name, tag=f"taxonomy_{sid}")
            )
            text = re.sub(r"^```(?:json)?|```$", "", str(meta.text or "").strip(), flags=re.MULTILINE).strip()
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                raw_dict = json.loads(text[start : end + 1])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini taxonomy generation failed for %s: %s", sid, exc)

        stock_map = dict(valid_stock_map) if valid_stock_map else {}
        if not stock_map:
            stock_map = load_registry_stock_map()
            for k, v in self._records.items():
                stock_map.setdefault(k, str(v.get("stock_name", k)))
        record = sanitize_taxonomy_record(
            raw_dict,
            stock_id=sid,
            stock_name=s_name,
            official_industry=off_ind,
            valid_stock_map=stock_map,
            as_of=today,
        )
        if not raw_dict:
            # AI 失敗：回傳官方分類退回值但不寫入快取，避免毒化 90 天 TTL。
            return record
        self._records[sid] = record
        self.save_local()
        if sync_sheet:
            self.sync_to_gsheet()
        return record

    def ensure_batch(
        self,
        stocks_df: pd.DataFrame,
        valid_stock_map: Mapping[str, str],
        *,
        as_of: date | None = None,
        call_interval_sec: float = 4.2,
        sync_sheet: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """批次補齊訊號股之細分產業與同業標籤，若有任何新增則同步寫回本地 JSON 與 Google Sheets。"""
        self.load()
        today = as_of or datetime.now(TAIPEI_TZ).date()
        updated_any = False

        if stocks_df is not None and not stocks_df.empty:
            dedup = stocks_df.drop_duplicates(subset=["stock_id"])
            for row in dedup.itertuples(index=False):
                sid = str(row.stock_id).strip()
                if not sid or sid == "-":
                    continue
                existing = self._records.get(sid)
                if existing and self._is_fresh(existing, today):
                    continue
                s_name = str(getattr(row, "stock_name", "") or valid_stock_map.get(sid, sid)).strip()
                off_ind = str(getattr(row, "industry_category", "") or "其他").strip()
                before = self._records.get(sid)
                self.get_or_create_sync(
                    sid,
                    stock_name=s_name,
                    official_industry=off_ind,
                    valid_stock_map=valid_stock_map,
                    as_of=today,
                    sync_sheet=False,
                )
                if self._records.get(sid) is not before:
                    updated_any = True
                time.sleep(call_interval_sec)

        if updated_any and sync_sheet:
            self.sync_to_gsheet()
        return self._records

    def _pull_from_gsheet(self) -> None:
        creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        sheet_id = os.getenv("GSHEET_SPREADSHEET_ID", "").strip()
        if not creds or not sheet_id:
            return
        try:
            gc = gspread.service_account(filename=creds)
            sh = gc.open_by_key(sheet_id)
            ws = sh.worksheet(self.sheet_tab_name)
            rows = ws.get_all_records()
            for r in rows:
                sid = str(r.get("stock_id", "")).strip()
                if sid:
                    self._records[sid] = {col: r.get(col, "") for col in TAXONOMY_HEADERS}
            if self._records:
                self.save_local()
        except Exception:  # noqa: BLE001
            pass

    def sync_to_gsheet(self) -> int:
        creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        sheet_id = os.getenv("GSHEET_SPREADSHEET_ID", "").strip()
        if not creds or not sheet_id or not self._records:
            return 0
        try:
            gc = gspread.service_account(filename=creds)
            sh = gc.open_by_key(sheet_id)
            existing_tabs = {ws.title: ws for ws in sh.worksheets()}
            if self.sheet_tab_name not in existing_tabs:
                ws = sh.add_worksheet(
                    title=self.sheet_tab_name,
                    rows=max(len(self._records) + 100, 500),
                    cols=len(TAXONOMY_HEADERS),
                )
            else:
                ws = existing_tabs[self.sheet_tab_name]

            sorted_recs = [
                self._records[k] for k in sorted(self._records.keys())
            ]
            matrix: list[list[Any]] = [TAXONOMY_HEADERS]
            for rec in sorted_recs:
                matrix.append([rec.get(col, "") for col in TAXONOMY_HEADERS])

            ws.clear()
            end_cell = gspread.utils.rowcol_to_a1(len(matrix), len(TAXONOMY_HEADERS))
            ws.update(
                range_name=f"A1:{end_cell}",
                values=matrix,
                value_input_option="USER_ENTERED",
            )
            return len(sorted_recs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync 08_Stock_Taxonomy to Google Sheets: %s", exc)
            return 0
