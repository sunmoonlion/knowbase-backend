"""用户资料的内部接口（SDD 0042）：只给三个应用的后端调，前端不直接连。

两种身份，路径分开：
- 用户本人：`Authorization: Bearer <换来的用户令牌>`（`sub` 是用户，`azp` 认应用分区）；
- 服务：`Authorization: Bearer <服务身份令牌>`，权限照跨应用的约定写成「目标:关系」：
  `knowbase:user-data-grant-test`（knowlab 签测试授权）、`knowbase:user-data-read`
  （凭授权读；以调用方那个应用的名义读）、`knowbase:user-data-purchase`（knowmart
  成交复制）、`knowbase:user-data-import`（应用把自己原来存的资料搬进自己的分区，
  只能导进调用方自己那个应用的分区；knowlab 知识库搬家用）。按关系校验，调用方是谁
  由校验认出来（infrastructure/security/relation_auth.py）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.application.errors.exceptions import AppException
from app.application.services.user_data_service import UserDataService
from app.domain.user_data import (
    APPS,
    MAX_ITEM_BYTES,
    App,
    Consent,
    Grant,
    Item,
    Owner,
    Pin,
    UserDataError,
)
from app.infrastructure.repositories.user_data import SqlUserDataRepository
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_grant_test_service,
    require_import_service,
    require_purchase_service,
    require_read_service,
)
from app.infrastructure.security.user_token import require_user_owner
from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.storage.user_objects import S3UserObjects
from app.interfaces.http.internal.host_guard import internal_host_only
from app.interfaces.schemas.user_data import (
    ConsentCreate,
    ConsentRead,
    FormatRead,
    GrantedItem,
    GrantRead,
    GrantView,
    ItemCheck,
    ItemRead,
    PinIn,
    PurchaseCreate,
    TestGrantCreate,
    UserGrantCreate,
)
from core.config import Settings, get_settings

router = APIRouter(
    prefix="/internal/v1/user-data",
    tags=["内部-用户资料"],
    dependencies=[Depends(internal_host_only)],
)

# 四种关系各自要的权限（relation_auth 按 knowbase:<关系> 核）
GRANT_TEST = "knowbase:user-data-grant-test"
PURCHASE = "knowbase:user-data-purchase"
READ = "knowbase:user-data-read"
IMPORT = "knowbase:user-data-import"


def get_user_data_service(
    settings: Settings = Depends(get_settings),
) -> UserDataService:
    return UserDataService(
        SqlUserDataRepository(lambda: get_postgres().session_factory),
        S3UserObjects(settings),
        prefix=settings.user_data_prefix,
    )


async def _call(work: Callable[[], Any]) -> Any:
    try:
        return await work()
    except UserDataError as exc:
        raise AppException(
            code=exc.code, status_code=exc.status, msg=exc.message
        ) from None


def _pins(items: list[PinIn]) -> tuple[Pin, ...]:
    return tuple(Pin(p.item_id, p.sha256) for p in items)


def _item(item: Item) -> ItemRead:
    return ItemRead(
        item_id=item.id,
        app=item.owner.app,
        name=item.name,
        media_type=item.media_type,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        origin=item.origin,
        origin_ref=item.origin_ref,
        created_at=item.created_at,
        no_resale=item.no_resale,
    )


def _consent(consent: Consent) -> ConsentRead:
    return ConsentRead(
        consent_id=consent.id,
        app=consent.owner.app,
        purpose=consent.purpose,
        ref=consent.ref,
        items=[PinIn(item_id=p.item_id, sha256=p.sha256) for p in consent.pins],
        created_at=consent.created_at,
        withdrawn_at=consent.withdrawn_at,
    )


def _grant(grant: Grant) -> dict[str, Any]:
    return {
        "grant_id": grant.id,
        "kind": grant.kind,
        "app": grant.owner.app,
        "reader_app": grant.reader_app,
        "ref": grant.ref,
        "scope": grant.scope,
        "items": [PinIn(item_id=p.item_id, sha256=p.sha256) for p in grant.pins],
        "issued_at": grant.issued_at,
        "expires_at": grant.expires_at,
        "revoked_at": grant.revoked_at,
    }


def _content(item: Item, content: bytes) -> Response:
    return Response(
        content=content,
        media_type=item.media_type,
        headers={"X-Content-SHA256": item.sha256, "Cache-Control": "no-store"},
    )


def app_of(caller: ServiceCaller) -> App:
    """服务以哪个应用的名义读、导入：就是校验认出的调用方，要是三个应用之一。"""
    if caller.caller not in APPS:
        raise AppException(
            code="service_scope_denied",
            status_code=403,
            msg="The service caller is not an app with a user data partition",
        )
    return caller.caller  # type: ignore[return-value]


# 服务身份的四种依赖（require_*_service）从 relation_auth 引进模块一级：测试里按对象替换


async def require_reader(
    caller: ServiceCaller = Depends(require_read_service),
) -> tuple[str, App]:
    """读的一方：（服务身份 sub，以哪个应用的名义）。"""
    return caller.principal.subject, app_of(caller)


# —— 用户本人 ——


@router.post("/items", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
async def upload_item(
    request: Request,
    name: str = Query(min_length=1, max_length=200),
    owner: Owner = Depends(require_user_owner),
    service: UserDataService = Depends(get_user_data_service),
):
    """请求体就是文件本身；超过 50 MB 的不收完就拒。"""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ITEM_BYTES:
            raise AppException(
                code="too_large", status_code=413, msg="file is larger than 50 MB"
            )
        chunks.append(chunk)
    item = await _call(lambda: service.upload(owner, name, b"".join(chunks)))
    return _item(item)


@router.get("/items", response_model=list[ItemRead])
async def list_items(
    owner: Owner = Depends(require_user_owner),
    service: UserDataService = Depends(get_user_data_service),
):
    return [_item(i) for i in await _call(lambda: service.items_of(owner))]


@router.get("/items/{item_id}", response_model=ItemRead)
async def get_item(
    item_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserDataService = Depends(get_user_data_service),
):
    return _item(await _call(lambda: service.get(owner, item_id)))


@router.get("/items/{item_id}/content")
async def download_item(
    item_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserDataService = Depends(get_user_data_service),
):
    item, content = await _call(lambda: service.download(owner, item_id))
    return _content(item, content)


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserDataService = Depends(get_user_data_service),
):
    await _call(lambda: service.delete(owner, item_id))


@router.post(
    "/consents", response_model=ConsentRead, status_code=status.HTTP_201_CREATED
)
async def create_consent(
    payload: ConsentCreate,
    owner: Owner = Depends(require_user_owner),
    service: UserDataService = Depends(get_user_data_service),
):
    consent = await _call(
        lambda: service.consent(
            owner, payload.purpose, payload.ref, _pins(payload.items)
        )
    )
    return _consent(consent)


@router.delete("/consents/{consent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_consent(
    consent_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserDataService = Depends(get_user_data_service),
):
    await _call(lambda: service.withdraw_consent(owner, consent_id))


@router.post("/grants", response_model=GrantRead, status_code=status.HTTP_201_CREATED)
async def create_user_grant(
    payload: UserGrantCreate,
    owner: Owner = Depends(require_user_owner),
    service: UserDataService = Depends(get_user_data_service),
):
    grant = await _call(
        lambda: service.grant_by_user(
            owner,
            payload.kind,
            payload.reader_app,
            payload.ref,
            _pins(payload.items),
            payload.ttl_seconds,
        )
    )
    return GrantRead(**_grant(grant))


@router.post("/grants/{grant_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_user_grant(
    grant_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserDataService = Depends(get_user_data_service),
):
    await _call(lambda: service.revoke_by_user(owner, grant_id))


# —— knowlab：测试授权 ——


@router.post(
    "/test-grants", response_model=GrantRead, status_code=status.HTTP_201_CREATED
)
async def create_test_grant(
    payload: TestGrantCreate,
    caller: ServiceCaller = Depends(require_grant_test_service),
    service: UserDataService = Depends(get_user_data_service),
):
    grant = await _call(
        lambda: service.grant_test(
            caller.principal.subject,
            payload.owner,
            payload.consent_id,
            payload.reader_app,
            payload.ref,
            _pins(payload.items),
            payload.ttl_seconds,
        )
    )
    return GrantRead(**_grant(grant))


@router.post("/test-grants/{grant_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_test_grant(
    grant_id: str,
    caller: ServiceCaller = Depends(require_grant_test_service),
    service: UserDataService = Depends(get_user_data_service),
):
    await _call(lambda: service.revoke_by_service(caller.principal.subject, grant_id))


# —— 应用搬自己的资料 ——


@router.post("/imports", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
async def import_item(
    request: Request,
    owner: str = Query(min_length=1, max_length=255),
    name: str = Query(min_length=1, max_length=200),
    ref: str = Query(min_length=1, max_length=255),
    caller: ServiceCaller = Depends(require_import_service),
    service: UserDataService = Depends(get_user_data_service),
):
    """请求体就是文件本身。

    导进调用方自己那个应用的分区（校验认出的调用方），按 ref 幂等。
    """
    app = app_of(caller)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ITEM_BYTES:
            raise AppException(
                code="too_large", status_code=413, msg="file is larger than 50 MB"
            )
        chunks.append(chunk)
    item = await _call(
        lambda: service.import_item(
            Owner(owner, app),
            name,
            b"".join(chunks),
            ref,
        )
    )
    return _item(item)


# —— 读的一方：凭授权读 ——


@router.get("/grants/{grant_id}", response_model=GrantView)
async def view_grant(
    grant_id: str,
    reader: tuple[str, App] = Depends(require_reader),
    service: UserDataService = Depends(get_user_data_service),
):
    grant = await _call(lambda: service.grant_view(reader[1], grant_id))
    items = await service.repo.items([p.item_id for p in grant.pins])
    granted = [
        GrantedItem(
            item_id=i.id,
            name=i.name,
            media_type=i.media_type,
            size_bytes=i.size_bytes,
            sha256=i.sha256,
        )
        for p in grant.pins
        if (i := items.get(p.item_id)) is not None and i.deleted_at is None
    ]
    return GrantView(**_grant(grant), granted=granted)


@router.post("/grants/{grant_id}/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_grant(
    grant_id: str,
    reader: tuple[str, App] = Depends(require_reader),
    service: UserDataService = Depends(get_user_data_service),
):
    """读的一方交还授权：委托、会话结束就撤，不等时限（SDD 0042 第四节）。"""
    await _call(lambda: service.release(reader[1], grant_id))


@router.get("/grants/{grant_id}/items/{item_id}/content")
async def read_granted_content(
    grant_id: str,
    item_id: str,
    reader: tuple[str, App] = Depends(require_reader),
    service: UserDataService = Depends(get_user_data_service),
):
    subject, app = reader
    item, content = await _call(
        lambda: service.read(app, subject, grant_id, item_id, "content")
    )
    return _content(item, content)  # type: ignore[arg-type]


@router.get("/grants/{grant_id}/items/{item_id}/check", response_model=ItemCheck)
async def check_granted(
    grant_id: str,
    item_id: str,
    reader: tuple[str, App] = Depends(require_reader),
    service: UserDataService = Depends(get_user_data_service),
):
    """读的一方用缓存的内容前核一次：授权还算不算数、版本对不对（不给内容，留读数记录）。"""
    subject, app = reader
    item = await _call(lambda: service.check(app, subject, grant_id, item_id))
    return ItemCheck(item_id=item.id, sha256=item.sha256)


@router.get("/grants/{grant_id}/items/{item_id}/format", response_model=FormatRead)
async def read_granted_format(
    grant_id: str,
    item_id: str,
    reader: tuple[str, App] = Depends(require_reader),
    service: UserDataService = Depends(get_user_data_service),
):
    subject, app = reader
    item, view = await _call(
        lambda: service.read(app, subject, grant_id, item_id, "format")
    )
    return FormatRead(
        item_id=item.id,
        name=item.name,
        media_type=item.media_type,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        sheets=list(view.sheets),
        header=view.header,
    )


# —— knowmart：成交复制 ——


@router.post("/purchases", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
async def purchase_copy(
    payload: PurchaseCreate,
    caller: ServiceCaller = Depends(require_purchase_service),
    service: UserDataService = Depends(get_user_data_service),
):
    item = await _call(
        lambda: service.purchase(
            caller.principal.subject,
            payload.seller,
            payload.seller_item_id,
            payload.buyer,
            payload.buyer_app,
            payload.transaction_ref,
            payload.no_resale,
        )
    )
    return _item(item)
