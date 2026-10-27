"""统一词表（SDD 0049）：描述资料与写专家的数据声明共用这一份。

描述分维度并列：类型、主体、期间、版本形态进「认这一份」的键；事项、行业、语言、格式、
研报的几维只用来筛选。封闭的维度在这里有取值清单（类型、版本形态、事项、行业、语言、
格式、研报的盈利预测、市场、深度、更新频率），开放的只核格式（主体、期间）或照原文记
（来源机构、研究员、评级）。

knowbase 管这份词表，带版本号：只加取值不升版本，改名、删除、改词义才升；旧描述、
旧声明能查出「用的是旧词」。第 2 版（10-27）把类型改成大类（发布者）加细类（文献类型）。
"""

from __future__ import annotations

import re
from datetime import date

VOCAB_VERSION = 2

# 类型的大类 = 发布者：编码 →（名字，发布者）
DOC_CLASSES: dict[str, tuple[str, str]] = {
    "disclosure": ("公告", "上市公司及信披义务人"),
    "research": ("研报", "研究机构"),
    "news": ("资讯", "媒体"),
    "briefing": ("简报", "平台"),
    "policy": ("政策法规", "政府机关与监管机构（含交易所、协会）"),
    "statistics": ("统计数据", "统计机构"),
    "other": ("其他", "本人上传的杂项"),
}

# 类型的细类 = 文献类型：编码 →（名字，大类）；一个细类只属于一个大类
DOC_TYPES: dict[str, tuple[str, str]] = {
    "annual_report": ("年报", "disclosure"),
    "semiannual_report": ("半年报", "disclosure"),
    "q1_report": ("一季报", "disclosure"),
    "q3_report": ("三季报", "disclosure"),
    "interim_announcement": ("临时公告", "disclosure"),
    "prospectus": ("招股书", "disclosure"),
    "listing_announcement": ("上市公告书", "disclosure"),
    "ir_activity": ("投资者关系活动记录", "disclosure"),
    "ir_qa": ("互动问答", "disclosure"),
    # 研报照 Wind 研报平台的 15 个「类别」
    "research_morning": ("晨会纪要", "research"),
    "research_macro": ("宏观研究", "research"),
    "research_strategy": ("策略研究", "research"),
    "research_industry": ("行业研究", "research"),
    "research_company": ("公司研究", "research"),
    "research_quant": ("金融工程", "research"),
    "research_market": ("市场综述", "research"),
    "research_fixed_income": ("固定收益", "research"),
    "research_commodity": ("商品期货", "research"),
    "research_derivatives": ("衍生品", "research"),
    "research_fund": ("基金研究", "research"),
    "research_fx": ("外汇市场", "research"),
    "research_reits": ("REITs研究", "research"),
    "research_esg": ("ESG研究", "research"),
    "research_other": ("其他报告", "research"),
    "financial_news": ("财经资讯", "news"),
    "recommended_news": ("推荐资讯", "news"),
    "negative_news": ("负面资讯", "news"),
    "brief": ("简报", "briefing"),
    "law": ("法律", "policy"),
    "administrative_regulation": ("行政法规", "policy"),
    "departmental_rule": ("部门规章", "policy"),
    "normative_document": ("规范性文件", "policy"),
    "local_regulation": ("地方性法规", "policy"),
    "judicial_interpretation": ("司法解释", "policy"),
    "policy_interpretation": ("政策解读", "policy"),
    "business_rule": ("业务规则", "policy"),
    "regulatory_letter": ("监管函件", "policy"),
    "statistical_bulletin": ("统计公报", "statistics"),
    "statistical_data": ("统计数据", "statistics"),
    "census": ("普查", "statistics"),
    "webpage": ("网页", "other"),
    "other_table": ("其他表格", "other"),
    "other_text": ("其他文本", "other"),
}

# 定期报告的报告期：一律从年初起算，到报告期末（三季报按「年初至报告期末」）；
# 描述里的期间、声明展开出的期间都按这个写，才对得上。其余类型的期间是披露时间窗。
# 值：（报告期末的月、日，法定披露截止：报告期末之后第几年的几月几日）
PERIODIC: dict[str, tuple[tuple[int, int], tuple[int, int, int]]] = {
    "annual_report": ((12, 31), (1, 4, 30)),
    "semiannual_report": ((6, 30), (0, 8, 31)),
    "q1_report": ((3, 31), (0, 4, 30)),
    "q3_report": ((9, 30), (0, 10, 31)),
}
# 可以按描述去采的：上市证券的公告（互动问答以后另接渠道）、发给公司的监管函件；
# 机构的政策法规（业务规则以后另接）、统计数据。研报、资讯、简报、其他采不来，
# 只能本人上传（网页要给网址）
SECURITY_COLLECTABLE: frozenset[str] = frozenset(
    {
        *PERIODIC,
        "interim_announcement",
        "prospectus",
        "listing_announcement",
        "ir_activity",
        "regulatory_letter",
    }
)
ORG_COLLECTABLE: frozenset[str] = frozenset(
    t
    for t, (_, cls) in DOC_TYPES.items()
    if cls in ("policy", "statistics")
    and t not in ("business_rule", "regulatory_letter")
)

