"""Configuration and schema definitions for /stock_ai fundamental caching.

SASD v3.1.0 (100% Verified against FinMind Real-Machine Schema for 2357/2330)
"""

from __future__ import annotations

from datetime import date
from typing import TypedDict


class AnchorSpec(TypedDict):
    types: list[str]
    origins: list[str]


SHEET_SCHEMAS: dict[str, list[str]] = {
    "00_Watchlist_Master": [
        "stock_id",
        "stock_name",
        "industry_category",
        "max_rev_raw_date",
        "last_rev_ym",
        "max_fin_raw_date",
        "last_fin_qtr",
        "max_div_raw_date",
        "max_chip_raw_date",
        "last_probe_at",
        "ai_cache_day",
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
        "net_income_total",
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
        "cash_and_equiv",
        "fvpl_financial_assets",
        "fvoci_financial_assets",
        "ac_financial_assets",
        "accounts_receivable",
        "inventory",
        "prepayments",
        "current_assets",
        "ppe_assets",
        "intangible_assets",
        "total_assets",
        "short_term_debt",
        "contract_liab_current",
        "accounts_payable",
        "other_payables",
        "current_liabilities",
        "bonds_payable",
        "long_term_debt",
        "total_liabilities",
        "capital_stock",
        "retained_earnings",
        "parent_equity",
        "total_equity",
        "updated_at",
    ],
    "04_Cash_Flow": [
        "pk_key",
        "stock_id",
        "raw_date",
        "year_qtr",
        "depreciation",
        "amortization",
        "interest_income",
        "interest_expense",
        "cf_change_ar",
        "cf_change_inv",
        "cf_change_ap",
        "operating_cf",
        "capex",
        "investing_cf",
        "cf_debt_net_change",
        "financing_cf",
        "free_cf",
        "updated_at",
    ],
    "05_AI_Metrics_Wide": [
        "pk_key",
        "stock_id",
        "raw_date",
        "year_qtr",
        "rev_亿元",
        "gross_margin_pct",
        "op_expense_pct",
        "op_margin_pct",
        "non_op_share_pct",
        "parent_net_margin_pct",
        "eps",
        "ttm_eps",
        "parent_roe_pct",
        "dio_days",
        "dso_days",
        "dpo_days",
        "ccc_days",
        "contract_liab_亿元",
        "ocf_亿元",
        "capex_亿元",
        "fcf_亿元",
        "ocf_to_ni_pct",
        "ebitda_亿元",
        "total_debt_亿元",
        "net_debt_ratio_pct",
        "current_ratio_pct",
        "quick_ratio_pct",
        "interest_coverage",
        "qtr_rev_yoy_pct",
        "updated_at",
    ],
    "06_Dividend_Policy": [
        "pk_key",
        "stock_id",
        "raw_date",
        "div_year",
        "cash_dividend",
        "stock_dividend",
        "total_dividend",
        "total_cash_dividend_亿元",
        "annual_eps",
        "payout_ratio_pct",
        "ex_dividend_date",
        "payment_date",
        "updated_at",
    ],
    "07_Chip_Summary": [
        "pk_key",
        "stock_id",
        "raw_date",
        "trade_week",
        "foreign_holding_pct",
        "foreign_holding_chg_20d",
        "foreign_net_buy_5d_lots",
        "foreign_net_buy_20d_lots",
        "trust_net_buy_5d_lots",
        "trust_net_buy_20d_lots",
        "dealer_net_buy_5d_lots",
        "dealer_net_buy_20d_lots",
        "three_inst_net_5d_lots",
        "three_inst_net_20d_lots",
        "updated_at",
    ],
    "MA_Signal_Tracker": [
        "執行日期",
        "K線日期",
        "市場別",
        "產業類別",
        "股票代號",
        "股票名稱",
        "當日新進",
        "連續天數",
        "每日執行狀況",
        "訊號類型",
        "17MA近期走勢",
        "收盤價",
        "3MA",
        "17MA",
        "45MA",
        "117MA",
        "189MA",
        "17與45MA差距(%)",
        "均線排列檢核",
        "更新時間",
    ],
}


