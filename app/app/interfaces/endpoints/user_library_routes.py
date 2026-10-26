"""资料库的内部接口 `user-data/v2`（SDD 0043）：数据第四版，资料不再按应用分区。

和 v1（`user_data_routes.py`）并存：兼容期里三个应用照旧用 v1，切过来以后再收 v1
（SDD 0043 第九节）。身份同 v1 的本人接口：`Authorization: Bearer <换来的用户令牌>`，
`sub` 是用户；`azp` 不再认分区，只记成代办的应用（读数留痕里写这一栏）。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.application.errors.exceptions import AppException
from app.application.services.user_library_service import UserLibraryService
from app.domain.user_data import MAX_ITEM_BYTES, Owner
from app.domain.user_library import Description, ShelfEntry
from app.domain.vocabulary import snapshot
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.security.user_token import require_user_owner
from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.storage.user_objects import S3UserObjects
from app.interfaces.endpoints.user_data_routes import _call
from app.interfaces.http.internal.host_guard import internal_host_only
from app.interfaces.schemas.user_library import (
    DescriptionIn,
    DescriptionRead,
    HistoryEntryRead,
    HoldingRead,
    LicenseIn,
    MaterialRead,
    ShelfEntryRead,
    VersionRead,
    VocabularyRead,
)
from core.config import Settings, get_settings

router = APIRouter(
    prefix="/internal/v2/user-data",
    tags=["内部-资料库"],
    dependencies=[Depends(internal_host_only)],
)


def get_user_library_service(
    settings: Settings = Depends(get_settings),
) -> UserLibraryService:
    return UserLibraryService(
        SqlUserLibraryRepository(lambda: get_postgres().session_factory),
        S3UserObjects(settings),
        prefix=settings.user_data_prefix,
    )


async def _body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ITEM_BYTES:
            raise AppException(
                code="too_large", status_code=413, msg="file is larger than 50 MB"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _description(given: DescriptionIn) -> Description:
    return Description(
        subject=given.subject,
        doc_type=given.doc_type,
        period_start=given.period_start,
        period_end=given.period_end,
        disclosed_on=given.disclosed_on,
        data_format=given.data_format,
    )


def _entry(entry: ShelfEntry, subject: str) -> ShelfEntryRead:
    m, h, v = entry.material, entry.holding, entry.version
    d = m.description
    return ShelfEntryRead(
        category=entry.category,  # type: ignore[arg-type]
        material=MaterialRead(
            material_id=m.id,
            name=m.name,
            mine=m.owner == subject,
            content_origin=m.content_origin,
            license=m.license,
            description=DescriptionRead(
                subject=d.subject,
                doc_type=d.doc_type,
                period_start=d.period_start,
                period_end=d.period_end,
                disclosed_on=d.disclosed_on,
                data_format=d.data_format,
                status=d.status,
                vocab_version=d.vocab_version,
                resolvable=d.resolvable,
            ),
            current_version=m.current_version,
            created_at=m.created_at,
            deleted=m.deleted_at is not None,
            legacy_app=m.legacy_app,
        ),
        holding=HoldingRead(basis=h.basis, license=h.license, version_no=h.version_no),
        version=VersionRead(
            version_no=v.version_no,
            sha256=v.sha256,
            size_bytes=v.size_bytes,
            media_type=v.media_type,
            created_at=v.created_at,
        ),
    )


@router.get("/vocabulary", response_model=VocabularyRead)
async def vocabulary(owner: Owner = Depends(require_user_owner)):
    return VocabularyRead(**snapshot())  # type: ignore[arg-type]


@router.post(
    "/materials", response_model=ShelfEntryRead, status_code=status.HTTP_201_CREATED
)
async def upload_material(
    request: Request,
    name: str = Query(min_length=1, max_length=200),
    subject: str | None = Query(default=None, max_length=64),
    doc_type: str | None = Query(default=None, max_length=32),
    period_start: date | None = None,
    period_end: date | None = None,
    disclosed_on: date | None = None,
    confirm: bool = False,
    license: str = Query(default="self", max_length=16),
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """请求体就是文件本身；描述放在查询参数里，勾了 `confirm` 才算用户确认过。"""
    content = await _body(request)
    description = Description(
        subject=subject,
        doc_type=doc_type,
        period_start=period_start,
        period_end=period_end,
        disclosed_on=disclosed_on,
    )
    material, _ = await _call(
        lambda: service.upload(
            owner.subject,
            name,
            content,
            description=description,
            confirm=confirm,
            license_=license,
        )
    )
    entry = await _call(lambda: service.get(owner.subject, material.id))
    return _entry(entry, owner.subject)


@router.get("/materials", response_model=list[ShelfEntryRead])
async def shelf(
    category: str | None = None,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    entries = await _call(lambda: service.shelf(owner.subject, category))
    return [_entry(e, owner.subject) for e in entries]


@router.get("/materials/{material_id}", response_model=ShelfEntryRead)
async def get_material(
    material_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    return _entry(
        await _call(lambda: service.get(owner.subject, material_id)), owner.subject
    )


@router.post(
    "/materials/{material_id}/versions",
    response_model=ShelfEntryRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_version(
    material_id: str,
    request: Request,
    name: str | None = Query(default=None, max_length=200),
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    content = await _body(request)
    await _call(lambda: service.add_version(owner.subject, material_id, name, content))
    return _entry(
        await _call(lambda: service.get(owner.subject, material_id)), owner.subject
    )


@router.get("/materials/{material_id}/versions", response_model=list[VersionRead])
async def versions(
    material_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    return [
        VersionRead(
            version_no=v.version_no,
            sha256=v.sha256,
            size_bytes=v.size_bytes,
            media_type=v.media_type,
            created_at=v.created_at,
        )
        for v in await _call(lambda: service.versions(owner.subject, material_id))
    ]


@router.get("/materials/{material_id}/versions/{version_no}/content")
async def download(
    material_id: str,
    version_no: int,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    version, content = await _call(
        lambda: service.download(owner.subject, owner.app, material_id, version_no)
    )
    return Response(
        content=content,
        media_type=version.media_type,
        headers={"X-Content-SHA256": version.sha256, "Cache-Control": "no-store"},
    )


@router.put("/materials/{material_id}/description", response_model=ShelfEntryRead)
async def describe(
    material_id: str,
    payload: DescriptionIn,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    await _call(
        lambda: service.describe(
            owner.subject, material_id, _description(payload), confirm=payload.confirm
        )
    )
    return _entry(
        await _call(lambda: service.get(owner.subject, material_id)), owner.subject
    )


@router.put("/materials/{material_id}/license", response_model=ShelfEntryRead)
async def set_license(
    material_id: str,
    payload: LicenseIn,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    await _call(
        lambda: service.set_license(
            owner.subject, material_id, payload.license, attest=payload.attest
        )
    )
    return _entry(
        await _call(lambda: service.get(owner.subject, material_id)), owner.subject
    )


@router.get("/materials/{material_id}/history", response_model=list[HistoryEntryRead])
async def history(
    material_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    return [
        HistoryEntryRead(**h)  # type: ignore[arg-type]
        for h in await _call(lambda: service.history(owner.subject, material_id))
    ]


@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove(
    material_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    await _call(lambda: service.remove(owner.subject, material_id))
