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
    confirm: bool = False


class DescriptionRead(BaseModel):
    subject: str | None
    doc_type: str | None
    period_start: date | None
    period_end: date | None
    disclosed_on: date | None
    data_format: str | None
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


class HoldingRead(BaseModel):
    basis: Literal["upload", "collect", "purchase", "reference", "output"]
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
    what: Literal["description", "license"]
    before: dict
    after: dict


class VocabularyRead(BaseModel):
    version: int
    doc_types: dict[str, str]
    formats: dict[str, str]
    subject_kinds: list[str]


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
