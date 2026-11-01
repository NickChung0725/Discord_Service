"""Taishin Securities Nova API SDK wrapper (`taishin_client.py`).

SASD v5.1.0 Guardrails Implemented:
1. Official SDK import alignment & `auth-init` (`register_api_auth`).
2. Recursive Rust PyO3 extension object normalizer (`normalize_sdk_obj`).
3. Dual-spelling statement compatibility (`account_statment` / `account_statement`)
   and dynamic signature / date-format probing (`call_sdk_with_date_range`).
4. 30-day chunk splitter (`iter_date_chunks`) with single-login session enforcement.
5. 0050 Intraday-Odd order execution safety gate (`TAISHIN_ENABLE_TRADING` + `--confirm-live-order`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
import inspect
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any

try:
    from taishin_sdk import (  # type: ignore[import-not-found]
        TaishinSDK,
        Order,
        TimeInForce,
        OrderType,
        PriceType,
        MarketType,
        BSAction,
        Account,
        QueryType,
    )

    HAS_TAISHIN_SDK = True
except ImportError:
    HAS_TAISHIN_SDK = False

    class _FallbackEnum(Enum):
        pass

    class TimeInForce(_FallbackEnum):  # type: ignore[no-redef]
        ROD = "ROD"
        IOC = "IOC"
        FOK = "FOK"

    class OrderType(_FallbackEnum):  # type: ignore[no-redef]
        Stock = "Stock"
        Margin = "Margin"
        Short = "Short"

    class PriceType(_FallbackEnum):  # type: ignore[no-redef]
        Limit = "Limit"
        Market = "Market"
        Flat = "Flat"

    class MarketType(_FallbackEnum):  # type: ignore[no-redef]
        Common = "Common"
        IntradayOdd = "IntradayOdd"
        Odd = "Odd"
        Fixing = "Fixing"

    class BSAction(_FallbackEnum):  # type: ignore[no-redef]
        Buy = "Buy"
        Sell = "Sell"

    class QueryType(_FallbackEnum):  # type: ignore[no-redef]
        All = "All"

    @dataclass
    class Account:  # type: ignore[no-redef]
        account: str = ""
        branch: str = ""
        account_type: str = "stock"

    @dataclass
    class Order:  # type: ignore[no-redef]
        buy_sell: Any = None
        symbol: str = ""
        price: float | str = 0.0
        quantity: int = 0
        market_type: Any = None
        price_type: Any = None
        time_in_force: Any = None
        order_type: Any = None

    class TaishinSDK:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "taishin_sdk 尚未安裝於目前 Python 環境。請確認已透過 pip 安裝 taishin_sdk wheel，"
                "並使用 `uv sync --inexact` 避免移除手動安裝之套件。"
            )


logger = logging.getLogger(__name__)


class TaishinClientError(RuntimeError):
    """Raised when Taishin SDK configuration, login, or API invocation fails."""


# --------------------------------------------------------------------------- #
# Guardrail 2: Rust PyO3 binary extension object normalization
# --------------------------------------------------------------------------- #
def normalize_sdk_obj(obj: Any, *, _depth: int = 0) -> Any:
    """Recursively convert Rust PyO3 objects (without `__dict__`) into pure Python primitives.

    Uses `dir(obj)` to inspect public (non-underscore) attributes and converts them into
    JSON-serializable `dict`, `list`, `str`, `float`, `int`, or `bool`.
    """
    if _depth > 12:
        return str(obj)

    if obj is None or isinstance(obj, (bool, int, str)):
        return obj

    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj

    if isinstance(obj, Decimal):
        return float(obj)

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()

    if isinstance(obj, Enum):
        return normalize_sdk_obj(obj.value, _depth=_depth + 1)

    # Detect Rust PyO3 enum variants (e.g. BSAction.Buy, MarketType.Common) whose dir(obj)
    # exposes sibling enum variants of the exact same type, which would otherwise recurse deeply.
    obj_type = type(obj)
    obj_str = str(obj)
    if obj_str.startswith(f"{obj_type.__name__}."):
        return obj_str.split(".", 1)[1]

    if isinstance(obj, Mapping):
        return {
            str(k): normalize_sdk_obj(v, _depth=_depth + 1)
            for k, v in obj.items()
        }

    if isinstance(obj, (list, tuple, set, frozenset)):
        return [normalize_sdk_obj(item, _depth=_depth + 1) for item in obj]

    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")

    # Handle standard Python objects or Rust PyO3 extension objects via dir(obj)
    result: dict[str, Any] = {}
    for attr in dir(obj):
        if attr.startswith("_"):
            continue
        try:
            val = getattr(obj, attr)
        except Exception:  # noqa: BLE001
            continue
        if callable(val):
            continue
        if type(val) is obj_type:
            # PyO3 enum class-level variant reference on an enum instance
            return obj_str
        result[attr] = normalize_sdk_obj(val, _depth=_depth + 1)

    if result:
        return result

    return str(obj)


# --------------------------------------------------------------------------- #
# Date parsing, formatting, and chunking helpers
# --------------------------------------------------------------------------- #
def parse_flexible_date(value: Any) -> date | None:
    """Parse dates in 'YYYY-MM-DD', 'YYYYMMDD', 'YYYY/MM/DD', or ROC '114/10/01' / '1141001'."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    # Strip time part if present
    text = text.split("T")[0].split(" ")[0].strip()

    # Standard YYYY-MM-DD or YYYY/MM/DD
    m_iso = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", text)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            return None

    # Standard YYYYMMDD (8 digits)
    if re.match(r"^\d{8}$", text):
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None

    # ROC YYY/MM/DD or YYY-MM-DD (e.g. 114/05/12)
    m_roc = re.match(r"^(\d{2,3})[-/](\d{1,2})[-/](\d{1,2})$", text)
    if m_roc:
        try:
            year = int(m_roc.group(1)) + 1911
            return date(year, int(m_roc.group(2)), int(m_roc.group(3)))
        except ValueError:
            return None

    # ROC YYYMMDD (7 digits, e.g. 1140512)
    if re.match(r"^\d{7}$", text):
        try:
            year = int(text[:3]) + 1911
            month = int(text[3:5])
            day = int(text[5:7])
            return date(year, month, day)
        except ValueError:
            return None

    return None


