"""Configuration and schema definitions for /stock_ai fundamental caching (v3.0.0).

Includes:
- Dual-track FinMind mapping (English 'type' + Chinese 'origin_name' fallback)
- Deep accounting items (R&D/Selling/Admin expenses, Non-op items, Parent Net Income,
  AP, Contract Liabilities, Interest-bearing Debt, D&A, Working Capital CF, CCC, EBITDA)
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
        "selling_expenses",
        "admin_expenses",
        "rd_expenses",
        "operating_expenses",
        "operating_income",
        "interest_income",
        "other_income",
        "other_gains_losses",
        "finance_costs",
        "equity_method_income",
        "non_op_income_total",
        "pretax_income",
        "income_tax_expense",
        "net_income",
        "net_income_parent",
        "net_income_nci",
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
        "accounts_receivable",
        "inventory",
        "prepayments",
        "total_liabilities",
        "current_liabilities",
        "accounts_payable",
        "contract_liabilities",
        "short_term_debt",
        "short_term_bills",
        "current_portion_lt_debt",
        "long_term_debt",
        "bonds_payable",
        "parent_equity",
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
        "depreciation",
        "amortization",
        "cf_change_ar",
        "cf_change_inv",
        "cf_change_ap",
        "cash_dividends_paid",
        "updated_at",
    ],
    "05_AI_Metrics_Wide": [
        "pk_key",
        "stock_id",
        "raw_date",
        "year_qtr",
        "revenue_亿元",
        "gross_margin_pct",
        "selling_exp_pct",
        "admin_exp_pct",
        "rd_exp_pct",
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


# 同時收錄 FinMind 英文 type 與證交所中文 origin_name 關鍵字
ACCOUNT_ANCHOR_MAP: dict[str, list[str]] = {
    # ================= 綜合損益表 (IS) =================
    "revenue": [
        "Revenue",
        "OperatingRevenue",
        "NetRevenue",
        "InterestAndOtherNetRevenue",
        "營業收入合計",
        "淨收益",
    ],
    "cost_of_goods_sold": [
        "CostOfGoodsSold",
        "OperatingCosts",
        "營業成本合計",
        "營業成本",
    ],
    "gross_profit": [
        "GrossProfit",
        "NetOperatingMargin",
        "營業毛利（毛損）淨額",
        "營業毛利（毛損）",
        "營業毛利(毛損)淨額",
        "營業毛利(毛損)",
    ],
    "selling_expenses": [
        "SellingExpenses",
        "SellingAndMarketingExpenses",
        "推銷費用",
        "推銷費用合計",
    ],
    "admin_expenses": [
        "AdministrativeExpenses",
        "GeneralAndAdministrativeExpenses",
        "管理費用",
        "管理費用合計",
    ],
    "rd_expenses": [
        "ResearchAndDevelopmentExpenses",
        "ResearchAndDevelopment",
        "研究發展費用",
        "研究發展費用合計",
    ],
    "operating_expenses": [
        "OperatingExpenses",
        "TotalOperatingExpenses",
        "營業費用合計",
    ],
    "operating_income": [
        "OperatingIncome",
        "OperatingProfit",
        "營業利益（損失）",
        "營業利益(損失)",
        "營業淨利（淨損）",
    ],
    "interest_income": [
        "InterestIncome",
        "TotalInterestIncome",
        "利息收入",
        "利息收入合計",
    ],
    "other_income": [
        "OtherIncome",
        "其他收入",
    ],
    "other_gains_losses": [
        "OtherGainsAndLosses",
        "OtherGainsAndLossesNet",
        "其他利益及損失淨額",
        "其他利益及損失",
    ],
    "finance_costs": [
        "FinanceCosts",
        "InterestExpense",
        "財務成本淨額",
        "財務成本",
        "利息費用",
    ],
    "equity_method_income": [
        "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod",
        "InvestmentIncome",
        "採用權益法認列之關聯企業及合資損益之份額淨額",
        "採用權益法認列之關聯企業及合資損益之份額",
    ],
    "non_op_income_total": [
        "TotalNonoperatingIncomeAndExpense",
        "NonOperatingIncomeAndExpenses",
        "營業外收入及支出合計",
    ],
    "pretax_income": [
        "PreTaxIncome",
        "IncomeBeforeTax",
        "ContinuingOperationsIncomeBeforeTax",
        "稅前淨利（淨損）",
        "稅前淨利(淨損)",
        "繼續營業單位稅前淨利（淨損）",
    ],
    "income_tax_expense": [
        "IncomeTaxExpense",
        "IncomeTaxExpenseContinuingOperations",
        "所得稅費用（利益）合計",
        "所得稅費用(利益)合計",
        "所得稅費用（利益）",
    ],
    "net_income": [
        "IncomeAfterTaxes",
        "ProfitLoss",
        "NetIncome",
        "本期淨利（淨損）",
        "本期淨利(淨損)",
    ],
    "net_income_parent": [
        "EquityAttributableToOwnersOfParent",
        "ProfitLossAttributableToOwnersOfParent",
        "EquityAttributableToOwnersOfParent_Income",
        "母公司業主（淨利／損）",
        "母公司業主(淨利／損)",
        "歸屬於母公司業主之淨利（損）",
    ],
    "net_income_nci": [
        "NoncontrollingInterests",
        "非控制權益（淨利／損）",
        "非控制權益(淨利／損)",
    ],
    "eps": [
        "EPS",
        "BasicEarningsPerShare",
        "基本每股盈餘合計",
        "基本每股盈餘",
    ],
    # ================= 資產負債表 (BS) =================
    "total_assets": [
        "TotalAssets",
        "Assets",
        "資產總計",
        "資產總額",
    ],
    "current_assets": [
        "CurrentAssets",
        "TotalCurrentAssets",
        "流動資產合計",
    ],
    "cash_and_equiv": [
        "CashAndCashEquivalents",
        "CashAndDueFromBanks",
        "現金及約當現金",
        "現金及約當現金合計",
    ],
    "fvpl_assets": [
        "CurrentFinancialAssetsAtFairValueThroughProfitOrLoss",
        "透過損益按公允價值衡量之金融資產－流動",
    ],
    "fvoci_assets": [
        "CurrentFinancialAssetsAtFairValueThroughOtherComprehensiveIncome",
        "NoncurrentFinancialAssetsAtFairValueThroughOtherComprehensiveIncome",
        "透過其他綜合損益按公允價值衡量之金融資產－流動",
        "透過其他綜合損益按公允價值衡量之金融資產－非流動",
    ],
    "ac_assets": [
        "CurrentFinancialAssetsAtAmortisedCost",
        "NoncurrentFinancialAssetsAtAmortisedCost",
        "按攤銷後成本衡量之金融資產－流動",
    ],
    "accounts_receivable": [
        "AccountsReceivableNet",
        "NotesAndAccountsReceivableNet",
        "應收帳款淨額",
    ],
    "inventory": [
        "Inventories",
        "Inventory",
        "存貨",
        "存貨合計",
    ],
    "prepayments": [
        "Prepayments",
        "預付款項",
        "預付款項合計",
    ],
    "total_liabilities": [
        "TotalLiabilities",
        "Liabilities",
        "負債總計",
        "負債總額",
    ],
    "current_liabilities": [
        "CurrentLiabilities",
        "TotalCurrentLiabilities",
        "流動負債合計",
    ],
    "accounts_payable": [
        "AccountsPayable",
        "NotesAndAccountsPayable",
        "應付帳款",
        "應付帳款合計",
    ],
    "contract_liabilities": [
        "CurrentContractLiabilities",
        "ContractLiabilities_Current",
        "合約負債－流動",
        "合約負債-流動",
    ],
    "short_term_debt": [
        "ShortTermBorrowings",
        "ShortTermLoans",
        "短期借款",
        "短期借款合計",
    ],
    "short_term_bills": [
        "ShortTermNotesAndBillsPayable",
        "應付短期票券",
    ],
    "current_portion_lt_debt": [
        "LongTermLiabilitiesCurrentPortion",
        "CurrentPortionOfLongTermBorrowings",
        "一年或一營業週期內到期長期負債",
    ],
    "long_term_debt": [
        "LongTermBorrowings",
        "LongTermDebt",
        "長期借款",
    ],
    "bonds_payable": [
        "BondsPayable",
        "應付公司債",
    ],
    "parent_equity": [
        "EquityAttributableToOwnersOfParent",
        "歸屬於母公司業主之權益合計",
        "歸屬於母公司業主之權益",
    ],
    "total_equity": [
        "Equity",
        "TotalEquity",
        "StockholdersEquity",
        "權益總計",
        "權益總額",
    ],
    # ================= 現金流量表 (CF) =================
    "operating_cf": [
        "CashFlowsFromOperatingActivities",
        "CashProvidedByOperatingActivities",
        "NetCashInflowFromOperatingActivities",
        "營業活動之淨現金流入（流出）",
        "營業活動之淨現金流入(流出)",
    ],
    "investing_cf": [
        "CashProvidedByInvestingActivities",
        "CashFlowsProvidedFromInvestingActivities",
        "CashFlowsFromInvestingActivities",
        "NetCashUsedInInvestingActivities",
        "NetCashInflowFromInvestingActivities",
        "投資活動之淨現金流入（流出）",
        "投資活動之淨現金流入(流出)",
    ],
    "financing_cf": [
        "CashFlowsProvidedFromFinancingActivities",
        "CashProvidedByFinancingActivities",
        "CashFlowsFromFinancingActivities",
        "NetCashUsedInFinancingActivities",
        "NetCashInflowFromFinancingActivities",
        "籌資活動之淨現金流入（流出）",
        "籌資活動之淨現金流入(流出)",
        "融資活動之淨現金流入（流出）",
    ],
    "capex": [
        "PropertyAndPlantAndEquipment",
        "AcquisitionOfPropertyPlantAndEquipment",
        "PayTheCashOfPropertyPlantAndEquipment",
        "取得不動產、廠房及設備",
    ],
    "depreciation": [
        "DepreciationExpense",
        "Depreciation",
        "折舊費用",
    ],
    "amortization": [
        "AmortizationExpense",
        "Amortization",
        "攤銷費用",
    ],
    "cf_change_ar": [
        "DecreaseIncreaseInAccountsReceivable",
        "DecreaseIncreaseInNotesAndAccountsReceivable",
        "應收帳款（增加）減少",
        "應收帳款(增加)減少",
    ],
    "cf_change_inv": [
        "DecreaseIncreaseInInventories",
        "DecreaseIncreaseInInventory",
        "存貨（增加）減少",
        "存貨(增加)減少",
    ],
    "cf_change_ap": [
        "IncreaseDecreaseInAccountsPayable",
        "應付帳款增加（減少）",
        "應付帳款增加(減少)",
    ],
    "cash_dividends_paid": [
        "CashDividendsPaid",
        "發放現金股利",
    ],
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
        "2. 三率、三項費用率、CCC現金轉換週期、自由現金流、EBITDA與成長率皆已由系統後端精確算妥（現金流量表已由累季還原為真實單季），請直接引用表格數字，嚴禁自行計算或捏造。\n"
        "3. 回答請先給一句話結論，再列數據佐證與原因拆解（條列）。"
    ),
    "full_diagnosis": (
        "請根據數據給出：\n"
        "- 一句話核心結論\n"
        "- 0~100 分體質評分\n"
        "- 3 大亮點（含期別與數字，涵蓋本業三率/費用率、CCC週轉或現金流）\n"
        "- 2 大風險（含期別與數字）\n"
        "- 深度財務拆解（含本業vs業外歸因、營運資金存貨/應收/應付變化、ROE與自由現金流含金量）\n"
    ),
    "targeted_qa": (
        "請只針對使用者問題作答，並引用表格內的期別與數字作為佐證。"
    ),
}