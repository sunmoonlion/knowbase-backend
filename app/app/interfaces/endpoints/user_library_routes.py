"""资料库的内部接口 `user-data/v2`（SDD 0043）：数据第四版，资料不再按应用分区。

v1（`user-data/v1`）10-28 已删（decisions ⑰）。本人接口的身份：
`Authorization: Bearer <换来的用户令牌>`，`sub` 是用户；`azp` 不认分区，只记成代办的
应用（读数留痕里写这一栏）。
"""

from __future__ import annotations

import json
from datetime import date
from email.parser import BytesParser
from email.policy import default

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.application.errors.exceptions import AppException
from app.application.services.user_library_service import (
    MAX_PUBLIC_BYTES,
    UserLibraryService,
)
from app.domain.user_data import MAX_ITEM_BYTES, Owner
from app.domain.user_library import (
    Account,
    Description,
    ShelfEntry,
    SubscriptionScope,
    Use,
)
from app.domain.vocabulary import class_of, snapshot
from app.infrastructure.external.first_page import read_first_page
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_binding_check_service,
    require_directory_service,
    require_purchase_service,
    require_supply_service,
)
from app.infrastructure.security.user_token import (
    require_subscribing_owner,
    require_user_owner,
)
from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.storage.user_objects import S3UserObjects
from app.interfaces.endpoints.user_data_errors import call as _call
from app.interfaces.http.internal.host_guard import internal_host_only
from app.interfaces.schemas.user_library import (
    AccountIn,
    AccountRead,
    BeneficiaryIn,
    BindingChecksIn,
    BindingChecksRead,
    BindingStateRead,
    BulkUseIn,
    BulkUseRead,
    CollectedOriginalRead,
    DescriptionIn,
    DescriptionRead,
    ExpertIn,
    ExpertRead,
    FreezeIn,
    FrozenBindingRead,
    FrozenLicenseIn,
    HistoryEntryRead,
    HoldingRead,
    LicenseIn,
    MarketOffersIn,
    MarketOffersRead,
    MarketOfferStateRead,
    MaterialFormatRead,
    MaterialRead,
    PlatformConsentIn,
    PublicOriginalRead,
    PurchaseIn,
    PurchaseRead,
    ReferencesIn,
    ReferencesRead,
    ShelfEntryRead,
    SubscriptionRead,
    SubscriptionScopeIn,
    TableFormatRead,
    UseIn,
    UseRead,
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
        first_page=read_first_page,
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
        edition=given.edition,
        topics=tuple(given.topics),
        industry=given.industry,
        language=given.language,
        attributes=dict(given.attributes),
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
                doc_class=class_of(d.doc_type) if d.doc_type else None,
                period_start=d.period_start,
                period_end=d.period_end,
                disclosed_on=d.disclosed_on,
                data_format=d.data_format,
                edition=d.edition,
                topics=list(d.topics),
                industry=d.industry,
                language=d.language,
                attributes=dict(d.attributes),
                status=d.status,
                vocab_version=d.vocab_version,
                resolvable=d.resolvable,
            ),
            current_version=m.current_version,
            created_at=m.created_at,
            deleted=m.deleted_at is not None,
            legacy_app=m.legacy_app,
            provenance=m.provenance,
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


