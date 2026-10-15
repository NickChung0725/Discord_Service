"""Configuration and schema definitions for /stock_ai fundamental caching (v3.1 FinMind Verified).

Aligned 100% with actual FinMind TaiwanStockFinancialStatements, TaiwanStockBalanceSheet,
and TaiwanStockCashFlowsStatement schemas.
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
        "cost_of_goods_sold",
        "gross_profit",
        "operating_expenses",
        "operating_income",
        "non_op_income_total",
        "pretax_income",
        "income_tax_expense",
        "net_income",
        "net_income_parent",
        "net_income_nci",
        "other_comprehensive_income",
        "eps",
        "updated_at",
    ],
    "03_Balance_Sheet": [
        "pk_key",
        "stock_id",
        "raw_date",
        "year_qtr",
        "total_assets",
        "current_assets",
        "cash_and_equiv",
        "fvpl_assets",
        "fvoci_assets",
        "ac_assets",
        "equity_method_investments",
        "accounts_receivable",
        "inventory",
        "prepayments",
        "total_liabilities",
        "current_liabilities",
        "accounts_payable",
        "other_payables",
        "contract_liabilities",
        "short_term_debt",
        "long_term_debt",
        "parent_equity",
        "total_equity",
        "updated_at",
    ],
    # 在 SHEET_SCHEMAS 中，將 "04_Cash_Flow" 精簡為 100% 有值的 16 個欄位：
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
        "depreciation",
        "amortization",
        "interest_expense_cf",
        "interest_income_cf",
        "cf_change_ar",
        "cf_change_inv",
        "cf_change_ap",
        "updated_at",
    ],
    "05_AI_Metrics_Wide": [
        "pk_key",
        "stock_id",
        "raw_date",
        "year_qtr",
        "revenue_亿元",
        "gross_margin_pct",
        "op_expense_pct",
        "op_margin_pct",
        "non_op_share_pct",
        "net_margin_pct",
        "parent_net_margin_pct",
        "roe_pct",
        "roa_pct",
        "debt_ratio_pct",
        "net_debt_ratio_pct",
        "current_ratio_pct",
        "quick_ratio_pct",
        "interest_coverage",
        "dio_days",
        "dso_days",
        "dpo_days",
        "ccc_days",
        "eps",
        "ttm_eps",
        "ocf_亿元",
        "capex_亿元",
        "fcf_亿元",
        "ebitda_亿元",
        "ocf_to_ni_pct",
        "qtr_rev_yoy_pct",
        "updated_at",
    ],
}


# 100% 對齊 FinMind 真實 type 與中文 origin_name
ACCOUNT_ANCHOR_MAP: dict[str, list[str]] = {
    # ================= 02_綜合損益表 (IS - 共17項全覆蓋) =================
    "revenue": ["Revenue", "OperatingRevenue", "NetRevenue", "營業收入", "營業收入合計"],
    "cost_of_goods_sold": ["CostOfGoodsSold", "OperatingCosts", "營業成本", "營業成本合計"],
    "gross_profit": ["GrossProfit", "營業毛利（毛損）", "營業毛利（毛損）淨額"],
    "operating_expenses": ["OperatingExpenses", "營業費用", "營業費用合計"],
    "operating_income": ["OperatingIncome", "營業利益（損失）", "營業淨利（淨損）"],
    "non_op_income_total": ["TotalNonoperatingIncomeAndExpense", "營業外收入及支出", "營業外收入及支出合計"],
    "pretax_income": ["PreTaxIncome", "稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）"],
    "income_tax_expense": ["TAX", "IncomeTaxExpense", "所得稅費用（利益）", "所得稅費用（利益）合計"],
    "net_income": ["IncomeAfterTaxes", "ProfitLoss", "本期淨利（淨損）"],
    "net_income_parent": [
        "EquityAttributableToOwnersOfParent",
        "淨利（淨損）歸屬於母公司業主",
        "母公司業主（淨利／損）",
    ],
    "net_income_nci": [
        "NoncontrollingInterests",
        "淨利（淨損）歸屬於非控制權益",
        "非控制權益（淨利／損）",
    ],
    "other_comprehensive_income": ["OtherComprehensiveIncome", "其他綜合損益（淨額）"],
    "eps": ["EPS", "BasicEarningsPerShare", "基本每股盈餘"],

    # ================= 03_資產負債表 (BS - 對齊 FinMind 實際大小寫) =================
    "total_assets": ["TotalAssets", "Assets", "資產總額", "資產總計"],
    "current_assets": ["CurrentAssets", "流動資產合計"],
    "cash_and_equiv": ["CashAndCashEquivalents", "現金及約當現金"],
    # 金融資產 (支援流動+非流動加總於 _anchor_sum)
    "fvpl_assets_curr": ["CurrentFinancialAssetsAtFairvalueThroughProfitOrLoss", "透過損益按公允價值衡量之金融資產－流動"],
    "fvpl_assets_noncurr": ["NonCurrentFinancialAssetsAtFairvalueThroughProfitOrLoss", "透過損益按公允價值衡量之金融資產－非流動"],
    "fvoci_assets_curr": ["FinancialAssetsAtFairvalueThroughOtherComprehensiveIncome", "透過其他綜合損益按公允價值衡量之金融資產－流動"],
    "fvoci_assets_noncurr": ["FinancialAssetsAtFairvalueThroughOtherComprehensiveIncomeNonCurrent", "透過其他綜合損益按公允價值衡量之金融資產－非流動"],
    "ac_assets_curr": ["FinancialAssetsAtAmortizedCost", "按攤銷後成本衡量之金融資產－流動"],
    "ac_assets_noncurr": ["FinancialAssetsAtAmortizedCostNonCurrent", "按攤銷後成本衡量之金融資產－非流動"],
    "equity_method_investments": ["InvestmentAccountedForUsingEquityMethod", "採用權益法之投資"],
    "bills_receivable": ["BillsReceivableNet", "應收票據淨額"],
    "accounts_receivable_only": ["AccountsReceivableNet", "應收帳款淨額"],
    "inventory": ["Inventories", "Inventory", "存貨"],
    "prepayments": ["Prepayments", "預付款項"],
    "total_liabilities": ["Liabilities", "TotalLiabilities", "負債總額", "負債總計"],
    "current_liabilities": ["CurrentLiabilities", "流動負債合計"],
    "accounts_payable": ["AccountsPayable", "NotesAndAccountsPayable", "應付帳款"],
    "other_payables": ["OtherPayables", "其他應付款"],
    "contract_liabilities": ["CurrentContractLiabilities", "合約負債", "合約負債－流動"],
    "short_term_debt": ["ShorttermBorrowings", "ShortTermBorrowings", "短期借款"],
    "long_term_debt": ["LongtermBorrowings", "LongTermBorrowings", "長期借款"],
    "parent_equity": ["EquityAttributableToOwnersOfParent", "歸屬於母公司業主之權益合計"],
    "total_equity": ["Equity", "TotalEquity", "權益總額", "權益總計"],

    # ================= 04_現金流量表 (CF - 含業外細項與營運資金變動) =================
    "operating_cf": [
        "CashFlowsFromOperatingActivities",
        "CashProvidedByOperatingActivities",
        "NetCashInflowFromOperatingActivities",
        "營業活動之淨現金流入（流出）",
    ],
    "investing_cf": [
        "CashProvidedByInvestingActivities",
        "CashFlowsProvidedFromInvestingActivities",
        "CashFlowsFromInvestingActivities",
        "NetCashUsedInInvestingActivities",
        "投資活動之淨現金流入（流出）",
    ],
    "financing_cf": [
        "CashFlowsProvidedFromFinancingActivities",
        "CashProvidedByFinancingActivities",
        "CashFlowsFromFinancingActivities",
        "NetCashUsedInFinancingActivities",
        "籌資活動之淨現金流入（流出）",
        "融資活動之淨現金流入（流出）",
    ],
    "capex": [
        "PropertyAndPlantAndEquipment",
        "AcquisitionOfPropertyPlantAndEquipment",
        "PayTheCashOfPropertyPlantAndEquipment",
        "取得不動產、廠房及設備",
        "固定資產(增加)減少",
    ],
    "depreciation": ["Depreciation", "DepreciationExpense", "折舊費用"],
    "amortization": ["Amortization", "AmortizationExpense", "攤銷費用"],
    "interest_expense_cf": ["InterestExpense", "利息費用", "利息費用提列(迴轉)數"],
    "interest_income_cf": ["InterestIncome", "利息收入"],
    "dividend_income_cf": ["DividendIncome", "股利收入", "收取之股利"],
    "equity_method_cf": [
        "InvestmentLossIncomeRecognizedUnderEquityMethod",
        "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod",
        "採用權益法認列之關聯企業及合資損失（利益）之份額",
        "採用權益法認列之關聯企業及合資損失(利益)之份額",
    ],
    "cf_change_ar": [
        "ReceivableIncrease",
        "AccountsReceivable",
        "DecreaseIncreaseInAccountsReceivable",
        "應收帳款（增加）減少",
        "應收帳款(增加)減少",
    ],
    "cf_change_inv": [
        "InventoryIncrease",
        "InventoryIncreaseDecrease",
        "DecreaseIncreaseInInventories",
        "存貨（增加）減少",
        "存貨(增加)減少",
    ],
    "cf_change_ap": [
        "AccountsPayable",
        "IncreaseDecreaseInAccountsPayable",
        "應付帳款增加（減少）",
        "應付帳款增加(減少)",
        "應付帳款",
    ],
    "cash_dividends_paid": ["CashDividendsPaid", "發放現金股利"],
}


BALANCE_TOLERANCE_RATIO = 0.0005
BALANCE_TOLERANCE_ABS = 1000.0


def get_expected_watermarks(today: date) -> tuple[str, str]:
    year = today.year
    month = today.month
    day = today.day

    rev_year = year
    rev_month = month - 1
    if rev_month == 0:
        rev_year -= 1
        rev_month = 12
    expected_rev_ym = f"{rev_year:04d}-{rev_month:02d}"

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
        "2. 三率、營業費用率、CCC現金轉換週期、自由現金流、EBITDA與成長率皆已由系統後端精確算妥，請直接引用表格數字，嚴禁自行計算或捏造。\n"
        "3. 回答請先給一句話結論，再列數據佐證與原因拆解（條列）。"
    ),
    "full_diagnosis": (
        "請根據數據給出：\n"
        "- 一句話核心結論\n"
        "- 0~100 分體質評分\n"
        "- 3 大亮點（含期別與數字）\n"
        "- 2 大風險（含期別與數字）\n"
        "- 深度財務拆解（含本業vs業外歸因、營運資金存貨/應收/應付變化、ROE與自由現金流含金量）\n"
    ),
    "targeted_qa": (
        "請只針對使用者問題作答，並引用表格內的期別與數字作為佐證。"
    ),
}