"""统一词表（SDD 0043 第 2.5 节）：描述资料与写专家的数据声明共用这一份。

knowbase 管这份词表，带版本号。描述里的主体、类型、格式只认这里的值；词表改版时
版本号加一，旧描述、旧声明能查出「用的是旧词」。自由文本只做展示，不参与匹配。
"""

from __future__ import annotations

import re
from datetime import date

VOCAB_VERSION = 1

# 类型（封闭枚举）：第一版先覆盖定期报告与用户最常传的几样
DOC_TYPES: dict[str, str] = {
    "annual_report": "年报",
    "semiannual_report": "半年报",
    "q1_report": "一季报",
    "q3_report": "三季报",
    "announcement": "临时公告",
    "prospectus": "招股书",
    "policy": "政策文件",
    "statistics": "统计数据",
    "webpage": "网页",
    "other_table": "其他表格",
    "other_text": "其他文本",
}

# 定期报告的报告期：一律从年初起算，到报告期末（三季报按「年初至报告期末」）；
# 描述里的期间、声明展开出的期间都按这个写，才对得上。
# 值：（报告期末的月、日，法定披露截止：报告期末之后第几年的几月几日）
PERIODIC: dict[str, tuple[tuple[int, int], tuple[int, int, int]]] = {
    "annual_report": ((12, 31), (1, 4, 30)),
    "semiannual_report": ((6, 30), (0, 8, 31)),
    "q1_report": ((3, 31), (0, 4, 30)),
    "q3_report": ((9, 30), (0, 10, 31)),
}
# 可以按描述去采的：上市证券的定期报告、公告、招股书；机构的政策文件、统计数据。
# 其余（其他表格、其他文本）是私有资料，采不来，只能本人上传；网页要给网址
SECURITY_COLLECTABLE: frozenset[str] = frozenset(
    {*PERIODIC, "announcement", "prospectus"}
)
ORG_COLLECTABLE: frozenset[str] = frozenset({"policy", "statistics"})

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
    if doc_type not in DOC_TYPES:
        raise VocabularyError("doc_type", "doc_type is not in the vocabulary")
    return doc_type


def check_format(data_format: str) -> str:
    if data_format not in FORMATS:
        raise VocabularyError("data_format", "data_format is not in the vocabulary")
    return data_format


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
    """按描述能不能去采（能不能真采到由 knowfeed 预切合说了算）。"""
    kind = subject_kind(subject)
    if kind.endswith("_security"):
        return doc_type in SECURITY_COLLECTABLE
    return doc_type in ORG_COLLECTABLE


def format_of_media(media_type: str) -> str | None:
    return MEDIA_FORMAT.get(media_type)


def snapshot() -> dict[str, object]:
    """给调用方（knowbase web端、knowlab 写声明）看的整份词表。"""
    return {
        "version": VOCAB_VERSION,
        "doc_types": dict(DOC_TYPES),
        "formats": dict(FORMATS),
        "subject_kinds": [kind for kind, _ in _SUBJECT_PATTERNS],
        "periodic": {
            t: {
                "period_end": f"{m:02d}-{d:02d}",
                "deadline": f"+{y}y {dm:02d}-{dd:02d}",
            }
            for t, ((m, d), (y, dm, dd)) in PERIODIC.items()
        },
    }
