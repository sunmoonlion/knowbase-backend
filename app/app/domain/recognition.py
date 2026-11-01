"""上传时自动识别描述（SDD 0044 第 2.3 节、0049 第六节）。

选文件后 knowbase 先按文件名、首页文字、表头猜一份描述，交给本人确认或改；识别出来的
一律是「待确认」，本人确认了才参与解析（0043 第 2.5 节）。研报的几维（来源机构、研究员、
评级、盈利预测、深度、更新频率）从首页认（0049 第 3.3 节）。

按标题认细类、事项的规则照 0049 第四节（knowfeed 认巨潮公告用的是同一套说法），
规则带版本号 `RULES_VERSION`：改规则就升版本，已识别过的描述不回头改；每次识别把用的
规则版本记进这份资料的改动留痕（`recognized`），以后能查出是按哪一版规则猜的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.domain.user_library import Description
from app.domain.vocabulary import (
    INDUSTRIES,
    PERIODIC,
    VOCAB_VERSION,
    class_of,
    report_period,
)

# 识别规则的版本：改了下面任何一条规则（关键词、先后、取值）就加一
RULES_VERSION = 2
# 首页文字最多看多少字
MAX_TEXT_CHARS = 6000

# —— 公告：按标题认细类（0049 第四节） ——

_SUPERVISION = ("问询函", "关注函", "监管工作函", "警示函", "监管措施", "纪律处分")
# 先认长的、特殊的，再认短的：「半年度报告」不能被「年度报告」先认走
_TITLE_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("prospectus", ("招股说明书", "招股意向书")),
    ("listing_announcement", ("上市公告书",)),
    ("ir_activity", ("投资者关系活动记录",)),
    ("semiannual_report", ("半年度报告", "半年报")),
    ("q1_report", ("第一季度报告", "一季度报告", "一季报")),
    ("q3_report", ("第三季度报告", "三季度报告", "三季报")),
    ("annual_report", ("年度报告", "年报")),
)
# 定期报告的标题里不该有的词：有就是关于这份报告的临时公告（如「年报问询函的回复」）
_NOT_THE_REPORT = ("问询", "关注函", "更正", "补充", "说明会", "延期", "预约", "提示性")

# —— 事项：标题里的关键词（0049 第 3.2 节、第四节）；可多选 ——

_TOPIC_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("results", ("业绩预告", "业绩快报", "业绩说明会", "业绩预增", "业绩预减")),
    ("ipo", ("首次公开发行", "招股说明书", "招股意向书", "上市公告书")),
    (
        "refinancing",
        ("向特定对象发行", "非公开发行", "配股", "可转债", "可转换公司债", "公司债"),
    ),
    ("dividend", ("权益分派", "利润分配", "分红")),
    ("meetings", ("董事会", "监事会", "股东大会", "股东会")),
    (
        "governance",
        ("内部控制", "社会责任", "ESG", "辞职", "聘任", "会计师事务所", "公司章程"),
    ),
    (
        "operations",
        (
            "重大合同",
            "中标",
            "对外投资",
            "购买资产",
            "出售资产",
            "关联交易",
            "担保",
            "财务资助",
            "诉讼",
            "仲裁",
            "政府补助",
            "募集资金",
            "会计政策变更",
            "差错更正",
        ),
    ),
    ("restructuring", ("重大资产重组", "吸收合并", "破产重整", "要约收购")),
    (
        "equity",
        (
            "权益变动",
            "增持",
            "减持",
            "质押",
            "冻结",
            "回购",
            "股权激励",
            "解除限售",
            "解禁",
            "协议转让",
            "司法拍卖",
        ),
    ),
    (
        "trading",
        ("异常波动", "停牌", "复牌", "风险提示", "澄清", "更正", "补充公告", "退市"),
    ),
    (
        "intermediary",
        ("审计报告", "法律意见书", "核查意见", "保荐", "评估报告", "评级报告"),
    ),
    ("supervision", _SUPERVISION),
)

# —— 研报（0049 第 3.1、3.3 节） ——

_RESEARCH_SIGNS = (
    "证券研究报告",
    "研究报告",
    "研究所",
    "研究部",
    "投资评级",
    "分析师",
    "执业证书",
)
# 细类：先看标题，标题认不出再看首页；先认特殊的
_RESEARCH_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("research_morning", ("晨会", "早报", "晨报")),
    ("research_quant", ("金融工程", "量化")),
    ("research_fixed_income", ("固定收益", "固收", "债券", "信用债", "利率债", "转债")),
    ("research_commodity", ("商品期货", "期货")),
    ("research_derivatives", ("衍生品", "期权")),
    ("research_reits", ("REITs", "REIT")),
    ("research_esg", ("ESG",)),
    ("research_fx", ("外汇", "汇率")),
    ("research_fund", ("基金研究", "基金")),
    ("research_macro", ("宏观",)),
    ("research_strategy", ("策略",)),
    ("research_market", ("市场综述", "市场回顾")),
    ("research_industry", ("行业研究", "行业点评", "行业深度", "行业周报", "行业报告")),
    ("research_company", ("公司研究", "公司点评", "公司深度", "首次覆盖")),
)
_RATINGS = (
    "强烈推荐",
    "谨慎推荐",
    "推荐",
    "买入",
    "增持",
    "持有",
    "中性",
    "减持",
    "卖出",
    "跑赢行业",
    "跑输行业",
    "优于大市",
    "强于大市",
    "同步大市",
    "弱于大市",
    "看好",
    "看淡",
)
_RATING = re.compile(r"评级[\s：:为]{0,3}(" + "|".join(_RATINGS) + ")")
_RATING_IN_TITLE = re.compile(r"[（(](" + "|".join(_RATINGS) + r")[）)]")
_FORECAST_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("initiation", ("首次覆盖",)),
    ("rating_change", ("上调评级", "下调评级", "评级上调", "评级下调", "调整评级")),
    (
        "estimate_change",
        (
            "上调盈利预测",
            "下调盈利预测",
            "盈利预测上调",
            "盈利预测下调",
            "调整盈利预测",
            "上调目标价",
            "下调目标价",
        ),
    ),
)
_FREQUENCY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("daily", ("日报", "晨会", "早报", "晨报")),
    ("weekly", ("周报", "周度")),
    ("monthly", ("月报", "月度")),
)
# 来源机构：前面不是汉字（行首、空白、标点），后面是机构的后缀
_INSTITUTION = re.compile(r"(?:^|[^一-龥])([一-龥]{2,6}(?:证券|期货|研究院|研究所))")
_INSTITUTION_GENERIC = ("证券研究", "研究所", "研究院")
_RESEARCH_ARM = re.compile(r"(证券|期货)(研究所|研究院)$")
_ANALYST = re.compile(r"(?:分析师|研究员)[\s：:]{0,3}([一-龥]{2,4})")
_NOT_A_NAME = ("执业", "证书", "登记", "编号", "声明", "承诺", "联系")
MAX_ANALYSTS = 20

# —— 主体、日期 ——

_CN_CODE = re.compile(r"(?<!\d)(\d{6})\.(SH|SZ|BJ)(?![A-Za-z])", re.IGNORECASE)
_HK_CODE = re.compile(r"(?<!\d)(\d{4,5})\.HK(?![A-Za-z])", re.IGNORECASE)
_BARE_CODE = re.compile(r"[（(]\s*(\d{6})(?:\.(?:CH|CN))?\s*[)）]")
_LABELED_CODE = re.compile(r"(?:证券代码|股票代码|代码)[\s：:]{0,3}(\d{6})")
_DATE = re.compile(r"(?<!\d)(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})")
_YEAR = re.compile(r"(?<!\d)(20\d{2})\s*年")
_CJK = re.compile(r"[一-龥]")
_LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class Recognition:
    """识别的结果：一份待确认的描述、认出了哪几栏、按哪一版规则、有没有看到首页。"""

    description: Description
    fields: tuple[str, ...]
    rules_version: int = RULES_VERSION
    read_first_page: bool = False

    def as_log(self) -> dict[str, object]:
        """记进改动留痕的样子：规则版本与认出的那几栏的值。"""
        values = self.description.as_dict()
        return {
            "rules_version": self.rules_version,
            "read_first_page": self.read_first_page,
            "fields": list(self.fields),
            "values": {
                k: values[k]
                for k in (
                    "subject",
                    "doc_type",
                    "period_start",
                    "period_end",
                    "disclosed_on",
                    "edition",
                    "topics",
                    "industry",
                    "language",
                    "attributes",
                )
            },
        }


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def _exchange(code: str) -> str | None:
    """六位代码按首位推交易所：6、9 上交所；0、2、3 深交所；4、8 北交所。"""
    if code[0] in "69":
        return "SH"
    if code[0] in "023":
        return "SZ"
    if code[0] in "48":
        return "BJ"
    return None


def _subject(*texts: str) -> str | None:
    for text in texts:
        found = _CN_CODE.search(text)
        if found:
            return f"{found.group(1)}.{found.group(2).upper()}"
        found = _HK_CODE.search(text)
        if found:
            return f"{int(found.group(1)):05d}.HK"
        for pattern in (_BARE_CODE, _LABELED_CODE):
            found = pattern.search(text)
            if found and (market := _exchange(found.group(1))):
                return f"{found.group(1)}.{market}"
    return None


def _first_date(*texts: str) -> date | None:
    for text in texts:
        for found in _DATE.finditer(text):
            try:
                return date(int(found[1]), int(found[2]), int(found[3]))
            except ValueError:
                continue
    return None


def _disclosure_type(title: str) -> str | None:
    if _has(title, _SUPERVISION):
        # 公司的回复是临时公告（事项记监管），监管机构发的原函是监管函件
        return "interim_announcement" if "回复" in title else "regulatory_letter"
    for doc_type, words in _TITLE_TYPES:
        if _has(title, words):
            if doc_type in PERIODIC and _has(title, _NOT_THE_REPORT):
                return "interim_announcement"
            return doc_type
    return None


def _topics(title: str) -> tuple[str, ...]:
    return tuple(code for code, words in _TOPIC_WORDS if _has(title, words))


def _research_type(title: str, text: str, subject: str | None) -> str:
    for source in (title, text[:800]):
        for doc_type, words in _RESEARCH_TYPES:
            if _has(source, words):
                return doc_type
    return "research_company" if subject else "research_other"


def _institution(text: str) -> str | None:
    for found in _INSTITUTION.finditer(text):
        # 「华泰证券研究所」记机构名「华泰证券」
        name = _RESEARCH_ARM.sub(r"\1", found.group(1))
        if not any(name.startswith(g) or name == g for g in _INSTITUTION_GENERIC):
            return name
    return None


def _analysts(text: str) -> list[str]:
    names: list[str] = []
    for found in _ANALYST.finditer(text):
        name = found.group(1)
        if _has(name, _NOT_A_NAME) or name in names:
            continue
        names.append(name)
        if len(names) >= MAX_ANALYSTS:
            break
    return names


def _research_attributes(
    title: str, text: str, subject: str | None
) -> dict[str, object]:
    both = f"{title}\n{text}"
    attributes: dict[str, object] = {}
    if institution := _institution(text) or _institution(title):
        attributes["institution"] = institution
    if analysts := _analysts(text):
        attributes["analysts"] = analysts
    rating = _RATING_IN_TITLE.search(title) or _RATING.search(both)
    if rating:
        attributes["rating"] = rating.group(1)
    if forecast := [c for c, words in _FORECAST_WORDS if _has(both, words)]:
        attributes["forecast"] = forecast
    if subject:
        attributes["market"] = "hk" if subject.endswith(".HK") else "cn"
    if "深度" in title:
        attributes["depth"] = "deep"
    for code, words in _FREQUENCY_WORDS:
        if _has(title, words):
            attributes["frequency"] = code
            break
    return attributes


def _language(title: str, text: str) -> str | None:
    # 只凭文件名不猜语言：文件名常是英文或拼音，和内容的语言无关
    if not text.strip():
        return None
    sample = f"{title}\n{text}"[:MAX_TEXT_CHARS]
    cjk, latin = len(_CJK.findall(sample)), len(_LATIN.findall(sample))
    if cjk == 0 and latin == 0:
        return None
    if cjk >= max(20, latin // 10):
        return "zh"
    return "en" if latin > cjk else None


def recognize(
    name: str, first_page: str = "", data_format: str | None = None
) -> Recognition:
    """按文件名与首页文字猜描述。认不出的栏空着；认出的都要本人确认。"""
    title = _stem(name).strip()
    text = (first_page or "")[:MAX_TEXT_CHARS]
    head = text[:400]
    fields: list[str] = []

    subject = _subject(title, text)
    doc_type = _disclosure_type(title)
    research = doc_type is None and (
        _has(title, _RESEARCH_SIGNS) or _has(text[:3000], _RESEARCH_SIGNS)
    )
    if research:
        doc_type = _research_type(title, text, subject)
    if doc_type is None and data_format in ("csv", "excel"):
        doc_type = "other_table"
    elif doc_type is None and data_format in ("text", "markdown"):
        doc_type = "other_text"

    period_start = period_end = None
    disclosed_on = None
    if doc_type in PERIODIC:
        years = [int(y) for y in _YEAR.findall(title)] or [
            int(y) for y in _YEAR.findall(head)
        ]
        if years:
            period_start, period_end = report_period(doc_type, years[0])
    else:
        disclosed_on = _first_date(title, text)

    edition = "full"
    if "摘要" in title:
        edition = "summary"
    elif "英文" in title or "english" in title.lower():
        edition = "english"

    topics = _topics(title)
    attributes: dict[str, object] = {}
    industry = None
    if doc_type is not None and class_of(doc_type) == "research":
        attributes = _research_attributes(title, text, subject)
        industry = next((i for i in INDUSTRIES if i in title), None)
    language = _language(title, text)

    description = Description(
        subject=subject,
        doc_type=doc_type,
        period_start=period_start,
        period_end=period_end,
        disclosed_on=disclosed_on,
        data_format=data_format,
        status="unconfirmed",
        vocab_version=VOCAB_VERSION,
        edition=edition,
        topics=topics,
        industry=industry,
        language=language,
        attributes=attributes,
    )
    for field_name, value in (
        ("subject", subject),
        ("doc_type", doc_type),
        ("period", period_end),
        ("disclosed_on", disclosed_on),
        ("edition", edition != "full"),
        ("topics", topics),
        ("industry", industry),
        ("language", language),
    ):
        if value:
            fields.append(field_name)
    fields.extend(f"attributes.{k}" for k in attributes)
    return Recognition(
        description=description.checked(),
        fields=tuple(fields),
        read_first_page=bool(text.strip()),
    )
