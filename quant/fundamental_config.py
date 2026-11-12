"""Configuration and schema definitions for /stock_ai fundamental caching.

SASD v3.1.0 (100% Verified against FinMind Real-Machine Schema for 2357/2330)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TypedDict


@dataclass(frozen=True)
class FundamentalEngineConfig:
    # 股數反推防呆門檻：避免 |EPS| 過小造成除以零或四捨五入分母膨脹誤差
    MIN_SAFE_EPS_FOR_SHARE_INFERENCE: float = 0.25
    MAX_HISTORICAL_QUARTERS_LOOKBACK: int = 8

    # 本業前瞻 EPS 推算之等效有效稅率係數 (1 - 20% 營所稅)
    EFFECTIVE_AFTER_TAX_FACTOR: float = 0.80

    # 營收動能與季節性參數
    NORMAL_MONTH_MA3_TOLERANCE: float = 0.95      # 一般月份允許最新月營收 >= 近3月均線 * 0.95
    QTD_STRONG_RUN_RATE_1M: float = 0.36          # 季內第1個月達成上季總營收 >= 36% 視為超前 (基準33.3%)
    QTD_STRONG_RUN_RATE_2M: float = 0.72          # 季內前2個月達成上季總營收 >= 72% 視為超前 (基準66.7%)

    # 三率變動顯著性門檻 (單位：百分點 percentage points，1.5 代表 150 bps)
    MARGIN_EXPANSION_BPS_THRESHOLD: float = 1.5
    MARGIN_CONTRACTION_BPS_THRESHOLD: float = -1.5

    # 橫斷面相對估值折溢價門檻
    SECTOR_PE_UNDERVALUED_RATIO: float = 0.85     # Forward PE < 同業中位數 * 0.85 視為低估
    SECTOR_PE_OVERHEATED_RATIO: float = 1.35      # Forward PE > 同業中位數 * 1.35 視為溢價偏高


DEFAULT_FUNDAMENTAL_ENGINE_CONFIG = FundamentalEngineConfig()


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
        "ai_model_version",
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
        # --- v5.5.0 新增個股信用交易與斷頭風控欄位 ---
        "margin_balance_lots",          # 最新融資餘額 (張)
        "margin_utilization_pct",       # 最新融資使用率 (%) = 融資餘額 / 融資限額 * 100
        "margin_chg_5d_lots",           # 近 5 日融資增減 (張)
        "margin_chg_20d_lots",          # 近 20 日融資增減 (張)
        "short_margin_ratio_pct",       # 券資比 (%) = 融券餘額 / 融資餘額 * 100
        "est_margin_maint_pct",         # 估算個股近20日融資維持率 (%)
        "chip_quadrant_tag",            # 資法人對作四象限標籤
        "updated_at",
    ],
    "08_Stock_Taxonomy": [
        "stock_id",  # 股票代號 (PK, 4碼字串如 "2327")
        "stock_name",  # 股票名稱
        "official_industry",  # L1 證交所/FinMind 官方大類
        "sector_group",  # L2 六大板塊分群 (Enum)
        "sub_industry",  # L3 細分次產業（用於 Stage 2 同業限額分群）
        "valuation_regime",  # 估值體系屬性 (景氣循環股/穩健價值與高息股/成長型科技與利基股/高本益比題材研發股)
        "component_nature",  # 主動元件/被動元件/機構與模組周邊/系統與終端設備/非電子/不適用
        "supply_chain_role",  # 價值鏈位置 (Enum)
        "core_products",  # 主力產品與業務簡述 (50字內)
        "theme_tags",  # 概念題材標籤 (JSON Array 字串)
        "peers",  # 台股直接競爭同業 (JSON Array 字串)
        "updated_at",  # 更新日期 YYYY-MM-DD (TTL 90天)
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
        "成交量(張)",
        "5日均量(張)",
        "20日均量(張)",
        "量能狀態(量比)",
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
    "AI_Daily_Picks": [
        "pk_key",
        "scan_date",
        "rank",
        "category",
        "stock_id",
        "stock_name",
        "industry_category",
        "close_at_pick",
        "ma17_at_pick",
        "ma45_at_pick",
        "signal_type",
        "ma17_trend",
        "streak_days",
        "fundamental_score",
        "final_pick_score",
        "outlook_verdict",
        "entry_thesis",
        "risk_note",
        "war_room_url",
        "updated_at",
        "tactical_type",
        "target_price_1",
        "target_price_2",
        "stop_loss_price",
        "Audit_Grade",
        "Execution_Tactic",
        "Ideal_Buy_Or_Ref",
        "Chase_Limit_Price",
        "Hard_Stop_Loss",
        "TP1_Target_Or_POC",
        "Deterministic_RR",
        "Bear_Case_Warning",
        "Invalidation_Triggers",
    ],
    "Rebound_Watchlist": [
        "pk_key",
        "stock_id",
        "stock_name",
        "industry_category",
        "scan_date",
        "close_price",
        "high_60d",
        "drop_amount",
        "drop_pct",
        "support_ma",
        "support_price",
        "target_ma17",
        "potential_return_pct",
        "volume_status",
        "market_light",
        "ai_summary_json",
        "ai_model_version",
        "war_room_url",
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
        origins=["資產總額", "資產總計", "資產合計"],
    ),
    "total_liabilities": _a(
        types=["TotalLiabilities", "Liabilities"],
        origins=["負債總額", "負債總計", "負債合計"],
    ),
    "current_liabilities": _a(
        types=["CurrentLiabilities", "TotalCurrentLiabilities"],
        origins=["流動負債合計", "流動負債總計"],
    ),
    "capital_stock": _a(
        types=["CapitalStock", "OrdinaryShare"],
        origins=["股本合計", "普通股股本", "股本"],
    ),
    "retained_earnings": _a(
        types=["RetainedEarnings", "TotalRetainedEarnings"],
        origins=["保留盈餘合計", "保留盈餘"],
    ),
    "parent_equity": _a(
        types=["EquityAttributableToOwnersOfParent"],
        origins=["歸屬於母公司業主之權益合計", "歸屬於母公司業主之權益"],
    ),
    "total_equity": _a(
        types=["TotalEquity", "Equity"],
        origins=["權益總額", "權益總計", "權益合計", "股東權益總計", "股東權益合計"],
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
        "【數據包引用與防偽鐵則】\n"
        "請嚴格根據下方由系統從 Google Sheets 快取資料庫與本地價格庫提取的真實數據進行分析：\n"
        "1. 數據精確性：引用任何數據時，必須明確標註期別（如 2026Q2 或 2026-09）與精確數值；嚴禁將季度(Q)與月份(M)混用（如嚴禁寫出 2026Q9）。\n"
        "2. 零捏造與免心算原則：所有三率、費用率、CCC、FCF、EBITDA、成長率、均線、PE、PB、殖利率與乖離率皆已由系統後端精確算妥，請直接引用表格與標頭數字，嚴禁自行重新計算或捏造表格外數據。"
    ),
    # Stage 1：單股全能審計（/stock_ai 與排程選股共用）。{DATA_PACKAGE_PLACEHOLDER} 由 analyzer 以 .replace 填入。
    "full_diagnosis": """你是一位極度嚴謹的台股基本面財務審計師、量化系統架構師與實戰操盤手。
