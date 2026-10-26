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