# 版本形态（定期报告进键）
EDITIONS: dict[str, str] = {"full": "全文", "summary": "摘要", "english": "英文版"}

# 事项：讲什么事，可多选，只用来筛选（上交所《公告格式指南》56 号收拢成这几样）
TOPICS: dict[str, str] = {
    "results": "业绩",
    "ipo": "发行上市",
    "refinancing": "再融资",
    "dividend": "分红",
    "meetings": "会议",
    "governance": "治理",
    "operations": "经营与交易",
    "restructuring": "重组与收购",
    "equity": "股本与股东",
    "trading": "交易提示与风险",
    "intermediary": "中介意见",
    "supervision": "监管",
}

# 行业：照 Wind 研报平台的 29 个，取值就是这个名字
INDUSTRIES: tuple[str, ...] = (
    "石油石化",
    "煤炭",
    "有色金属",
    "电力及公用事业",
    "钢铁",
    "基础化工",
    "建筑",
    "建材",
    "轻工制造",
    "机械",
    "电力设备",
    "国防军工",
    "汽车",
    "商贸零售",
    "消费者服务",
    "家电",
    "纺织服装",
    "医药",
    "食品饮料",
    "农林牧渔",
    "银行",
    "非银行金融",
    "房地产",
    "交通运输",
    "电子元器件",
    "通信",
    "计算机",
    "传媒",
    "综合",
)

LANGUAGES: dict[str, str] = {"zh": "中文", "en": "英文", "other": "其他"}

# 研报的几维（Wind 研报平台的筛选条件）：封闭的有清单，开放的照原文记
RESEARCH_CLOSED: dict[str, dict[str, str]] = {
    "forecast": {
        "initiation": "首次覆盖",
        "rating_change": "评级变动",
        "estimate_change": "预测变动",
    },
    "market": {"cn": "A股", "hk": "港股", "us": "美股", "other": "其他"},
    "depth": {"deep": "深度", "regular": "一般"},
    "frequency": {
        "daily": "日",
        "weekly": "周",
        "monthly": "月",
        "quarterly": "季",
        "yearly": "年",
        "irregular": "不定期",
    },
}
RESEARCH_OPEN: tuple[str, ...] = ("institution", "analysts", "rating")
MAX_ANALYSTS = 20
MAX_OPEN_TEXT = 100

# 格式：和上传收的内容类型对得上（pdf、Excel、CSV、文本、Markdown），外加网页
FORMATS: dict[str, str] = {
    "pdf": "PDF",
    "excel": "Excel",
    "csv": "CSV",
    "text": "文本",
    "markdown": "Markdown",
    "html": "网页",
}

# 主体：上市证券用「代码.市场」；不是上市公司的（发文机关、统计口径等）用
# 「org:统一社会信用代码」，没有信用代码的由 knowbase 管理端登记分配「org:kb-<编号>」
_SUBJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cn_security", re.compile(r"^\d{6}\.(SH|SZ|BJ)$")),
    ("hk_security", re.compile(r"^\d{5}\.HK$")),
    ("us_security", re.compile(r"^[A-Z][A-Z0-9.-]{0,9}\.US$")),
    ("org_credit_code", re.compile(r"^org:[0-9A-HJ-NPQRTUWXY]{18}$")),
    ("org_registered", re.compile(r"^org:kb-[a-z0-9-]{3,48}$")),
)

# 上传的内容类型 → 格式
MEDIA_FORMAT: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
    "application/vnd.ms-excel": "excel",
    "text/csv; charset=utf-8": "csv",
    "text/plain; charset=utf-8": "text",
    "text/markdown; charset=utf-8": "markdown",
    "text/html; charset=utf-8": "html",
}


class VocabularyError(ValueError):
    """不在词表里的值。`field` 是哪一栏。"""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def subject_kind(subject: str) -> str:
    """主体编号是哪一种；不认得就拒。"""
    for kind, pattern in _SUBJECT_PATTERNS:
        if pattern.fullmatch(subject or ""):
            return kind
    raise VocabularyError("subject", "subject is not a known code")


def check_doc_type(doc_type: str) -> str:
    """描述只记细类。"""
    if doc_type not in DOC_TYPES:
        raise VocabularyError("doc_type", "doc_type is not a type in the vocabulary")
    return doc_type


def check_doc_type_or_class(doc_type: str) -> str:
    """声明与按描述查可以写细类，也可以只写大类（这个发布者的都行）。"""
    if doc_type not in DOC_TYPES and doc_type not in DOC_CLASSES:
        raise VocabularyError("doc_type", "doc_type is not in the vocabulary")
    return doc_type


def types_of(doc_type: str) -> tuple[str, ...]:
    """细类就是它自己；大类是它下面的全部细类。"""
    if doc_type in DOC_TYPES:
        return (doc_type,)
    return tuple(t for t, (_, cls) in DOC_TYPES.items() if cls == doc_type)