請嚴格根據下方由系統從資料庫提取的真實數據進行深度診斷。

【核心審計鐵則（違反任一條即視為審計失敗）】
1. 數據精確與期別強制標註：引用任何數據時，必須明確標註期別（如 2026Q2、2026-09 或 2026-W45）與精確數值；嚴禁將季度(Q)與月份(M)混用（如嚴禁寫出 2026Q9）。注意各表數值單位（金額已統一轉為「億元」，每股/股價為「元」，籌碼為「張」）。
2. 零捏造與免心算原則：所有財務比率、CCC、FCF、均線、PE、PB、近一年現金殖利率與 17MA 乖離率皆已由後端算妥並顯示於標頭與各表，請直接引用，嚴禁自行捏造表格外數據或強行心算除法。若特定欄位標示為 N/A 或 ⚠️ 資料缺漏/過期，請如實陳述「資料缺漏」。
3. 拒絕湊數與截斷：分析亮點與風險時「重質不重量」（1~4 點動態調整）。體質極差或技術面嚴重超買之標的，請如實列出多項風險，嚴禁強行湊滿亮點；若有重大異常（如量比 > 4.0x 天量、正乖離 > +12%、法人連續大賣、單季毛利率驟降、業外嚴重扭曲），必須全數列入風險。
4. 全域風控門檻統一（與 Stage 2 投資長標準 100% 對齊）：
   - 流動性硬傷：當日成交量 < 500 張（極度冷門一票否決為 < 300 張）。
   - 天量過熱硬傷：量比（今日量 / 20日均量）> 4.0x。
   - 正乖離過熱硬傷：最新收盤價高於 17MA + 12%（即正乖離率 > +12.0%）。
   - 均線走弱硬傷：17MA 近 5 日走勢下彎（斜率 < 0%）。
