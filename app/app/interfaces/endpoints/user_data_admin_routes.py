"""管理端看用户资料（SDD 0044 第四节）：用户资料的用量、词表、被禁用资料的处理。

通常的管理职能（所有者 10-26）。身份是管理端的会话；看用 knowbase 管理的基本
权限（admin、operator 都有）。禁用、依法清除动的是用户的资料，再要一项
`knowbase:user-data:dispose`（只有 admin 有，和别的应用「交付、退回」一样），
并写明原因、留删除事件（0043 第 2.8 节）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_run_service import UserRunService
from app.domain.security import Principal
from app.domain.user_data import QUOTA_BYTES
from app.domain.vocabulary import VOCAB_VERSION, snapshot
from app.interfaces.endpoints.user_data_errors import call as _call
from app.interfaces.endpoints.user_library_routes import get_user_library_service
from app.interfaces.endpoints.user_run_routes import get_user_run_service
from app.interfaces.http.middleware.auth import (
    require_admin_scopes,
    require_knowbase_admin,
)
from app.interfaces.schemas.user_library import VocabularyRead
from core.config import get_settings

DISPOSE_SCOPE = f"{get_settings().app_slug}:user-data:dispose"

router = APIRouter(prefix="/admin/v1/user-data", tags=["管理-用户资料"])

require_dispose = require_admin_scopes(
    f"{get_settings().app_slug}:admin", DISPOSE_SCOPE
)

Viewer = Annotated[Principal, Depends(require_knowbase_admin)]
Disposer = Annotated[Principal, Depends(require_dispose)]
Library = Annotated[UserLibraryService, Depends(get_user_library_service)]
Runs = Annotated[UserRunService, Depends(get_user_run_service)]


class UsageRowRead(BaseModel):
    subject: str
    bytes_used: int
    materials: int


class UsageRead(BaseModel):
    quota_bytes: int
    users: list[UsageRowRead]


class DescriptionStatRead(BaseModel):
    vocab_version: int | None
    status: str
    doc_type: str | None
    count: int


class AdminVocabularyRead(BaseModel):
    """词表（在代码里，带版本号；改名、删除、改词义才升版本，0049 第二节）
    与描述的分布。"""

    version: int
    vocabulary: VocabularyRead
    descriptions: list[DescriptionStatRead]


class AdminMaterialRead(BaseModel):
    material_id: str
    owner: str
    name: str
    content_origin: str
    license: str
    doc_type: str | None
    description_status: str
    vocab_version: int
    current_version: int
    created_at: datetime
    deleted_at: datetime | None
    disabled_at: datetime | None


class DeletionRead(BaseModel):
    id: str
    material_id: str | None
    action: str
    reason: str
    state: str
    pending: list[str]


class DisposeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=80)


class PurgeIn(DisposeIn):
    # 内容本身是必须删的个人信息：别人还合法用着也删（通知、补偿买家在 knowmart）
    personal_info: bool = False


class PurgeRead(BaseModel):
    # 真删了内容；别人还合法用着又不是必须删的个人信息时只撤所有者的关系
    purged: bool
    result: Literal["purged", "relation_only"]


@router.get("/usage", response_model=UsageRead)
async def usage(
    _: Viewer, service: Library, limit: int = Query(default=100, ge=1, le=500)
):
    rows = await _call(lambda: service.usage_overview(limit))
    return UsageRead(
        quota_bytes=QUOTA_BYTES,
        users=[UsageRowRead(**r) for r in rows],  # type: ignore[arg-type]
    )


@router.get("/vocabulary", response_model=AdminVocabularyRead)
async def vocabulary(_: Viewer, service: Library):
    stats = await _call(lambda: service.description_overview())
    return AdminVocabularyRead(
        version=VOCAB_VERSION,
        vocabulary=VocabularyRead(**snapshot()),  # type: ignore[arg-type]
        descriptions=[DescriptionStatRead(**s) for s in stats],  # type: ignore[arg-type]
    )


@router.get("/materials", response_model=list[AdminMaterialRead])
async def materials(
    viewer: Viewer,
    service: Library,
    owner: str | None = Query(default=None, min_length=1, max_length=255),
    material_id: str | None = Query(default=None, min_length=1, max_length=64),
    disabled: bool = False,
):

    found = await _call(
        lambda: service.find_for_admin(
            owner=owner, material_id=material_id, disabled=disabled
        )
    )
    return [
        AdminMaterialRead(
            material_id=m.id,
            owner=m.owner,
            name=m.name,
            content_origin=m.content_origin,
            license=m.license,
            doc_type=m.description.doc_type,
            description_status=m.description.status,
            vocab_version=m.description.vocab_version,
            current_version=m.current_version,
            created_at=m.created_at,
            deleted_at=m.deleted_at,
            disabled_at=m.disabled_at,
        )
        for m in found
    ]


@router.get("/materials/{material_id}/deletions", response_model=list[DeletionRead])
async def deletions(material_id: str, _: Viewer, runs: Runs):
    events = await _call(lambda: runs.deletions_of_material(material_id))
    return [DeletionRead(**e) for e in events]  # type: ignore[arg-type]


@router.post("/materials/{material_id}/disable", status_code=204)
async def disable(material_id: str, payload: DisposeIn, user: Disposer, runs: Runs):
    """禁用（违规处置）：停一切新读取，在跑的授权也撤；留删除事件。"""
    await _call(lambda: runs.disable(user.subject, material_id, payload.reason))


@router.post("/materials/{material_id}/purge", response_model=PurgeRead)
async def purge(material_id: str, payload: PurgeIn, user: Disposer, runs: Runs):
    """依法清除：没有别人的使用权，或内容是必须删的个人信息时真删。"""
    purged = await _call(
        lambda: runs.purge_material(
            user.subject,
            material_id,
            payload.reason,
            personal_info=payload.personal_info,
        )
    )
    return PurgeRead(purged=purged, result="purged" if purged else "relation_only")