def format_iso_date(value: Any, default: str = "") -> str:
    """Normalize any supported date representation into 'YYYY-MM-DD'."""
    parsed = parse_flexible_date(value)
    return parsed.isoformat() if parsed is not None else default


def iter_date_chunks(
    from_date: date | str,
    to_date: date | str,
    chunk_days: int = 30,
) -> list[tuple[date, date]]:
    """Split `[from_date, to_date]` into chunks of at most `chunk_days` days each."""
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")

    start_dt = parse_flexible_date(from_date)
    end_dt = parse_flexible_date(to_date)
    if start_dt is None or end_dt is None:
        raise ValueError(f"Invalid date range: from_date={from_date!r}, to_date={to_date!r}")
    if start_dt > end_dt:
        raise ValueError(f"from_date ({start_dt}) cannot be after to_date ({end_dt})")

    chunks: list[tuple[date, date]] = []
    cursor = start_dt
    step = timedelta(days=chunk_days - 1)
    while cursor <= end_dt:
        chunk_end = min(end_dt, cursor + step)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


# --------------------------------------------------------------------------- #
# Guardrail 3: Statement dual compatibility & dynamic signature probing
# --------------------------------------------------------------------------- #
def get_statement_method(sdk: Any) -> Callable[..., Any] | None:
    """Resolve `account_statment` (official typo) or `account_statement` on SDK or sub-clients."""
    fn = getattr(sdk, "account_statment", getattr(sdk, "account_statement", None))
    if callable(fn):
        return fn
    for sub_name in ("stock", "accounting", "account"):
        sub = getattr(sdk, sub_name, None)
        if sub is not None:
            fn = getattr(sub, "account_statment", getattr(sub, "account_statement", None))
            if callable(fn):
                return fn
    return None