5. 先審計後定論：請嚴格依序執行 Step 1 至 Step 7，完成所有交叉拆解後，才推導出體質評分與 JSON 摘要。

==================================================
{DATA_PACKAGE_PLACEHOLDER}
==================================================

【審計任務步驟（請依序輸出完整文字分析）】
- Step 1【損益趨勢與本業純度拆解】：
  分析近 8 季（表 1）毛利率、營益率與營業費用率是「三率連續擴張、本業轉佳、持平震盪、毛利受壓、還是本業虧損」？檢視業外佔稅前淨利比（non_op_share_pct = 業外淨收支 ÷ 稅前淨利；絕對值 > 20% 代表顯著受業外匯兌/轉投資/處分損益影響；若本業微利接近損益兩平時分母極小會導致該比率飆高，請同步對照 op_margin_pct 與 parent_net_margin_pct 差額），判斷單季 EPS 與 TTM EPS 是否受業外一次性損益扭曲。
- Step 2【營運資金、先行指標與現金流定性】：
  結合表 2（DIO/DSO/DPO/CCC、合約負債）與表 3、表 4（OCF、CapEx、FCF、ROE、淨負債比）。
  * 關鍵定性：將自由現金流（free_cf）明確定性為以下四類之一：
    1.「充沛正現金流」（單季與累計 FCF 穩定為正，且 OCF/NI > 80%）
    2.「戰略擴產型流出」（單季 FCF 為負，但 ROE >= 15% + 合約負債或營收高成長 + CapEx 顯著投入）
    3.「營運資金短期佔用」（單季 FCF 為負，主因旺季備料或應收帳款季節性增加，但本業獲利穩健）
    4.「結構性失血流出」（本業衰退或虧損 + 存貨滯銷/應收帳款逾期堆積導致之負現金流）
- Step 3【月營收動能與法說展望交叉查核】：
  * 3A（擷取展望）：僅根據「近期法說會與管理層未來展望檢索」及「即時新聞」中管理層對訂單/產能/毛利的具體公開說法進行提取（忽略單純盤中股價漲跌或僅有媒體標題而無內容之項目）。若無具體實質展望內容，claims 必須填寫「無」，且展望評級強制設為「無資料」。
  * 3B（財報驗證）：以表 1~表 5 數據（特別是合約負債與近 3 個月營收 YoY 趨勢）檢驗展望真偽，給出評級：高度可信 / 待驗證 / 存疑 / 無資料。注意：若最新月營收期別落後當前日期超過 2 個月以上（標示 ⚠️ 資料缺漏/過期），必須標註資料過期風險。
