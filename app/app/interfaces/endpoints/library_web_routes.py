"""knowbase web端自家的资料库接口 `/api/web/v1/library`（SDD 0044）。

knowbase web端是用户管自己数据的唯一入口（数据拓扑第三节）。身份是本人在 knowbase
网页登录的会话（Cookie，改动要带 CSRF，和模板的 `/api/web/v1` 一样）；会话里的 `sub`
就是别的应用换来的本人令牌里的那个 `sub`。业务一律走 `user-data/v2` 用的同一套服务
（`UserLibraryService`、`UserRunService`），这里只换身份的认法，不另写一份逻辑。

读数留痕里代办的应用记 `knowbase`（本人在 knowbase 网页上自己下载）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.application.errors.exceptions import AppException
from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_run_service import UserRunService
from app.domain.cross_app import clean_origin, known_apps
from app.domain.recognition import Recognition
from app.domain.security import Principal
from app.domain.user_data import KINDS, MAX_ITEM_BYTES
from app.domain.user_library import DIRECT_UPLOAD_APPS, Description
from app.domain.vocabulary import class_of, snapshot
from app.interfaces.endpoints.user_data_errors import call as _call
from app.interfaces.endpoints.user_library_routes import (
    _account,
    _body,
    _description,
    _entry,
    _subscription,
    _use,
    get_user_library_service,
)
from app.interfaces.endpoints.user_run_routes import _rendition, get_user_run_service
from app.interfaces.http.middleware.auth import get_web_current_user
from app.interfaces.schemas.user_library import (
    AccountIn,
    AccountRead,
    BulkUseIn,
    BulkUseRead,
    DescriptionIn,
    ExpertRead,
    HistoryEntryRead,
    LicenseIn,
    SubscriptionRead,
    UseIn,
    UseRead,
    VersionRead,
    VocabularyRead,
)
from app.interfaces.schemas.user_runs import ConvertIn, ReadEventRead, RenditionRead
from app.interfaces.schemas.web_library import (
    ImpactRead,
    PlatformConsentRead,
    RecognitionRead,
    RecognizedDescription,
    UsageRead,
    WebEntryRead,
)
from core.config import Settings, get_settings

router = APIRouter(prefix="/web/v1/library", tags=["Web 资料库"])

# 本人在 knowbase 网页上自己做的：读数留痕里代办的应用写它
ACTING_APP = "knowbase"
MAX_ATTRIBUTES_CHARS = 4000

WebUser = Annotated[Principal, Depends(get_web_current_user)]
Library = Annotated[UserLibraryService, Depends(get_user_library_service)]
Runs = Annotated[UserRunService, Depends(get_user_run_service)]


def web_library_settings() -> Settings:
    """测试用依赖覆盖换配置（直达上传认哪些来源应用）。"""
    return get_settings()


Config = Annotated[Settings, Depends(web_library_settings)]


async def _web_entry(
    service: UserLibraryService, subject: str, material_id: str
) -> WebEntryRead:
    entry = await _call(lambda: service.get(subject, material_id))
    shown = await service.shown(subject)
    return WebEntryRead(
        **_entry(entry, subject).model_dump(), shown=shown.get(entry.material.id, [])
    )


def _recognition(found: Recognition) -> RecognitionRead:
    d = found.description
    return RecognitionRead(
        description=RecognizedDescription(
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
        ),
        fields=list(found.fields),
        rules_version=found.rules_version,
        read_first_page=found.read_first_page,
    )


def _attributes(raw: str | None) -> dict[str, object]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppException(
            code="attributes_invalid", status_code=422, msg="attributes is not JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise AppException(
            code="attributes_invalid", status_code=422, msg="attributes is an object"
        )
    return parsed


def _display_to(settings: Settings, source: str | None, ref: str | None) -> str | None:
    """直达上传的来源应用（0044 第 2.4 节）：链接带来的 `from` 不可信，只认配置里登记的
    来源；认得、又有知识库栏的才顺带展示给它，别的当作没带。"""
    found = clean_origin(
        source, ref, known_apps=known_apps(settings.cross_app_sources())
    )
    if found is None or found.app not in DIRECT_UPLOAD_APPS:
        return None
    return found.app


# —— 词表、用量、识别 ——


@router.get("/vocabulary", response_model=VocabularyRead)
async def vocabulary(_: WebUser):
    return VocabularyRead(**snapshot())  # type: ignore[arg-type]


@router.get("/usage", response_model=UsageRead)
async def usage(user: WebUser, service: Library):
    used, quota = await _call(lambda: service.usage(user.subject))
    return UsageRead(
        bytes_used=used,
        quota_bytes=quota,
        max_item_bytes=MAX_ITEM_BYTES,
        accepted=sorted(KINDS),
    )


@router.post("/recognitions", response_model=RecognitionRead)
async def recognize(
    request: Request,
    user: WebUser,
    service: Library,
    name: str = Query(min_length=1, max_length=200),
):
    """选文件后先识别描述：请求体是文件本身，只读不存（0044 第 2.3 节）。"""
    content = await _body(request)
    # 读 PDF 首页是算力活，放到线程里，不卡住别的请求
    found = await _call(lambda: asyncio.to_thread(service.recognize, name, content))
    return _recognition(found)


# —— 总栏、上传、详情 ——


@router.get("/materials", response_model=list[WebEntryRead])
async def shelf(user: WebUser, service: Library, category: str | None = None):
    entries = await _call(lambda: service.shelf(user.subject, category))
    shown = await service.shown(user.subject)
    return [
        WebEntryRead(
            **_entry(e, user.subject).model_dump(),
            shown=shown.get(e.material.id, []),
        )
        for e in entries
    ]


@router.post(
    "/materials", response_model=WebEntryRead, status_code=status.HTTP_201_CREATED
)
async def upload(
    request: Request,
    user: WebUser,
    service: Library,
    settings: Settings = Depends(get_settings),
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
    attributes: str | None = Query(default=None, max_length=MAX_ATTRIBUTES_CHARS),
    confirm: bool = False,
    license: str = Query(default="self", max_length=16),
    attest: bool = False,
    process: bool = False,
    source: str | None = Query(default=None, alias="from", max_length=64),
    ref: str | None = Query(default=None, max_length=256),
):
    """上传：请求体是文件本身，描述在查询参数里（研报的几维 `attributes` 是一段 JSON）。

    勾了 `confirm` 才算本人确认过；许可档选高档要勾 `attest`；`process` 是本人采用加工。
    直达上传带 `from`、`ref`：来源应用认得就在同一个事务里记「展示给来源应用」。
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
        attributes=_attributes(attributes),
    )
    material, _ = await _call(
        lambda: service.upload(
            user.subject,
            name,
            content,
            description=description,
            confirm=confirm,
            license_=license,
            attest=attest,
            process=process,
            display_to=_display_to(settings, source, ref),
        )
    )
    return await _web_entry(service, user.subject, material.id)