def class_of(doc_type: str) -> str:
    return DOC_TYPES[doc_type][1] if doc_type in DOC_TYPES else doc_type


def is_periodic(doc_type: str) -> bool:
    return doc_type in PERIODIC


def check_format(data_format: str) -> str:
    if data_format not in FORMATS:
        raise VocabularyError("data_format", "data_format is not in the vocabulary")
    return data_format


def check_edition(edition: str) -> str:
    if edition not in EDITIONS:
        raise VocabularyError("edition", "edition is full, summary or english")
    return edition


def check_topics(topics: tuple[str, ...]) -> tuple[str, ...]:
    if len(topics) > len(TOPICS) or len(set(topics)) != len(topics):
        raise VocabularyError("topics", "list each topic once")
    if any(t not in TOPICS for t in topics):
        raise VocabularyError("topics", "a topic is not in the vocabulary")
    return topics


def check_industry(industry: str) -> str:
    if industry not in INDUSTRIES:
        raise VocabularyError("industry", "industry is not in the vocabulary")
    return industry


def check_language(language: str) -> str:
    if language not in LANGUAGES:
        raise VocabularyError("language", "language is zh, en or other")
    return language


def check_attributes(doc_type: str | None, attributes: dict[str, object]) -> None:
    """研报的几维：只有研报能带；封闭的从清单里选，开放的照原文、限长。"""
    if not attributes:
        return
    if doc_type is None or class_of(doc_type) != "research":
        raise VocabularyError("attributes", "only research reports carry attributes")
    for key, value in attributes.items():
        if key == "forecast":
            if not isinstance(value, list) or len(set(value)) != len(value):
                raise VocabularyError("attributes", "forecast is a list, each once")
            if any(v not in RESEARCH_CLOSED["forecast"] for v in value):
                raise VocabularyError("attributes", "forecast is not in the vocabulary")
        elif key in RESEARCH_CLOSED:
            if value not in RESEARCH_CLOSED[key]:
                raise VocabularyError("attributes", f"{key} is not in the vocabulary")
        elif key == "analysts":
            if (
                not isinstance(value, list)
                or len(value) > MAX_ANALYSTS
                or any(
                    not isinstance(v, str) or not v.strip() or len(v) > MAX_OPEN_TEXT
                    for v in value
                )
            ):
                raise VocabularyError("attributes", "analysts is a list of names")
        elif key in RESEARCH_OPEN:
            if not isinstance(value, str) or not value.strip() or len(value) > 100:
                raise VocabularyError("attributes", f"{key} is a short text")
        else:
            raise VocabularyError("attributes", f"{key} is not a research attribute")


def check_period(start: date | None, end: date | None) -> None:
    if (start is None) != (end is None):
        raise VocabularyError("period", "period needs both start and end")
    if start is not None and end is not None and start > end:
        raise VocabularyError("period", "period starts after it ends")


def report_period(doc_type: str, year: int) -> tuple[date, date]:
    """某年这种定期报告的报告期（年初到报告期末）。"""
    (month, day), _ = PERIODIC[doc_type]
    return date(year, 1, 1), date(year, month, day)


def disclosure_deadline(doc_type: str, year: int) -> date:
    _, (later, month, day) = PERIODIC[doc_type]
    return date(year + later, month, day)


def collectable(subject: str, doc_type: str) -> bool:
    """按描述能不能去采（能不能真采到由 knowfeed 预切合说了算）；大类看它下面
    有没有能采的细类。"""
    kind = subject_kind(subject)
    allowed = SECURITY_COLLECTABLE if kind.endswith("_security") else ORG_COLLECTABLE
    return any(t in allowed for t in types_of(doc_type))


def format_of_media(media_type: str) -> str | None:
    return MEDIA_FORMAT.get(media_type)


def snapshot() -> dict[str, object]:
    """给调用方（knowbase web端、knowlab 写声明）看的整份词表。"""
    return {
        "version": VOCAB_VERSION,
        "doc_classes": {
            c: {"name": name, "publisher": publisher}
            for c, (name, publisher) in DOC_CLASSES.items()
        },
        "doc_types": {
            t: {"name": name, "class": cls} for t, (name, cls) in DOC_TYPES.items()
        },
        "editions": dict(EDITIONS),
        "topics": dict(TOPICS),
        "industries": list(INDUSTRIES),
        "languages": dict(LANGUAGES),
        "research": {
            "closed": {k: dict(v) for k, v in RESEARCH_CLOSED.items()},
            "open": list(RESEARCH_OPEN),
        },
        "formats": dict(FORMATS),
        "subject_kinds": [kind for kind, _ in _SUBJECT_PATTERNS],
        "periodic": {
            t: {
                "period_end": f"{m:02d}-{d:02d}",
                "deadline": f"+{y}y {dm:02d}-{dd:02d}",
            }
            for t, ((m, d), (y, dm, dd)) in PERIODIC.items()
        },
        "collectable": {
            "security": sorted(SECURITY_COLLECTABLE),
            "org": sorted(ORG_COLLECTABLE),
        },
    }