- Step 4【法人與融資籌碼結構、斷頭維持率與股利保護】：
  * 檢視表 6 外資與投信近 5 日、20 日買賣超方向（同買 / 土洋對作 / 同步調節），並與「融資 5 日/20 日增減、融資使用率、券資比、估算融資維持率 (est_margin_maint_pct)」交叉比對：
  * 明確判定籌碼歸屬：「🟢 法人鎖碼散戶退場（最優）」、「🟡 資法人同買（留意短線浮額）」、「🔴 法人倒貨散戶融資接刀（極度危險，須列入核心風險並大幅扣分）」或「⚡ 急跌後融資斷頭大減清洗完畢」。若估算融資維持率落於 130%~142% 追繳臨界區且融資未退，須發出斷頭踩踏警告。
  * 直接引用標頭預計算之「近一年現金殖利率」與配息率，評估下檔防禦保護力。
- Step 5【技術位階與量價配合度】：
  結合表 7 均線排列、17MA 走勢斜率、量比（今日量/20日均量）與 17MA 乖離率。明確檢核是否觸發【核心審計鐵則 4】之四大硬傷（量比 > 4.0x、正乖離 > +12%、17MA 下彎、日量 < 500 張）。
- Step 6【實戰交易計畫與紅軍壓力測試】：
  * 操作定位（tactical_type）：嚴格歸類為以下四者之一：
    - `🎯 短中期波段 (5~20日)`：基本面穩健、均線多頭且乖離 < +12%、量價配合良好。
    - `⚡ 短線動能價差`：題材或技術面強勢、但乖離偏大或基本面僅具轉機性。
    - `🏦 長線價值抱單`：低估值、高殖利率、高現金流、適合逢低長線配置。
    - `🚫 觀望避開`：觸發本業虧損無支撐、正乖離 > +12% 嚴重超漲、量比 > 4.0x 天量出貨、17MA 顯著下彎或日量 < 300 張。
  * 進場觸發條件（entry_trigger）：
    - 若量縮整理（量比 < 0.75x）：須設定「拉回 17MA/45MA 支撐區低接」或「帶量突破 3MA 且日量達 5 日均量 1.2 倍以上始進場」。
    - 若量能平穩/溫和放量（0.75x ~ 3.5x）且乖離合理（< +8%）：設定於「3MA ~ 17MA 區間分批承接」。
    - 若超漲過熱（正乖離 >= +8% 或已突破 +12%）或歸類為觀望避開：必須設定「嚴禁現價追高，靜待拉回 17MA 或關鍵支撐區且量縮止穩後再評估」。
  * 目標價與停損測幅（估值與防呆鐵則）：
    - 估值乘數限制：請依據標頭之 `valuation_regime` 設定目標本益比（景氣循環股 PE 嚴禁超過 8~10 倍；穩健價值與高息股 10~15 倍；成長型科技與利基股 16~25 倍）。
    - 目標價防呆：
      (a) 當收盤價未超漲（正乖離 <= +12%）且非觀望避開時，`target_price_1` 與 `target_price_2` 必須高於最新收盤價，且 `target_price_2 > target_price_1`。
      (b) 【超漲與觀望豁免條款】：若最新收盤價已高於 `17MA+12%`（正乖離 > +12%）或操作定位為 `🚫 觀望避開`，代表短線嚴重超買或觸發硬傷，此時嚴禁為了高於收盤價而盲目喊出高估目標價！請以「拉回建議低接價」作為基準設定反彈目標價，或直接標註「現價超漲不設追高目標價，待拉回 17MA 重估」。
    - 嚴格停損價（stop_loss_price）：設定於「建議進場價 -4%」或「實質跌破 17MA / 45MA 關鍵防守均線」（必須低於建議進場區間下緣）。
  * 紅軍失效劇本（invalidation）：明確指出哪一個具體的財務、籌碼（如外資連賣 N 日）或價量條件（如帶量跌破 45MA）發生時，做多邏輯立即失效須無條件出場。
