from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import (
    knowledge_ingestion_service,
    knowledge_retrieval_service,
)
from app.domain.security import Principal
from app.infrastructure.security.service_auth import (
    require_knowbase_ingest_service,
    require_knowbase_retrieve_service,
)
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.http.internal.host_guard import internal_host_only
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

router = APIRouter(prefix="/knowledge", tags=["知识入库"])
internal_router = APIRouter(
    prefix="/internal/v1/knowledge",
    tags=["内部-知识入库"],
    dependencies=[Depends(internal_host_only)],  # 公网地址来的当没有（审读第 1 条）
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
