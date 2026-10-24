from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AppName = Literal["knowbench", "knowlab", "knowmart"]


class PinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=64)
    sha256: str = Field(min_length=64, max_length=64)


class ItemRead(BaseModel):
    item_id: str
    app: AppName
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    origin: Literal["upload", "collect", "purchase"]
    origin_ref: str | None
    created_at: datetime


class ConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(min_length=2, max_length=80)
    ref: str = Field(min_length=1, max_length=255)
    # 同意依据可以比一条授权大（knowlab 的测试同意覆盖整个知识库，SDD 0042 第七节）
    items: list[PinIn] = Field(min_length=1, max_length=1000)


class ConsentRead(BaseModel):
    consent_id: str
    app: AppName
    purpose: str
    ref: str
    items: list[PinIn]
    created_at: datetime
    withdrawn_at: datetime | None


class UserGrantCreate(BaseModel):
    """用户本人签：委托、会话、孵化。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["delegation", "session", "incubation"]
    reader_app: AppName
    ref: str = Field(min_length=1, max_length=255)
    items: list[PinIn] = Field(min_length=1, max_length=64)
    ttl_seconds: int | None = Field(default=None, gt=0, le=7 * 24 * 3600)


class TestGrantCreate(BaseModel):
    """knowlab 凭同意依据签测试授权。"""

    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=255)
    consent_id: str = Field(min_length=1, max_length=64)
    reader_app: AppName
    ref: str = Field(min_length=1, max_length=255)
    items: list[PinIn] = Field(min_length=1, max_length=64)
    ttl_seconds: int | None = Field(default=None, gt=0, le=7 * 24 * 3600)


class GrantRead(BaseModel):
    grant_id: str
    kind: Literal["test", "delegation", "session", "incubation"]
    app: AppName
    reader_app: AppName
    ref: str
    scope: Literal["content", "format"]
    items: list[PinIn]
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class GrantedItem(BaseModel):
    item_id: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str


class GrantView(GrantRead):
    """读的一方看到的：钉住的那几份叫什么、什么格式（不含内容）。"""

    granted: list[GrantedItem]


class ItemCheck(BaseModel):
    """核一次的回答：这份还能按这个授权读，版本是这个。"""

    item_id: str
    sha256: str


class FormatRead(BaseModel):
    item_id: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    sheets: list[str]
    header: str | None


class PurchaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seller: str = Field(min_length=1, max_length=255)
    seller_item_id: str = Field(min_length=1, max_length=64)
    buyer: str = Field(min_length=1, max_length=255)
    buyer_app: Literal["knowbench", "knowlab"]
    transaction_ref: str = Field(min_length=1, max_length=255)
