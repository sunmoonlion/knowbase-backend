from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.datasets import DatasetRegistry
from app.application.services import (
    knowledge_ingestion_service,
    knowledge_retrieval_service,
)
from app.domain.datasets import (
    DatasetRegistration,
    DatasetVersionConflict,
    InvalidDatasetRegistration,
)
from app.domain.security import Principal
from app.infrastructure.external.dataset_store import DatasetUnavailable, parse_object
from app.infrastructure.repositories.dataset_registry import SqlDatasetRegistry
from app.infrastructure.security.service_auth import (
    require_knowbase_ingest_service,
    require_knowbase_retrieve_service,
)
from app.infrastructure.storage.postgres import get_db_session, get_postgres
from app.interfaces.schemas.datasets import DatasetRead, DatasetRegister
from app.interfaces.schemas.knowledge import (
    KnowledgeIngestionCreate,
    KnowledgeIngestionRead,
    KnowledgeIngestionRetryRequest,
    KnowledgeIngestionStatusUpdate,
    RAGFlowConfigCheckRead,
)
from app.interfaces.schemas.retrieval import (
    KnowledgeRetrievalRequest,
    KnowledgeRetrievalResponse,
)
from core.config import Settings, get_settings

router = APIRouter(prefix="/knowledge", tags=["知识入库"])
internal_router = APIRouter(
    prefix="/internal/v1/knowledge",
    tags=["内部-知识入库"],
)