def _a(*, types: list[str], origins: list[str]) -> AnchorSpec:
    return {"types": types, "origins": origins}


# ACCOUNT_ANCHOR_MAP: 100% aligned with FinMind real output
ACCOUNT_ANCHOR_MAP: dict[str, AnchorSpec] = {
    # ================= 02_綜合損益表 (IS) =================
    "revenue": _a(
        types=["Revenue", "OperatingRevenue", "NetRevenue"],
        origins=["營業收入", "營業收入合計", "淨收益"],
    ),
    "cost_of_goods_sold": _a(
        types=["CostOfGoodsSold", "OperatingCosts"],
        origins=["營業成本", "營業成本合計"],
    ),
    "gross_profit": _a(
        types=["GrossProfit", "NetOperatingMargin"],
        origins=["營業毛利（毛損）", "營業毛利（毛損）淨額"],
    ),
    "operating_expenses": _a(
        types=["OperatingExpenses", "TotalOperatingExpenses"],
        origins=["營業費用", "營業費用合計"],
    ),
    "operating_income": _a(
        types=["OperatingIncome", "OperatingProfit"],
        origins=["營業利益（損失）", "營業淨利（淨損）"],
    ),
    "non_op_income_total": _a(
        types=["TotalNonoperatingIncomeAndExpense", "NonOperatingIncomeAndExpenses"],
        origins=["營業外收入及支出", "營業外收入及支出合計"],
    ),
    "pretax_income": _a(
        types=["PreTaxIncome", "IncomeBeforeTax"],
        origins=["稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）"],
    ),
    "income_tax_expense": _a(
        types=["TAX", "IncomeTaxExpense", "IncomeTaxExpenseContinuingOperations"],
        origins=["所得稅費用（利益）", "所得稅費用（利益）合計"],
    ),
    "net_income_total": _a(
        types=["IncomeAfterTaxes", "ProfitLoss", "IncomeFromContinuingOperations"],
        origins=["本期淨利（淨損）", "繼續營業單位本期淨利（淨損）"],
    ),
    "net_income_parent": _a(
        types=["EquityAttributableToOwnersOfParent", "ProfitLossAttributableToOwnersOfParent"],
        origins=["淨利（淨損）歸屬於母公司業主", "母公司業主（淨利／損）", "歸屬於母公司業主之淨利（損）"],
    ),
    "net_income_nci": _a(
        types=["NoncontrollingInterests"],
        origins=["淨利（淨損）歸屬於非控制權益", "非控制權益（淨利／損）"],
    ),
    "eps": _a(
        types=["EPS", "BasicEarningsPerShare"],
        origins=["基本每股盈餘", "基本每股盈餘合計"],
    ),

    # ================= 03_資產負債表 (BS) =================
    "cash_and_equiv": _a(
        types=["CashAndCashEquivalents"],
        origins=["現金及約當現金", "現金及約當現金合計"],
    ),
    "current_assets": _a(
        types=["CurrentAssets", "TotalCurrentAssets"],
        origins=["流動資產合計"],
    ),
    "ppe_assets": _a(
        types=["PropertyPlantAndEquipment"],
        origins=["不動產、廠房及設備"],
    ),
    "intangible_assets": _a(
        types=["IntangibleAssets"],
        origins=["無形資產"],
    ),
    "total_assets": _a(
        types=["TotalAssets", "Assets"],
        origins=["資產總額", "資產總計"],
    ),
    "total_liabilities": _a(
        types=["Liabilities", "TotalLiabilities"],
        origins=["負債總額", "負債總計"],
    ),
    "current_liabilities": _a(
        types=["CurrentLiabilities", "TotalCurrentLiabilities"],
        origins=["流動負債合計"],
    ),
    "capital_stock": _a(
        types=["CapitalStock", "OrdinaryShare"],
        origins=["股本合計", "普通股股本"],
    ),
    "retained_earnings": _a(
        types=["RetainedEarnings"],
        origins=["保留盈餘合計"],
    ),
    "parent_equity": _a(
        types=["EquityAttributableToOwnersOfParent"],
        origins=["歸屬於母公司業主之權益合計"],
    ),
    "total_equity": _a(
        types=["Equity", "TotalEquity"],
        origins=["權益總額", "權益總計"],
    ),
    "inventory": _a(types=["Inventories", "Inventory"], origins=["存貨", "存貨合計"]),
    "prepayments": _a(types=["Prepayments", "OtherPrepayments"], origins=["預付款項", "預付款項合計"]),

    # financial assets: current + noncurrent (summed in cache)
    "fvpl_financial_assets_curr": _a(
        types=[
            "CurrentFinancialAssetsAtFairvalueThroughProfitOrLoss",
            "CurrentFinancialAssetsAtFairValueThroughProfitOrLoss",
        ],
        origins=["透過損益按公允價值衡量之金融資產－流動"],
    ),
    "fvpl_financial_assets_noncurr": _a(
        types=[
            "NonCurrentFinancialAssetsAtFairvalueThroughProfitOrLoss",
            "NoncurrentFinancialAssetsAtFairValueThroughProfitOrLoss",
        ],
        origins=["透過損益按公允價值衡量之金融資產－非流動"],
    ),
    "fvoci_financial_assets_curr": _a(
        types=[
            "FinancialAssetsAtFairvalueThroughOtherComprehensiveIncome",
            "CurrentFinancialAssetsAtFairValueThroughOtherComprehensiveIncome",
        ],
        origins=["透過其他綜合損益按公允價值衡量之金融資產－流動"],
    ),
    "fvoci_financial_assets_noncurr": _a(
        types=[
            "FinancialAssetsAtFairvalueThroughOtherComprehensiveIncomeNonCurrent",
            "NoncurrentFinancialAssetsAtFairValueThroughOtherComprehensiveIncome",
        ],
        origins=["透過其他綜合損益按公允價值衡量之金融資產－非流動"],
    ),
    "ac_financial_assets_curr": _a(
        types=[
            "FinancialAssetsAtAmortizedCost",
            "CurrentFinancialAssetsAtAmortisedCost",
            "CurrentFinancialAssetsAtAmortizedCost",
        ],
        origins=["按攤銷後成本衡量之金融資產－流動"],
    ),
    "ac_financial_assets_noncurr": _a(
        types=[
            "FinancialAssetsAtAmortizedCostNonCurrent",
            "NoncurrentFinancialAssetsAtAmortisedCost",
            "NoncurrentFinancialAssetsAtAmortizedCost",
        ],
        origins=["按攤銷後成本衡量之金融資產－非流動"],
    ),

    # accounts receivable: notes/bills + accounts (summed in cache)
    "accounts_receivable_only": _a(
        types=["AccountsReceivableNet", "NotesAndAccountsReceivableNet"],
        origins=["應收帳款淨額", "應收帳款"],
    ),
    "notes_receivable": _a(
        types=["BillsReceivableNet", "NotesReceivableNet", "NotesReceivable"],
        origins=["應收票據淨額", "應收票據"],
    ),

    # payables
    "accounts_payable": _a(
        types=["AccountsPayable", "NotesAndAccountsPayable"],
        origins=["應付帳款", "應付票據"],
    ),
    "other_payables": _a(
        types=["OtherPayables"],
        origins=["其他應付款"],
    ),

    # contract liabilities: current + noncurrent (summed in cache)
    "contract_liab_curr": _a(
        types=["CurrentContractLiabilities", "ContractLiabilities_Current"],
        origins=["合約負債", "合約負債－流動"],
    ),
    "contract_liab_noncurr": _a(
        types=["NoncurrentContractLiabilities"],
        origins=["合約負債－非流動"],
    ),

    # interest-bearing debt components
    "short_term_debt": _a(
        types=["ShorttermBorrowings", "ShortTermBorrowings", "ShortTermLoans"],
        origins=["短期借款", "短期借款合計"],
    ),
    "bonds_payable": _a(types=["BondsPayable"], origins=["應付公司債", "應付公司債合計"]),
    "long_term_debt": _a(
        types=["LongtermBorrowings", "LongTermBorrowings", "LongTermDebt"],
        origins=["長期借款", "長期借款合計"],
    ),

    # ================= 04_現金流量表 (CF) =================
    "depreciation": _a(types=["Depreciation", "DepreciationExpense"], origins=["折舊費用"]),
    "amortization": _a(types=["AmortizationExpense", "Amortization"], origins=["攤銷費用"]),
    "interest_income": _a(types=["InterestIncome"], origins=["利息收入"]),
    "interest_expense": _a(types=["InterestExpense", "PayTheInterest"], origins=["利息費用", "支付之利息"]),
    "cf_change_ar": _a(
        types=[
            "ReceivableIncrease",
            "DecreaseIncreaseInAccountsReceivable",
            "DecreaseIncreaseInNotesAndAccountsReceivable",
        ],
        origins=["應收帳款（增加）減少", "應收帳款(增加)減少"],
    ),
    "cf_change_inv": _a(
        types=[
            "InventoryIncrease",
            "DecreaseIncreaseInInventories",
            "DecreaseIncreaseInInventory",
        ],
        origins=["存貨（增加）減少", "存貨(增加)減少"],
    ),
    "cf_change_ap": _a(
        types=["AccountsPayable", "IncreaseDecreaseInAccountsPayable"],
        origins=["應付帳款", "應付帳款增加（減少）", "應付帳款增加(減少)"],
    ),
    "operating_cf": _a(
        types=[
            "CashFlowsFromOperatingActivities",
            "NetCashInflowFromOperatingActivities",
            "CashProvidedByOperatingActivities",
        ],
        origins=["營業活動之淨現金流入（流出）", "營業活動之淨現金流入"],
    ),
    "capex": _a(
        types=["PropertyAndPlantAndEquipment", "AcquisitionOfPropertyPlantAndEquipment"],
        origins=["取得不動產、廠房及設備"],
    ),
    "investing_cf": _a(
        types=[
            "CashProvidedByInvestingActivities",
            "CashFlowsProvidedFromInvestingActivities",
            "CashFlowsFromInvestingActivities",
        ],
        origins=["投資活動之淨現金流入（流出）"],
    ),
    "financing_cf": _a(
        types=[
            "CashFlowsProvidedFromFinancingActivities",
            "CashProvidedByFinancingActivities",
            "CashFlowsFromFinancingActivities",
        ],
        origins=["籌資活動之淨現金流入（流出）", "融資活動之淨現金流入（流出）"],
    ),

    # debt change components (summed in cache)
    "cf_debt_st_increase": _a(
        types=["IncreaseInShortTermLoans", "DecreaseInShortTermLoans"],
        origins=["短期借款增加", "短期借款減少"],
    ),
    "cf_debt_lt_proceeds": _a(types=["ProceedsFromLongTermDebt"], origins=["舉借長期借款"]),
    "cf_debt_lt_repayments": _a(
        types=["RepaymentOfLongTermDebt", "RepaymentsOfLongTermDebt"],
        origins=["償還長期借款"],
    ),
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
        "- 股利政策與近期籌碼（外資/投信/自營商）變化重點\n"
        "- 若數據包內附有「表 7：近期日線與均線排列動態 (MA3/17/45/117/189)」，請一併結合均線排列與 17MA 近期走勢給出技術面觀察（若無則略過）\n"
        "- 若有重大新聞，請結合新聞標題作風險/催化劑推論\n"
    ),
    "targeted_qa": (
        "請只針對使用者問題作答，並引用表格內的期別與數字作為佐證。"
    ),
}