"""按描述查、解析、确认、偏好的内部接口 `user-data/v2`（SDD 0043 第三节，块 2）。

本人接口由应用凭本人令牌转交（`azp` 就是解析给哪个应用）；knowfeed 用服务身份
（关系 `user-data-supply`）核确认。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, status

from app.application.services.user_resolution_service import UserResolutionService
from app.domain.user_data import Owner
from app.domain.user_resolution import (
    Candidate,
    Confirmation,
    DescriptionKey,
    NeedSpec,
    PeriodRule,
    Resolution,
    TaskParams,
)
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.repositories.user_resolution import (
    SqlUserResolutionRepository,
)
from app.infrastructure.repositories.user_runs import SqlUserRunRepository
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_read_service,
    require_supply_service,
)
from app.infrastructure.security.user_token import require_user_owner
from app.infrastructure.storage.postgres import get_postgres
from app.interfaces.endpoints.user_data_routes import _call
from app.interfaces.http.internal.host_guard import internal_host_only
from app.interfaces.schemas.user_resolution import (
    AddNeedsIn,
    BranchRead,
    CandidateRead,
    ConfirmationCheckRead,
    ConfirmationRead,
    ConfirmIn,
    DescriptionKeyModel,
    LookupRead,
    NeedSpecIn,
    PreferenceRead,
    ResolutionRead,
    ResolveIn,
    ResultItemRead,
    ReviseNeedsIn,
    TaskParamsIn,
)
from core.config import get_settings

router = APIRouter(
    prefix="/internal/v2/user-data",
    tags=["内部-资料库-解析"],
    dependencies=[Depends(internal_host_only)],
)


def get_user_resolution_service() -> UserResolutionService:
    def sessions():
        return get_postgres().session_factory

    return UserResolutionService(
        SqlUserResolutionRepository(sessions),
        SqlUserLibraryRepository(sessions),
        SqlUserRunRepository(sessions),
        public_access=get_settings().public_access_enabled,
    )


def _need(n: NeedSpecIn) -> NeedSpec:
    p = n.period
    return NeedSpec(
        key=n.key,
        name=n.name,
        doc_type=n.doc_type,
        period=PeriodRule(
            kind=p.kind,
            start=p.start,
            end=p.end,
            count=p.count,
            disclosed_by_task_date=p.disclosed_by_task_date,
            months=p.months,
        ),
        formats=tuple(n.formats),
        required=n.required,
        min_count=n.min_count,
        max_count=n.max_count,
        reading=n.reading,
        sources=tuple(n.sources),
        priority=n.priority,
        tables=tuple((t.sheet, tuple(t.columns)) for t in n.tables),
        caliber=n.caliber,
        description=n.description,
        edition=n.edition,
        topics=tuple(n.topics),
        industry=n.industry,
        subject=n.subject,
    )


def _params(p: TaskParamsIn) -> TaskParams:
    return TaskParams(task_date=p.task_date)


def _candidate(c: Candidate) -> CandidateRead:
    return CandidateRead(
        material_id=c.material_id,
        basis=c.basis,
        version_no=c.version_no,
        sha256=c.sha256,
        content_origin=c.content_origin,
        disclosed_on=c.disclosed_on,
        data_format=c.data_format,
        rendition_kind=c.rendition_kind,
        rendition_no=c.rendition_no,
        rendition_sha256=c.rendition_sha256,
        name=c.name,
        media_type=c.media_type,
        size_bytes=c.size_bytes,
    )


def _resolution(r: Resolution) -> ResolutionRead:
    return ResolutionRead(
        resolution_id=r.id,
        revision=r.revision,
        consumer=r.consumer,
        contract_version=r.contract_version,
        vocab_version=r.vocab_version,
        task_ref=r.task_ref,
        items=[
            ResultItemRead(
                need_key=i.need_key,
                key=DescriptionKeyModel(
                    **{**i.key.__dict__, "topics": list(i.key.topics)}
                ),
                status=i.status,
                chosen=[_candidate(c) for c in i.chosen],
                candidates=[_candidate(c) for c in i.candidates],
                reason=i.reason,
            )
            for i in r.items
        ],
        created_at=r.created_at,
    )


def _check(c: Confirmation) -> ConfirmationCheckRead:
    return ConfirmationCheckRead(
        confirmation_id=c.id,
        subject=c.subject,
        app=c.app,
        kind=c.kind,
        task_ref=c.task_ref,
        resolution_id=c.resolution_id,
        resolution_revision=c.resolution_revision,
        needs=list(c.needs),
        items=list(c.items),
        supply=c.supply,
        expires_at=c.expires_at,
    )


@router.get("/lookup", response_model=LookupRead)
async def lookup(
    subject: str = Query(min_length=1, max_length=64),
    doc_type: str = Query(min_length=1, max_length=32),
    period_start: date | None = None,
    period_end: date | None = None,
    edition: str = Query(default="full", max_length=16),
    owner: Owner = Depends(require_user_owner),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    """按描述查：只认进键的维度；不列、不浏览、不模糊搜。"""
    key = DescriptionKey(subject, doc_type, period_start, period_end, edition=edition)
    found, zone = await _call(lambda: service.lookup(owner.subject, owner.app, key))
    return LookupRead(status=found, zone=zone)


@router.post(
    "/resolutions", response_model=ResolutionRead, status_code=status.HTTP_201_CREATED
)
async def resolve(
    payload: ResolveIn,
    owner: Owner = Depends(require_user_owner),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    resolution = await _call(
        lambda: service.resolve(
            owner.subject,
            owner.app,
            payload.consumer,
            payload.contract_version,
            [_need(n) for n in payload.needs],
            _params(payload.params),
            payload.task_ref,
        )
    )
    return _resolution(resolution)


@router.get("/resolutions/{resolution_id}", response_model=ResolutionRead)
async def get_resolution(
    resolution_id: str,
    revision: int | None = Query(default=None, ge=1),
    owner: Owner = Depends(require_user_owner),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    resolution = await _call(
        lambda: service.get_resolution(
            owner.subject, owner.app, resolution_id, revision
        )
    )
    return _resolution(resolution)


@router.post(
    "/resolutions/{resolution_id}/revisions",
    response_model=ResolutionRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_needs(
    resolution_id: str,
    payload: AddNeedsIn,
    owner: Owner = Depends(require_user_owner),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    """运行中冒出的新需求：出新修订，已有条目不动。"""
    resolution = await _call(
        lambda: service.add_needs(
            owner.subject,
            owner.app,
            resolution_id,
            [_need(n) for n in payload.needs],
        )
    )
    return _resolution(resolution)


@router.put(
    "/resolutions/{resolution_id}/needs",
    response_model=ResolutionRead,
    status_code=status.HTTP_201_CREATED,
)
async def revise_needs(
    resolution_id: str,
    payload: ReviseNeedsIn,
    owner: Owner = Depends(require_user_owner),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    """用户确认前改了清单：整份描述换成改过的，重新解析，出新修订。"""
    resolution = await _call(
        lambda: service.revise_needs(
            owner.subject,
            owner.app,
            resolution_id,
            [_need(n) for n in payload.needs],
        )
    )
    return _resolution(resolution)


@router.post(
    "/service/resolutions/{resolution_id}/revisions",
    response_model=ResolutionRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_needs_for_run(
    resolution_id: str,
    payload: AddNeedsIn,
    subject: str = Query(min_length=1, max_length=255),
    caller: ServiceCaller = Depends(require_read_service),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    """执行的应用替运行中的专家提新需求：只出修订，确认仍要本人。"""
    resolution = await _call(
        lambda: service.add_needs_for_run(
            caller.caller, subject, resolution_id, [_need(n) for n in payload.needs]
        )
    )
    return _resolution(resolution)


@router.post(
    "/service/resolutions/{resolution_id}/refresh",
    response_model=ResolutionRead,
)
async def refresh_for_run(
    resolution_id: str,
    subject: str = Query(min_length=1, max_length=255),
    caller: ServiceCaller = Depends(require_read_service),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    """数据到了，执行的应用请重解析：已定的不动，没定的重找；没变就回当前修订。"""
    resolution = await _call(
        lambda: service.refresh_for_run(caller.caller, subject, resolution_id)
    )
    return _resolution(resolution)


@router.get("/service/resolutions/{resolution_id}", response_model=ResolutionRead)
async def get_for_run(
    resolution_id: str,
    subject: str = Query(min_length=1, max_length=255),
    revision: int | None = Query(default=None, ge=1),
    caller: ServiceCaller = Depends(require_read_service),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    resolution = await _call(
        lambda: service.get_for_run(caller.caller, subject, resolution_id, revision)
    )
    return _resolution(resolution)


@router.post(
    "/confirmations",
    response_model=ConfirmationRead,
    status_code=status.HTTP_201_CREATED,
)
async def confirm(
    payload: ConfirmIn,
    owner: Owner = Depends(require_user_owner),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    """用户点一次；回确认编号（7 天有效）与入口分支。"""
    items = [i.model_dump(mode="json", exclude_none=True) for i in payload.items]
    confirmation, branches = await _call(
        lambda: service.confirm(
            owner.subject,
            owner.app,
            payload.kind,
            items,
            payload.supply,
            task_ref=payload.task_ref,
            resolution_id=payload.resolution_id,
            revision=payload.revision,
        )
    )
    return ConfirmationRead(
        confirmation_id=confirmation.id,
        kind=confirmation.kind,
        expires_at=confirmation.expires_at,
        items=list(confirmation.items),
        branches=[BranchRead(**b) for b in branches],  # type: ignore[arg-type]
    )


@router.get("/preferences", response_model=list[PreferenceRead])
async def preferences(
    consumer: str = Query(min_length=1, max_length=160),
    owner: Owner = Depends(require_user_owner),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    """偏好（先只读）：用户 × 使用者 × 描述（摘要）。"""
    found = await _call(lambda: service.preferences(owner.subject, consumer))
    return [
        PreferenceRead(
            key_id=p.key_id,
            material_ids=list(p.material_ids),
            updated_at=p.updated_at,
        )
        for p in found
    ]


@router.get(
    "/service/confirmations/{confirmation_id}", response_model=ConfirmationCheckRead
)
async def check_confirmation(
    confirmation_id: str,
    subject: str = Query(min_length=1, max_length=255),
    caller: ServiceCaller = Depends(require_supply_service),
    service: UserResolutionService = Depends(get_user_resolution_service),
):
    """knowfeed 受理前核确认：真、是这个用户的、7 天内。"""
    confirmation = await _call(
        lambda: service.check_confirmation(caller.caller, confirmation_id, subject)
    )
    return _check(confirmation)
