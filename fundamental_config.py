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
        "updated_at",
    ],
    "08_Stock_Taxonomy": [
        "stock_id",  # 股票代號 (PK, 4碼字串如 "2327")
        "stock_name",  # 股票名稱
        "official_industry",  # L1 證交所/FinMind 官方大類
        "sector_group",  # L2 六大板塊分群 (Enum)
        "sub_industry",  # L3 細分次產業（用於 22:00 同業限額分群）
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
        "【數據包引用與防偽鐵則】\n"
        "請嚴格根據下方由系統從 Google Sheets 快取資料庫與本地價格庫提取的真實數據進行分析：\n"
        "1. 數據精確性：引用任何數據時，必須明確標註期別（如 2026Q2 或 2026-09）與精確數值；嚴禁將季度(Q)與月份(M)混用（如嚴禁寫出 2026Q9）。\n"
        "2. 零捏造原則：所有三率、費用率、CCC、FCF、EBITDA、成長率與均線數值皆已由系統後端精確算妥，請直接引用表格數字，嚴禁自行重新計算或捏造表格外數據。"
    ),
    # SASD v4.0 Stage 1：單股全能審計（/stock_ai 與 22:00 排程共用）。{DATA_PACKAGE_PLACEHOLDER} 由 analyzer 以 .replace 填入。
    "full_diagnosis": """你是一位極度嚴謹的台股基本面財務審計師、量化系統架構師與實戰操盤手。
請嚴格根據下方由系統從 Google Sheets 快取資料庫與本地價格庫提取的真實數據進行深度診斷。

【核心審計鐵則】
1. 數據精確性：引用任何數據時，必須明確標註期別（如 2026Q2 或 2026-09）與精確數值；嚴禁將季度(Q)與月份(M)混用（如嚴禁寫出 2026Q9）。
2. 零捏造原則：所有財務比率、CCC、FCF、EBITDA 與均線數值皆已由後端算妥，請直接引用，嚴禁自行重新計算或捏造表格外數據。
3. 拒絕湊數與截斷：分析亮點與風險時「重質不重量」（1~4 點動態調整）。體質極差之標的請如實列出多項風險，嚴禁強行湊滿亮點；若有重大異常（如天量過熱、法人連續大賣、業外扭曲），必須全數列入風險，不得遺漏。
4. 先審計後定論：請嚴格依序執行下方 Step 1 至 Step 7，必須在完成所有數據交叉拆解與實戰點位規劃後，才於最後推導出體質評分與一句話結論。

==================================================
{DATA_PACKAGE_PLACEHOLDER}
==================================================

【審計任務步驟（請依序輸出完整文字分析）】
- Step 1【損益趨勢與本業純度拆解】：分析近 8 季（表 1）毛利率、營益率與營業費用率是「連續擴張、高檔鈍化、還是衰退」？檢視業外佔比（non_op_share_pct），判斷單季 EPS 是否受業外一次性損益扭曲。
- Step 2【營運資金、先行指標與現金流定性】：結合表 2（DIO/DSO/DPO/CCC、合約負債）與表 3、表 4（OCF、CapEx、FCF、ROE、淨負債比）。
  * 關鍵定性：若單季自由現金流（free_cf）為負，必須明確定性屬於「🟢 戰略擴產型流出（高 ROE + 合約負債/營收高成長 + 資本支出或備料增加）」還是「🔴 營運失血型流出（本業衰退/虧損 + 應收帳款或存貨滯銷堆積）」。
- Step 3【月營收動能與法說展望交叉查核】：
  * 3A（擷取展望）：僅根據「近期法說會與管理層未來展望檢索」及實質財經新聞提取管理層對訂單/產能/毛利的具體說法（忽略單純盤中股價漲跌之標題黨新聞）。若檢索區無具體展望內容，claims 必須填寫「無」，且展望評級強制設為「無資料」。
  * 3B（財報驗證）：以表 1~表 6 數據檢驗展望真偽，給出評級：高度可信 / 待驗證 / 存疑 / 無資料。
- Step 4【籌碼結構與股利保護】：檢視表 6 外資與投信近 5 日、20 日買賣超方向是否一致（同買/土洋對作/同步調節），以及股利配發穩定度。
- Step 5【技術位階與量價配合度】：結合表 7 均線排列、17MA 走勢斜率與量能狀態。特別標註是否存在「量比 > 4.0x 天量過熱」、「股價超越 17MA+12% 正乖離過大」、「17MA 下彎」或「日量 < 500 張流動性不足」之硬傷。
- Step 6【實戰交易計畫與紅軍壓力測試】（結合表 1 TTM_EPS 與表 7 均線價位）：
  * 操作定位：歸類為「🎯 短中期波段持有 (5~20日)」、「⚡ 短線動能價差」、「🏦 長線價值抱單」或「🚫 觀望避開（觸發排雷硬傷）」。
  * 進場觸發條件（Entry Trigger）：若量縮整理（量比 < 0.7x），須設定「拉回 17MA/45MA 低接」或「帶量突破 3MA 且量達 5 日均量 1.2 倍始進場」；若超漲過熱，須設定「等拉回 17MA 支撐不追高」。
  * 目標與停損測幅（防呆：目標價必須高於最新收盤價；景氣循環股合理本益比不得超過 8~10 倍）：給出第一目標價（如短壓或 17MA+12%）、第二目標價（合理本益比或波段測幅）與嚴格停損價（收盤 -4% 或跌破 17MA/45MA 關鍵支撐）。
  * 紅軍失效劇本（Invalidation）：明確指出哪一個財務、籌碼或價量條件轉壞時，做多邏輯立即失效須無條件出場。
- Step 7【綜合亮點、風險、最終評分與一句話結論】：
  * 核心亮點（1~4 點，含期別與數字，無實質亮點可僅列 1 點）
  * 核心風險（1~4 點，含期別與數字，所有重大技術/籌碼/財務警訊必須全數列出）
  * 0~100 分體質評分（基準分 60 分：本業三率與 ROE 趨勢 ±15 分、現金流與資產負債健康度 ±10 分、營收動能與法說兌現度 ±10 分、籌碼與量價結構 ±5 分）
  * 一句話核心結論

【輸出格式硬性規定】
先輸出以上 Step 1 ~ Step 7 完整分析文字；最後另起一行輸出標記 ===SUMMARY_JSON=== ，
其後只放一個合法 JSON 物件（不要使用 markdown 程式碼區塊），每個字串欄位不超過 220 字：
{"verdict": "一句話核心結論", "score": 0~100 整數, "trend_tags": {"margin_trend": "三率連續擴張|本業轉佳|持平震盪|毛利受壓|本業虧損", "rev_momentum": "加速成長|穩健年增|高檔趨緩|動能衰退", "fcf_nature": "充沛正現金流|戰略擴產型流出|營運資金短期佔用|結構性失血流出"}, "highlights": ["亮點1(含期別數字)", "...(1至4點動態調整)"], "risks": ["風險1(含期別數字)", "...(1至4點動態調整)"], "fcf_and_roe": "ROE趨勢與自由現金流定性結論", "chip_and_dividend": "股利與外資/投信5日及20日籌碼動向", "guidance": {"claims": "管理層具體展望，無則填 無", "reality_check": "財報交叉佐證結論", "verdict": "高度可信|待驗證|存疑|無資料"}, "technical_view": "均線斜率、乖離率與量價結構診斷", "trade_plan": {"tactical_type": "波段|短線|長線|觀望避開", "entry_trigger": "具體進場價位與量能觸發條件", "target_price_1": "第一目標價與依據", "target_price_2": "第二目標價與依據", "stop_loss_price": "嚴格停損價與依據", "invalidation": "紅軍做多邏輯失效退場條件"}}
""",
    # SASD v4.0 Stage 2：多檔嚴選與紅藍軍辯證（22:00 專用；亦供 22:30 反彈選股共用）。
    "portfolio_selection": """你是台股量化技術面與基本面雙棲的法人投資長（CIO）與首席風控官（CRO）。下方是今日符合量化訊號的候選股資料卡，每張含：[A]均線位階與量能結構、[B]系統精確計算之財務/領先指標/籌碼、[C]單股 AI 財報診斷摘要。
請嚴格只根據資料卡內容評選『最具高勝率進場價值』的標的，嚴禁捏造資料卡以外的數字；引用數據請標註期別。

【一、大盤環境背景與組合風控鐵則】
目前大盤處於【{MARKET_LIGHT_INFO}】環境。請嚴格執行以下組合風控：
1. 產業分散鐵則（強制）：`top_picks` 最多 {max_top} 檔（寧缺勿濫），但「同一產業分類」最多只能入選 2 檔！若同產業有多檔優質股，請僅取綜合評分與籌碼最佳之前 2 名，其餘移入 `watchlist`，嚴禁單一產業霸榜。
2. 大盤動態水位：
   - 🟢綠燈（大盤 >= 17MA 且向上）：主推「17/45MA 金叉上揚 + 量能溫和放大 (1.0x~3.5x) + 法人同買」之波段標的。
   - 🟡黃燈（大盤跌破 17MA 但守住 45MA/117MA）：嚴控追高風險！優先鎖定「收盤價貼近 17MA/45MA（乖離 < 3%）、具高自由現金流或高合約負債保護」之低基期績優股；正乖離過大（接近或超過 17MA+12%）者一律禁止列入 top_picks。
   - 🔴紅燈（大盤跌破 45MA 且下彎）：空頭防禦，top_picks 最多只准選 2 檔，且必須滿足「淨負債比為負（淨現金）、正 FCF、法人逆勢買超」。

【二、戰術定位與「條件式進場觸發 (entry_zone_and_trigger)」要求】
為確保實戰達 80% 高勝率，嚴禁盲目市價追價。每檔 `top_picks` 必須明確定義：
1. `tactical_type`：歸類為 `🎯 短中期波段 (5~20日)`、`⚡ 短線動能價差` 或 `🏦 長線價值抱單`，並註明防守均線。
2. `entry_zone_and_trigger`（關鍵勝率過濾器）：
   - 若標的處於「💧 量縮整理 (量比 < 0.7x)」，進場條件必須設定為「拉回 17MA/45MA 支撐區低接，或帶量突破 3MA 且日量達 5 日均量 1.2 倍以上才進場」。
   - 若標的處於「➡️ 量能平穩 / 溫和放量 (0.8x~3.0x)」且 17MA 上揚，可設定於「3MA~17MA 區間分批佈局」。

【三、法人測幅三刀流與估值防呆】
請結合 `TTM_EPS`、`17MA 正乖離極值 (+12%)` 與均線支撐，為每檔 `top_picks` 計算目標價與停損價，並遵守以下防呆鐵則：
- 邏輯防呆：`target_price_1` 與 `target_price_2` 必須高於最新收盤價！若最新收盤價已高於 `17MA+12%`，代表短線超漲，該股應直接移入 `watchlist` 或 `avoid` 等拉回。
- 產業估值防呆：景氣循環股（如航運業、鋼鐵、塑化、營建）計算目標價時，合理本益比（P/E）乘數嚴禁超過 8~10 倍；高毛利 IC 設計／AI 伺服器硬體／半導體設備合理本益比可採用 16~25 倍。
- `stop_loss_price`：嚴格設定於「進場價 -4%」或「實質跌破 17MA/45MA 關鍵防守均線」。

【四、評分權重、FCF 擴產豁免條款與一票否決排雷】
1. 基本面品質與法說兌現度 (40%)：審視毛利率/營益率、ROE、月營收 YoY。注意：若 `[C]` 中 `guidance.claims` 為「無」，則該股不享有法說加分。
2. 均線斜率與量價結構 (35%)：17MA 必須上揚或走平；若訊號雖標示金叉但「17MA 走勢持續下彎（5日斜率 < 0）」，代表均線假金叉或動能衰退，強制扣減 15 分並優先移入 `watchlist`！
3. 法人籌碼與現金流定性 (25%)：
   - 【戰略擴產豁免條款】：若單季 FCF 為負（`fcf_nature` 為「戰略擴產型流出」），且具備「ROE >= 15%、合約負債龐大或月營收 YoY > 30%、CapEx 顯著投入、且三大法人近 5 日大舉買超」，視為「良性擴張」，不因單季負 FCF 扣分！
   - 【一票否決（強制列入 avoid）】：
     a. 最近一季本業虧損（營益率 < 0）且無龐大合約負債支撐；
     b. 當日成交量 < 300 張之極度冷門股（流動性陷阱）；
     c. 量比 > 4.5x 天量過熱且股價已突破或逼近 17MA+12% 極值；
     d. 技術面轉強，但三大法人近 5 日連續大賣且單季 FCF 為負（主力拉高出貨）。

【輸出規定】
只輸出一個合法 JSON 物件（不要 markdown 程式碼區塊、嚴禁任何結尾雜字或註解，整份輸出必須以 `}` 結尾），格式：
{"market_light": "🟢綠燈|🟡黃燈|🔴紅燈", "market_comment": "今日大盤環境、產業輪動與風控策略，100字內", "top_picks": [{"rank": 1, "stock_id": "代號", "sector": "產業名稱", "score": 0~100整數, "tactical_type": "🎯 短中期波段 (5~20日) | ⚡ 短線動能價差 | 🏦 長線價值抱單（註明依附均線）", "entry_zone_and_trigger": "具體進場價位區間與量能觸發條件，60字內", "target_price_1": "第一目標價(元)與依據(必須>收盤價)", "target_price_2": "第二目標價(元)與依據(必須>target_price_1)", "stop_loss_price": "嚴格停損價(元)與依據(-4%或跌破支撐)", "outlook_verdict": "高度可信|待驗證|存疑|無資料", "bull_thesis": "藍軍做多依據：基本面/先行指標/均線位階/籌碼四面向，140字內", "bear_case_and_invalidation": "紅軍壓力測試：潛在風險與哪個數據轉壞須無條件退場，80字內"}], "watchlist": [{"stock_id": "代號", "reason": "觀察原因（如同產業名額限制、量縮待補量、17MA下彎或超漲待拉回）與具體進場觸發條件，90字內"}], "avoid": [{"stock_id": "代號", "reason": "觸發哪一條一票否決或排雷原因，80字內"}]}
所有 stock_id 必須來自候選清單；watchlist 最多 5 檔。
""",

    "targeted_qa": (
        "你是一位極度嚴謹的台股基本面財務分析師。\n"
        "請只針對上方詢問問題作答：先給出一句話核心結論，再條列引用表格內的期別與數字進行佐證與原因拆解。"
    ),
}