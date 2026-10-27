"""运行接口的模型（契约 `user-data/v2` 的一部分，SDD 0043 块 1c）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RenditionKindName = Literal["text", "pages", "table", "index", "parse"]
GrantBasisName = Literal[
    "display", "frozen_binding", "platform", "processing", "reference", "public"
]


class GrantItemIn(BaseModel):
    """申请的一项：钉哪一版（与读法）、凭什么依据。

    依据：`display` + `app:<应用>`（展示给执行的应用）、`frozen_binding` +
    `expert:<专家>@<版本>`（作者交付时冻结的绑定）、`platform` +
    `platform:<应用>:<过程>:<编号>`（平台过程）。
    """

    model_config = ConfigDict(extra="forbid")

    material_id: str = Field(min_length=1, max_length=64)
    version_no: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendition_kind: RenditionKindName | None = None
    rendition_no: int | None = Field(default=None, ge=1)
    rendition_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    basis: GrantBasisName
    basis_ref: str = Field(min_length=1, max_length=160)


class RunGrantIn(BaseModel):
    """本人签：委托、会话（knowbench），孵化（knowlab）。

    同一个 ref 再签是运行中追加。
    """

    model_config = ConfigDict(extra="forbid")

    process: Literal["delegation", "session", "incubation"]
    ref: str = Field(min_length=1, max_length=255)
    items: list[GrantItemIn] = Field(min_length=1, max_length=200)
    ttl_seconds: int | None = Field(default=None, gt=0)


class ConfirmedRunGrantIn(BaseModel):
    """执行的应用凭本人的确认续签（等数据的委托数据齐了）：签进去的只能是那一修订
    清单里的候选，或冻结给这位专家这一版的绑定。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=255)
    confirmation_id: str = Field(min_length=1, max_length=64)
    resolution_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    ref: str = Field(min_length=1, max_length=255)
    items: list[GrantItemIn] = Field(min_length=1, max_length=200)
    ttl_seconds: int | None = Field(default=None, gt=0)


class ServiceRunGrantIn(BaseModel):
    """knowlab 签：测试（凭展示依据）、平台过程（凭 `platform:` 依据，运营或机器）。"""

    model_config = ConfigDict(extra="forbid")

    process: Literal["test", "platform"]
    beneficiary: str = Field(min_length=1, max_length=255)
    ref: str = Field(min_length=1, max_length=255)
    items: list[GrantItemIn] = Field(min_length=1, max_length=200)
    reader_role: Literal["app", "operator", "machine"] = "app"
    ttl_seconds: int | None = Field(default=None, gt=0)


class PinRead(BaseModel):
    material_id: str
    version_no: int
    sha256: str
    rendition_kind: str | None
    rendition_no: int | None
    rendition_sha256: str | None


class GrantItemRead(BaseModel):
    material_id: str
    version_no: int
    basis: str
    basis_ref: str
    content_owner: str


class RunGrantRead(BaseModel):
    grant_id: str
    process: str
    executor_app: str
    beneficiary: str
    ref: str
    revision: int
    reader_role: str
    scope: Literal["content", "format"]
    input_set_id: str
    items: list[GrantItemRead]
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class RunGrantView(RunGrantRead):
    """执行的应用看到的：外加钉住的组合（写清单 v2 用）。"""

    pins: list[PinRead]


class SessionRead(BaseModel):
    """本人登记的一段会话；模型在会话里首读一份时 knowbase 凭它签小授权。"""

    session_id: str
    expires_at: datetime


class PlatformEndIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=255)
    reason: Literal["finished", "rejected", "cancelled"]


class ConvertIn(BaseModel):
    """把一份产出转为数据；许可默认只许本人用，不得高于上游，改高要勾「我有权」。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    license: Literal["self", "via_expert", "sale"] = "self"
    attest: bool = False


class ReadEventRead(BaseModel):
    at: datetime
    reader: str
    operation: str
    app: str | None
    grant_id: str | None
    sha256: str
    beneficiary: str | None
    reader_role: str | None


class RenditionRead(BaseModel):
    kind: str
    rendition_no: int
    processor: str
    processor_version: str
    status: Literal["pending", "ready", "failed"]
    sha256: str | None
    detail: dict[str, object]
    created_at: datetime
    ready_at: datetime | None


class GrantedFormatRead(BaseModel):
    material_id: str
    version_no: int
    media_type: str
    size_bytes: int
    sha256: str
    sheets: list[str]
    header: str | None


class OutputWritten(BaseModel):
    material_id: str
    version_no: int
    sha256: str


class PendingProcessingRead(BaseModel):
    owner: str
    material_id: str
    version_no: int


class ProcessingGrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=255)
    material_id: str = Field(min_length=1, max_length=64)
    version_no: int = Field(ge=1)