@router.get("/materials/{material_id}", response_model=WebEntryRead)
async def get_material(material_id: str, user: WebUser, service: Library):
    return await _web_entry(service, user.subject, material_id)


@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove(material_id: str, user: WebUser, service: Library):
    await _call(lambda: service.remove(user.subject, material_id))


@router.get("/materials/{material_id}/impact", response_model=ImpactRead)
async def impact(material_id: str, user: WebUser, runs: Runs):
    return ImpactRead(**await _call(lambda: runs.impact(user.subject, material_id)))  # type: ignore[arg-type]


@router.put("/materials/{material_id}/description", response_model=WebEntryRead)
async def describe(
    material_id: str, payload: DescriptionIn, user: WebUser, service: Library
):
    await _call(
        lambda: service.describe(
            user.subject, material_id, _description(payload), confirm=payload.confirm
        )
    )
    return await _web_entry(service, user.subject, material_id)


@router.put("/materials/{material_id}/license", response_model=WebEntryRead)
async def set_license(
    material_id: str, payload: LicenseIn, user: WebUser, service: Library
):
    await _call(
        lambda: service.set_license(
            user.subject, material_id, payload.license, attest=payload.attest
        )
    )
    return await _web_entry(service, user.subject, material_id)


@router.get("/materials/{material_id}/history", response_model=list[HistoryEntryRead])
async def history(material_id: str, user: WebUser, service: Library):
    return [
        HistoryEntryRead(**h)  # type: ignore[arg-type]
        for h in await _call(lambda: service.history(user.subject, material_id))
    ]


