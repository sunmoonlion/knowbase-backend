"""按描述查与解析（SDD 0043 第三节）：一样数据的描述、展开、解析结果、确认。

「一样数据的描述」（契约 `data-request/v1`）只在这里定义一处：每件委托要什么数据，
由委托第 0 步用户自己的模型读专家逻辑和用户问题提出、用户改过（decisions ⑭）——
主体（或不限主体）、类型、期间规则、格式、几份、来源限制、多候选时怎么挑；knowbase
按任务日期把它展开成一条条精确的描述（主体 × 类型 × 期间），逐条在请的人名下找。

描述只按词表里的值匹配：主体、类型、报告期（定期报告的报告期按词表的规矩写），
格式与来源限制只做筛选；口径、说明是自由文本，只展示。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from app.domain.user_data import UserDataError, refused
from app.domain.vocabulary import (
    PERIODIC,
    VocabularyError,
    check_doc_type_or_class,
    check_edition,
    check_format,
    check_industry,
    check_period,
    check_topics,
    disclosure_deadline,
    report_period,
    subject_kind,
)

CONTRACT_VERSION = "data-request/v1"
NEED_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_NEEDS = 20
MAX_LATEST = 10
MAX_RECENT_MONTHS = 60
MAX_ITEMS = 200
# 确认 7 天内有效，只对那张清单修订的条目有效
CONFIRMATION_TTL = timedelta(days=7)

Reading = Literal["original", "text", "pages", "table"]
READINGS: tuple[Reading, ...] = ("original", "text", "pages", "table")
Priority = Literal["public_first", "upload_first", "newest"]
PRIORITIES: tuple[Priority, ...] = ("public_first", "upload_first", "newest")
SOURCES: tuple[str, ...] = ("disclosure", "collected", "upload", "output")
TABULAR = frozenset({"excel", "csv"})
# 解析时公开原件优先于上传（0043 第 3.2 节）；产出转来的排最后
_PUBLIC_FIRST = {"disclosure": 0, "collected": 1, "upload": 2, "output": 3}
_UPLOAD_FIRST = {"upload": 0, "output": 1, "disclosure": 2, "collected": 3}

Status = Literal[
    "resolved",  # 已定：某个输入组合
    "not_shown",  # 你有没放开：只对本人的数据提示
    "to_confirm",  # 待确认：他名下还没有
    "not_collectable",  # 采不来（私有资料）：去 knowbase 上传
    "ambiguous",  # 有歧义：开跑前请他选
    "waiting",  # 等数据：确认过、正在取
    "unusable",  # 不能用：禁用、许可不允许
]


def _vocab(work) -> None:
    try:
        work()
    except VocabularyError as exc:
        raise UserDataError(422, f"{exc.field}_invalid", str(exc)) from None


@dataclass(frozen=True)
class PeriodRule:
    """期间规则（SDD 0049）：固定期间；定期报告「最近 N 个完整的报告期」（可加
    「截至任务日期已披露」）；其余类型「最近 N 个月内披露的」（`recent`）或不限
    期间（`none`）。定期报告的期间是报告期，其余类型的期间是披露时间窗。"""

    kind: Literal["fixed", "latest", "recent", "none"]
    start: date | None = None
    end: date | None = None
    count: int | None = None
    disclosed_by_task_date: bool = False
    months: int | None = None

    def checked(self, doc_type: str) -> PeriodRule:
        if self.kind == "fixed":
            _vocab(lambda: check_period(self.start, self.end))
            if self.start is None:
                raise refused("period_invalid", "a fixed period needs start and end")
        elif self.kind == "latest":
            if doc_type not in PERIODIC:
                raise refused("period_invalid", "latest N works for periodic reports")
            if not isinstance(self.count, int) or not 1 <= self.count <= MAX_LATEST:
                raise refused("period_invalid", f"latest takes 1 to {MAX_LATEST}")
        elif self.kind == "recent":
            if doc_type in PERIODIC:
                raise refused("period_invalid", "use latest N for periodic reports")
            if (
                not isinstance(self.months, int)
                or not 1 <= self.months <= MAX_RECENT_MONTHS
            ):
                raise refused(
                    "period_invalid", f"recent takes 1 to {MAX_RECENT_MONTHS} months"
                )
        elif self.kind != "none":
            raise refused(
                "period_invalid", "period rule is fixed, latest, recent or none"
            )
        return self


def query_json(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "request_id" or v is not None}
    if not isinstance(value, dict) or set(value) - {"request_id"} != {
        "channel",
        "service",
        "tool",
        "arguments",
    }:
        raise refused(
            "query_invalid", "query needs channel, service, tool and arguments"
        )
    if "request_id" in value and (
        not isinstance(value["request_id"], str)
        or not re.fullmatch(r"[a-f0-9]{32}", value["request_id"])
    ):
        raise refused("query_invalid", "invalid query request identity")
    for name, pattern in (
        ("channel", r"[a-z][a-z0-9-]{1,39}"),
        ("service", r"[a-z][a-z0-9_]{0,63}"),
        ("tool", r"[a-z][a-z0-9_]{0,99}"),
    ):
        if not isinstance(value[name], str) or not re.fullmatch(pattern, value[name]):
            raise refused("query_invalid", "invalid query route")
    if not isinstance(value["arguments"], dict) or len(value["arguments"]) > 50:
        raise refused("query_invalid", "query arguments must be an object")
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise refused("query_invalid", "query must be JSON") from None
    if len(canonical.encode()) > 20000:
        raise refused("query_invalid", "query is too large")
    return canonical


@dataclass(frozen=True)
class NeedSpec:
    """一样数据的描述（第 0 步提出、用户改过的）。主体为空就是不限主体。"""

    key: str
    name: str
    doc_type: str
    period: PeriodRule
    formats: tuple[str, ...]
    required: bool = True
    min_count: int = 1
    max_count: int = 1
    reading: Reading = "original"
    sources: tuple[str, ...] = ()  # 空：不限来源
    priority: Priority | None = None
    # 表格类的表与列：（表名或空，几列）；解析不按它挑，开跑前按它核
    tables: tuple[tuple[str | None, tuple[str, ...]], ...] = ()
    caliber: str | None = None  # 口径：只展示，不参与匹配
    description: str = ""
    # 进键的版本形态；筛选的事项（任一对上即可）、行业
    edition: str = "full"
    topics: tuple[str, ...] = ()
    industry: str | None = None
    subject: str | None = None
    source_query: str | None = None

    def checked(self) -> NeedSpec:
        if not NEED_KEY.fullmatch(self.key):
            raise refused("need_key_invalid", "need key is invalid")
        if not self.name.strip() or len(self.name) > 80 or len(self.description) > 500:
            raise refused("need_name_invalid", "need name is invalid")
        if self.subject is not None:
            _vocab(lambda: subject_kind(self.subject))  # type: ignore[arg-type]
        _vocab(lambda: check_doc_type_or_class(self.doc_type))
        _vocab(lambda: check_edition(self.edition))
        _vocab(lambda: check_topics(self.topics))
        if self.industry is not None:
            _vocab(lambda: check_industry(self.industry))  # type: ignore[arg-type]
        if not self.formats or len(set(self.formats)) != len(self.formats):
            raise refused("formats_invalid", "list each format once")
        for f in self.formats:
            _vocab(lambda f=f: check_format(f))
        self.period.checked(self.doc_type)
        if self.source_query is not None:
            query_json(json.loads(self.source_query))
            if (
                self.doc_type != "other_text"
                or self.period.kind != "none"
                or set(self.formats) != {"text"}
                or self.reading not in ("original", "text")
                or self.edition != "full"
            ):
                raise refused(
                    "query_invalid",
                    "supplier responses are text evidence, not report originals; "
                    "dates belong in query arguments",
                )
            if self.sources and set(self.sources) != {"collected"}:
                raise refused(
                    "query_invalid", "supplier responses use collected sources"
                )
        if not 1 <= self.min_count <= self.max_count <= MAX_ITEMS:
            raise refused("count_invalid", "counts are 1 <= min <= max")
        if self.reading not in READINGS:
            raise refused("reading_invalid", "reading is invalid")
        if any(s not in SOURCES for s in self.sources):
            raise refused("sources_invalid", "source restriction is invalid")
        if self.priority is not None and self.priority not in PRIORITIES:
            raise refused("priority_invalid", "priority is invalid")
        if self.tables and not set(self.formats) <= TABULAR:
            raise refused("tables_invalid", "tables are for Excel or CSV data")
        if len(self.tables) > 20 or any(
            not columns or len(columns) > 50 for _, columns in self.tables
        ):
            raise refused("tables_invalid", "1 to 50 columns per table, 20 tables")
        if self.caliber is not None and len(self.caliber) > 200:
            raise refused("caliber_invalid", "caliber is too long")
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "name": self.name,
            "doc_type": self.doc_type,
            "period": {
                "kind": self.period.kind,
                "start": self.period.start.isoformat() if self.period.start else None,
                "end": self.period.end.isoformat() if self.period.end else None,
                "count": self.period.count,
                "disclosed_by_task_date": self.period.disclosed_by_task_date,
                "months": self.period.months,
            },
            "formats": list(self.formats),
            "required": self.required,
            "min_count": self.min_count,
            "max_count": self.max_count,
            "reading": self.reading,
            "sources": list(self.sources),
            "priority": self.priority,
            "tables": [
                {"sheet": sheet, "columns": list(columns)}
                for sheet, columns in self.tables
            ],
            "caliber": self.caliber,
            "description": self.description,
            "edition": self.edition,
            "topics": list(self.topics),
            "industry": self.industry,
            "subject": self.subject,
            **(
                {"source_query": json.loads(self.source_query)}
                if self.source_query
                else {}
            ),
        }


@dataclass(frozen=True)
class TaskParams:
    """解析用的任务参数：只有任务日期（「最近 N 期」「截至任务日期已披露」按它算）。
    主体写在每一样描述上（10-28 起不再由请专家的人另给）。"""

    task_date: date

    def checked(self) -> TaskParams:
        return self

    def as_dict(self) -> dict[str, object]:
        return {"task_date": self.task_date.isoformat()}


@dataclass(frozen=True)
class DescriptionKey:
    """一条描述（SDD 0049）：进键的主体 × 类型 × 期间 × 版本形态，外加筛选的事项、
    行业（缩小范围用，不改「在不在」怎么认）。

    类型可以是细类或大类；定期报告的期间是报告期（一条对一份），其余类型的期间是
    披露时间窗（一条对窗里的一批）；不限期间的两头都空；不限主体的主体为空。缺省的版本形态（全文）、
    空的事项与行业不写进字典，旧的字典照样读得回来。
    """

    subject: str | None
    doc_type: str
    period_start: date | None = None
    period_end: date | None = None
    edition: str = "full"
    topics: tuple[str, ...] = ()
    industry: str | None = None
    source_query: str | None = None

    def checked(self) -> DescriptionKey:
        if self.subject is not None:
            _vocab(lambda: subject_kind(self.subject))  # type: ignore[arg-type]
        _vocab(lambda: check_doc_type_or_class(self.doc_type))
        _vocab(lambda: check_period(self.period_start, self.period_end))
        _vocab(lambda: check_edition(self.edition))
        _vocab(lambda: check_topics(self.topics))
        if self.industry is not None:
            _vocab(lambda: check_industry(self.industry))  # type: ignore[arg-type]
        if self.source_query is not None:
            query_json(json.loads(self.source_query))
            if (
                self.doc_type != "other_text"
                or self.period_start
                or self.period_end
                or self.edition != "full"
            ):
                raise refused(
                    "query_invalid", "query responses have their own exact identity"
                )
        return self

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "subject": self.subject,
            "doc_type": self.doc_type,
            "period_start": self.period_start.isoformat()
            if self.period_start
            else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }
        if self.edition != "full":
            out["edition"] = self.edition
        if self.topics:
            out["topics"] = list(self.topics)
        if self.industry is not None:
            out["industry"] = self.industry
        if self.source_query is not None:
            out["source_query"] = json.loads(self.source_query)
        return out

    @staticmethod
    def from_dict(data: dict[str, object]) -> DescriptionKey:
        start, end = data.get("period_start"), data.get("period_end")
        topics = data.get("topics") or ()
        return DescriptionKey(
            subject=str(data["subject"]) if data.get("subject") else None,
            source_query=query_json(data.get("source_query")),
            doc_type=str(data["doc_type"]),
            period_start=date.fromisoformat(str(start)) if start else None,
            period_end=date.fromisoformat(str(end)) if end else None,
            edition=str(data.get("edition") or "full"),
            topics=tuple(str(t) for t in topics),  # type: ignore[union-attr]
            industry=str(data["industry"]) if data.get("industry") else None,
        )


@dataclass(frozen=True)
class Wanted:
    """展开出的一条：哪一样、哪条描述；`tentative` 是还没到法定截止的最新一期，
    只有已经拿到、且在任务日期前披露的才用（「截至任务日期已披露」）。"""

    need_key: str
    key: DescriptionKey
    tentative: bool = False


def expand(need: NeedSpec, params: TaskParams) -> list[Wanted]:
    """一样数据的描述 × 任务日期 → 一条条描述（新的期在前）。

    定期报告一期一条；其余类型一条就是一个披露时间窗（或不限期间）。
    """
    rule = need.period
    subject = need.subject

    def key(subject: str | None, start: date | None = None, end: date | None = None):
        return DescriptionKey(
            subject,
            need.doc_type,
            start,
            end,
            edition=need.edition,
            topics=need.topics,
            industry=need.industry,
            source_query=need.source_query,
        )

    out: list[Wanted] = []
    if rule.kind == "none":
        out.append(Wanted(need.key, key(subject)))
    elif rule.kind == "recent":
        assert rule.months is not None
        start = months_before(params.task_date, rule.months)
        out.append(Wanted(need.key, key(subject, start, params.task_date)))
    elif rule.kind == "fixed":
        assert rule.start is not None and rule.end is not None
        if need.doc_type in PERIODIC:
            for year in range(rule.end.year, rule.start.year - 1, -1):
                start, end = report_period(need.doc_type, year)
                if rule.start <= end <= rule.end:
                    out.append(Wanted(need.key, key(subject, start, end)))
        else:
            out.append(Wanted(need.key, key(subject, rule.start, rule.end)))
    else:
        out.extend(_latest(need, subject, rule, params.task_date, key))
    if len(out) > MAX_ITEMS:
        raise refused("too_many_items", f"the needs expand to more than {MAX_ITEMS}")
    return out


def months_before(day: date, months: int) -> date:
    """往前推几个月的同一天（那个月没有这一天就取月底）。"""
    total = day.year * 12 + day.month - 1 - months
    year, month = divmod(total, 12)
    month += 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, d)
        except ValueError:
            continue
    raise AssertionError("unreachable")  # pragma: no cover


def _latest(
    need: NeedSpec, subject: str | None, rule: PeriodRule, task_date: date, key
) -> list[Wanted]:
    """最近 N 个完整的报告期（报告期末在任务日期之前）；要「截至任务日期已披露」的，
    还没到法定截止的那一期标 tentative，多展开一期备着。"""
    count = rule.count or 1
    out: list[Wanted] = []
    year = task_date.year
    firm = 0
    while firm < count:
        start, end = report_period(need.doc_type, year)
        year -= 1
        if end >= task_date:
            continue
        tentative = (
            rule.disclosed_by_task_date
            and disclosure_deadline(need.doc_type, end.year) > task_date
        )
        out.append(Wanted(need.key, key(subject, start, end), tentative))
        if not tentative:
            firm += 1
    return out


@dataclass(frozen=True)
class Candidate:
    """请的人名下一份对得上的资料。"""

    material_id: str
    version_no: int
    sha256: str
    content_origin: str
    disclosed_on: date | None
    created_at: datetime
    shown: bool
    data_format: str | None = None
    usable: bool = True
    # 开跑时凭什么签：display（他放开给这个应用的）、reference（他名下的引用）、
    # public（公开原件层，开关开着时谁都能读）
    basis: str = "display"
    rendition_kind: str | None = None
    rendition_no: int | None = None
    rendition_sha256: str | None = None
    # 列给请的人看的：资料名字、那一版的类型与大小
    name: str = ""
    media_type: str = ""
    size_bytes: int = 0


@dataclass(frozen=True)
class ResultItem:
    need_key: str
    key: DescriptionKey
    status: Status
    chosen: tuple[Candidate, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    reason: str | None = None


def rank(
    candidates: list[Candidate], priority: Priority | None
) -> list[list[Candidate]]:
    """按优先策略分档（同档的分不出先后）；没写策略的，所有候选同一档。"""
    if priority is None:
        return [candidates] if candidates else []

    def score(c: Candidate) -> tuple:
        if priority == "public_first":
            return (_PUBLIC_FIRST[c.content_origin],)
        if priority == "upload_first":
            return (_UPLOAD_FIRST[c.content_origin],)
        # newest：披露时间新的在前，没写的按入库时间
        return (-(c.disclosed_on or c.created_at.date()).toordinal(),)

    groups: dict[tuple, list[Candidate]] = {}
    for c in candidates:
        groups.setdefault(score(c), []).append(c)
    return [groups[k] for k in sorted(groups)]


@dataclass(frozen=True)
class Resolution:
    id: str
    revision: int
    subject: str
    app: str
    consumer: str
    contract_version: str
    vocab_version: int
    needs: tuple[NeedSpec, ...]
    params: TaskParams
    items: tuple[ResultItem, ...]
    created_at: datetime
    task_ref: str | None = None


ConfirmationKind = Literal["description", "url", "topic"]


@dataclass(frozen=True)
class Confirmation:
    """用户点一次留下的确认：哪张清单修订、哪些条目、当时的供给条件。"""

    id: str
    subject: str
    app: str
    kind: ConfirmationKind
    items: tuple[dict[str, object], ...]
    supply: dict[str, object]
    created_at: datetime
    expires_at: datetime
    task_ref: str | None = None
    resolution_id: str | None = None
    resolution_revision: int | None = None
    needs: tuple[dict[str, object], ...] = field(default_factory=tuple)

    def valid(self, now: datetime) -> bool:
        return now < self.expires_at


@dataclass(frozen=True)
class Preference:
    """换过的记成偏好（用户 × 使用者 × 描述）：只是排序输入，每次仍核当前权利；
    词表改版就失效。"""

    subject: str
    consumer: str
    key_id: str
    material_ids: tuple[str, ...]
    vocab_version: int
    updated_at: datetime


def key_id(key: DescriptionKey) -> str:
    """偏好按描述记：描述字典的摘要。"""
    body = json.dumps(key.as_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def candidate_dict(c: Candidate) -> dict[str, object]:
    return {
        "material_id": c.material_id,
        "version_no": c.version_no,
        "sha256": c.sha256,
        "content_origin": c.content_origin,
        "disclosed_on": c.disclosed_on.isoformat() if c.disclosed_on else None,
        "created_at": c.created_at.isoformat(),
        "shown": c.shown,
        "data_format": c.data_format,
        "usable": c.usable,
        "basis": c.basis,
        "rendition_kind": c.rendition_kind,
        "rendition_no": c.rendition_no,
        "rendition_sha256": c.rendition_sha256,
        "name": c.name,
        "media_type": c.media_type,
        "size_bytes": c.size_bytes,
    }


def candidate_from(data: dict[str, object]) -> Candidate:
    disclosed = data.get("disclosed_on")
    return Candidate(
        material_id=str(data["material_id"]),
        version_no=int(data["version_no"]),  # type: ignore[arg-type]
        sha256=str(data["sha256"]),
        content_origin=str(data["content_origin"]),
        disclosed_on=date.fromisoformat(str(disclosed)) if disclosed else None,
        created_at=datetime.fromisoformat(str(data["created_at"])),
        shown=bool(data["shown"]),
        data_format=data.get("data_format"),  # type: ignore[arg-type]
        usable=bool(data.get("usable", True)),
        basis=str(data.get("basis") or "display"),
        rendition_kind=data.get("rendition_kind"),  # type: ignore[arg-type]
        rendition_no=data.get("rendition_no"),  # type: ignore[arg-type]
        rendition_sha256=data.get("rendition_sha256"),  # type: ignore[arg-type]
        name=str(data.get("name") or ""),
        media_type=str(data.get("media_type") or ""),
        size_bytes=int(data.get("size_bytes") or 0),  # type: ignore[call-overload]
    )


def item_dict(item: ResultItem) -> dict[str, object]:
    return {
        "need_key": item.need_key,
        "key": item.key.as_dict(),
        "status": item.status,
        "chosen": [candidate_dict(c) for c in item.chosen],
        "candidates": [candidate_dict(c) for c in item.candidates],
        "reason": item.reason,
    }


def item_from(data: dict[str, object]) -> ResultItem:
    return ResultItem(
        need_key=str(data["need_key"]),
        key=DescriptionKey.from_dict(data["key"]),  # type: ignore[arg-type]
        status=data["status"],  # type: ignore[arg-type]
        chosen=tuple(candidate_from(c) for c in data.get("chosen") or ()),  # type: ignore[union-attr]
        candidates=tuple(candidate_from(c) for c in data.get("candidates") or ()),  # type: ignore[union-attr]
        reason=data.get("reason"),  # type: ignore[arg-type]
    )


def need_from(data: dict[str, object]) -> NeedSpec:
    period = data["period"]
    assert isinstance(period, dict)
    return NeedSpec(
        source_query=query_json(data.get("source_query")),
        key=str(data["key"]),
        name=str(data["name"]),
        doc_type=str(data["doc_type"]),
        period=PeriodRule(
            kind=period["kind"],
            start=date.fromisoformat(period["start"]) if period.get("start") else None,
            end=date.fromisoformat(period["end"]) if period.get("end") else None,
            count=period.get("count"),
            disclosed_by_task_date=bool(period.get("disclosed_by_task_date")),
            months=period.get("months"),
        ),
        formats=tuple(data["formats"]),  # type: ignore[arg-type]
        required=bool(data["required"]),
        min_count=int(data["min_count"]),  # type: ignore[arg-type]
        max_count=int(data["max_count"]),  # type: ignore[arg-type]
        reading=data["reading"],  # type: ignore[arg-type]
        sources=tuple(data.get("sources") or ()),  # type: ignore[arg-type]
        priority=data.get("priority"),  # type: ignore[arg-type]
        tables=tuple(
            (t.get("sheet"), tuple(t["columns"]))
            for t in data.get("tables") or ()  # type: ignore[union-attr]
        ),
        caliber=data.get("caliber"),  # type: ignore[arg-type]
        description=str(data.get("description") or ""),
        edition=str(data.get("edition") or "full"),
        topics=tuple(data.get("topics") or ()),  # type: ignore[arg-type]
        industry=data.get("industry"),  # type: ignore[arg-type]
        subject=data.get("subject"),  # type: ignore[arg-type]
    )


def params_from(data: dict[str, object]) -> TaskParams:
    return TaskParams(task_date=date.fromisoformat(str(data["task_date"])))