@router.post(
    "/ingestions",
    response_model=KnowledgeIngestionRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_ingestion(
    payload: KnowledgeIngestionCreate,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await knowledge_ingestion_service.submit_ingestion(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@internal_router.post(
    "/ingestions",
    response_model=KnowledgeIngestionRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_internal_ingestion(
    payload: KnowledgeIngestionCreate,
    service_principal: Principal = Depends(require_knowbase_ingest_service),
    session: AsyncSession = Depends(get_db_session),
):
    # The service principal is verified by the dependency and recorded in the
    # accepted status journal; it is never accepted from the JSON payload.
    try:
        return await knowledge_ingestion_service.submit_ingestion(
            session, payload, service_principal=service_principal
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@internal_router.post(
    "/retrievals",
    response_model=KnowledgeRetrievalResponse,
    status_code=status.HTTP_200_OK,
)
async def retrieve_internal_knowledge(
    payload: KnowledgeRetrievalRequest,
    service_principal: Principal = Depends(require_knowbase_retrieve_service),
    session: AsyncSession = Depends(get_db_session),
):
    return await knowledge_retrieval_service.retrieve_knowledge(
        session,
        payload,
        service_principal=service_principal,
    )


@router.get("/ingestions", response_model=list[KnowledgeIngestionRead])
async def list_ingestions(
    source_app: str | None = Query(default=None),
    source_document_id: uuid.UUID | None = Query(default=None),
    source_document_version_id: uuid.UUID | None = Query(default=None),
    target_dataset: str | None = Query(default=None),
    ingestion_status: str | None = Query(default=None, alias="status"),
    ragflow_document_id: str | None = Query(default=None),
    idempotency_key: str | None = Query(default=None),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
):
    return await knowledge_ingestion_service.list_ingestion_jobs(
        session,
        source_app=source_app,
        source_document_id=source_document_id,
        source_document_version_id=source_document_version_id,
        target_dataset=target_dataset,
        status=ingestion_status,
        ragflow_document_id=ragflow_document_id,
        idempotency_key=idempotency_key,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )


@router.get("/ragflow/config-check", response_model=RAGFlowConfigCheckRead)
async def check_ragflow_config():
    return await knowledge_ingestion_service.get_ragflow_config_check()


@router.get("/ingestions/{ingestion_id}", response_model=KnowledgeIngestionRead)
async def get_ingestion(
    ingestion_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    job = await knowledge_ingestion_service.get_ingestion_job(session, ingestion_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ingestion job not found")
    return job


@router.post("/ingestions/{ingestion_id}/status", response_model=KnowledgeIngestionRead)
async def update_ingestion_status(
    ingestion_id: uuid.UUID,
    payload: KnowledgeIngestionStatusUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await knowledge_ingestion_service.update_ingestion_status(
            session,
            ingestion_id=ingestion_id,
            status=payload.status,
            last_error=payload.last_error,
            metadata=payload.metadata,
            knowledge_document_id=payload.knowledge_document_id,
            ragflow_document_id=payload.ragflow_document_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/ingestions/{ingestion_id}/dispatch", response_model=KnowledgeIngestionRead
)
async def dispatch_ingestion(
    ingestion_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    try:
        return await knowledge_ingestion_service.request_ingestion_job(
            session, ingestion_id=ingestion_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/ingestions/{ingestion_id}/retry", response_model=KnowledgeIngestionRead)
async def retry_ingestion(
    ingestion_id: uuid.UUID,
    payload: KnowledgeIngestionRetryRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        job = await knowledge_ingestion_service.retry_ingestion_job(
            session,
            ingestion_id=ingestion_id,
            force=payload.force,
            reason=payload.reason,
        )
    except ValueError as exc:
        message = str(exc)
        if "not found" in message:
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=409, detail=message) from exc

    return job


def get_dataset_registry() -> DatasetRegistry:
    return SqlDatasetRegistry(lambda: get_postgres().session_factory)


@internal_router.post(
    "/datasets", response_model=DatasetRead, status_code=status.HTTP_201_CREATED
)
async def register_dataset(
    payload: DatasetRegister,
    service_principal: Principal = Depends(require_knowbase_ingest_service),
    registry: DatasetRegistry = Depends(get_dataset_registry),
    settings: Settings = Depends(get_settings),
):
    """登记数据集的一个版本，并使它成为现行版本（0008-info 段三）。

    只接受来源方声明已通过质量检查的数据集；文件不经这里上传，知识服务按登记的
    位置与校验值自己去取，取到的内容对不上就不用。
    """
    if not settings.knowbase_dataset_registry_enabled:
        raise HTTPException(status_code=404, detail="dataset registry is disabled")
    if not payload.quality_passed:
        raise HTTPException(
            status_code=422, detail="only datasets that passed quality checks"
        )
    try:
        bucket, key = parse_object(payload.object)
        registration = DatasetRegistration(
            dataset_id=payload.dataset_id,
            data_version=payload.data_version,
            title=payload.title,
            bucket=bucket,
            object_key=key,
            object_version_id=payload.object_version_id,
            sha256=payload.sha256,
            size_bytes=payload.size_bytes,
            start_date=payload.start_date,
            end_date=payload.end_date,
            source_app=payload.source_app,
            source_ref=payload.source_ref,
            security_code=payload.security_code,
        )
        registration.validate(
            allowed_buckets=frozenset(
                b.strip()
                for b in settings.knowbase_dataset_allowed_buckets.split(",")
                if b.strip()
            )
        )
        if registration.dataset_id == settings.knowbase_dataset_id:
            raise InvalidDatasetRegistration(
                "dataset_id is reserved for the default dataset"
            )
    except (InvalidDatasetRegistration, DatasetUnavailable) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        # 登记人取自已验证的服务身份，不取自请求体
        return await registry.register(
            registration, registered_by=service_principal.subject
        )
    except DatasetVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@internal_router.get("/datasets", response_model=list[DatasetRead])
async def list_registered_datasets(
    service_principal: Principal = Depends(require_knowbase_ingest_service),
    registry: DatasetRegistry = Depends(get_dataset_registry),
    settings: Settings = Depends(get_settings),
):
    if not settings.knowbase_dataset_registry_enabled:
        raise HTTPException(status_code=404, detail="dataset registry is disabled")
    return await registry.active()
