"""按描述查、解析、确认、偏好的模型（契约 `user-data/v2` 的一部分，SDD 0043 第三节）。

声明的模型在 `data_needs.py`（契约 `knowbot-data-needs/v2`）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.interfaces.schemas.data_needs import DataNeed, PeriodRule

Status = Literal[
    "resolved",
    "not_shown",
    "to_confirm",
    "not_collectable",
    "ambiguous",
    "waiting",
    "unusable",
]


# 声明的模型只有一份：契约 `knowbot-data-needs/v2`
PeriodRuleIn = PeriodRule
NeedSpecIn = DataNeed


class TaskParamsIn(BaseModel):
    """请专家的人发起任务时给的：主体、任务日期，必要时覆盖期间。"""

    model_config = ConfigDict(extra="forbid")

    subjects: list[str] = Field(min_length=1, max_length=10)
    task_date: date
    period_start: date | None = None
    period_end: date | None = None


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer: str = Field(min_length=1, max_length=160)
    contract_version: Literal["knowbot-manifest/v2"]
    needs: list[NeedSpecIn] = Field(min_length=1, max_length=20)
    params: TaskParamsIn
    task_ref: str | None = Field(default=None, max_length=255)


class AddNeedsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needs: list[NeedSpecIn] = Field(min_length=1, max_length=20)


class DescriptionKeyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=64)
    doc_type: str = Field(min_length=1, max_length=32)
    period_start: date | None = None
    period_end: date | None = None


class CandidateRead(BaseModel):
    material_id: str
    # 开跑时凭什么签：display、reference、public
    basis: str = "display"
    version_no: int
    sha256: str
    content_origin: str
    disclosed_on: date | None
    data_format: str | None
    rendition_kind: str | None
    rendition_no: int | None
    rendition_sha256: str | None


class ResultItemRead(BaseModel):
    need_key: str
    key: DescriptionKeyModel
    status: Status
    chosen: list[CandidateRead]
    candidates: list[CandidateRead]
    reason: str | None


class ResolutionRead(BaseModel):
    resolution_id: str
    revision: int
    consumer: str
    contract_version: str
    vocab_version: int
    task_ref: str | None
    items: list[ResultItemRead]
    created_at: datetime


class LookupRead(BaseModel):
    """只答这一份在不在、在哪个范围：shown 放开给这个应用了，held 在、没放开，
    unusable 在、不能用，absent 他名下没有。"""

    status: Literal["shown", "held", "unusable", "absent"]
    zone: Literal["own", "public"] | None


class ConfirmItemIn(BaseModel):
    """按描述的给 key（有清单的再给 need_key）；给网址的给 url；按主题勾的给
    candidate_ref 与 url。"""

    model_config = ConfigDict(extra="forbid")

    key: DescriptionKeyModel | None = None
    need_key: str | None = Field(default=None, max_length=40)
    reading: Literal["original", "text", "pages", "table"] | None = None
    required: bool | None = None
    url: str | None = Field(default=None, max_length=2000)
    candidate_ref: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=200)


class ConfirmIn(BaseModel):
    """用户点一次。`supply` 是预切合结果与可接受的来源限制、用途条件（照存）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["description", "url", "topic"]
    items: list[ConfirmItemIn] = Field(min_length=1, max_length=200)
    supply: dict[str, object] = Field(default_factory=dict)
    task_ref: str | None = Field(default=None, max_length=255)
    resolution_id: str | None = Field(default=None, max_length=64)
    revision: int | None = Field(default=None, ge=1)


class BranchRead(BaseModel):
    """入口分支：ready 已经齐了（照用），supply 要交 knowfeed（带确认编号）。"""

    item_id: str
    branch: Literal["ready", "supply"]


class ConfirmationRead(BaseModel):
    confirmation_id: str
    kind: str
    expires_at: datetime
    items: list[dict[str, object]]
    branches: list[BranchRead]


class ConfirmationCheckRead(BaseModel):
    """knowfeed 受理前看到的：是谁、哪个应用、哪些条目、当时的供给条件。"""

    confirmation_id: str
    subject: str
    app: str
    kind: str
    task_ref: str | None
    resolution_id: str | None
    resolution_revision: int | None
    needs: list[dict[str, object]]
    items: list[dict[str, object]]
    supply: dict[str, object]
    expires_at: datetime


class PreferenceRead(BaseModel):
    need_key: str
    material_ids: list[str]
    updated_at: datetime