def resolve_sdk_method(sdk: Any, *method_names: str) -> Callable[..., Any] | None:
    """Find the first available method from `method_names` on `sdk` or `sdk.stock` / `sdk.accounting`."""
    for name in method_names:
        fn = getattr(sdk, name, None)
        if callable(fn):
            return fn
    for sub_name in ("stock", "accounting", "account"):
        sub = getattr(sdk, sub_name, None)
        if sub is None:
            continue
        for name in method_names:
            fn = getattr(sub, name, None)
            if callable(fn):
                return fn
    return None


_LIST_CONTAINER_KEYS = (
    "position_summaries",
    "filled_records",
    "profit_loss_summary",
    "profit_loss_summaries",
    "settlements",
    "data",
    "records",
    "items",
    "details",
    "list",
    "result",
    "results",
    "statements",
    "inventories",
    "fills",
    "pnl",
)

_RECORD_DATE_KEYS = (
    "trade_date",
    "filled_date",
    "fill_date",
    "t_date",
    "tdate",
    "ord_date",
    "order_date",
    "date",
    "record_date",
    "s_date",
    "settlement_date",
    "settle_date",
    "sdate",
)


def extract_record_list(raw_Or_normalized: Any) -> list[dict[str, Any]]:
    """Convert normalized SDK output into a flat `list[dict[str, Any]]`."""
    norm = normalize_sdk_obj(raw_Or_normalized)
    if norm is None:
        return []
    if isinstance(norm, list):
        return [item if isinstance(item, dict) else {"value": item} for item in norm]
    if isinstance(norm, dict):
        for key in _LIST_CONTAINER_KEYS:
            candidate = norm.get(key)
            if isinstance(candidate, list):
                return [item if isinstance(item, dict) else {"value": item} for item in candidate]
        # Fallback: if any field in the dict is a list of dicts (e.g. PyO3 response wrapper)
        for candidate in norm.values():
            if isinstance(candidate, list) and (not candidate or isinstance(candidate[0], dict)):
                return [item if isinstance(item, dict) else {"value": item} for item in candidate]
        # If it looks like a single record dict (not just empty status metadata)
        if any(
            k in norm
            for k in (
                "stock_no",
                "symbol",
                "code",
                "stock_id",
                "order_no",
                "qty",
                "bank_balance",
                "balance",
                "avail_balance",
            )
        ):
            return [norm]
        # PaymentResponse from today_settlement (only when non-empty / non-zero)
        if "net_amount" in norm and (
            str(norm.get("s_date") or "").strip()
            or str(norm.get("t_date") or "").strip()
            or str(norm.get("net_amount") or "0").strip() not in ("", "0", "0.0", "0.00")
        ):
            return [norm]
        return []
    return []


def extract_record_date(record: Mapping[str, Any]) -> date | None:
    """Find and parse the primary date field of a normalized SDK record."""
    for key in _RECORD_DATE_KEYS:
        if key in record and record[key] not in (None, ""):
            parsed = parse_flexible_date(record[key])
            if parsed is not None:
                return parsed
    return None


