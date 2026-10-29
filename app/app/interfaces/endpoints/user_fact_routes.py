"""事实的内部接口 `user-data/v2`（SDD 0047 第四节）：写回、按运行查、跨全市场查、指标。

- knowfeed（`user-data-supply`）凭加工授权写回抽好的表；
- 执行的应用（`user-data-read`）查这次运行钉住的那几份；
- 本人（用户令牌，应用转交）跨全市场查公开原件的事实，平台总开关管。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.application.services.user_run_service import UserRunService
from app.domain.chart_of_accounts import METRICS
from app.domain.chart_of_accounts import snapshot as chart_snapshot
from app.domain.user_data import Owner
from app.domain.user_facts import FactQuery, FactRow, MetricRow, RawFact, StandardFact
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_supply_service,
)
from app.infrastructure.security.user_token import require_user_owner
from app.interfaces.endpoints.user_data_errors import call as _call
from app.interfaces.endpoints.user_run_routes import (
    _rendition,
    get_user_run_service,
    require_executor,
)
from app.interfaces.http.internal.host_guard import internal_host_only
from app.interfaces.schemas.user_facts import (
    ChartRead,
    FactQueryIn,
    FactRowRead,
    FactsIn,
    MetricQueryIn,
    MetricRowRead,
    RunFactsIn,
    RunMetricIn,
)
from app.interfaces.schemas.user_runs import RenditionRead

router = APIRouter(
    prefix="/internal/v2/user-data",
    tags=["内部-资料库-事实"],
    dependencies=[Depends(internal_host_only)],
)


def _query(q: FactQueryIn) -> FactQuery:
    return FactQuery(
        layer=q.layer,
        subjects=tuple(q.subjects),
        codes=tuple(q.codes),
        labels=tuple(q.labels),
        period_from=q.period_from,
        period_to=q.period_to,
        duration=q.duration,
        consolidation=q.consolidation,
        value_min=q.value_min,
        value_max=q.value_max,
        order=q.order,
        limit=q.limit,
    )


def _row(r: FactRow) -> FactRowRead:
    return FactRowRead(**r.__dict__)


def _metric(m: MetricRow) -> MetricRowRead:
    return MetricRowRead(
        subject=m.subject,
        period_end=m.period_end,
        duration=m.duration,
        consolidation=m.consolidation,
        value=m.value,
        inputs=[_row(i) for i in m.inputs],
    )


@router.get("/chart", response_model=ChartRead)
async def chart(owner: Owner = Depends(require_user_owner)):
    """科目表与指标定义（编码、中文名、属于哪张表、适用哪类公司）。"""
    data = chart_snapshot()
    return ChartRead(
        **data,  # type: ignore[arg-type]
        metrics=[
            {"key": m.key, "name": m.name, "kind": m.kind, "description": m.description}
            for m in METRICS.values()
        ],
    )


@router.post(
    "/processing/grants/{grant_id}/facts",
    response_model=RenditionRead,
    status_code=status.HTTP_201_CREATED,
)
async def write_facts(
    grant_id: str,
    payload: FactsIn,
    caller: ServiceCaller = Depends(require_supply_service),
    service: UserRunService = Depends(get_user_run_service),
):
    """knowfeed 写回抽好的表：原样层与标准化层一批，恒等式质检记在读法上。"""
    raw = [RawFact(**r.model_dump()) for r in payload.raw]
    standard = [
        StandardFact(**{**s.model_dump(), "derived_from": tuple(s.derived_from)})
        for s in payload.standard
    ]
    rendition = await _call(
        lambda: service.write_facts(
            caller.caller, grant_id, payload.processor_version, raw, standard
        )
    )
    return _rendition(rendition)


@router.post("/run-facts", response_model=list[FactRowRead])
async def run_facts(
    payload: RunFactsIn,
    executor: tuple[str, str] = Depends(require_executor),
    service: UserRunService = Depends(get_user_run_service),
):
    """执行的应用查这次运行钉住的那几份的事实（每份留一条读数）。"""
    reader, app = executor
    rows = await _call(
        lambda: service.query_facts(
            app, reader, payload.beneficiary, payload.run_ref, _query(payload)
        )
    )
    return [_row(r) for r in rows]


@router.post("/run-metrics", response_model=list[MetricRowRead])
async def run_metrics(
    payload: RunMetricIn,
    executor: tuple[str, str] = Depends(require_executor),
    service: UserRunService = Depends(get_user_run_service),
):
    reader, app = executor
    rows = await _call(
        lambda: service.query_metric(
            payload.metric,
            _query(payload),
            executor_app=app,
            reader=reader,
            beneficiary=payload.beneficiary,
            run_ref=payload.run_ref,
        )
    )
    return [_metric(m) for m in rows]


@router.post("/market-facts", response_model=list[FactRowRead])
async def market_facts(
    payload: FactQueryIn,
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
):
    """跨全市场查公开原件的事实（筛选、排序、排名），条件由用户给；过资格、权限、
    平台总开关那道门（SDD 0047 第 4.4 节）。"""
    rows = await _call(lambda: service.query_market(owner.subject, _query(payload)))
    return [_row(r) for r in rows]


@router.post("/market-metrics", response_model=list[MetricRowRead])
async def market_metrics(
    payload: MetricQueryIn,
    owner: Owner = Depends(require_user_owner),
    service: UserRunService = Depends(get_user_run_service),
):
    rows = await _call(
        lambda: service.query_metric(
            payload.metric, _query(payload), subject=owner.subject
        )
    )
    return [_metric(m) for m in rows]
