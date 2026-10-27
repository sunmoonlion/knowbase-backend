"""统一科目表（SDD 0047 第 4.1 节）：标准化层的数按这里的编码存。

编码是我们自己的（`IS.` 利润表、`BS.` 资产负债表、`CF.` 现金流量表），中文名按财政部
一般企业财务报表格式的报表项目；银行另有一组。`cas_element` 是对应的财政部《企业
会计准则通用分类标准》元素名：第 1 版还没有拿到标准文件，先空着，拿到后逐项填，
不凭记忆编。`aliases` 是报表上常见的其他写法，加工器把原样层的科目名对到编码时用。

科目表带版本：改版出新版本，旧事实照旧指它当时的版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CHART_VERSION = 1

CompanyType = Literal["general", "bank", "insurance", "securities"]
COMPANY_TYPES: tuple[CompanyType, ...] = ("general", "bank", "insurance", "securities")
Statement = Literal["income", "balance", "cashflow"]


@dataclass(frozen=True)
class ChartItem:
    code: str
    name: str
    statement: Statement
    company_types: tuple[CompanyType, ...]
    aliases: tuple[str, ...] = ()
    cas_element: str | None = None


_G: tuple[CompanyType, ...] = ("general",)
_B: tuple[CompanyType, ...] = ("bank",)
_GB: tuple[CompanyType, ...] = ("general", "bank")


def _items(
    statement: Statement, types: tuple[CompanyType, ...], rows: list[tuple]
) -> list[ChartItem]:
    return [
        ChartItem(code, name, statement, types, tuple(aliases))
        for code, name, *rest in rows
        for aliases in [rest[0] if rest else ()]
    ]


_ITEMS: list[ChartItem] = [
    # —— 利润表（一般企业） ——
    *_items(
        "income",
        _G,
        [
            ("IS.REVENUE", "营业收入"),
            ("IS.TOTAL_REVENUE", "营业总收入"),
            ("IS.TOTAL_COST", "营业总成本"),
            ("IS.COST", "营业成本"),
            ("IS.TAXES_SURCHARGES", "税金及附加", ["营业税金及附加"]),
            ("IS.SELLING", "销售费用"),
            ("IS.ADMIN", "管理费用"),
            ("IS.RND", "研发费用"),
            ("IS.FINANCE", "财务费用"),
            ("IS.INTEREST_EXPENSE", "其中：利息费用", ["利息费用"]),
            ("IS.INTEREST_INCOME", "利息收入"),
            ("IS.OTHER_INCOME", "其他收益"),
            ("IS.INVESTMENT_INCOME", "投资收益"),
            ("IS.INVESTMENT_INCOME_JV", "对联营企业和合营企业的投资收益"),
            ("IS.FAIR_VALUE_CHANGE", "公允价值变动收益"),
            ("IS.CREDIT_IMPAIRMENT", "信用减值损失"),
            ("IS.ASSET_IMPAIRMENT", "资产减值损失"),
            ("IS.ASSET_DISPOSAL", "资产处置收益"),
            ("IS.NON_OPERATING_INCOME", "营业外收入"),
            ("IS.NON_OPERATING_EXPENSE", "营业外支出"),
        ],
    ),
    *_items(
        "income",
        _GB,
        [
            ("IS.OPERATING_PROFIT", "营业利润"),
            ("IS.PROFIT_BEFORE_TAX", "利润总额"),
            ("IS.INCOME_TAX", "所得税费用"),
            ("IS.NET_PROFIT", "净利润"),
            (
                "IS.NET_PROFIT_PARENT",
                "归属于母公司所有者的净利润",
                ["归属于母公司股东的净利润", "归属于上市公司股东的净利润"],
            ),
            (
                "IS.NET_PROFIT_PARENT_DEDUCTED",
                "扣除非经常性损益后归属于母公司股东的净利润",
                [
                    "归属于上市公司股东的扣除非经常性损益的净利润",
                    "扣除非经常性损益后归属于母公司所有者的净利润",
                ],
            ),
            ("IS.MINORITY_PROFIT", "少数股东损益"),
            ("IS.EPS_BASIC", "基本每股收益"),
            ("IS.EPS_DILUTED", "稀释每股收益"),
        ],
    ),
    # —— 利润表（银行） ——
    *_items(
        "income",
        _B,
        [
            ("IS.BANK_OPERATING_INCOME", "营业收入"),
            ("IS.BANK_NET_INTEREST", "利息净收入"),
            ("IS.BANK_INTEREST_INCOME", "利息收入"),
            ("IS.BANK_INTEREST_EXPENSE", "利息支出"),
            ("IS.BANK_NET_FEE", "手续费及佣金净收入"),
            ("IS.BANK_INVESTMENT_INCOME", "投资收益"),
            ("IS.BANK_OPERATING_EXPENSE", "营业支出"),
            ("IS.BANK_ADMIN", "业务及管理费"),
            ("IS.BANK_CREDIT_IMPAIRMENT", "信用减值损失"),
        ],
    ),
    # —— 资产负债表（一般企业） ——
    *_items(
        "balance",
        _G,
        [
            ("BS.CASH", "货币资金"),
            ("BS.TRADING_FIN_ASSETS", "交易性金融资产"),
            ("BS.NOTES_RECEIVABLE", "应收票据"),
            ("BS.ACCOUNTS_RECEIVABLE", "应收账款"),
            ("BS.RECEIVABLES_FINANCING", "应收款项融资"),
            ("BS.PREPAYMENTS", "预付款项"),
            ("BS.OTHER_RECEIVABLES", "其他应收款"),
            ("BS.INVENTORIES", "存货"),
            ("BS.CONTRACT_ASSETS", "合同资产"),
            ("BS.OTHER_CURRENT_ASSETS", "其他流动资产"),
            ("BS.TOTAL_CURRENT_ASSETS", "流动资产合计"),
            ("BS.LT_EQUITY_INVEST", "长期股权投资"),
            ("BS.INVESTMENT_PROPERTY", "投资性房地产"),
            ("BS.FIXED_ASSETS", "固定资产"),
            ("BS.CIP", "在建工程"),
            ("BS.RIGHT_OF_USE", "使用权资产"),
            ("BS.INTANGIBLES", "无形资产"),
            ("BS.GOODWILL", "商誉"),
            ("BS.DEFERRED_TAX_ASSETS", "递延所得税资产"),
            ("BS.TOTAL_NONCURRENT_ASSETS", "非流动资产合计"),
            ("BS.SHORT_BORROWINGS", "短期借款"),
            ("BS.NOTES_PAYABLE", "应付票据"),
            ("BS.ACCOUNTS_PAYABLE", "应付账款"),
            ("BS.ADVANCES_RECEIVED", "预收款项"),
            ("BS.CONTRACT_LIABILITIES", "合同负债"),
            ("BS.PAYROLL_PAYABLE", "应付职工薪酬"),
            ("BS.TAXES_PAYABLE", "应交税费"),
            ("BS.OTHER_PAYABLES", "其他应付款"),
            ("BS.NONCURRENT_LIAB_DUE_1Y", "一年内到期的非流动负债"),
            ("BS.TOTAL_CURRENT_LIABILITIES", "流动负债合计"),
            ("BS.LT_BORROWINGS", "长期借款"),
            ("BS.BONDS_PAYABLE", "应付债券"),
            ("BS.LEASE_LIABILITIES", "租赁负债"),
            ("BS.TOTAL_NONCURRENT_LIABILITIES", "非流动负债合计"),
            ("BS.PAID_IN_CAPITAL", "实收资本（或股本）", ["股本", "实收资本"]),
            ("BS.CAPITAL_RESERVE", "资本公积"),
            ("BS.TREASURY_STOCK", "减：库存股", ["库存股"]),
            ("BS.OTHER_COMPREHENSIVE_INCOME", "其他综合收益"),
            ("BS.SURPLUS_RESERVE", "盈余公积"),
            ("BS.RETAINED_EARNINGS", "未分配利润"),
        ],
    ),
    *_items(
        "balance",
        _GB,
        [
            ("BS.TOTAL_ASSETS", "资产总计"),
            ("BS.TOTAL_LIABILITIES", "负债合计"),
            (
                "BS.EQUITY_PARENT",
                "归属于母公司所有者权益合计",
                [
                    "归属于母公司股东权益合计",
                    "归属于母公司所有者权益（或股东权益）合计",
                ],
            ),
            ("BS.MINORITY_EQUITY", "少数股东权益"),
            (
                "BS.TOTAL_EQUITY",
                "所有者权益合计",
                ["股东权益合计", "所有者权益（或股东权益）合计"],
            ),
            (
                "BS.TOTAL_LIABILITIES_EQUITY",
                "负债和所有者权益总计",
                ["负债和股东权益总计", "负债和所有者权益（或股东权益）总计"],
            ),
        ],
    ),
    # —— 资产负债表（银行） ——
    *_items(
        "balance",
        _B,
        [
            ("BS.BANK_CASH_CENTRAL", "现金及存放中央银行款项"),
            ("BS.BANK_LOANS", "发放贷款和垫款"),
            ("BS.BANK_DEPOSITS", "吸收存款"),
        ],
    ),
    # —— 现金流量表（一般企业） ——
    *_items(
        "cashflow",
        _G,
        [
            ("CF.SALES_CASH", "销售商品、提供劳务收到的现金"),
            ("CF.OPERATING_INFLOW", "经营活动现金流入小计"),
            ("CF.PURCHASE_CASH", "购买商品、接受劳务支付的现金"),
            ("CF.OPERATING_OUTFLOW", "经营活动现金流出小计"),
            (
                "CF.CAPEX",
                "购建固定资产、无形资产和其他长期资产支付的现金",
            ),
            ("CF.DIVIDENDS_INTEREST_PAID", "分配股利、利润或偿付利息支付的现金"),
        ],
    ),
    *_items(
        "cashflow",
        _GB,
        [
            ("CF.NET_OPERATING", "经营活动产生的现金流量净额"),
            ("CF.NET_INVESTING", "投资活动产生的现金流量净额"),
            ("CF.NET_FINANCING", "筹资活动产生的现金流量净额"),
            ("CF.FX_EFFECT", "汇率变动对现金及现金等价物的影响"),
            ("CF.NET_CHANGE_CASH", "现金及现金等价物净增加额"),
            ("CF.BEGINNING_CASH", "期初现金及现金等价物余额"),
            ("CF.ENDING_CASH", "期末现金及现金等价物余额"),
        ],
    ),
]

CHART: dict[str, ChartItem] = {i.code: i for i in _ITEMS}
assert len(CHART) == len(_ITEMS), "chart codes are unique"


def chart_item(code: str) -> ChartItem:
    if code not in CHART:
        raise KeyError(code)
    return CHART[code]


def snapshot() -> dict[str, object]:
    """给加工器、专家、web端看的整张科目表。"""
    return {
        "version": CHART_VERSION,
        "company_types": list(COMPANY_TYPES),
        "items": [
            {
                "code": i.code,
                "name": i.name,
                "statement": i.statement,
                "company_types": list(i.company_types),
                "aliases": list(i.aliases),
                "cas_element": i.cas_element,
            }
            for i in _ITEMS
        ],
    }


# —— 指标（语义层换底，0047 第 4.4 节） ——

MetricKind = Literal["ratio", "growth"]


@dataclass(frozen=True)
class Metric:
    """一个指标：分子、分母都是科目编码之和或差（`+`/`-` 前缀），growth 是同比。"""

    key: str
    name: str
    kind: MetricKind
    numerator: tuple[str, ...]
    denominator: tuple[str, ...] = ()
    description: str = ""


METRICS: dict[str, Metric] = {
    m.key: m
    for m in [
        Metric(
            "gross_margin",
            "毛利率",
            "ratio",
            ("+IS.REVENUE", "-IS.COST"),
            ("+IS.REVENUE",),
            "(营业收入 − 营业成本) / 营业收入",
        ),
        Metric(
            "net_margin",
            "净利率",
            "ratio",
            ("+IS.NET_PROFIT",),
            ("+IS.REVENUE",),
            "净利润 / 营业收入",
        ),
        Metric(
            "debt_ratio",
            "资产负债率",
            "ratio",
            ("+BS.TOTAL_LIABILITIES",),
            ("+BS.TOTAL_ASSETS",),
            "负债合计 / 资产总计",
        ),
        Metric(
            "roe_simple",
            "净资产收益率（期末）",
            "ratio",
            ("+IS.NET_PROFIT_PARENT",),
            ("+BS.EQUITY_PARENT",),
            "归母净利润 / 期末归母所有者权益（不取平均）",
        ),
        Metric(
            "ocf_to_profit",
            "经营现金流 / 净利润",
            "ratio",
            ("+CF.NET_OPERATING",),
            ("+IS.NET_PROFIT",),
            "经营活动产生的现金流量净额 / 净利润",
        ),
        Metric(
            "revenue_growth",
            "营业收入同比增长",
            "growth",
            ("+IS.REVENUE",),
            (),
            "本期营业收入 / 上年同期 − 1",
        ),
    ]
}