def filter_records_by_date_range(
    records: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    """Filter `list[dict]` by `trade_date` / `record_date` within `[start_date, end_date]` inclusive."""
    filtered: list[dict[str, Any]] = []
    for rec in records:
        rec_date = extract_record_date(rec)
        if rec_date is None:
            # Keep records that don't carry a date attribute so downstream mappers can inspect them
            filtered.append(rec)
        elif start_date <= rec_date <= end_date:
            if "trade_date" in rec and rec["trade_date"]:
                rec = {**rec, "trade_date": rec_date.isoformat()}
            filtered.append(rec)
    return filtered


def _is_no_data_sdk_error(err: BaseException) -> bool:
    """Return True if the SDK exception represents an empty query result or rollover state (5001/6045/9906)."""
    text = str(err)
    lower = text.lower()
    return any(
        marker in text or marker in lower
        for marker in (
            "5001",
            "6045",
            "9906",
            "查無資料",
            "無資料",
            "已結帳完成",
            "結帳轉檔中",
            "no data",
            "not found",
            "empty",
        )
    )


def call_sdk_with_date_range(
    fn: Callable[..., Any],
    acc: Any,
    start_date: date | str,
    end_date: date | str,
) -> list[dict[str, Any]]:
    """Dynamically probe SDK method signature and date formats, then filter in Python by date range.

    Probing order:
    1. Inspect `inspect.signature(fn)` if available to check accepted parameter count.
    2. Try `(acc, start_yyyymmdd, end_yyyymmdd)` ➔ `(acc, start_yyyy_mm_dd, end_yyyy_mm_dd)` ➔ `(acc,)`.
    3. Normalize the result into `list[dict]` via `normalize_sdk_obj` and filter by `[start_date, end_date]`.
    """
    start_dt = parse_flexible_date(start_date)
    end_dt = parse_flexible_date(end_date)
    if start_dt is None or end_dt is None:
        raise ValueError(f"Invalid date range: {start_date!r} ~ {end_date!r}")

    start_yyyymmdd = start_dt.strftime("%Y%m%d")
    end_yyyymmdd = end_dt.strftime("%Y%m%d")
    start_iso = start_dt.strftime("%Y-%m-%d")
    end_iso = end_dt.strftime("%Y-%m-%d")

    candidate_args: list[tuple[Any, ...]] = [
        (acc, start_yyyymmdd, end_yyyymmdd),
        (acc, start_iso, end_iso),
        (acc,),
    ]

    try:
        sig = inspect.signature(fn)
        params = [
            p
            for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        if not has_varargs:
            if len(params) == 1:
                candidate_args = [(acc,)]
            elif len(params) == 0:
                candidate_args = [()]
    except (ValueError, TypeError):
        # Rust PyO3 built-in functions may not expose a Python inspect.signature
        pass

    last_error: BaseException | None = None
    raw_result: Any = None
    called_ok = False

    for args in candidate_args:
        try:
            raw_result = fn(*args)
            called_ok = True
            break
        except (TypeError, ValueError) as err:
            last_error = err
            continue
        except Exception as err:  # noqa: BLE001
            if _is_no_data_sdk_error(err):
                return []
            # Some SDK methods raise RuntimeError/Exception on date format mismatch (YYYYMMDD vs YYYY-MM-DD)
            last_error = err
            err_text = str(err).lower()
            if any(tok in err_text for tok in ("date", "format", "argument", "param", "signature", "type")):
                continue
            raise

    if not called_ok:
        if last_error is not None:
            if _is_no_data_sdk_error(last_error):
                return []
            raise last_error
        return []

    records = extract_record_list(raw_result)
    return filter_records_by_date_range(records, start_dt, end_dt)


# --------------------------------------------------------------------------- #
# TaishinClient wrapper
# --------------------------------------------------------------------------- #
@dataclass
class TaishinCredentials:
    person_id: str
    password: str
    cert_path: str
    cert_password: str
    account_no: str = ""

    @classmethod
    def from_env(cls) -> TaishinCredentials:
        person_id = (os.getenv("TAISHIN_PERSON_ID") or "").strip()
        configured_cert = (os.getenv("TAISHIN_CERT_PATH") or "").strip()
        if configured_cert:
            cert_path = configured_cert
        elif Path("certs/taishin_cert.pfx").is_file():
            cert_path = "certs/taishin_cert.pfx"
        elif person_id and Path(f"certs/{person_id}.pfx").is_file():
            cert_path = f"certs/{person_id}.pfx"
        else:
            pfx_files = list(Path("certs").glob("*.pfx")) if Path("certs").is_dir() else []
            cert_path = str(pfx_files[0]) if pfx_files else "certs/taishin_cert.pfx"

        return cls(
            person_id=person_id,
            password=(os.getenv("TAISHIN_PASSWORD") or "").strip(),
            cert_path=cert_path,
            cert_password=(os.getenv("TAISHIN_CERT_PASSWORD") or "").strip(),
            account_no=(os.getenv("TAISHIN_ACCOUNT_NO") or "").strip(),
        )

    def validate(self) -> None:
        missing = []
        if not self.person_id:
            missing.append("TAISHIN_PERSON_ID")
        if not self.password:
            missing.append("TAISHIN_PASSWORD")
        if not self.cert_password:
            missing.append("TAISHIN_CERT_PASSWORD")
        if missing:
            raise TaishinClientError(f"缺少台新 API 必要環境變數：{', '.join(missing)}")
        if not Path(self.cert_path).is_file():
            raise TaishinClientError(f"找不到台新憑證檔案：{self.cert_path}")


class TaishinClient:
    """High-level wrapper around `TaishinSDK` enforcing single login and PyO3 normalization."""

    def __init__(
        self,
        sdk: Any | None = None,
        credentials: TaishinCredentials | None = None,
        account: Any | None = None,
    ) -> None:
        self.credentials = credentials or TaishinCredentials.from_env()
        self._sdk = sdk
        self._account = account
        self._logged_in = False
        self.login_count = 0

    @property
    def sdk(self) -> Any:
        if self._sdk is None:
            self._sdk = TaishinSDK()
        return self._sdk

    def ensure_logged_in(self) -> Any:
        """Log in at most once per process/client lifecycle and return the selected `Account`."""
        if self._logged_in and self._account is not None:
            return self._account

        if self._sdk is None:
            self.credentials.validate()
            self._sdk = TaishinSDK()

        if not self._logged_in:
            login_fn = getattr(self._sdk, "login", None)
            if callable(login_fn):
                accounts = self._invoke_login(login_fn)
                self._logged_in = True
                self.login_count += 1
                if self._account is None:
                    self._account = self._select_account(accounts)
            else:
                self._logged_in = True

        if self._account is None:
            # Check sdk.accounts or fallback to Account(account=account_no)
            sdk_accounts = getattr(self._sdk, "accounts", None)
            if callable(sdk_accounts):
                sdk_accounts = sdk_accounts()
            self._account = self._select_account(sdk_accounts)

        return self._account

    def _invoke_login(self, login_fn: Callable[..., Any]) -> Any:
        """Call `sdk.login` supporting positional or keyword credentials."""
        c = self.credentials
        for call in (
            lambda: login_fn(c.person_id, c.password, c.cert_path, c.cert_password),
            lambda: login_fn(
                person_id=c.person_id,
                password=c.password,
                cert_path=c.cert_path,
                cert_password=c.cert_password,
            ),
            lambda: login_fn(c.person_id, c.password, c.cert_path),
        ):
            try:
                return call()
            except TypeError:
                continue
        return login_fn(c.person_id, c.password, c.cert_path, c.cert_password)

    def _select_account(self, accounts: Any) -> Any:
        target_no = self.credentials.account_no
        if isinstance(accounts, (list, tuple)) and accounts:
            if target_no:
                for acc in accounts:
                    norm = normalize_sdk_obj(acc)
                    if isinstance(norm, dict):
                        acc_no = str(
                            norm.get("account")
                            or norm.get("account_no")
                            or norm.get("acct_no")
                            or ""
                        ).strip()
                        if acc_no == target_no or target_no in acc_no:
                            return acc
            return accounts[0]
        if accounts is not None and not isinstance(accounts, (bool, int, str, list, tuple)):
            return accounts
        return Account(account=target_no or "DEFAULT")

    def get_account_no_str(self) -> str:
        if self.credentials.account_no:
            return self.credentials.account_no
        if self._account is not None:
            norm = normalize_sdk_obj(self._account)
            if isinstance(norm, dict):
                for k in ("account", "account_no", "acct_no"):
                    if norm.get(k):
                        return str(norm[k]).strip()
        return ""

    # ----------------------------------------------------------------------- #
    # Guardrail 1: First-time API activation (`auth-init`)
    # ----------------------------------------------------------------------- #
    def register_api_auth(self) -> dict[str, Any]:
        """Execute `login` + `sdk.register_api_auth(acc)` once for initial account activation."""
        acc = self.ensure_logged_in()
        fn = resolve_sdk_method(self.sdk, "register_api_auth")
        if fn is None:
            raise TaishinClientError("目前 taishin_sdk 實例不支援 `register_api_auth` 方法。")
        raw = fn(acc)
        norm = normalize_sdk_obj(raw)
        return norm if isinstance(norm, dict) else {"result": norm}

    # ----------------------------------------------------------------------- #
    # Historical & Daily Query Methods
    # ----------------------------------------------------------------------- #
    def fetch_account_statement(self, start_date: date | str, end_date: date | str) -> list[dict[str, Any]]:
        acc = self.ensure_logged_in()
        fn = get_statement_method(self.sdk)
        if fn is None:
            logger.warning("SDK does not provide account_statment / account_statement.")
            return []
        return call_sdk_with_date_range(fn, acc, start_date, end_date)

    def fetch_filled_history(self, start_date: date | str, end_date: date | str) -> list[dict[str, Any]]:
        acc = self.ensure_logged_in()
        fn = resolve_sdk_method(self.sdk, "filled_history")
        if fn is None:
            return []
        return call_sdk_with_date_range(fn, acc, start_date, end_date)

    def fetch_filled_history_detail(self, start_date: date | str, end_date: date | str) -> list[dict[str, Any]]:
        acc = self.ensure_logged_in()
        fn = resolve_sdk_method(self.sdk, "filled_history_detail", "filled_detail_history")
        if fn is None:
            return []
        return call_sdk_with_date_range(fn, acc, start_date, end_date)

    def fetch_realized_profit_and_losses(
        self, start_date: date | str, end_date: date | str
    ) -> list[dict[str, Any]]:
        acc = self.ensure_logged_in()
        fn = resolve_sdk_method(
            self.sdk,
            "realized_profit_and_loses",
            "realized_profit_and_losses",
        )
        if fn is None:
            return []
        return call_sdk_with_date_range(fn, acc, start_date, end_date)

    def fetch_history_settlement(self, start_date: date | str, end_date: date | str) -> list[dict[str, Any]]:
        acc = self.ensure_logged_in()
        fn = resolve_sdk_method(self.sdk, "history_settlement", "settlement_history")
        if fn is None:
            return []
        return call_sdk_with_date_range(fn, acc, start_date, end_date)

    def fetch_today_settlement(self) -> list[dict[str, Any]]:
        acc = self.ensure_logged_in()
        fn = resolve_sdk_method(self.sdk, "today_settlement", "settlement_today", "settlements")
        if fn is None:
            return []
        try:
            try:
                raw = fn(acc)
            except TypeError:
                raw = fn()
        except Exception as err:  # noqa: BLE001
            if _is_no_data_sdk_error(err):
                return []
            raise
        return extract_record_list(raw)

    def fetch_bank_balance(self) -> float | None:
        acc = self.ensure_logged_in()
        balance_keys = (
            "available_balance",
            "bank_balance",
            "avail_balance",
            "balance",
            "cash_balance",
            "ledger_balance",
            "remain_balance",
            "amount",
        )
        for method_name in ("bank_balance", "skbank_balance", "account_balance", "margin_balance"):
            fn = resolve_sdk_method(self.sdk, method_name)
            if fn is None:
                continue
            try:
                try:
                    raw = fn(acc)
                except TypeError:
                    raw = fn()
            except Exception as err:  # noqa: BLE001
                if _is_no_data_sdk_error(err):
                    continue
                raise
            norm = normalize_sdk_obj(raw)
            if isinstance(norm, (int, float)):
                return float(norm)
            if isinstance(norm, dict):
                for key in balance_keys:
                    if norm.get(key) not in (None, ""):
                        try:
                            return float(str(norm[key]).replace(",", ""))
                        except ValueError:
                            continue
            if isinstance(norm, list) and norm:
                first = norm[0]
                if isinstance(first, dict):
                    for key in balance_keys:
                        if first.get(key) not in (None, ""):
                            try:
                                return float(str(first[key]).replace(",", ""))
                            except ValueError:
                                continue
        return None

    def fetch_inventories(self) -> list[dict[str, Any]]:
        acc = self.ensure_logged_in()
        fn = resolve_sdk_method(self.sdk, "inventories", "inventory", "positions")
        if fn is None:
            return []
        try:
            try:
                raw = fn(acc)
            except TypeError:
                try:
                    raw = fn(acc, QueryType.All)
                except Exception:  # noqa: BLE001
                    raw = fn()
        except Exception as err:  # noqa: BLE001
            if _is_no_data_sdk_error(err):
                return []
            raise
        return extract_record_list(raw)

    # ----------------------------------------------------------------------- #
    # 0050 Intraday-Odd Order Execution Gate
    # ----------------------------------------------------------------------- #
    def place_0050_intraday_odd_order(
        self,
        *,
        qty_shares: int,
        price: float,
        enable_trading: bool = False,
        confirm_live_order: bool = False,
    ) -> dict[str, Any]:
        """Place an Intraday-Odd BUY order for 0050 ONLY when both safety gates are enabled."""
        if qty_shares <= 0:
            raise ValueError("qty_shares must be > 0")
        if price <= 0:
            raise ValueError("price must be > 0")

        if not (enable_trading and confirm_live_order):
            return {
                "order_mode": "DRY_RUN",
                "order_no": "DRY_RUN",
                "order_qty_shares": int(qty_shares),
                "order_price": float(price),
                "executed": False,
            }

        acc = self.ensure_logged_in()
        price_str = f"{float(price):.2f}".rstrip("0").rstrip(".") if isinstance(price, float) else str(price)
        order_kwargs = {
            "buy_sell": BSAction.Buy,
            "symbol": "0050",
            "price": price_str,
            "quantity": int(qty_shares),
            "market_type": MarketType.IntradayOdd,
            "price_type": PriceType.Limit,
            "time_in_force": TimeInForce.ROD,
            "order_type": OrderType.Stock,
        }
        try:
            order = Order(**order_kwargs)
        except TypeError:
            order = Order(
                BSAction.Buy,
                "0050",
                int(qty_shares),
                MarketType.IntradayOdd,
                PriceType.Limit,
                TimeInForce.ROD,
                OrderType.Stock,
                price_str,
            )

        stock_api = getattr(self.sdk, "stock", None)
        place_fn = getattr(stock_api, "place_order", None) if stock_api is not None else None
        if not callable(place_fn):
            place_fn = resolve_sdk_method(self.sdk, "place_order")
        if not callable(place_fn):
            raise TaishinClientError("SDK 未提供 `stock.place_order` 或 `place_order` 方法。")

        raw_resp = place_fn(acc, order)
        norm_resp = normalize_sdk_obj(raw_resp)
        order_no = ""
        if isinstance(norm_resp, dict):
            for k in ("order_no", "ord_no", "seq_no", "id"):
                if norm_resp.get(k):
                    order_no = str(norm_resp[k]).strip()
                    break
        if not order_no:
            order_no = str(norm_resp or "LIVE_SUBMITTED")

        return {
            "order_mode": "LIVE_ORDER",
            "order_no": order_no,
            "order_qty_shares": int(qty_shares),
            "order_price": float(price),
            "raw_response": norm_resp,
            "executed": True,
        }


# --------------------------------------------------------------------------- #
# CLI Inspection Helper (`inspect-sdk`)
# --------------------------------------------------------------------------- #
def inspect_sdk_capabilities(*, live: bool = False, client: TaishinClient | None = None) -> dict[str, Any]:
    """Inspect installed `taishin_sdk` symbols/signatures; if `live=True`, login and sample APIs."""
    import sys

    sdk_mod = sys.modules.get("taishin_sdk")
    exported_symbols = [name for name in dir(sdk_mod) if not name.startswith("_")] if sdk_mod else []

    sdk_methods: dict[str, str] = {}
    for attr in dir(TaishinSDK):
        if attr.startswith("_"):
            continue
        member = getattr(TaishinSDK, attr, None)
        if callable(member):
            try:
                sdk_methods[attr] = str(inspect.signature(member))
            except (ValueError, TypeError):
                sdk_methods[attr] = "(...)"

    report: dict[str, Any] = {
        "has_taishin_sdk": HAS_TAISHIN_SDK,
        "exported_symbols": exported_symbols,
        "taishin_sdk_methods": sdk_methods,
        "live_samples": {},
    }

    if live:
        active_client = client or TaishinClient()
        acc = active_client.ensure_logged_in()
        report["account"] = normalize_sdk_obj(acc)
        sdk_instance_attrs: dict[str, Any] = {}
        for attr in dir(active_client.sdk):
            if attr.startswith("_"):
                continue
            member = getattr(active_client.sdk, attr, None)
            if callable(member):
                try:
                    sdk_instance_attrs[attr] = f"method{inspect.signature(member)}"
                except (ValueError, TypeError):
                    sdk_instance_attrs[attr] = "method(...)"
            else:
                sub_methods: dict[str, str] = {}
                for sub_attr in dir(member):
                    if sub_attr.startswith("_"):
                        continue
                    sub_m = getattr(member, sub_attr, None)
                    if callable(sub_m):
                        text_sig = getattr(sub_m, "__text_signature__", None)
                        if text_sig:
                            sub_methods[sub_attr] = str(text_sig)
                        else:
                            try:
                                sub_methods[sub_attr] = str(inspect.signature(sub_m))
                            except (ValueError, TypeError):
                                sub_methods[sub_attr] = "(...)"
                sdk_instance_attrs[attr] = sub_methods or type(member).__name__
        report["sdk_instance_members"] = sdk_instance_attrs
        today = date.today()
        week_ago = today - timedelta(days=7)

        probes: list[tuple[str, Callable[[], Any]]] = [
            ("inventories", active_client.fetch_inventories),
            ("account_statment", lambda: active_client.fetch_account_statement(week_ago, today)),
            ("filled_history", lambda: active_client.fetch_filled_history(week_ago, today)),
            ("filled_history_detail", lambda: active_client.fetch_filled_history_detail(week_ago, today)),
            (
                "realized_profit_and_loses",
                lambda: active_client.fetch_realized_profit_and_losses(week_ago, today),
            ),
            ("today_settlement", active_client.fetch_today_settlement),
            ("history_settlement", lambda: active_client.fetch_history_settlement(week_ago, today)),
            ("bank_balance", active_client.fetch_bank_balance),
        ]
        for name, probe_fn in probes:
            try:
                data = probe_fn()
                if isinstance(data, list):
                    sample_keys = list(data[0].keys()) if data and isinstance(data[0], dict) else []
                    report["live_samples"][name] = {
                        "count": len(data),
                        "sample_keys": sample_keys,
                        "first_record": data[0] if data else None,
                    }
                else:
                    report["live_samples"][name] = {"value": data}
            except Exception as err:  # noqa: BLE001
                report["live_samples"][name] = {"error": f"{type(err).__name__}: {err}"}

    return report
