"""资料库接口的模型（契约 `user-data/v2` 的真源，SDD 0043）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["uploaded", "collected", "purchased", "output"]
LicenseName = Literal["self", "via_expert", "sale"]


class DescriptionIn(BaseModel):
    """本人填的描述；整份替换。`confirm` 勾了才算用户确认过（参与解析）。"""

    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, max_length=64)
    doc_type: str | None = Field(default=None, max_length=32)
    period_start: date | None = None
    period_end: date | None = None
    disclosed_on: date | None = None
    data_format: str | None = Field(default=None, max_length=16)
    # 词表第 2 版的几维（SDD 0049）：版本形态进键；事项、行业、语言、研报的几维只筛选
    edition: str = Field(default="full", max_length=16)
    topics: list[str] = Field(default_factory=list, max_length=12)
    industry: str | None = Field(default=None, max_length=16)
    language: str | None = Field(default=None, max_length=8)
    attributes: dict[str, object] = Field(default_factory=dict)
    confirm: bool = False


class DescriptionRead(BaseModel):
    subject: str | None
    doc_type: str | None
    # 细类所属的大类（发布者），由词表推出
    doc_class: str | None = None
    period_start: date | None
    period_end: date | None
    disclosed_on: date | None
    data_format: str | None
    edition: str = "full"
    topics: list[str] = Field(default_factory=list)
    industry: str | None = None
    language: str | None = None
    attributes: dict[str, object] = Field(default_factory=dict)
    status: Literal["unconfirmed", "confirmed", "checked"]
    vocab_version: int
    resolvable: bool


class VersionRead(BaseModel):
    version_no: int
    sha256: str
    size_bytes: int
    media_type: str
    created_at: datetime


class MaterialRead(BaseModel):
    material_id: str
    name: str
    mine: bool
    content_origin: Literal["disclosure", "collected", "upload", "output"]
    license: LicenseName
    description: DescriptionRead
    current_version: int
    created_at: datetime
    deleted: bool
    legacy_app: str | None
    # 出处：产出（哪个应用、任务、运行、输入组合）、转为数据（从哪份产出转来）
    provenance: dict[str, object] | None = None


class HoldingRead(BaseModel):
    basis: Literal["upload", "collect", "purchase", "reference", "output", "convert"]
    license: LicenseName
    version_no: int | None


class ShelfEntryRead(BaseModel):
    category: Category
    material: MaterialRead
    holding: HoldingRead
    version: VersionRead


class LicenseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    license: LicenseName
    attest: bool = False


class HistoryEntryRead(BaseModel):
    at: datetime
    # recognized：上传时的识别（规则版本、认出的值，0049 第六节）
    what: Literal["description", "license", "recognized"]
    before: dict
    after: dict


class VocabularyRead(BaseModel):
    """整份词表（SDD 0049）：类型的大类与细类，各封闭维度的取值清单，开放维度的格式。"""

    version: int
    doc_classes: dict[str, dict[str, str]]
    doc_types: dict[str, dict[str, str]]
    editions: dict[str, str]
    topics: dict[str, str]
    industries: list[str]
    languages: dict[str, str]
    research: dict[str, object]
    formats: dict[str, str]
    subject_kinds: list[str]
    # 定期报告的报告期末与法定披露截止（声明展开「最近 N 期」按它算）
    periodic: dict[str, dict[str, str]] = Field(default_factory=dict)
    # 按描述能采的细类：上市证券的、机构编号的
    collectable: dict[str, list[str]] = Field(default_factory=dict)


# —— 块 1b：展示即同意、账号设置、名下专家、买家使用权 ——


class UseIn(BaseModel):
    """展示给 `app:knowbench` / `app:knowlab`、交给 `market`、绑给 `expert:<专家>`。"""

    model_config = ConfigDict(extra="forbid")

    use: str = Field(min_length=3, max_length=80)


class UseRead(BaseModel):
    use: str
    created_at: datetime
    created_via: str
    version_no: int | None
    material_id: str | None = None


class BulkUseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_ids: list[str] = Field(min_length=1, max_length=200)
    use: str = Field(min_length=3, max_length=80)
    on: bool


class BulkUseRead(BaseModel):
    done: int


class AccountIn(BaseModel):
    """本人第一次进 knowbase web端看过开关、点了一下：开或关都算依据。"""

    model_config = ConfigDict(extra="forbid")

    default_display: bool


class AccountRead(BaseModel):
    default_display: bool
    seen: bool
    displays_by_default: bool


class ExpertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=200)
    latest_version: str | None = Field(default=None, max_length=32)


class ExpertRead(BaseModel):
    expert_id: str
    name: str
    latest_version: str | None


class PurchaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seller: str = Field(min_length=1, max_length=255)
    material_id: str = Field(min_length=1, max_length=64)
    buyer: str = Field(min_length=1, max_length=255)
    transaction_ref: str = Field(min_length=1, max_length=255)
    resale_allowed: bool = False


class PurchaseRead(BaseModel):
    material_id: str
    buyer: str
    version_no: int | None
    license: LicenseName


class PlatformItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_id: str = Field(min_length=1, max_length=64)
    version_no: int | None = Field(default=None, ge=1)


class PlatformConsentIn(BaseModel):
    """本人在 knowlab 选平台、交审、提交交付时确认的范围：这次过程能看哪几份。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["incubate", "test", "review", "rerun"]
    ref: str = Field(min_length=1, max_length=64)
    # 可以空：这次过程不看任何资料也记一条本人同意（撤了就没了）
    items: list[PlatformItemIn] = Field(default_factory=list, max_length=200)


