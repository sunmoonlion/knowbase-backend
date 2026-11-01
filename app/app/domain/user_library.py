"""资料库：数据第四版的用户资料（SDD 0043 第二节）。

和 v1（SDD 0042，按「用户 × 应用」分区，10-28 已删）的不同：
- 资料不再按应用分区：一份资料归一个用户，出现在哪个应用由同意依据（展示）管；
- 资料有内容版本（内容的替换）与读法（同一版本的派生形态，读法也有版本）；
- 内容来源挂在资料上，用户取得它的依据（上传、采集、购买、引用、产出）挂在使用权上；
- 描述用统一词表（`vocabulary.py`，SDD 0049），带确认状态，只有确认过的参与解析。

资料就是 `user_data_item` 的一行（v1 时代的那几栏缓存当前版本），v1 的旧行在迁移 0010
回填了第 1 版、所有者的使用权与「展示给原来那个应用」。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Literal

from app.domain.user_data import UserDataError, refused
from app.domain.vocabulary import (
    RESEARCH_CLOSED,
    VOCAB_VERSION,
    VocabularyError,
    check_attributes,
    check_doc_type,
    check_doc_type_or_class,
    check_edition,
    check_format,
    check_industry,
    check_language,
    check_period,
    check_topics,
    subject_kind,
    types_of,
)

ContentOrigin = Literal["disclosure", "collected", "upload", "output"]
CONTENT_ORIGINS: tuple[ContentOrigin, ...] = (
    "disclosure",
    "collected",
    "upload",
    "output",
)

# 许可用法：只许本人用、可以经专家给别人用、可以出售（数据拓扑第 5.3 节）
License = Literal["self", "via_expert", "sale"]
LICENSES: tuple[License, ...] = ("self", "via_expert", "sale")
_LICENSE_RANK: dict[str, int] = {"self": 0, "via_expert": 1, "sale": 2}

# 描述的确认状态：只有 checked（入库质检过）、confirmed（用户确认过）参与解析
DescriptionStatus = Literal["unconfirmed", "confirmed", "checked"]
RESOLVABLE: tuple[DescriptionStatus, ...] = ("confirmed", "checked")

# 取得依据：用户凭什么有这份资料；总栏按它分类（0043 第 2.1 节）。
# convert：本人把一份产出「转为数据」，进第一区、能被解析，归「我上传的」一栏
Basis = Literal["upload", "collect", "purchase", "reference", "output", "convert"]
BASES: tuple[Basis, ...] = (
    "upload",
    "collect",
    "purchase",
    "reference",
    "output",
    "convert",
)
CATEGORY_OF_BASIS: dict[Basis, str] = {
    "upload": "uploaded",
    "convert": "uploaded",
    "collect": "collected",
    "reference": "collected",
    "purchase": "purchased",
    "output": "output",
}

RenditionKind = Literal["text", "pages", "table", "index", "parse"]
RENDITION_KINDS: tuple[RenditionKind, ...] = (
    "text",
    "pages",
    "table",
    "index",
    "parse",
)
RenditionStatus = Literal["pending", "ready", "failed"]


def license_rank(license_: str) -> int:
    return _LICENSE_RANK[license_]


def check_license(license_: str) -> License:
    if license_ not in LICENSES:
        raise refused("license_invalid", "license is invalid")
    return license_  # type: ignore[return-value]


@dataclass(frozen=True)
class Description:
    """是什么（SDD 0049）：进键的类型（细类）、主体、期间、版本形态；筛选的事项、
    行业、语言、格式、研报的几维；外加披露时间、确认状态与词表版本。

    期间：定期报告是报告期；其余类型可以记内容覆盖的期间，解析按披露时间匹配。
    """

    subject: str | None = None
    doc_type: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    disclosed_on: date | None = None
    data_format: str | None = None
    status: DescriptionStatus = "unconfirmed"
    vocab_version: int = VOCAB_VERSION
    edition: str = "full"
    topics: tuple[str, ...] = ()
    industry: str | None = None
    language: str | None = None
    attributes: dict[str, object] = field(default_factory=dict)

    def checked(self) -> Description:
        """按词表核每一栏；不认得的拒（422 带是哪一栏）。"""
        try:
            if self.subject is not None:
                subject_kind(self.subject)
            if self.doc_type is not None:
                check_doc_type(self.doc_type)
            if self.data_format is not None:
                check_format(self.data_format)
            check_period(self.period_start, self.period_end)
            check_edition(self.edition)
            check_topics(self.topics)
            if self.industry is not None:
                check_industry(self.industry)
            if self.language is not None:
                check_language(self.language)
            check_attributes(self.doc_type, self.attributes)
        except VocabularyError as exc:
            raise UserDataError(422, f"{exc.field}_invalid", str(exc)) from None
        return self

    @property
    def resolvable(self) -> bool:
        return self.status in RESOLVABLE and self.vocab_version == VOCAB_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "doc_type": self.doc_type,
            "period_start": self.period_start.isoformat()
            if self.period_start
            else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "disclosed_on": self.disclosed_on.isoformat()
            if self.disclosed_on
            else None,
            "data_format": self.data_format,
            "status": self.status,
            "vocab_version": self.vocab_version,
            "edition": self.edition,
            "topics": list(self.topics),
            "industry": self.industry,
            "language": self.language,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True)
class DescriptionFilter:
    """按描述的维度圈（SDD 0049；检索「先圈后找」的圈）：写了的都要对上，事项任一
    对上即可；类型可以写细类或大类。期间按报告期末筛，披露时间按披露日筛。"""

    doc_type: str | None = None
    subjects: tuple[str, ...] = ()
    period_from: date | None = None
    period_to: date | None = None
    disclosed_from: date | None = None
    disclosed_to: date | None = None
    edition: str | None = None
    topics: tuple[str, ...] = ()
    industry: str | None = None
    language: str | None = None
    # 研报的封闭几维：盈利预测任一对上，其余相等
    research: dict[str, str] = field(default_factory=dict)

    def checked(self) -> DescriptionFilter:
        try:
            if self.doc_type is not None:
                check_doc_type_or_class(self.doc_type)
            for subject in self.subjects:
                subject_kind(subject)
            if self.edition is not None:
                check_edition(self.edition)
            check_topics(self.topics)
            if self.industry is not None:
                check_industry(self.industry)
            if self.language is not None:
                check_language(self.language)
            for key, value in self.research.items():
                closed = RESEARCH_CLOSED.get(key)
                if closed is None or value not in closed:
                    raise VocabularyError("research", f"{key} is not a closed value")
        except VocabularyError as exc:
            raise UserDataError(422, f"{exc.field}_invalid", str(exc)) from None
        if len(self.subjects) > 200:
            raise refused("filter_invalid", "too many subjects")
        return self

    @property
    def empty(self) -> bool:
        return self == DescriptionFilter()

    def matches(self, d: Description) -> bool:
        if self.doc_type is not None and d.doc_type not in types_of(self.doc_type):
            return False
        if self.subjects and d.subject not in self.subjects:
            return False
        if not _within(d.period_end, self.period_from, self.period_to):
            return False
        if not _within(d.disclosed_on, self.disclosed_from, self.disclosed_to):
            return False
        if self.edition is not None and d.edition != self.edition:
            return False
        if self.topics and not set(self.topics) & set(d.topics):
            return False
        if self.industry is not None and d.industry != self.industry:
            return False
        if self.language is not None and d.language != self.language:
            return False
        for key, value in self.research.items():
            got = d.attributes.get(key)
            if key == "forecast":
                if not isinstance(got, list) or value not in got:
                    return False
            elif got != value:
                return False
        return True


def _within(day: date | None, start: date | None, end: date | None) -> bool:
    if start is None and end is None:
        return True
    if day is None:
        return False
    return (start is None or day >= start) and (end is None or day <= end)


@dataclass(frozen=True)
class Material:
    """一份资料（逻辑的一份）：挂描述、许可档、所有者、状态。"""

    id: str
    owner: str
    name: str
    content_origin: ContentOrigin
    license: License
    description: Description
    current_version: int
    created_at: datetime
    deleted_at: datetime | None = None
    disabled_at: datetime | None = None
    # v1 时代的应用分区；第四版新传的为空
    legacy_app: str | None = None
    # 出处：产出、转为数据才有
    provenance: dict[str, object] | None = None
    purged_at: datetime | None = None

    @property
    def live(self) -> bool:
        return self.deleted_at is None and self.disabled_at is None

    @property
    def readable(self) -> bool:
        """凭授权、凭使用权还能读：没禁用、没清除（所有者删了买家照旧能读）。"""
        return self.disabled_at is None and self.purged_at is None

    @property
    def in_output_zone(self) -> bool:
        """产出区的不参与解析；转为数据的另是一份（内容来源也是产出，但带转来的出处）。"""
        return self.content_origin == "output" and not (self.provenance or {}).get(
            "converted_from"
        )

    def with_description(self, description: Description) -> Material:
        return replace(self, description=description)


@dataclass(frozen=True)
class ContentVersion:
    """一个内容版本：内容的一次替换，只增不改。"""

    id: str
    material_id: str
    version_no: int
    sha256: str
    size_bytes: int
    media_type: str
    object_key: str
    created_at: datetime
    created_by: str
    ragflow_dataset_id: str | None = None
    ragflow_document_id: str | None = None
    ragflow_job_id: str | None = None
    ragflow_status: str | None = None


@dataclass(frozen=True)
class Rendition:
    """一个读法版本：某个内容版本的一种派生形态（正文、按页文字、表格、检索索引）。

    内容不在这一行：正文、按页文字、表格的事实行各在自己的表里（0043 第五节）；
    `sha256` 是按规范化内容算的摘要。
    """

    id: str
    version_id: str
    kind: RenditionKind
    rendition_no: int
    processor: str
    processor_version: str
    status: RenditionStatus
    created_at: datetime
    sha256: str | None = None
    detail: dict[str, object] = field(default_factory=dict)
    ready_at: datetime | None = None


@dataclass(frozen=True)
class Holding:
    """使用权：某个用户凭什么（取得依据）能怎么用一份资料。

    所有者天然有一条（上传、采集、产出）；买家凭交易有一条，只到交易那一个内容版本
    （`version_no`）；第二区引用另记（0043 第 2.4 节）。
    """

    id: str
    subject: str
    material_id: str
    basis: Basis
    license: License
    created_at: datetime
    version_no: int | None = None  # None：所有版本（所有者）
    ref: str | None = None  # 交易编号、需求依据编号
    ended_at: datetime | None = None
    end_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.ended_at is None

    @property
    def category(self) -> str:
        return CATEGORY_OF_BASIS[self.basis]

    def covers(self, version_no: int) -> bool:
        return self.version_no is None or self.version_no == version_no


@dataclass(frozen=True)
class ShelfEntry:
    """总栏里的一行：这个用户名下的一份资料，按他的取得依据分类。"""

    material: Material
    holding: Holding
    version: ContentVersion

    @property
    def category(self) -> str:
        return self.holding.category


def version_object_key(
    prefix: str, subject: str, material_id: str, version_no: int
) -> str:
    """内容版本的对象键：按用户分开，用户编号做了摘要；不再带应用分区。"""
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    root = f"{prefix.rstrip('/')}/{digest[:2]}/{digest}"
    return f"{root}/library/{material_id}/v{version_no}"


# 公开原件层的保留所有者：不是任何用户，也不是平台账号。公开披露存一份在它名下，
# 用户名下的第二区引用就是一条取得依据为「引用」的使用权（SDD 0043 第 2.2、2.4 节）
PUBLIC_OWNER = "public:disclosure"

# 总栏里的展示按钮对应的用途；knowmart 那一栏就是「交给 knowmart 待上架」
COLUMN_USES: dict[str, str] = {
    "knowbench": "app:knowbench",
    "knowlab": "app:knowlab",
    "knowmart": "market",
}
DEFAULT_DISPLAY_USES: tuple[str, ...] = ("app:knowbench", "app:knowlab")
_EXPERT_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_VERSION_LABEL = re.compile(r"^[0-9][0-9.]{0,31}$")


def check_material_use(use: str) -> str:
    """总栏里按资料点的那几种用途：展示给应用、交给 knowmart、绑给专家（下一版）。

    平台过程、订阅、加工另有入口（`platform_use` 等），不走这里。
    """
    if use in ("app:knowbench", "app:knowlab", "market"):
        return use
    if use.startswith("expert:") and _EXPERT_ID.fullmatch(use.removeprefix("expert:")):
        return use
    raise refused("use_invalid", "use is invalid")


def frozen_expert_use(expert_id: str, version: str) -> str:
    if not _EXPERT_ID.fullmatch(expert_id) or not _VERSION_LABEL.fullmatch(version):
        raise refused("expert_invalid", "expert or version is invalid")
    return f"expert:{expert_id}@{version}"


@dataclass(frozen=True)
class Use:
    """一条同意依据（第四版）。"""

    id: str
    subject: str
    use: str
    created_at: datetime
    created_via: str
    material_id: str | None = None
    version_no: int | None = None
    scope: dict[str, object] = field(default_factory=dict)
    withdrawn_at: datetime | None = None

    @property
    def live(self) -> bool:
        return self.withdrawn_at is None


@dataclass(frozen=True)
class LogEntry:
    """资料的一条改动留痕：改了什么（描述、许可档、识别）、改前、改后。"""

    what: Literal["description", "license", "recognized"]
    before: dict[str, object]
    after: dict[str, object]


# 直达上传能顺带展示给的应用：来源应用就是要这份资料的那个（0044 第 2.4 节）
DIRECT_UPLOAD_APPS: tuple[str, ...] = ("knowbench", "knowlab")


@dataclass(frozen=True)
class Account:
    subject: str
    default_display: bool = True
    default_display_seen_at: datetime | None = None

    @property
    def displays_by_default(self) -> bool:
        """开关开着、而且本人看过点过，才自动展示给全部应用。"""
        return self.default_display and self.default_display_seen_at is not None


@dataclass(frozen=True)
class Expert:
    expert_id: str
    owner: str
    name: str
    latest_version: str | None
    updated_at: datetime


# —— 平台过程、订阅、加工三种同意依据（块 1b 余下） ——

# 平台过程：孵化、测试、交付审核、自助重跑；范围是「这次过程」，所以用途里带过程编号
PLATFORM_KINDS: tuple[str, ...] = ("incubate", "test", "review", "rerun")
PLATFORM_APPS: tuple[str, ...] = ("knowlab",)
_REF = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# 订阅：必须有截止日，最长一年（0045 第 2.1 节）
SUBSCRIPTION_MAX_DAYS = 366
SUBSCRIPTION_BY: tuple[str, ...] = ("description", "url", "topic")


def platform_use(app: str, kind: str, ref: str) -> str:
    """`platform:<应用>:<过程>:<过程编号>`，如 `platform:knowlab:test:t-42`。"""
    if app not in PLATFORM_APPS:
        raise refused("platform_app_invalid", "no platform process in this app")
    if kind not in PLATFORM_KINDS:
        raise refused("platform_kind_invalid", "platform process kind is invalid")
    if not _REF.fullmatch(ref):
        raise refused("ref_invalid", "process ref is invalid")
    return f"platform:{app}:{kind}:{ref}"


def subscription_use(subscription_id: str, revision: int) -> str:
    """`subscription:<订阅>@<修订>`；续订出新修订，旧修订的依据照旧可查。"""
    if not _REF.fullmatch(subscription_id) or not 1 <= revision <= 10_000:
        raise refused("subscription_invalid", "subscription or revision is invalid")
    return f"subscription:{subscription_id}@{revision}"


def processing_use(version_id: str) -> str:
    """`processing:<内容版本>`：只这一份、只这个目的。"""
    return f"processing:{version_id}"


@dataclass(frozen=True)
class SubscriptionScope:
    """订阅的长期范围同意：订什么、起止、用途、自动取得方式（0045 第 2.1 节）。

    按描述订只认主体与类型，从开始那天起往后新发的；按网址订是栏目页、RSS 源；
    按主题订每轮的候选仍要本人勾（`pick_each_round`）。
    """

    by: str
    starts_on: date
    ends_on: date
    purpose: str
    subjects: tuple[str, ...] = ()
    doc_types: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()
    topic: str | None = None

    @property
    def pick_each_round(self) -> bool:
        return self.by == "topic"

    def checked(self) -> SubscriptionScope:
        if self.by not in SUBSCRIPTION_BY:
            raise refused("subscription_by_invalid", "subscribe by what is invalid")
        if self.ends_on < self.starts_on:
            raise refused("period_invalid", "subscription ends before it starts")
        if (self.ends_on - self.starts_on).days > SUBSCRIPTION_MAX_DAYS:
            raise refused("period_too_long", "a subscription lasts at most one year")
        if not self.purpose.strip() or len(self.purpose) > 200:
            raise refused("purpose_invalid", "purpose is required")
        try:
            for subject in self.subjects:
                subject_kind(subject)
            for doc_type in self.doc_types:
                check_doc_type_or_class(doc_type)
        except VocabularyError as exc:
            raise UserDataError(422, f"{exc.field}_invalid", str(exc)) from None
        wanted = {
            "description": bool(self.subjects and self.doc_types),
            "url": bool(self.urls),
            "topic": bool(self.topic and self.topic.strip()),
        }
        if not wanted[self.by]:
            raise refused(
                "subscription_scope_empty", f"subscribe by {self.by} needs it"
            )
        if any(not u.startswith(("https://", "http://")) for u in self.urls):
            raise refused("url_invalid", "url is invalid")
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "by": self.by,
            "starts_on": self.starts_on.isoformat(),
            "ends_on": self.ends_on.isoformat(),
            "purpose": self.purpose,
            "subjects": list(self.subjects),
            "doc_types": list(self.doc_types),
            "urls": list(self.urls),
            "topic": self.topic,
            "pick_each_round": self.pick_each_round,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> SubscriptionScope:
        return SubscriptionScope(
            by=str(data["by"]),
            starts_on=date.fromisoformat(str(data["starts_on"])),
            ends_on=date.fromisoformat(str(data["ends_on"])),
            purpose=str(data["purpose"]),
            subjects=tuple(data.get("subjects") or ()),  # type: ignore[arg-type]
            doc_types=tuple(data.get("doc_types") or ()),  # type: ignore[arg-type]
            urls=tuple(data.get("urls") or ()),  # type: ignore[arg-type]
            topic=data.get("topic"),  # type: ignore[arg-type]
        )
