"""Configuration and schema definitions for /stock_ai fundamental caching.

This module contains:
- Google Sheets tab names and column schemas
- FinMind account alias mappings (priority lookup)
- Statutory reporting deadlines and expected watermark helpers
- Prompt templates and validation tolerances
"""

from __future__ import annotations

from datetime import date

SHEET_SCHEMAS: dict[str, list[str]] = {
    "00_Watchlist_Master": [
        "stock_id",
        "stock_name",
        "industry_category",
        "max_rev_raw_date",
        "last_rev_ym",
        "max_fin_raw_date",
        "last_fin_qtr",
        "last_probe_at",
        "ai_cached_period",
        "ai_summary_json",
        "war_room_url",
        "last_accessed_at",
    ],
    "01_Monthly_Revenue": [
        "pk_key",
        "stock_id",
        "raw_date",
        "rev_ym",
        "revenue_year",
        "revenue_month",
        "revenue",
        "mom_pct",
        "yoy_pct",
        "updated_at",
    ],
    "02_Income_Statement": [
        "pk_key",
        "stock_id",
        "raw_date",
        "year_qtr",
        "revenue",
        "gross_profit",
        "operating_income",
        "pretax_income",
        "net_income",
        "eps",
        "updated_at",
    ],
    "03_Balance_Sheet": [
        "pk_key",
        "stock_id",
        "raw_date",
        "year_qtr",
        "total_assets",
        "cash_and_equiv",
        "accounts_receivable",
        "inventory",
        "total_liabilities",
        "total_equity",
        "updated_at",
    ],
    "04_Cash_Flow": [
        "pk_key",
        "stock_id",
        "raw_date",
        "year_qtr",
        "operating_cf",
        "investing_cf",
        "financing_cf",
        "capex",
        "free_cf",
        "updated_at",
    ],
    "05_AI_Metrics_Wide": [
        "pk_key",
        "stock_id",
        "raw_date",
        "year_qtr",
        "revenue_亿元",
        "gross_margin_pct",
        "op_margin_pct",
        "net_margin_pct",
        "roe_pct",
        "roa_pct",
        "debt_ratio_pct",
        "eps",
        "ttm_eps",
        "ocf_亿元",
        "capex_亿元",
        "fcf_亿元",
        "qtr_rev_yoy_pct",
        "updated_at",
    ],
}


ACCOUNT_ANCHOR_MAP: dict[str, list[str]] = {
    # Income statement
    "revenue": [
        "Revenue",
        "OperatingRevenue",
        "NetRevenue",
        "InterestAndOtherNetRevenue",
    ],
    "gross_profit": [
        "GrossProfit",
        "NetOperatingMargin",
    ],
    "operating_income": [
        "OperatingIncome",
        "OperatingProfit",
    ],
    "pretax_income": [
        "PreTaxIncome",
        "IncomeBeforeTax",
        "ContinuingOperationsIncomeBeforeTax",
    ],
    "net_income": [
        "IncomeAfterTaxes",
        "ProfitLoss",
        "NetIncome",
        "EquityAttributableToOwnersOfParent_Income",
    ],
    "eps": [
        "EPS",
        "BasicEarningsPerShare",
    ],
    # Balance sheet
    "total_assets": [
        "TotalAssets",
        "Assets",
    ],
    "cash_and_equiv": [
        "CashAndCashEquivalents",
        "CashAndDueFromBanks",
    ],
    "accounts_receivable": [
        "AccountsReceivableNet",
        "NotesAndAccountsReceivableNet",
    ],
    "inventory": [
        "Inventories",
        "Inventory",
    ],
    "total_liabilities": [
        "TotalLiabilities",
        "Liabilities",
    ],
    "total_equity": [
        "Equity",
        "TotalEquity",
        "StockholdersEquity",
    ],
    # Cash flow
    "operating_cf": [
        "CashFlowsFromOperatingActivities",
        "NetCashInflowFromOperatingActivities",
    ],
    "investing_cf": [
        "CashFlowsProvidedFromInvestingActivities",
        "NetCashUsedInInvestingActivities",
    ],
    "financing_cf": [
        "CashFlowsFromFinancingActivities",
        "NetCashUsedInFinancingActivities",
    ],
    "capex": [
        "PropertyAndPlantAndEquipment",
        "AcquisitionOfPropertyPlantAndEquipment",
    ],
}


BALANCE_TOLERANCE_RATIO = 0.0005
BALANCE_TOLERANCE_ABS = 1000.0


def get_expected_watermarks(today: date) -> tuple[str, str]:
    """Return (expected_rev_ym, expected_fin_qtr) based on Taiwan rules.

    Notes:
    - expected_rev_ym: YYYY-MM (the latest month whose revenue should be available)
    - expected_fin_qtr: YYYYQn (the latest quarter that should be disclosed)

    This helper is used by the cache manager to decide if sheet watermarks lag.
    """

    year = today.year
    month = today.month
    day = today.day

    # Monthly revenue: after day 10, last month should be disclosed.
    rev_year = year
    rev_month = month - 1
    if rev_month == 0:
        rev_year -= 1
        rev_month = 12
    expected_rev_ym = f"{rev_year:04d}-{rev_month:02d}"

    # Quarterly financial statements deadlines:
    # 04/01~05/15: last year Q4
    # 05/16~08/14: this year Q1
    # 08/15~11/14: this year Q2
    # 11/15~next year 03/31: this year Q3
    if (month == 4) or (month == 5 and day <= 15):
        expected_fin_year = year - 1
        expected_fin_q = 4
    elif (month == 5 and day >= 16) or month in (6, 7) or (month == 8 and day <= 14):
        expected_fin_year = year
        expected_fin_q = 1
    elif (month == 8 and day >= 15) or month in (9, 10) or (month == 11 and day <= 14):
        expected_fin_year = year
        expected_fin_q = 2
    else:
        expected_fin_year = year
        expected_fin_q = 3

    expected_fin_qtr = f"{expected_fin_year:04d}Q{expected_fin_q}"
    return expected_rev_ym, expected_fin_qtr


PROMPT_TEMPLATES: dict[str, str] = {
    "system_rules": (
        "你是一位極度嚴謹的台股基本面財務分析師與系統架構師。\n"
        "請嚴格根據下方由系統從 Google Sheets 快取資料庫提取的真實財務報表數據回答。\n"
        "1. 引用任何數據時，必須明確標註期別（如 2026Q2 或 2026-09）與數值。\n"
        "2. 毛利率、營益率、自由現金流與成長率皆已由系統後端精確算妥，請直接引用表格數字，嚴禁自行計算或捏造。\n"
        "3. 回答請先給一句話結論，再列數據佐證與原因拆解（條列）。"
    ),
    "full_diagnosis": (
        "請根據數據給出：\n"
        "- 一句話核心結論\n"
        "- 0~100 分體質評分\n"
        "- 3 大亮點（含期別與數字）\n"
        "- 2 大風險（含期別與數字）\n"
        "- 杜邦/ROE 拆解（以數據支持，避免空泛）\n"
    ),
    "targeted_qa": (
        "請只針對使用者問題作答，並引用表格內的期別與數字作為佐證。"
    ),
}
