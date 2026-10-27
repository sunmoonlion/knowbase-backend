"""按描述查与解析（SDD 0043 第三节）：数据需求声明 v2、任务参数、展开、解析结果、确认。

声明（`knowbot-manifest/v2` 的数据需求）只在这里定义一处：作者在 knowlab 写声明模板
（要什么类型、哪几期、什么格式、几份、来源限制、多候选时怎么挑），请专家的人发起
任务时给主体（一家或几家）、必要时覆盖期间；knowbase 把两者展开成一条条精确的描述
（主体 × 类型 × 报告期），逐条在请的人名下找。

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
    check_doc_type,
    check_format,
    check_period,
    disclosure_deadline,
    report_period,
    subject_kind,
)

CONTRACT_VERSION = "knowbot-manifest/v2"
NEED_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_NEEDS = 20
MAX_SUBJECTS = 10
MAX_LATEST = 10
MAX_ITEMS = 200
# 确认 7 天内有效，只对那张清单修订的条目有效
CONFIRMATION_TTL = timedelta(days=7)

Reading = Literal["original", "text", "pages", "table"]
READINGS: tuple[Reading, ...] = ("original", "text", "pages", "table")
Priority = Literal["public_first", "upload_first", "newest"]
PRIORITIES: tuple[Priority, ...] = ("public_first", "upload_first", "newest")
SOURCES: tuple[str, ...] = ("disclosure", "collected", "upload", "output")
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
    """期间规则：固定期间，或「最近 N 个完整的报告期」（可加「截至任务日期已披露」）；
    不是定期报告的可以不限期间（`none`）。"""

    kind: Literal["fixed", "latest", "none"]
    start: date | None = None
    end: date | None = None
    count: int | None = None
    disclosed_by_task_date: bool = False

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
        elif self.kind != "none":
            raise refused("period_invalid", "period rule is fixed, latest or none")
        return self


@dataclass(frozen=True)
class NeedSpec:
    """声明模板里的一样。"""

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
    caliber: str | None = None  # 口径：只展示，不参与匹配
    description: str = ""

    def checked(self) -> NeedSpec:
        if not NEED_KEY.fullmatch(self.key):
            raise refused("need_key_invalid", "need key is invalid")
        if not self.name.strip() or len(self.name) > 80 or len(self.description) > 500:
            raise refused("need_name_invalid", "need name is invalid")
        _vocab(lambda: check_doc_type(self.doc_type))
        if not self.formats or len(set(self.formats)) != len(self.formats):
            raise refused("formats_invalid", "list each format once")
        for f in self.formats:
            _vocab(lambda f=f: check_format(f))
        self.period.checked(self.doc_type)
        if not 1 <= self.min_count <= self.max_count <= MAX_ITEMS:
            raise refused("count_invalid", "counts are 1 <= min <= max")
        if self.reading not in READINGS:
            raise refused("reading_invalid", "reading is invalid")
        if any(s not in SOURCES for s in self.sources):
            raise refused("sources_invalid", "source restriction is invalid")
        if self.priority is not None and self.priority not in PRIORITIES:
            raise refused("priority_invalid", "priority is invalid")
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
            },
            "formats": list(self.formats),
            "required": self.required,
            "min_count": self.min_count,
            "max_count": self.max_count,
            "reading": self.reading,
            "sources": list(self.sources),
            "priority": self.priority,
            "caliber": self.caliber,
            "description": self.description,
        }


@dataclass(frozen=True)
class TaskParams:
    """请专家的人发起任务时给的：主体（一家或几家）、任务日期、必要时覆盖期间。"""

    subjects: tuple[str, ...]
    task_date: date
    period_start: date | None = None
    period_end: date | None = None

    def checked(self) -> TaskParams:
        if not 1 <= len(self.subjects) <= MAX_SUBJECTS:
            raise refused("subjects_invalid", f"give 1 to {MAX_SUBJECTS} subjects")
        if len(set(self.subjects)) != len(self.subjects):
            raise refused("subjects_invalid", "a subject is given twice")
        for s in self.subjects:
            _vocab(lambda s=s: subject_kind(s))
        _vocab(lambda: check_period(self.period_start, self.period_end))
        return self

    def normalized(self) -> str:
        """偏好的键用的规范化参数：主体排序、覆盖的期间；不含任务日期。"""
        return json.dumps(
            {
                "subjects": sorted(self.subjects),
                "period": [
                    self.period_start.isoformat() if self.period_start else None,
                    self.period_end.isoformat() if self.period_end else None,
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def params_hash(self) -> str:
        return hashlib.sha256(self.normalized().encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "subjects": list(self.subjects),
            "task_date": self.task_date.isoformat(),
            "period_start": self.period_start.isoformat()
            if self.period_start
            else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }


@dataclass(frozen=True)
class DescriptionKey:
    """一条精确的描述：主体 × 类型 × 报告期（不限期间的两头都空）。"""

    subject: str
    doc_type: str
    period_start: date | None = None
    period_end: date | None = None

    def checked(self) -> DescriptionKey:
        _vocab(lambda: subject_kind(self.subject))
        _vocab(lambda: check_doc_type(self.doc_type))
        _vocab(lambda: check_period(self.period_start, self.period_end))
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "doc_type": self.doc_type,
            "period_start": self.period_start.isoformat()
            if self.period_start
            else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> DescriptionKey:
        start, end = data.get("period_start"), data.get("period_end")
        return DescriptionKey(
            subject=str(data["subject"]),
            doc_type=str(data["doc_type"]),
            period_start=date.fromisoformat(str(start)) if start else None,
            period_end=date.fromisoformat(str(end)) if end else None,
        )


@dataclass(frozen=True)
class Wanted:
    """展开出的一条：哪一样、哪条描述；`tentative` 是还没到法定截止的最新一期，
    只有已经拿到、且在任务日期前披露的才用（「截至任务日期已披露」）。"""

    need_key: str
    key: DescriptionKey
    tentative: bool = False


def expand(need: NeedSpec, params: TaskParams) -> list[Wanted]:
    """声明 × 任务参数 → 一条条精确的描述（新的期在前）。"""
    rule = need.period
    if params.period_start is not None and rule.kind != "none":
        # 请的人覆盖期间：定期报告按覆盖范围里每年的那一期，其余就是这一段
        rule = PeriodRule("fixed", params.period_start, params.period_end)
    out: list[Wanted] = []
    for subject in params.subjects:
        if rule.kind == "none":
            out.append(Wanted(need.key, DescriptionKey(subject, need.doc_type)))
        elif rule.kind == "fixed":
            assert rule.start is not None and rule.end is not None
            if need.doc_type in PERIODIC:
                for year in range(rule.end.year, rule.start.year - 1, -1):
                    start, end = report_period(need.doc_type, year)
                    if rule.start <= end <= rule.end:
                        out.append(
                            Wanted(
                                need.key,
                                DescriptionKey(subject, need.doc_type, start, end),
                            )
                        )
            else:
                out.append(
                    Wanted(
                        need.key,
                        DescriptionKey(subject, need.doc_type, rule.start, rule.end),
                    )
                )
        else:
            out.extend(_latest(need, subject, rule, params.task_date))
    if len(out) > MAX_ITEMS:
        raise refused("too_many_items", f"the needs expand to more than {MAX_ITEMS}")
    return out


def _latest(
    need: NeedSpec, subject: str, rule: PeriodRule, task_date: date
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
        out.append(
            Wanted(
                need.key, DescriptionKey(subject, need.doc_type, start, end), tentative
            )
        )
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
    rendition_kind: str | None = None
    rendition_no: int | None = None
    rendition_sha256: str | None = None


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
    """换过的记成偏好：只是排序输入，每次仍核当前权利；词表改版就失效。"""

    subject: str
    consumer: str
    contract_version: str
    need_key: str
    params_hash: str
    material_ids: tuple[str, ...]
    vocab_version: int
    updated_at: datetime


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
        "rendition_kind": c.rendition_kind,
        "rendition_no": c.rendition_no,
        "rendition_sha256": c.rendition_sha256,
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
        rendition_kind=data.get("rendition_kind"),  # type: ignore[arg-type]
        rendition_no=data.get("rendition_no"),  # type: ignore[arg-type]
        rendition_sha256=data.get("rendition_sha256"),  # type: ignore[arg-type]
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
        key=str(data["key"]),
        name=str(data["name"]),
        doc_type=str(data["doc_type"]),
        period=PeriodRule(
            kind=period["kind"],
            start=date.fromisoformat(period["start"]) if period.get("start") else None,
            end=date.fromisoformat(period["end"]) if period.get("end") else None,
            count=period.get("count"),
            disclosed_by_task_date=bool(period.get("disclosed_by_task_date")),
        ),
        formats=tuple(data["formats"]),  # type: ignore[arg-type]
        required=bool(data["required"]),
        min_count=int(data["min_count"]),  # type: ignore[arg-type]
        max_count=int(data["max_count"]),  # type: ignore[arg-type]
        reading=data["reading"],  # type: ignore[arg-type]
        sources=tuple(data.get("sources") or ()),  # type: ignore[arg-type]
        priority=data.get("priority"),  # type: ignore[arg-type]
        caliber=data.get("caliber"),  # type: ignore[arg-type]
        description=str(data.get("description") or ""),
    )


def params_from(data: dict[str, object]) -> TaskParams:
    start, end = data.get("period_start"), data.get("period_end")
    return TaskParams(
        subjects=tuple(data["subjects"]),  # type: ignore[arg-type]
        task_date=date.fromisoformat(str(data["task_date"])),
        period_start=date.fromisoformat(str(start)) if start else None,
        period_end=date.fromisoformat(str(end)) if end else None,
    )
