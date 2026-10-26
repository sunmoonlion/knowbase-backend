"""资料库：数据第四版的用户资料（SDD 0043 第二节）。

和 v1（`user_data.py`，按「用户 × 应用」分区）的不同：
- 资料不再按应用分区：一份资料归一个用户，出现在哪个应用由同意依据（展示）管；
- 资料有内容版本（内容的替换）与读法（同一版本的派生形态，读法也有版本）；
- 内容来源挂在资料上，用户取得它的依据（上传、采集、购买、引用、产出）挂在使用权上；
- 描述用统一词表（`vocabulary.py`），带确认状态，只有确认过的参与解析。

v1 的表照旧用：资料就是 `user_data_item` 的一行（v1 的那几栏缓存当前版本），旧行在迁移时
回填了第 1 版、所有者的使用权与「展示给原来那个应用」。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Literal

from app.domain.user_data import UserDataError, refused
from app.domain.vocabulary import (
    VOCAB_VERSION,
    VocabularyError,
    check_doc_type,
    check_format,
    check_period,
    subject_kind,
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

# 取得依据：用户凭什么有这份资料；总栏按它分类（0043 第 2.1 节）
Basis = Literal["upload", "collect", "purchase", "reference", "output"]
BASES: tuple[Basis, ...] = ("upload", "collect", "purchase", "reference", "output")
CATEGORY_OF_BASIS: dict[Basis, str] = {
    "upload": "uploaded",
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
    """是什么：主体、类型、报告期间、披露时间、格式；外加确认状态与词表版本。"""

    subject: str | None = None
    doc_type: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    disclosed_on: date | None = None
    data_format: str | None = None
    status: DescriptionStatus = "unconfirmed"
    vocab_version: int = VOCAB_VERSION

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
        }


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

    @property
    def live(self) -> bool:
        return self.deleted_at is None and self.disabled_at is None

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


@dataclass(frozen=True)
class Rendition:
    """一个读法版本：某个内容版本的一种派生形态（正文、按页文字、表格、检索索引）。"""

    id: str
    version_id: str
    kind: RenditionKind
    rendition_no: int
    processor: str
    processor_version: str
    status: RenditionStatus
    created_at: datetime
    sha256: str | None = None
    object_key: str | None = None
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