@router.get(
    "/materials/{material_id}/versions/{version_no}/format",
    response_model=MaterialFormatRead,
)
async def material_format(
    material_id: str,
    version_no: int,
    response: Response,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    version, tables = await _call(
        lambda: service.read_format(owner.subject, owner.app, material_id, version_no)
    )
    response.headers["Cache-Control"] = "no-store"
    return MaterialFormatRead(
        material_id=material_id,
        version_no=version.version_no,
        sha256=version.sha256,
        media_type=version.media_type,
        tables=[
            TableFormatRead(name=t.name, columns=list(t.columns), rows=list(t.rows))
            for t in tables
        ],
    )


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
    edition: str = Query(default="full", max_length=16),
    topics: list[str] = Query(default_factory=list, max_length=12),
    industry: str | None = Query(default=None, max_length=16),
    language: str | None = Query(default=None, max_length=8),
    confirm: bool = False,
    license: str = Query(default="self", max_length=16),
    attest: bool = False,
    process: bool = False,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """请求体就是文件本身；描述放在查询参数里，勾了 `confirm` 才算用户确认过。
    研报的几维（来源机构、研究员等）上传后经改描述填。许可档选高于「只许本人用」的
    要勾 `attest`（本人勾了「我有权」）。

    `process`：本人按页面说明采用加工（交 knowfeed 加工这一版）。
    """
    content = await _body(request)
    description = Description(
        subject=subject,
        doc_type=doc_type,
        period_start=period_start,
        period_end=period_end,
        disclosed_on=disclosed_on,
        edition=edition,
        topics=tuple(topics),
        industry=industry,
        language=language,
    )
    material, _ = await _call(
        lambda: service.upload(
            owner.subject,
            name,
            content,
            description=description,
            confirm=confirm,
            license_=license,
            attest=attest,
            process=process,
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
    process: bool = False,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    content = await _body(request)
    await _call(
        lambda: service.add_version(
            owner.subject, material_id, name, content, process=process
        )
    )
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


# —— 块 1b：展示即同意、账号设置、名下专家 ——


def _use(use: Use) -> UseRead:
    return UseRead(
        use=use.use,
        created_at=use.created_at,
        created_via=use.created_via,
        version_no=use.version_no,
        material_id=use.material_id,
    )


def _account(account: Account) -> AccountRead:
    return AccountRead(
        default_display=account.default_display,
        seen=account.default_display_seen_at is not None,
        displays_by_default=account.displays_by_default,
    )


@router.get("/materials/{material_id}/uses", response_model=list[UseRead])
async def list_uses(
    material_id: str,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    return [
        _use(u) for u in await _call(lambda: service.uses(owner.subject, material_id))
    ]


@router.post(
    "/materials/{material_id}/uses",
    response_model=UseRead,
    status_code=status.HTTP_201_CREATED,
)
async def show(
    material_id: str,
    payload: UseIn,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """本人点展示按钮（本人的令牌就是同意依据的来源）。"""
    return _use(
        await _call(lambda: service.show(owner.subject, material_id, payload.use))
    )


@router.delete("/materials/{material_id}/uses", status_code=status.HTTP_204_NO_CONTENT)
async def hide(
    material_id: str,
    use: str = Query(min_length=3, max_length=80),
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    await _call(lambda: service.hide(owner.subject, material_id, use))


@router.post("/uses/bulk", response_model=BulkUseRead)
async def show_many(
    payload: BulkUseIn,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    done = await _call(
        lambda: service.show_many(
            owner.subject, payload.material_ids, payload.use, on=payload.on
        )
    )
    return BulkUseRead(done=done)


@router.get("/columns/{app}", response_model=list[ShelfEntryRead])
async def column(
    app: str,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """某个应用的知识库栏：只给那个应用自己代办时看（令牌的 azp 就是这个应用）。"""
    if owner.app != app:
        raise AppException(
            code="column_denied",
            status_code=403,
            msg="an app reads only its own column",
        )
    entries = await _call(lambda: service.column(owner.subject, app))
    return [_entry(e, owner.subject) for e in entries]


@router.get("/account", response_model=AccountRead)
async def get_account(
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    return _account(await _call(lambda: service.account(owner.subject)))


@router.put("/account", response_model=AccountRead)
async def acknowledge_account(
    payload: AccountIn,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    return _account(
        await _call(
            lambda: service.acknowledge_default_display(
                owner.subject, payload.default_display
            )
        )
    )


@router.get("/experts", response_model=list[ExpertRead])
async def my_experts(
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    return [
        ExpertRead(expert_id=e.expert_id, name=e.name, latest_version=e.latest_version)
        for e in await _call(lambda: service.experts(owner.subject))
    ]


# —— 平台过程、订阅、加工 ——

# 订阅在 knowfeed web端建，由 knowfeed 凭本人令牌转交（SDD 0045 第 2.1 节）
SUBSCRIPTION_APPS = ("knowfeed",)


def _subscription(use: Use) -> SubscriptionRead:
    subscription_id, revision = use.use.removeprefix("subscription:").rsplit("@", 1)
    scope = SubscriptionScope.from_dict(use.scope)
    return SubscriptionRead(
        subscription_id=subscription_id,
        revision=int(revision),
        by=scope.by,
        starts_on=scope.starts_on,
        ends_on=scope.ends_on,
        purpose=scope.purpose,
        subjects=list(scope.subjects),
        doc_types=list(scope.doc_types),
        urls=list(scope.urls),
        topic=scope.topic,
        pick_each_round=scope.pick_each_round,
        created_at=use.created_at,
    )


def _subscribing_app(owner: Owner) -> None:
    if owner.app not in SUBSCRIPTION_APPS:
        raise AppException(
            code="subscription_app_denied",
            status_code=403,
            msg="subscriptions are made in knowfeed",
        )


@router.post(
    "/platform-consents",
    response_model=list[UseRead],
    status_code=status.HTTP_201_CREATED,
)
async def consent_platform(
    payload: PlatformConsentIn,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """本人在 knowlab 确认平台过程的范围；应用就是代办的那个（令牌的 azp）。"""
    items = [(i.material_id, i.version_no) for i in payload.items]
    uses = await _call(
        lambda: service.consent_platform(
            owner.subject, owner.app, payload.kind, payload.ref, items
        )
    )
    return [_use(u) for u in uses]


@router.get("/platform-consents/{kind}/{ref}", response_model=list[UseRead])
async def platform_consents(
    kind: str,
    ref: str,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    uses = await _call(
        lambda: service.platform_consents(owner.subject, owner.app, kind, ref)
    )
    return [_use(u) for u in uses]


@router.get("/subscriptions", response_model=list[SubscriptionRead])
async def subscriptions(
    owner: Owner = Depends(require_subscribing_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    return [
        _subscription(u)
        for u in await _call(lambda: service.subscriptions(owner.subject))
    ]


@router.put(
    "/subscriptions/{subscription_id}/revisions/{revision}",
    response_model=SubscriptionRead,
)
async def subscribe(
    subscription_id: str,
    revision: int,
    payload: SubscriptionScopeIn,
    owner: Owner = Depends(require_subscribing_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """建订阅、续订：同一修订重放回原来那条；范围不同要出新修订（409）。"""
    _subscribing_app(owner)
    scope = SubscriptionScope(
        by=payload.by,
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        purpose=payload.purpose,
        subjects=tuple(payload.subjects),
        doc_types=tuple(payload.doc_types),
        urls=tuple(payload.urls),
        topic=payload.topic,
    )
    use = await _call(
        lambda: service.subscribe(
            owner.subject, subscription_id, revision, scope, f"user:{owner.app}"
        )
    )
    return _subscription(use)


@router.delete(
    "/subscriptions/{subscription_id}/revisions/{revision}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unsubscribe(
    subscription_id: str,
    revision: int,
    owner: Owner = Depends(require_subscribing_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    _subscribing_app(owner)
    await _call(lambda: service.unsubscribe(owner.subject, subscription_id, revision))


@router.get(
    "/service/subscriptions/{subscription_id}/revisions/{revision}",
    response_model=SubscriptionRead,
)
async def check_subscription(
    subscription_id: str,
    revision: int,
    subject: str = Query(min_length=1, max_length=255),
    caller: ServiceCaller = Depends(require_supply_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """knowfeed 后台生成新需求前核订阅范围同意还在（本人可能已在 knowbase 撤了）；
    撤了回 404。只读，只认 knowfeed。"""
    use = await _call(
        lambda: service.live_subscription(
            caller.caller, subject, subscription_id, revision
        )
    )
    return _subscription(use)


@router.post(
    "/materials/{material_id}/versions/{version_no}/processing",
    response_model=UseRead,
    status_code=status.HTTP_201_CREATED,
)
async def consent_processing(
    material_id: str,
    version_no: int,
    owner: Owner = Depends(require_user_owner),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """对已传的一版补采用加工（上传时没勾的）。"""
    return _use(
        await _call(
            lambda: service.consent_processing(owner.subject, material_id, version_no)
        )
    )


# —— 服务：knowlab 推名录、knowmart 成交 ——


@router.put("/experts/{expert_id}", response_model=ExpertRead)
async def push_expert(
    expert_id: str,
    payload: ExpertIn,
    caller: ServiceCaller = Depends(require_directory_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """knowlab 在专家、准专家建版本时推名录；只认 knowlab。"""
    if caller.caller != "knowlab":
        raise AppException(
            code="service_scope_denied",
            status_code=403,
            msg="only knowlab pushes the expert directory",
        )
    expert = await _call(
        lambda: service.upsert_expert(
            expert_id, payload.owner, payload.name, payload.latest_version
        )
    )
    return ExpertRead(
        expert_id=expert.expert_id,
        name=expert.name,
        latest_version=expert.latest_version,
    )


@router.post(
    "/experts/{expert_id}/versions/{version}/freeze",
    response_model=list[FrozenBindingRead],
    status_code=status.HTTP_201_CREATED,
)
async def freeze_binding(
    expert_id: str,
    version: str,
    payload: FreezeIn,
    caller: ServiceCaller = Depends(require_directory_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """knowlab 交付时冻结作者绑给这位专家的数据：每份钉在当时那一版。"""
    if caller.caller != "knowlab":
        raise AppException(
            code="service_scope_denied",
            status_code=403,
            msg="only knowlab freezes bindings",
        )
    frozen = await _call(
        lambda: service.freeze_binding(
            payload.author, expert_id, version, preview=payload.preview
        )
    )
    return [
        FrozenBindingRead(
            material_id=str(u.material_id),
            version_no=int(u.version_no or 0),
            sha256=sha256,
            basis_ref=u.use,
            license=str(u.scope.get("license") or "self"),  # type: ignore[arg-type]
        )
        for u, sha256 in frozen
    ]


@router.post(
    "/experts/{expert_id}/versions/{version}/frozen/{material_id}/license",
    response_model=FrozenBindingRead,
)
async def raise_frozen_license(
    expert_id: str,
    version: str,
    material_id: str,
    payload: FrozenLicenseIn,
    caller: ServiceCaller = Depends(require_directory_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """自用转公开时把冻结的那一份许可往高改（只许往高、要勾「我有权」、现在的许可
    够；留许可留痕）；knowlab 凭作者那次提交代办。"""
    use = await _call(
        lambda: service.raise_frozen_license(
            caller.caller,
            payload.author,
            expert_id,
            version,
            material_id,
            payload.license,
            attest=payload.attest,
        )
    )
    version_no = int(use.version_no or 0)
    pinned = await _call(lambda: service.repo.version(material_id, version_no))
    return FrozenBindingRead(
        material_id=material_id,
        version_no=version_no,
        sha256=pinned.sha256 if pinned is not None else "",
        basis_ref=use.use,
        license=str(use.scope.get("license")),  # type: ignore[arg-type]
    )


@router.post(
    "/service/collected-originals",
    response_model=list[CollectedOriginalRead],
    status_code=status.HTTP_201_CREATED,
)
async def deliver_collected(
    request: Request,
    channel: str = Query(min_length=2, max_length=40),
    channel_doc_id: str = Query(min_length=1, max_length=200),
    name: str = Query(min_length=1, max_length=200),
    license: str = Query(default="self", max_length=20),
    source_url: str | None = Query(default=None, max_length=2000),
    caller: ServiceCaller = Depends(require_supply_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """knowfeed 交非公开采集的原件（multipart），每位受益人各得名下资料。"""
    body = await _public_body(request)
    content_type = request.headers.get("content-type", "")
    file_content: bytes = b""
    beneficiaries_raw: str = ""

    if "multipart/form-data" in content_type:
        header = f"Content-Type: {content_type}\r\n\r\n".encode()
        msg = BytesParser(policy=default).parsebytes(header + body)
        for part in msg.iter_parts():
            part_name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            if not isinstance(payload, bytes):
                raise AppException(
                    code="multipart_invalid",
                    status_code=422,
                    msg="invalid multipart payload",
                )
            if part_name == "file":
                file_content = payload
            elif part_name == "beneficiaries":
                beneficiaries_raw = payload.decode("utf-8", "replace")
    else:
        file_content = body

    if not file_content:
        raise AppException(
            code="file_missing", status_code=422, msg="multipart missing file part"
        )
    try:
        raw_bens = json.loads(beneficiaries_raw) if beneficiaries_raw else []
        if not isinstance(raw_bens, list) or not raw_bens:
            raise ValueError("at least one beneficiary is required")
        b_list = [BeneficiaryIn(**b).model_dump(mode="json") for b in raw_bens]
    except (ValueError, TypeError) as exc:
        raise AppException(
            code="beneficiaries_invalid",
            status_code=422,
            msg="invalid beneficiaries JSON",
        ) from exc

    results = await _call(
        lambda: service.deliver_collected(
            caller.caller,
            channel,
            channel_doc_id,
            name,
            file_content,
            b_list,
            license_=license,
            source_url=source_url,
        )
    )
    return [CollectedOriginalRead(**r) for r in results]


@router.post(
    "/service/public-originals",
    response_model=PublicOriginalRead,
    status_code=status.HTTP_201_CREATED,
)
async def deliver_public(
    request: Request,
    channel: str = Query(min_length=2, max_length=40),
    channel_doc_id: str = Query(min_length=1, max_length=200),
    name: str = Query(min_length=1, max_length=200),
    subject: str = Query(min_length=1, max_length=64),
    doc_type: str = Query(min_length=1, max_length=32),
    period_start: date | None = None,
    period_end: date | None = None,
    disclosed_on: date | None = None,
    edition: str = Query(default="full", max_length=16),
    topics: list[str] = Query(default_factory=list, max_length=12),
    source_url: str | None = Query(default=None, max_length=2000),
    caller: ServiceCaller = Depends(require_supply_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """knowfeed 交一份公开披露（请求体就是文件）：同一渠道同一编号只存一份。"""
    content = await _public_body(request)
    before = await _call(lambda: _existing_version(service, channel, channel_doc_id))
    material, version, _ = await _call(
        lambda: service.deliver_public(
            caller.caller,
            channel,
            channel_doc_id,
            name,
            content,
            Description(
                subject=subject,
                doc_type=doc_type,
                period_start=period_start,
                period_end=period_end,
                disclosed_on=disclosed_on,
                edition=edition,
                topics=tuple(topics),
            ),
            source_url=source_url,
        )
    )
    return PublicOriginalRead(
        material_id=material.id,
        version_no=version.version_no,
        sha256=version.sha256,
        created=before != (material.id, version.version_no),
    )


@router.post(
    "/service/public-originals/{material_id}/references",
    response_model=ReferencesRead,
)
async def add_public_references(
    material_id: str,
    payload: ReferencesIn,
    caller: ServiceCaller = Depends(require_supply_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """给提了需求的用户各记一条可用的引用（取得依据「引用」）。"""
    made = await _call(
        lambda: service.add_public_references(
            caller.caller,
            material_id,
            payload.version_no,
            [(r.subject, r.ref) for r in payload.references],
        )
    )
    return ReferencesRead(recorded=len(made))


async def _existing_version(
    service: UserLibraryService, channel: str, channel_doc_id: str
) -> tuple[str, int] | None:
    found = await service.repo.find_public(channel, channel_doc_id)
    return (found.id, found.current_version) if found is not None else None


async def _public_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_PUBLIC_BYTES:
            raise AppException(
                code="too_large", status_code=413, msg="file is larger than 200 MB"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/purchases", response_model=PurchaseRead, status_code=status.HTTP_201_CREATED
)
async def purchase(
    payload: PurchaseIn,
    caller: ServiceCaller = Depends(require_purchase_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """knowmart 成交后给买家加使用权（不复制）；同一笔交易重试回已有的那条。"""
    if caller.caller != "knowmart":
        raise AppException(
            code="service_scope_denied",
            status_code=403,
            msg="only knowmart records purchases",
        )
    holding = await _call(
        lambda: service.purchase(
            payload.seller,
            payload.material_id,
            payload.buyer,
            payload.transaction_ref,
            resale_allowed=payload.resale_allowed,
        )
    )
    return PurchaseRead(
        material_id=holding.material_id,
        buyer=holding.subject,
        version_no=holding.version_no,
        license=holding.license,
    )


@router.post("/service/market-offers/check", response_model=MarketOffersRead)
async def check_market_offers(
    payload: MarketOffersIn,
    caller: ServiceCaller = Depends(require_purchase_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """knowmart 后台定时成批核条目里的资料还能不能卖（「待你决定」第 7 条）；只读。

    没有卖家本人的令牌，用成交那条服务关系（`user-data-purchase`），只认 knowmart。
    """
    states = await _call(
        lambda: service.offer_states(
            caller.caller, [(i.seller, i.material_id) for i in payload.items]
        )
    )
    return MarketOffersRead(
        items=[
            MarketOfferStateRead(seller=seller, material_id=material_id, state=state)  # type: ignore[arg-type]
            for seller, material_id, state in states
        ]
    )


@router.post("/service/expert-bindings/check", response_model=BindingChecksRead)
async def check_expert_bindings(
    payload: BindingChecksIn,
    caller: ServiceCaller = Depends(require_binding_check_service),
    service: UserLibraryService = Depends(get_user_library_service),
):
    """专家所绑的每份数据现在的状态与许可（0043 第 4.3 节「两种许可检查」；数据拓扑
    第十节第 5 条）；只读。

    knowmart 收清单时硬查、后台定时核在售专家（成交那条关系），knowlab 运营审核门预查
    （名录那条关系）。
    """
    states = await _call(
        lambda: service.binding_states(
            caller.caller,
            [
                (i.owner, i.material_id, i.version_no, i.basis_ref)
                for i in payload.items
            ],
        )
    )
    return BindingChecksRead(
        items=[
            BindingStateRead(
                owner=owner,
                material_id=material_id,
                version_no=version_no,
                state=state,  # type: ignore[arg-type]
                license=license_,  # type: ignore[arg-type]
                shareable=shareable,
                frozen_license=frozen,  # type: ignore[arg-type]
            )
            for (
                owner,
                material_id,
                version_no,
                state,
                license_,
                shareable,
                frozen,
            ) in states
        ]
    )