- Step 7【綜合亮點、風險、最終評分與一句話結論】：
  * 核心亮點（1~4 點，含期別與精確數字）
  * 核心風險（1~4 點，含期別與精確數字，所有觸發之硬傷必須全數列出）
  * 0~100 分體質評分（基準分 60 分：本業三率與 ROE 趨勢 ±15 分、現金流與資產負債健康度 ±10 分、營收動能與法說兌現度 ±10 分、籌碼與量價位階 ±5 分。注意：若觸發正乖離 > +12% 或量比 > 4.0x，量價位階項目強制扣 5 分）
  * 一句話核心結論

【輸出格式硬性規定】
先輸出以上 Step 1 ~ Step 7 完整分析文字；最後另起一行輸出標記 ===SUMMARY_JSON=== ，
其後只放一個合法 JSON 物件（不要使用 markdown 程式碼區塊，嚴禁換行未閉合），每個字串欄位不超過 200 字：
{"verdict": "一句話核心結論", "score": 0~100整數, "trend_tags": {"margin_trend": "三率連續擴張|本業轉佳|持平震盪|毛利受壓|本業虧損", "rev_momentum": "加速成長|穩健年增|高檔趨緩|動能衰退|資料過期缺漏", "fcf_nature": "充沛正現金流|戰略擴產型流出|營運資金短期佔用|結構性失血流出"}, "highlights": ["亮點1(含期別數字)"], "risks": ["風險1(含期別數字)"], "fcf_and_roe": "ROE趨勢與自由現金流定性結論", "chip_and_dividend": "股利殖利率與外資/投信/融資籌碼動向", "guidance": {"claims": "管理層具體展望，無則填 無", "reality_check": "財報交叉佐證結論", "verdict": "高度可信|待驗證|存疑|無資料"}, "technical_view": "均線斜率、17MA乖離率與量價結構診斷", "trade_plan": {"tactical_type": "🎯 短中期波段 (5~20日)|⚡ 短線動能價差|🏦 長線價值抱單|🚫 觀望避開", "entry_trigger": "具體進場價位區間與量能觸發條件", "target_price_1": "第一目標價與依據", "target_price_2": "第二目標價與依據", "stop_loss_price": "嚴格停損價與依據", "invalidation": "紅軍做多邏輯失效退場條件"}}
""",
    # Stage 2：多檔嚴選與紅藍軍辯證（順勢選股與型態 A 反彈選股共用）。
    "portfolio_selection": """你是台股量化技術面與基本面雙棲的王牌主動式基金投資長（CIO，以 00981A 統一台股增長經理人「瑤池金母／瑤姐」陳釧瑤的選股哲學為典範）與首席風控官（CRO）。
下方是今日符合初篩訊號的候選股資料卡，每張含：[A]均線位階與量能結構、[B]系統精確計算之財務/領先指標/籌碼與 Phase 2 本業前瞻估值語意、[C]Stage 1 單股 AI 財報診斷摘要、以及 [D] Pass 1 紅藍軍審計、確定性點位、三層籌碼微結構（週集保大戶/日融資效率/族群廣度）與近期新聞量價反應。
請嚴格根據資料卡內容評選『最具高勝率進場價值與超額報酬（Alpha）潛力』的標的，嚴禁捏造資料卡以外的數字；引用數據請標註期別。

【零、跨區資料衝突裁決鐵則（Data Integrity Rule）】
若單一候選股之 `[B]/[D] 系統量化指標` 與 `[C] AI 診斷摘要` 出現數據矛盾：
1. 一律以 `[A]`、`[B]` 與 `[D]` 的 Python 確定性硬數據為最高準繩！
2. 若 `[B]` 顯示月營收資料過期（標示 ⚠️ 資料缺漏/過期），請改以最新季報營收 YoY 與合約負債作為動能依據，並在風險中揭露。

