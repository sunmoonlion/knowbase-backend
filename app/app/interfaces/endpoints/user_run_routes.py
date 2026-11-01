"""运行的内部接口 `user-data/v2`（SDD 0043 块 1c）：运行授权、凭授权读、加工、产出区。

身份：
- 本人：`Authorization: Bearer <换来的用户令牌>`，`sub` 是用户，`azp` 是代办的应用；
- 服务：按关系校验的服务令牌（`relation_auth`），调用方就是那个应用。

禁用与清除只有服务方法（`UserRunService.disable`、`purge_*`），管理端入口随管理端做。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.application.errors.exceptions import AppException
from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_resolution_service import UserResolutionService
from app.application.services.user_run_service import UserRunService
from app.domain.user_data import Owner
from app.domain.user_library import Rendition
from app.domain.user_runs import GrantRequestItem, InputPin, InputSet, RunGrant
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.repositories.user_runs import SqlUserRunRepository
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_directory_service,
    require_grant_test_service,
    require_output_service,
    require_read_service,
    require_supply_service,
)
from app.infrastructure.security.user_token import require_user_owner
from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.storage.user_objects import S3UserObjects
from app.interfaces.endpoints.public_access import public_gate
from app.interfaces.endpoints.user_data_errors import call as _call
from app.interfaces.endpoints.user_library_routes import (
    _body,
    _entry,
    get_user_library_service,
)
from app.interfaces.endpoints.user_resolution_routes import (
    get_user_resolution_service,
)
from app.interfaces.http.internal.host_guard import internal_host_only
from app.interfaces.schemas.user_library import FreezeIn, FrozenVoidRead, ShelfEntryRead
from app.interfaces.schemas.user_runs import (
    ConfirmedRunGrantIn,
    ConvertIn,
    GrantedFormatRead,
    GrantItemIn,
    GrantItemRead,
    OutputWritten,
    PendingProcessingRead,
    PinRead,
    PlatformEndIn,
    PlatformProcessRead,
    ProcessingGrantIn,
    ReadEventRead,
    RenditionRead,
    RunGrantIn,
    RunGrantRead,
    RunGrantView,
    ServiceRunGrantIn,
    SessionRead,
)
from core.config import Settings, get_settings

router = APIRouter(
    prefix="/internal/v2/user-data",
    tags=["内部-资料库-运行"],
    dependencies=[Depends(internal_host_only)],
)

# 凭授权读的执行方：两个干活的应用，加 knowfeed（加工）
READERS = ("knowbench", "knowlab", "knowfeed")


def get_user_run_service(
    settings: Settings = Depends(get_settings),
) -> UserRunService:
    return UserRunService(
        SqlUserLibraryRepository(lambda: get_postgres().session_factory),
        SqlUserRunRepository(lambda: get_postgres().session_factory),
        S3UserObjects(settings),
        prefix=settings.user_data_prefix,
        gate=public_gate(settings),
    )


async def require_executor(
    caller: ServiceCaller = Depends(require_read_service),
) -> tuple[str, str]:
    """凭授权读的一方：（服务身份 sub，以哪个应用的名义）。"""
    if caller.caller not in READERS:
        raise AppException(
            code="service_scope_denied",
            status_code=403,
            msg="this caller does not read by grant",
        )
    return caller.principal.subject, caller.caller


def _items(items: list[GrantItemIn]) -> list[GrantRequestItem]:
    return [
        GrantRequestItem(
            pin=InputPin(
                material_id=i.material_id,
                version_no=i.version_no,
                sha256=i.sha256,
                rendition_kind=i.rendition_kind,
                rendition_no=i.rendition_no,
                rendition_sha256=i.rendition_sha256,
            ),
            basis=i.basis,
            basis_ref=i.basis_ref,
        )
        for i in items
    ]


def _grant(grant: RunGrant) -> RunGrantRead:
    return RunGrantRead(
        grant_id=grant.id,
        process=grant.process,
        executor_app=grant.executor_app,
        beneficiary=grant.beneficiary,
        ref=grant.ref,
        revision=grant.revision,
        reader_role=grant.reader_role,
        scope=grant.scope,
        input_set_id=grant.input_set_id,
        items=[
            GrantItemRead(
                material_id=i.material_id,
                version_no=i.version_no,
                basis=i.basis,
                basis_ref=i.basis_ref,
                content_owner=i.content_owner,
            )
            for i in grant.items
        ],
        issued_at=grant.issued_at,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
    )


def _view(grant: RunGrant, input_set: InputSet) -> RunGrantView:
    return RunGrantView(
        **_grant(grant).model_dump(),
        pins=[PinRead(**p.as_dict()) for p in input_set.pins],  # type: ignore[arg-type]
    )


def _rendition(r: Rendition) -> RenditionRead:
    return RenditionRead(
        kind=r.kind,
        rendition_no=r.rendition_no,
        processor=r.processor,
        processor_version=r.processor_version,
        status=r.status,
        sha256=r.sha256,
        detail=r.detail,
        created_at=r.created_at,
        ready_at=r.ready_at,
    )


def _knowlab(caller: ServiceCaller) -> str:
    if caller.caller != "knowlab":
        raise AppException(
            code="service_scope_denied",
            status_code=403,
            msg="only knowlab signs run grants as a service",
        )
    return caller.caller


# —— 本人 ——


@router.post(
    "/run-grants", response_model=RunGrantRead, status_code=status.HTTP_201_CREATED
)
async def grant_by_user(
    payload: RunGrantIn,
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
):
    """本人签委托、会话（knowbench）、孵化（knowlab）；开跑时签，与钉住同时完成。"""
    grant = await _call(
        lambda: service.grant_by_user(
            owner.subject,
            owner.app,
            payload.process,
            payload.ref,
            _items(payload.items),
            payload.ttl_seconds,
        )
    )
    return _grant(grant)


@router.post(
    "/sessions", response_model=SessionRead, status_code=status.HTTP_201_CREATED
)
async def register_session(
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
):
    """本人开一段会话（24 小时）；模型首读一份时由 knowbase 当场签小授权。"""
    session = await _call(lambda: service.register_session(owner.subject, owner.app))
    return SessionRead(session_id=session.id, expires_at=session.expires_at)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(
    session_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
):
    """会话结束：小授权一并撤。"""
    await _call(lambda: service.end_session(owner.subject, owner.app, session_id))


@router.delete("/run-grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_by_user(
    grant_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
):
    await _call(lambda: service.revoke_by_user(owner.subject, grant_id))


@router.delete(
    "/platform-consents/{kind}/{ref}", status_code=status.HTTP_204_NO_CONTENT
)
async def withdraw_platform(
    kind: str,
    ref: str,
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
):
    """本人撤回平台过程的依据：依据与凭它签的授权立即撤。"""
    await _call(lambda: service.withdraw_platform(owner.subject, owner.app, kind, ref))


@router.post(
    "/materials/{material_id}/convert",
    response_model=ShelfEntryRead,
    status_code=status.HTTP_201_CREATED,
)
async def convert(
    material_id: str,
    payload: ConvertIn,
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
    library: UserLibraryService = Depends(get_user_library_service),
):
    """把一份产出转为数据：新建一份资料，带出处，许可不升档。"""
    material, _ = await _call(
        lambda: service.convert(
            owner.subject,
            material_id,
            name=payload.name,
            license_=payload.license,
            attest=payload.attest,
        )
    )
    entry = await _call(lambda: library.get(owner.subject, material.id))
    return _entry(entry, owner.subject)


@router.get("/materials/{material_id}/reads", response_model=list[ReadEventRead])
async def reads(
    material_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
):
    """谁读过：只给所有者看；平台运营、系统加工读的带 `reader_role`。"""
    events = await _call(lambda: service.reads(owner.subject, material_id))
    return [ReadEventRead(**e) for e in events]  # type: ignore[arg-type]


@router.get(
    "/materials/{material_id}/versions/{version_no}/renditions",
    response_model=list[RenditionRead],
)
async def renditions(
    material_id: str,
    version_no: int,
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
):
    found = await _call(
        lambda: service.renditions(owner.subject, material_id, version_no)
    )
    return [_rendition(r) for r in found]


# —— knowlab：测试、平台过程 ——


@router.post(
    "/service/run-grants",
    response_model=RunGrantRead,
    status_code=status.HTTP_201_CREATED,
)
async def grant_by_service(
    payload: ServiceRunGrantIn,
    caller: ServiceCaller = Depends(require_grant_test_service),
    service: UserRunService = Depends(get_user_run_service),
):
    issuer = _knowlab(caller)
    grant = await _call(
        lambda: service.grant_by_service(
            issuer,
            payload.process,
            payload.beneficiary,
            payload.ref,
            _items(payload.items),
            reader_role=payload.reader_role,
            ttl_seconds=payload.ttl_seconds,
            platform=payload.platform_use,
        )
    )
    return _grant(grant)


@router.delete("/service/run-grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_by_service(
    grant_id: str,
    caller: ServiceCaller = Depends(require_grant_test_service),
    service: UserRunService = Depends(get_user_run_service),
):
    issuer = _knowlab(caller)
    await _call(lambda: service.revoke_by_service(issuer, grant_id))


@router.post(
    "/service/platform-processes/{kind}/{ref}/end",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def end_platform(
    kind: str,
    ref: str,
    payload: PlatformEndIn,
    caller: ServiceCaller = Depends(require_grant_test_service),
    service: UserRunService = Depends(get_user_run_service),
):
    """过程结束、不受理、取消：依据与授权立即撤。"""
    issuer = _knowlab(caller)
    await _call(
        lambda: service.end_platform(issuer, payload.subject, kind, ref, payload.reason)
    )


@router.get(
    "/service/platform-processes/{kind}/{ref}", response_model=PlatformProcessRead
)
async def platform_process(
    kind: str,
    ref: str,
    subject: str = Query(min_length=1, max_length=255),
    caller: ServiceCaller = Depends(require_grant_test_service),
    service: UserRunService = Depends(get_user_run_service),
):
    """这次平台过程的依据还有几份有效（只读）：knowlab 后台核在审的交付，作者在总栏
    撤了审核授权就照作者撤回处理。"""
    issuer = _knowlab(caller)
    live = await _call(lambda: service.platform_process(issuer, subject, kind, ref))
    return PlatformProcessRead(kind=kind, ref=ref, live=live)


@router.post(
    "/experts/{expert_id}/versions/{version}/freeze/void",
    response_model=FrozenVoidRead,
)
async def void_frozen(
    expert_id: str,
    version: str,
    payload: FreezeIn,
    caller: ServiceCaller = Depends(require_directory_service),
    service: UserRunService = Depends(get_user_run_service),
):
    """作废没交成的那次冻结（作者撤回、平台作废没交到 knowmart 的交付）；有别人凭它
    请过这一版的不许作废（409 frozen_in_use）。"""
    withdrawn = await _call(
        lambda: service.void_frozen(caller.caller, payload.author, expert_id, version)
    )
    return FrozenVoidRead(withdrawn=withdrawn)


# —— 执行的应用：凭授权读、交还 ——


@router.post(
    "/service/run-grants/by-confirmation",
    response_model=RunGrantRead,
    status_code=status.HTTP_201_CREATED,
)
async def grant_by_confirmation(
    payload: ConfirmedRunGrantIn,
    executor: tuple[str, str] = Depends(require_executor),
    resolutions: UserResolutionService = Depends(get_user_resolution_service),
    service: UserRunService = Depends(get_user_run_service),
):
    """等数据的委托数据齐了，knowbench 凭本人那次确认续签（所有者 10-27 定）。"""
    resolution, confirmed_needs = await _call(
        lambda: resolutions.confirmed_for_run(
            executor[1],
            payload.subject,
            payload.confirmation_id,
            payload.ref,
            payload.resolution_id,
            payload.revision,
        )
    )
    grant = await _call(
        lambda: service.grant_by_confirmation(
            executor[1],
            payload.subject,
            payload.confirmation_id,
            payload.ref,
            resolution,
            _items(payload.items),
            payload.ttl_seconds,
            confirmed_needs,
        )
    )
    return _grant(grant)


@router.get("/run-grants/{grant_id}", response_model=RunGrantView)
async def grant_view(
    grant_id: str,
    executor: tuple[str, str] = Depends(require_executor),
    service: UserRunService = Depends(get_user_run_service),
):
    grant, input_set = await _call(lambda: service.grant_view(executor[1], grant_id))
    return _view(grant, input_set)


@router.get("/run-grants/{grant_id}/materials/{material_id}/content")
async def read_content(
    grant_id: str,
    material_id: str,
    version_no: int | None = Query(default=None, ge=1),
    executor: tuple[str, str] = Depends(require_executor),
    service: UserRunService = Depends(get_user_run_service),
):
    reader, app = executor
    got = await _call(
        lambda: service.read(app, reader, grant_id, material_id, version_no, "content")
    )
    return Response(
        content=got.content,
        media_type=got.media_type,
        headers={"X-Content-SHA256": got.version.sha256, "Cache-Control": "no-store"},
    )


@router.get("/run-grants/{grant_id}/materials/{material_id}/rendition")
async def read_rendition(
    grant_id: str,
    material_id: str,
    version_no: int | None = Query(default=None, ge=1),
    executor: tuple[str, str] = Depends(require_executor),
    service: UserRunService = Depends(get_user_run_service),
):
    """读钉住的那个读法版本。

    正文回文字，按页文字回 `[{page, text}]`（同写回的格式）。
    """
    reader, app = executor
    got = await _call(
        lambda: service.read(
            app, reader, grant_id, material_id, version_no, "rendition"
        )
    )
    return Response(
        content=got.content,
        media_type=got.media_type,
        headers={
            "X-Content-SHA256": got.rendition.sha256 or "",
            "X-Rendition": f"{got.rendition.kind}/{got.rendition.rendition_no}",
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/run-grants/{grant_id}/materials/{material_id}/format",
    response_model=GrantedFormatRead,
)
async def read_format(
    grant_id: str,
    material_id: str,
    version_no: int | None = Query(default=None, ge=1),
    executor: tuple[str, str] = Depends(require_executor),
    service: UserRunService = Depends(get_user_run_service),
):
    reader, app = executor
    got = await _call(
        lambda: service.read(app, reader, grant_id, material_id, version_no, "format")
    )
    return GrantedFormatRead(
        material_id=got.item.material_id,
        version_no=got.version.version_no,
        media_type=got.version.media_type,
        size_bytes=got.version.size_bytes,
        sha256=got.version.sha256,
        sheets=list(got.format.sheets),
        header=got.format.header,
    )


@router.post("/run-grants/{grant_id}/release", status_code=status.HTTP_204_NO_CONTENT)
async def release(
    grant_id: str,
    executor: tuple[str, str] = Depends(require_executor),
    service: UserRunService = Depends(get_user_run_service),
):
    await _call(lambda: service.release(executor[1], grant_id))


@router.post("/runs/{ref}/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_ref(
    ref: str,
    executor: tuple[str, str] = Depends(require_executor),
    service: UserRunService = Depends(get_user_run_service),
):
    """会话、任务结束：这件事签过的全部交还。"""
    await _call(lambda: service.release_ref(executor[1], ref))


# —— 产出区：knowbench、knowlab 替用户写 ——


@router.post(
    "/outputs", response_model=OutputWritten, status_code=status.HTTP_201_CREATED
)
async def write_output(
    request: Request,
    beneficiary: str = Query(min_length=1, max_length=255),
    task_ref: str = Query(min_length=1, max_length=255),
    run_ref: str = Query(min_length=1, max_length=255),
    name: str = Query(min_length=1, max_length=200),
    evidence: bool = False,
    expert_id: str | None = Query(default=None, max_length=64),
    expert_version: str | None = Query(default=None, max_length=32),
    input_set_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    caller: ServiceCaller = Depends(require_output_service),
    service: UserRunService = Depends(get_user_run_service),
):
    """请求体就是文件；同一次运行同名的重放回原来那份，内容不同 409。"""
    content = await _body(request)
    provenance = {
        "expert_id": expert_id,
        "expert_version": expert_version,
        "input_set_id": input_set_id,
    }
    material, version = await _call(
        lambda: service.write_output(
            caller.caller,
            beneficiary,
            task_ref,
            run_ref,
            name,
            content,
            evidence=evidence,
            provenance={k: v for k, v in provenance.items() if v is not None},
        )
    )
    return OutputWritten(
        material_id=material.id, version_no=version.version_no, sha256=version.sha256
    )


# —— 加工：knowfeed ——


@router.get("/processing/pending", response_model=list[PendingProcessingRead])
async def pending_processing(
    limit: int = Query(default=50, ge=1, le=200),
    caller: ServiceCaller = Depends(require_supply_service),
    service: UserRunService = Depends(get_user_run_service),
):
    found = await _call(lambda: service.pending_processing(caller.caller, limit))
    return [
        PendingProcessingRead(owner=o, material_id=m, version_no=v) for o, m, v in found
    ]


@router.post(
    "/processing/grants",
    response_model=RunGrantRead,
    status_code=status.HTTP_201_CREATED,
)
async def issue_processing(
    payload: ProcessingGrantIn,
    caller: ServiceCaller = Depends(require_supply_service),
    service: UserRunService = Depends(get_user_run_service),
):
    grant = await _call(
        lambda: service.issue_processing(
            caller.caller, payload.owner, payload.material_id, payload.version_no
        )
    )
    return _grant(grant)


@router.post(
    "/processing/grants/{grant_id}/renditions/{kind}",
    response_model=RenditionRead,
    status_code=status.HTTP_201_CREATED,
)
async def write_rendition(
    grant_id: str,
    kind: str,
    request: Request,
    processor_version: str = Query(min_length=1, max_length=40),
    failed_reason: str | None = Query(default=None, max_length=500),
    title: str | None = Query(default=None, max_length=500),
    published_on: date | None = None,
    caller: ServiceCaller = Depends(require_supply_service),
    service: UserRunService = Depends(get_user_run_service),
):
    """写回读法，存进 knowbase 的 PostgreSQL（SDD 0043 第五节）：正文的请求体是 UTF-8
    文字，可带标题、发布日期；按页文字的请求体是 `[{"page": 1, "text": "…"}]`。
    表格写成事实行（`/facts`）。没成的不带请求体，给 `failed_reason`。"""
    content = await _body(request)
    if failed_reason is not None and content:
        raise AppException(
            code="rendition_ambiguous",
            status_code=422,
            msg="a failed rendition has no content",
        )
    rendition = await _call(
        lambda: service.write_rendition(
            caller.caller,
            grant_id,
            kind,
            processor_version,
            None if failed_reason is not None else content,
            {"reason": failed_reason} if failed_reason is not None else None,
            title=title,
            published_on=published_on,
        )
    )
    return _rendition(rendition)