@router.get("/materials/{material_id}/reads", response_model=list[ReadEventRead])
async def reads(material_id: str, user: WebUser, runs: Runs):
    """谁读过：只给所有者看。

    平台运营、系统加工读的带 reader_role；脱敏不泄露买家具体编号。
    """
    raw_events = await _call(lambda: runs.reads(user.subject, material_id))
    sanitized = []
    for e in raw_events:
        item = dict(e)
        if item.get("beneficiary") and item.get("beneficiary") != user.subject:
            item["beneficiary"] = None
        if item.get("reader") and item.get("reader") != user.subject:
            item["reader"] = (
                item.get("reader_role") or item.get("app") or "authorized_reader"
            )
        sanitized.append(item)
    return [ReadEventRead(**e) for e in sanitized]  # type: ignore[arg-type]


# —— 内容版本与读法 ——


@router.get("/materials/{material_id}/versions", response_model=list[VersionRead])
async def versions(material_id: str, user: WebUser, service: Library):
    return [
        VersionRead(
            version_no=v.version_no,
            sha256=v.sha256,
            size_bytes=v.size_bytes,
            media_type=v.media_type,
            created_at=v.created_at,
        )
        for v in await _call(lambda: service.versions(user.subject, material_id))
    ]


@router.post(
    "/materials/{material_id}/versions",
    response_model=WebEntryRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_version(
    material_id: str,
    request: Request,
    user: WebUser,
    service: Library,
    settings: Settings = Depends(get_settings),
    name: str | None = Query(default=None, max_length=200),
    process: bool = False,
    source: str | None = Query(default=None, alias="from", max_length=64),
    ref: str | None = Query(default=None, max_length=256),
):
    content = await _body(request)
    await _call(
        lambda: service.add_version(
            user.subject,
            material_id,
            name,
            content,
            process=process,
            display_to=_display_to(settings, source, ref),
        )
    )
    return await _web_entry(service, user.subject, material_id)


@router.get("/materials/{material_id}/versions/{version_no}/content")
async def download(material_id: str, version_no: int, user: WebUser, service: Library):
    version, content = await _call(
        lambda: service.download(user.subject, ACTING_APP, material_id, version_no)
    )
    return Response(
        content=content,
        media_type=version.media_type,
        headers={
            "X-Content-SHA256": version.sha256,
            "Cache-Control": "no-store",
            "Content-Disposition": "attachment",
        },
    )


@router.get(
    "/materials/{material_id}/versions/{version_no}/renditions",
    response_model=list[RenditionRead],
)
async def renditions(material_id: str, version_no: int, user: WebUser, runs: Runs):
    found = await _call(lambda: runs.renditions(user.subject, material_id, version_no))
    return [_rendition(r) for r in found]


@router.post(
    "/materials/{material_id}/versions/{version_no}/processing",
    response_model=UseRead,
    status_code=status.HTTP_201_CREATED,
)
async def consent_processing(
    material_id: str, version_no: int, user: WebUser, service: Library
):
    """对已传的一版补采用加工（上传时没勾的）。"""
    return _use(
        await _call(
            lambda: service.consent_processing(user.subject, material_id, version_no)
        )
    )


# —— 展示按钮、产出转为数据 ——


@router.get("/materials/{material_id}/uses", response_model=list[UseRead])
async def list_uses(material_id: str, user: WebUser, service: Library):
    return [
        _use(u) for u in await _call(lambda: service.uses(user.subject, material_id))
    ]


@router.post(
    "/materials/{material_id}/uses",
    response_model=UseRead,
    status_code=status.HTTP_201_CREATED,
)
async def show(material_id: str, payload: UseIn, user: WebUser, service: Library):
    """本人点展示按钮：本人在 knowbase 页面上的这一下就是同意依据。"""
    return _use(
        await _call(lambda: service.show(user.subject, material_id, payload.use))
    )


@router.delete("/materials/{material_id}/uses", status_code=status.HTTP_204_NO_CONTENT)
async def hide(
    material_id: str,
    user: WebUser,
    service: Library,
    use: str = Query(min_length=3, max_length=80),
):
    await _call(lambda: service.hide(user.subject, material_id, use))


@router.post("/uses/bulk", response_model=BulkUseRead)
async def show_many(payload: BulkUseIn, user: WebUser, service: Library):
    done = await _call(
        lambda: service.show_many(
            user.subject, payload.material_ids, payload.use, on=payload.on
        )
    )
    return BulkUseRead(done=done)


@router.post(
    "/materials/{material_id}/convert",
    response_model=WebEntryRead,
    status_code=status.HTTP_201_CREATED,
)
async def convert(
    material_id: str, payload: ConvertIn, user: WebUser, runs: Runs, service: Library
):
    """把一份产出转为数据：新建一份资料，带出处，许可不升档（0044 第 2.5 节）。"""
    material, _ = await _call(
        lambda: runs.convert(
            user.subject,
            material_id,
            name=payload.name,
            license_=payload.license,
            attest=payload.attest,
        )
    )
    return await _web_entry(service, user.subject, material.id)


# —— 设置、名下专家 ——


@router.get("/account", response_model=AccountRead)
async def get_account(user: WebUser, service: Library):
    return _account(await _call(lambda: service.account(user.subject)))


@router.put("/account", response_model=AccountRead)
async def acknowledge_account(payload: AccountIn, user: WebUser, service: Library):
    """第一次进来把「新数据默认展示给我的全部应用」亮出来，本人点一下就是依据。"""
    return _account(
        await _call(
            lambda: service.acknowledge_default_display(
                user.subject, payload.default_display
            )
        )
    )


@router.get("/experts", response_model=list[ExpertRead])
async def my_experts(user: WebUser, service: Library):
    return [
        ExpertRead(expert_id=e.expert_id, name=e.name, latest_version=e.latest_version)
        for e in await _call(lambda: service.experts(user.subject))
    ]


# —— 给平台的授权、订阅范围同意（0044 第 2.6 节） ——


@router.get("/platform-consents", response_model=list[PlatformConsentRead])
async def platform_consents(user: WebUser, runs: Runs):
    return [
        PlatformConsentRead(**c)  # type: ignore[arg-type]
        for c in await _call(lambda: runs.platform_overview(user.subject))
    ]


@router.delete(
    "/platform-consents/{app}/{kind}/{ref}", status_code=status.HTTP_204_NO_CONTENT
)
async def withdraw_platform(app: str, kind: str, ref: str, user: WebUser, runs: Runs):
    """本人撤回：依据与凭它签的全部授权立即撤。"""
    await _call(lambda: runs.withdraw_platform(user.subject, app, kind, ref))


@router.get("/subscriptions", response_model=list[SubscriptionRead])
async def subscriptions(user: WebUser, service: Library):
    return [
        _subscription(u)
        for u in await _call(lambda: service.subscriptions(user.subject))
    ]


@router.delete(
    "/subscriptions/{subscription_id}/revisions/{revision}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unsubscribe(
    subscription_id: str, revision: int, user: WebUser, service: Library
):
    """撤订阅范围同意：knowfeed 生成新需求前核这条依据，撤了就不再生成。"""
    await _call(lambda: service.unsubscribe(user.subject, subscription_id, revision))
