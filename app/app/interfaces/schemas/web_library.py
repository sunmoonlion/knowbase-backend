"""knowbase web端自家用的模型（SDD 0044）：不是跨应用契约，只给本仓的网页用。

总栏的一行、详情里用的几块，尽量直接用 `user-data/v2` 的模型（同一套服务、同一个样子），
这里只放 web端多要的：展示到哪里、空间用量、识别结果、影响清单、给平台的授权。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.interfaces.schemas.user_library import ShelfEntryRead


class WebEntryRead(ShelfEntryRead):
    # 这份资料展示到哪里：有效的用途（app:knowbench、app:knowlab、market、expert:…）
    shown: list[str]


class UsageRead(BaseModel):
    bytes_used: int
    quota_bytes: int
    max_item_bytes: int
    # 收哪几种文件（扩展名，后端 `kind_of` 的那份）
    accepted: list[str]


class RecognizedDescription(BaseModel):
    subject: str | None
    doc_type: str | None
    doc_class: str | None
    period_start: date | None
    period_end: date | None
    disclosed_on: date | None
    data_format: str | None
    edition: str
    topics: list[str]
    industry: str | None
    language: str | None
    attributes: dict[str, object]


class RecognitionRead(BaseModel):
    """识别出来的描述（待确认）、认出了哪几栏、按哪一版规则、有没有读到首页。"""

    description: RecognizedDescription
    fields: list[str]
    rules_version: int
    read_first_page: bool


class ImpactRead(BaseModel):
    """删、改低许可之前列出来给本人看的。"""

    # 除本人外还有几条使用权（买家、引用）：照旧可读
    other_holders: int
    # 交给着 knowmart：会跟着失效
    market: bool
    # 绑给了哪几位专家（下一版）、冻结在哪几个专家版本里（照旧可读）
    bound_experts: list[str]
    frozen_experts: list[str]
    # 钉住它、还在跑的授权几条
    active_grants: int


class PlatformItemRead(BaseModel):
    material_id: str | None
    version_no: int | None


class PlatformConsentRead(BaseModel):
    """我给平台的一次过程授权（0044 第 2.6 节）。"""

    app: str
    kind: str
    ref: str
    created_at: datetime
    until: datetime | None
    operator_reads: int
    items: list[PlatformItemRead]