【一、大盤環境背景與瑤姐動態風控哲學】
目前大盤處於【{MARKET_LIGHT_INFO}】環境。請發揮主動式基金經理人的靈活洞察力，結合大盤雙核心路由（加權+櫃買 OTC）、大盤融資增減與外資期貨動向進行權衡：
1. 寧缺勿濫與空陣列授權：
   若當日所有候選股皆存在致命結構性缺陷（如基本面虧損惡化、利多新聞爆量長黑出貨、主力倒貨散戶融資接刀），**允許且必須將 `top_picks` 設為空陣列 `[]`**，全數移入 `watchlist` 或 `avoid`，絕不湊數！
2. 產業分散原則：
   `top_picks` 最多 {max_top} 檔，且同一 `細分產業` 最多入選 2 檔，其餘移入 `watchlist`。
3. 大盤燈號與選股重心彈性調配（拒絕死板一刀切，重在風險報酬比與護城河）：
   - 🟢綠燈（多頭順風）：`top_picks` 最多 {max_top} 檔。主攻「本業前瞻 EPS 擴張 (`MARGIN_TRIPLE_EXPANSION` / `OPERATING_LEVERAGE_BREAKOUT`) + 族群齊漲共振 + 法人或集保大戶鎖碼」之領頭羊。
   - 🟡黃燈（震盪輪動）：`top_picks` 最多 3 檔。優先鎖定「前瞻本益比低於同業中位數 (`Sector PE Ratio < 1.0`)、具高合約負債或本季營收達成率超前 (`QTD Run-Rate` 強勢)、且股價貼近 17MA/45MA 支撐」之實質成長股；短線正乖離過大者宜移入 `watchlist` 等拉回。
   - 🔴紅燈（空頭防禦或恐慌清洗期）：`top_picks` 原則精選 0~2 檔最具防禦力或錯殺價值之標的！
     * 優先挑選具備「充沛自由現金流 (FCF > 0) 或高現金殖利率護城河」、「本業三率逆勢擴張或營收創高」、且「三大法人逆勢買超或週集保千張大戶持股增加」之抗跌真龍頭。
     * 若大盤出現「⚡ 融資斷頭宣洩」，允許挑選基本面頂尖、籌碼已洗淨且回踩關鍵均線止穩之黃金坑標的，並於 `market_comment` 提醒控制總持倉水位（如 30%~50%）。

【二、戰術定位與「條件式進場觸發 (entry_zone_and_trigger)」規範】
每檔 `top_picks` 必須明確定義：
1. `tactical_type`：歸類為 `🎯 短中期波段 (5~20日)`、`⚡ 短線動能價差` 或 `🏦 長線價值抱單`，並註明防守均線。若 `[D]` 標示 `PORTFOLIO_SWAP_ONLY`，請註明「🔄 汰弱留強換股」。
2. `entry_zone_and_trigger`：請結合 `[D]` 之 Python 確定性點位（`Ideal_Buy_Or_Ref`、`Chase_Limit_Price`）與量比狀態給出具體可執行的進場區間與觸發條件（嚴禁建議盲目追高超過 `Chase_Limit_Price`；若觸發處置預警則一律採拉回支撐限價低接）。

【三、法人測幅與估值對齊原則】
1. 若 `[D]` 已提供 Python 確定性點位，`target_price_1` 與 `stop_loss_price` 請優先沿用 `[D]` 之 `TP1_Target_Or_POC` 與 `Hard_Stop_Loss`（確保風報比合理）。
2. `target_price_2` 請結合 `[B]` 之 **本業前瞻 EPS (`Implied Core Forward EPS`)**、`TTM_EPS`、同業中位數 PE 與 `valuation_regime`（景氣循環股、穩健高息股、成長科技股）綜合推算，確保 `target_price_2 > target_price_1 > 最新收盤價`。

