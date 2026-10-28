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
        "- 若數據包內附有「表 7：近期日線與均線排列動態 (MA3/17/45/117/189 與量能)」，請一併結合均線排列、17MA 走勢與量價配合度（今日量/5日均量/20日均量與量比）給出技術面觀察（若無則略過）\n"
        "- 若有重大新聞，請結合新聞標題作風險/催化劑推論\n"
        "- 若數據包內附有「近期法說會與管理層未來展望檢索」，請依序執行兩步驟（若無相關報導，請明確寫「近期無公開法說展望」並改以月營收動能推估）：\n"
        "  Step A【擷取展望】：只根據檢索內容條列管理層對營收/毛利率/訂單/產能/新產品的具體說法，嚴禁捏造檢索內容以外的展望。\n"
        "  Step B【財報交叉佐證】：用表內數據檢驗 Step A 的真實性與可行性——\n"
        "    * 訂單強勁/需求滿載 ➔ 對照表 2 合約負債與表 5 月營收 YoY；\n"
        "    * 產能擴張 ➔ 對照表 3 資本支出與表 2 存貨週轉天數；\n"
        "    * 毛利/產品組合改善 ➔ 對照表 1 毛利率與營益率趨勢；\n"
        "    * 法說後市場反應 ➔ 對照表 6 外資/投信 5 日與 20 日買賣超。\n"
        "    最後給出展望可信度評級：高度可信 / 待驗證 / 存疑（附期別與數字依據）。\n"
        "\n"
        "【輸出格式硬性規定】先輸出以上完整分析文字；最後另起一行輸出標記 ===SUMMARY_JSON=== ，\n"
        "其後只放一個合法 JSON 物件（不要使用 markdown 程式碼區塊），欄位如下，每個字串欄位不超過 220 字：\n"
        "{\"verdict\": \"一句話核心結論\", \"score\": 0~100 整數, "
        "\"highlights\": [\"亮點1(含期別數字)\", \"亮點2\", \"亮點3\"], "
        "\"risks\": [\"風險1(含期別數字)\", \"風險2\"], "
        "\"fcf_and_roe\": \"ROE 與自由現金流含金量結論\", "
        "\"chip_and_dividend\": \"股利與法人籌碼重點\", "
        "\"guidance\": {\"claims\": \"法說展望重點，無則填 無\", \"reality_check\": \"財報交叉佐證結論\", "
        "\"verdict\": \"高度可信|待驗證|存疑|無資料\"}, "
        "\"technical_view\": \"均線排列、17MA 走勢與量價配合觀察，無資料則填 無\"}\n"
    ),
    "portfolio_selection": (
        "你是台股量化技術面與基本面雙棲的法人操盤手與基金經理人。下方是今日符合量化訊號的多檔候選股資料卡，"
        "每張含：[A]均線位階與量能結構、[B]系統精確計算之財務/領先指標/籌碼、[C]單股 AI 財報診斷摘要（含法說展望與財報交叉佐證）。\n"
        "請嚴格只根據資料卡內容評選『最適合進場投資』的標的，嚴禁捏造資料卡以外的數字；引用數據請標註期別。\n"
        "\n"
        "【一、大盤環境背景宣告】\n"
        "目前大盤處於【{market_light}】環境。請依據此宏觀風控水位，動態調整標的進場防守係數：\n"
        "- 🟢綠燈（大盤 >= 17MA 且向上）：偏多環境，順勢主推「17/45MA 金叉突破」與高動能波段標的。\n"
        "- 🟡黃燈（大盤跌破 17MA 但守住 45MA/117MA）：震盪盤整環境，提高「型態 A 長均線回踩反彈」與低基期績優股權重，嚴控追高風險。\n"
        "- 🔴紅燈（大盤跌破 45MA 且下彎）：空頭警戒，強制在報告標註防禦警示，嚴格過濾基本面與現金流，縮小部位並收緊停損。\n"
        "\n"
        "【二、戰術操作定位分流（強制要求在每檔 top_picks 輸出明確屬性）】\n"
        "每檔入選標的必須於 `tactical_type` 明確歸類為以下三者之一，並說明對應的均線依附防守線（如沿 17MA 或 45MA/117MA）：\n"
        "- `🎯 短中期波段持有 (5~20日)`：籌碼與基本面共振，依附 17MA 或 45MA 波段操作。\n"
        "- `⚡ 短線價差/當沖`：高乖離、急跌反彈搶短或爆量題材，嚴守 3MA/17MA 或回踩低點快進快出。\n"
        "- `🏦 長線價值抱單`：低本益比、高 ROE 與穩定正自由現金流，依附 45MA/117MA 長線佈局。\n"
        "\n"
        "【三、法人測幅三刀流計算要求】\n"
        "請結合 `05_AI_Metrics_Wide` 的 `TTM_EPS` 與合理本益比區間、`17MA 正乖離極值 (+12%)` 以及 `N 字箱型等幅測距 / 前高壓力`，"
        "在結構化 JSON 中為每檔 `top_picks` 具體給出：\n"
        "- `target_price_1`（第一目標價，如反彈至 17MA 或短壓區）\n"
        "- `target_price_2`（第二目標價，如 17MA+12% 正乖離、前波高點或本益比目標價）\n"
        "- `stop_loss_price`（嚴格停損價，如進場價 -4% 或跌破 45MA/117MA 支撐價）\n"
        "\n"
        "【四、評分權重與勝率優化排雷確認】\n"
        "1. 基本面成長、獲利品質與法說展望兌現度 40%：月營收 YoY/MoM、毛利率與營益率擴張、ROE、自由現金流含金量；"
        "法說展望有合約負債/CapEx/營收佐證者加分，喊多但領先指標背離者（畫大餅）扣分。\n"
        "2. 均線位階、動能與量價結構 35%：順勢股看 17MA 上揚與帶量突破（1.2~3.5x）；反彈股看回踩 45MA/117MA 有守、收盤強度 >=0.55 與反彈至 17MA 空間 >=5%；"
        "低量冷門股（<500張）、無量假突破、天量過熱（>4.5x）扣分。\n"
        "3. 法人籌碼共振與自由現金流排雷 25%：必須審視法人籌碼（近 5 日買賣超）與自由現金流（FCF），"
        "若技術面反彈或突破但法人連續大賣且 FCF 為負，強制列入「風險警示或降級（avoid / watchlist）」標的！\n"
        "一票否決（必須列入 avoid）：最近一季本業虧損且無訂單/合約負債支撐；淨負債比極高且速動比率<50%或利息保障倍數<1.5；"
        "技術面反彈/轉強但法人近 5 日連續大賣且自由現金流 FCF 為負。\n"
        "\n"
        "輸出規定：只輸出一個合法 JSON 物件（不要 markdown 程式碼區塊、嚴禁任何結尾雜字、註解或額外單字，陣列必須以 `]` 正確閉合，整份輸出必須以 `}` 結尾），格式：\n"
        "{\"market_light\": \"🟢綠燈|🟡黃燈|🔴紅燈\", "
        "\"market_comment\": \"今日大盤紅綠燈環境與候選股整體風控觀察，100 字內\", "
        "\"top_picks\": [{\"rank\": 1, \"stock_id\": \"代號\", \"score\": 0~100 整數, "
        "\"tactical_type\": \"🎯 短中期波段持有 (5~20日) | ⚡ 短線價差/當沖 | 🏦 長線價值抱單（並註明依附均線）\", "
        "\"target_price_1\": \"第一目標價(元)與依據\", "
        "\"target_price_2\": \"第二目標價(元)與依據\", "
        "\"stop_loss_price\": \"嚴格停損價(元)與依據(-4%或跌破支撐)\", "
        "\"outlook_verdict\": \"高度可信|待驗證|存疑|無資料\", "
        "\"thesis\": \"為何選它：基本面/法說佐證/均線位階/籌碼四面向，150 字內\", "
        "\"risk\": \"主要風險 1~2 點，80 字內\"}], "
        "\"watchlist\": [{\"stock_id\": \"代號\", \"reason\": \"觀察原因與等待條件，80 字內\"}], "
        "\"avoid\": [{\"stock_id\": \"代號\", \"reason\": \"避開或降級原因（如法人大賣+FCF為負），80 字內\"}]}\n"
        "top_picks 最多 {max_top} 檔，寧缺勿濫（可為空陣列）；watchlist 最多 5 檔；所有 stock_id 必須來自候選清單。"
    ),

    "targeted_qa": (
        "請只針對使用者問題作答，並引用表格內的期別與數字作為佐證。"
    ),
}