class SubscriptionScopeIn(BaseModel):
    """建订阅、续订时的长期范围同意（SDD 0045 第 2.1 节）。"""

    model_config = ConfigDict(extra="forbid")

    by: Literal["description", "url", "topic"]
    starts_on: date
    ends_on: date
    purpose: str = Field(min_length=1, max_length=200)
    subjects: list[str] = Field(default_factory=list, max_length=50)
    doc_types: list[str] = Field(default_factory=list, max_length=11)
    urls: list[str] = Field(default_factory=list, max_length=20)
    topic: str | None = Field(default=None, max_length=200)


class SubscriptionRead(BaseModel):
    subscription_id: str
    revision: int
    by: str
    starts_on: date
    ends_on: date
    purpose: str
    subjects: list[str]
    doc_types: list[str]
    urls: list[str]
    topic: str | None
    pick_each_round: bool
    created_at: datetime


class FreezeIn(BaseModel):
    """交付时冻结谁的绑定（专家的作者）。"""

    model_config = ConfigDict(extra="forbid")

    author: str = Field(min_length=1, max_length=255)
    # 只回「现在冻结会冻成什么」、不写（提交前预查许可）
    preview: bool = False


class FrozenLicenseIn(BaseModel):
    """自用转公开时把冻结的那一份许可往高改：作者是谁、改到哪一档、本人勾了「我有权」。"""

    model_config = ConfigDict(extra="forbid")

    author: str = Field(min_length=1, max_length=255)
    license: LicenseName
    attest: bool


class FrozenVoidRead(BaseModel):
    withdrawn: int


class FrozenBindingRead(BaseModel):
    """冻结下来的一份：资料、钉在哪一版与它的摘要、凭的
    `expert:<专家>@<版本>`、交付那一刻的许可（knowlab 照它写进清单 v2 的
    `bindings`）。"""

    material_id: str
    version_no: int
    sha256: str
    basis_ref: str
    license: LicenseName


class BeneficiaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=255)
    item_ref: str = Field(min_length=1, max_length=160)
    prefill: dict[str, Any] | None = None
    basis: dict[str, Any] | None = None


class CollectedOriginalRead(BaseModel):
    subject: str
    item_ref: str
    material_id: str
    version_no: int
    created: bool
    has_readings: bool = False


class PublicOriginalRead(BaseModel):
    """knowfeed 交来的公开原件存下的那一份：资料、交来的那一版。"""

    material_id: str
    version_no: int
    sha256: str
    created: bool


class ReferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=255)
    ref: str = Field(min_length=1, max_length=255)


class ReferencesIn(BaseModel):
    """给提了需求的用户记引用：谁、凭哪条需求依据（0045 第 2.3 节）。"""

    model_config = ConfigDict(extra="forbid")

    version_no: int = Field(ge=1)
    references: list[ReferenceIn] = Field(min_length=1, max_length=500)


class ReferencesRead(BaseModel):
    recorded: int


# —— knowmart 成批核条目里的资料（后台定时下架，「待你决定」第 7 条） ——


class MarketOfferItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seller: str = Field(min_length=1, max_length=255)
    material_id: str = Field(min_length=1, max_length=64)


class MarketOffersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MarketOfferItemIn] = Field(min_length=1, max_length=200)


class MarketOfferStateRead(BaseModel):
    seller: str
    material_id: str
    # offered：还交给着 knowmart、是卖家的、可以出售；not_for_sale：许可改低了；
    # gone：没有、不是卖家的、删了、不再交给 knowmart
    state: Literal["offered", "not_for_sale", "gone"]


class MarketOffersRead(BaseModel):
    items: list[MarketOfferStateRead]


class BindingCheckItemIn(BaseModel):
    """专家绑定的一份：作者（绑的那个人）、资料、钉的那一版。"""

    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=255)
    material_id: str = Field(min_length=1, max_length=64)
    version_no: int = Field(ge=1)
    # 冻结了的带上凭的 `expert:<专家>@<版本>`：再回冻结时的许可
    basis_ref: str | None = Field(
        default=None, pattern=r"^expert:[A-Za-z0-9._-]{1,64}@[0-9][0-9.]{0,31}$"
    )


class BindingChecksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BindingCheckItemIn] = Field(min_length=1, max_length=200)


class BindingStateRead(BaseModel):
    owner: str
    material_id: str
    version_no: int
    # live：在、作者还有这一版的使用权；deleted：作者（或卖家）删了；
    # disabled：平台禁用；gone：没有、不是作者能用的、那一版不在
    state: Literal["live", "deleted", "disabled", "gone"]
    # 作者现在对这一份的许可（买来的是成交时定的那一档）；gone 的不说
    license: LicenseName | None
    # 冻结时记下的许可（问的时候带了 basis_ref 才有）
    frozen_license: LicenseName | None = None
    # 能不能经专家给别人用：在、现在的许可（带了 basis_ref 的还有冻结的那一档）都在
    # 「可以经专家给别人用」以上（0043 第 4.3 节）
    shareable: bool


class BindingChecksRead(BaseModel):
    items: list[BindingStateRead]