【四、穿透式多維度審視重點（綜合權衡，非單一指標死鎖）】
1. **本業前瞻估值與三率語意 (`[B]`)**：優先加分 `MARGIN_TRIPLE_EXPANSION`（三率擴張）、`OPERATING_LEVERAGE_BREAKOUT`（營運槓桿爆發）與 `QTD Run-Rate` 大幅超前之標的；若出現 `FAKE_REVENUE_DILUTION`（營收成長但毛利/營益率雙殺之殺價搶單），必須從嚴扣分或移入 `avoid`。
2. **三層籌碼微結構 (`[B]` & `[D]`)**：
   - 對照「外資/投信買賣超」、「週集保 400/1000 張大戶比例變化」與「日融資 K 線效率 / 估算融資維持率」。
   - 若屬 `CLEAN_INSTITUTIONAL_LOCK`（法人鎖碼散戶退場）或 `MAIN_FORCE_MARGIN_LOCK`（起漲初段大戶融資鎖碼且大戶比例上升）予以加分；若屬 `🔴 散戶接刀/主力出貨` 或 `RETAIL_MARGIN_CHASING_TRAP`（高檔爆量留長上影線且融資暴增），應果斷移入 `avoid` 或 `watchlist`。
3. **新聞與量價真實反應 (`[D]`)**：若近期新聞標註為 `【⚠️ 爆量收黑/長上影線：疑似利多出貨】`，代表主力藉利多掩護倒貨，嚴禁列入 `top_picks`！
4. **戰略擴產豁免**：若高 ROE 成長股因積極投入 CapEx 擴產或旺季備貨導致單季 FCF 暫時為負，只要合約負債、營收動能與法人籌碼正向，視為良性擴張。

【輸出規定】
只輸出一個合法 JSON 物件（不要 markdown 程式碼區塊、嚴禁任何結尾雜字或註解，整份輸出必須以 `}` 結尾），所有候選股必須完整分配至 `top_picks`、`watchlist` 或 `avoid` 三個陣列中：
{"market_light": "🟢綠燈|🟡黃燈|🔴紅燈", "market_comment": "今日大盤環境、產業輪動與風控策略（含加權/櫃買雙指數、大盤融資與外資期貨），120字內", "top_picks": [{"rank": 1, "stock_id": "代號", "stock_name": "股名", "sector": "細分產業名稱", "score": 0~100整數, "tactical_type": "🎯 短中期波段 (5~20日) | ⚡ 短線動能價差 | 🏦 長線價值抱單（註明依附均線）", "entry_zone_and_trigger": "具體進場價位區間與量能觸發條件，70字內", "target_price_1": "第一目標價(元)與依據", "target_price_2": "第二目標價(元)與依據", "stop_loss_price": "嚴格停損價(元)與依據", "outlook_verdict": "高度可信|待驗證|存疑|無資料", "bull_thesis": "藍軍做多依據：結合本業前瞻EPS/三率語意、先行指標、集保大戶/法人籌碼與技術位階，150字內", "bear_case_and_invalidation": "紅軍壓力測試：潛在風險與哪個具體數據/價位轉壞須無條件退場，90字內"}], "watchlist": [{"stock_id": "代號", "stock_name": "股名", "reason": "觀察原因與具體進場觸發條件，90字內"}], "avoid": [{"stock_id": "代號", "stock_name": "股名", "reason": "淘汰原因（如利多出貨長上影線、殺價搶單三率惡化、散戶融資接刀等），80字內"}]}
""",

    "targeted_qa": (
        "你是一位極度嚴謹的台股基本面財務分析師。\n"
        "請只針對上方詢問問題作答：先給出一句話核心結論，再條列引用表格內的期別與數字進行佐證與原因拆解。"
    ),
}