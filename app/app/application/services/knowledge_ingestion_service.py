from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.knowledge import KnowledgeIngestionCreate
from app.application.errors.exceptions import ForbiddenError
from app.application.ports.knowledge_provider import (
    ProviderError,
    ProviderIngestionResult,
    ProviderParseCancelledError,
    ProviderParseError,
)
from app.application.services.durable_tasks import enqueue_task
from app.application.services.ingestion_authorization import (
    SNAPSHOT_KEY,
    binding_snapshot,
    require_job_binding,
)
from app.application.services.ingestion_execution import (
    EXECUTION_KEY,
    EXECUTION_MARKER,
    IngestionExecution,
    database_now,
    execution_state,
    guard_generation,
    save_execution,
)
from app.application.services.provider_delivery import (
    ProviderOutcomeUnknown,
    ingest_with_receipts,
    upload_identity,
)
from app.domain.security import Principal
from app.infrastructure.external.artifact_content import resolve_artifact_content
from app.infrastructure.external.knowledge_provider import provider_definition
from app.infrastructure.external.ragflow import check_ragflow_config
from app.infrastructure.models.knowledge import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
)
from core.config import get_settings

TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "ragflow_config_error",
    "ragflow_parse_failed",
    "artifact_unreadable",
    "external_api_error",
    "artifact_verified",
    "legacy_binding_missing",
}
RETRY_BLOCKED_STATUSES = {"ragflow_config_error"}
PROCESSOR_NAME = "artifact-contract-verifier"
RAGFLOW_PROCESSOR_NAME = "ragflow-ingestion-worker"
KNOWLEDGE_DOCUMENT_NAMESPACE = uuid.UUID("2d1540e8-d8b2-44b3-8da2-55f6c3027334")
KNOWLEDGE_VERSION_NAMESPACE = uuid.UUID("9987a94c-9635-4124-b65e-738301c97831")


def build_idempotency_key(payload: KnowledgeIngestionCreate) -> str:
    return payload.idempotency_key


def _status_entry(
    status: str,
    *,
    last_error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "status": status,
        "at": datetime.now(UTC).isoformat(),
    }
    if last_error:
        entry["last_error"] = last_error
    if metadata:
        entry["metadata"] = metadata
    return entry


def _payload_dict(
    payload: KnowledgeIngestionCreate, idempotency_key: str
) -> dict[str, Any]:
    result = payload.model_dump(mode="json", exclude_none=True)
    result["idempotency_key"] = idempotency_key
    return result


async def submit_ingestion(
    session: AsyncSession,
    payload: KnowledgeIngestionCreate,
    *,
    service_principal: Principal | None = None,
) -> KnowledgeIngestionJob:
    idempotency_key = build_idempotency_key(payload)
    if EXECUTION_KEY in payload.document.metadata:
        raise ForbiddenError("document metadata cannot set ingestion execution state")

    accepted_metadata: dict[str, Any] = {
        SNAPSHOT_KEY: binding_snapshot(get_settings(), payload.dataset_key),
        EXECUTION_KEY: EXECUTION_MARKER,
    }
    if service_principal is not None:
        accepted_metadata.update(
            {
                "service_principal": {
                    "actor_type": service_principal.actor_type,
                    "subject": service_principal.subject,
                    "issuer": service_principal.issuer,
                    "audience": service_principal.audience,
                    "app": service_principal.app,
                    "surface": service_principal.surface,
                    "scopes": sorted(service_principal.scopes),
                    "policy_version": service_principal.policy_version,
                }
            }
        )

    statement = insert(KnowledgeIngestionJob).values(
        source_app=payload.source_app,
        source_document_id=payload.source_document_id,
        source_document_version_id=payload.source_document_version_id,
        target_dataset=payload.dataset_key,
        profile_key="markdown",
        idempotency_key=idempotency_key,
        title=payload.document.title,
        canonical_url=payload.document.canonical_url,
        source_name=payload.document.source_name,
        content_hash=payload.document.content_hash,
        source_artifact_refs=[payload.artifact.model_dump(mode="json")],
        metadata_json={
            **payload.document.metadata,
            EXECUTION_KEY: IngestionExecution().model_dump(mode="json"),
        },
        payload=_payload_dict(payload, idempotency_key),
        status="accepted",
        status_history=[_status_entry("accepted", metadata=accepted_metadata)],
    )
    await session.execute(
        statement.on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    job = await get_ingestion_by_idempotency_key(session, idempotency_key)
    if job is None:
        raise RuntimeError("ingestion acceptance did not persist")
    if job.payload != _payload_dict(payload, idempotency_key):
        raise ValueError("idempotency key already represents another ingestion intent")
    require_job_binding(job, get_settings())
    if job.status not in TERMINAL_STATUSES:
        await _enqueue_ingestion(session, job)
    await session.commit()
    await session.refresh(job)
    return job


async def _enqueue_ingestion(session: AsyncSession, job: KnowledgeIngestionJob) -> None:
    require_job_binding(job, get_settings())
    generation = execution_state(job).generation
    await enqueue_task(
        session,
        topic="knowledge.ingest.v1",
        key=str(upload_identity(job)),
        payload={"ingestion_id": str(job.id), "generation": generation, "step": 0},
        deduplication_key=f"knowbase:ingest:{job.id}:{generation}",
    )


async def request_ingestion_job(
    session: AsyncSession, *, ingestion_id: uuid.UUID
) -> KnowledgeIngestionJob:
    job = (
        await session.execute(
            select(KnowledgeIngestionJob)
            .where(KnowledgeIngestionJob.id == ingestion_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        raise ValueError(f"ingestion job not found: {ingestion_id}")
    if job.status not in TERMINAL_STATUSES:
        await _enqueue_ingestion(session, job)
    await session.commit()
    await session.refresh(job)
    return job


async def get_ingestion_job(
    session: AsyncSession, ingestion_id: uuid.UUID
) -> KnowledgeIngestionJob | None:
    return await session.get(KnowledgeIngestionJob, ingestion_id)


async def get_ingestion_by_idempotency_key(
    session: AsyncSession, idempotency_key: str
) -> KnowledgeIngestionJob | None:
    result = await session.execute(
        select(KnowledgeIngestionJob)
        .where(KnowledgeIngestionJob.idempotency_key == idempotency_key)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_ingestion_jobs(
    session: AsyncSession,
    *,
    source_app: str | None,
    source_document_id: uuid.UUID | None,
    source_document_version_id: uuid.UUID | None,
    target_dataset: str | None,
    status: str | None,
    ragflow_document_id: str | None,
    idempotency_key: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    limit: int,
    offset: int,
) -> list[KnowledgeIngestionJob]:
    query = select(KnowledgeIngestionJob)
    if source_app:
        query = query.where(KnowledgeIngestionJob.source_app == source_app)
    if source_document_id:
        query = query.where(
            KnowledgeIngestionJob.source_document_id == source_document_id
        )
    if source_document_version_id:
        query = query.where(
            KnowledgeIngestionJob.source_document_version_id
            == source_document_version_id
        )
    if target_dataset:
        query = query.where(KnowledgeIngestionJob.target_dataset == target_dataset)
    if status:
        query = query.where(KnowledgeIngestionJob.status == status)
    if ragflow_document_id:
        query = query.where(
            KnowledgeIngestionJob.ragflow_document_id == ragflow_document_id
        )
    if idempotency_key:
        query = query.where(KnowledgeIngestionJob.idempotency_key == idempotency_key)
    if created_from:
        query = query.where(KnowledgeIngestionJob.created_at >= created_from)
    if created_to:
        query = query.where(KnowledgeIngestionJob.created_at <= created_to)
    query = (
        query.order_by(KnowledgeIngestionJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_ingestion_status(
    session: AsyncSession,
    *,
    ingestion_id: uuid.UUID,
    status: str,
    last_error: str | None,
    metadata: dict,
    knowledge_document_id: str | None,
    ragflow_document_id: str | None,
    commit: bool = True,
) -> KnowledgeIngestionJob:
    if SNAPSHOT_KEY in metadata or EXECUTION_KEY in metadata:
        # Admin status metadata must not manufacture an acceptance snapshot,
        # including for legacy jobs whose status_history is empty.
        raise ForbiddenError(
            "status updates cannot set an ingestion binding or execution state"
        )
    job = await get_ingestion_job(session, ingestion_id)
    if job is None:
        raise ValueError(f"ingestion job not found: {ingestion_id}")

    history = list(job.status_history or [])
    history.append(_status_entry(status, last_error=last_error, metadata=metadata))
    job.status = status
    job.last_error = last_error
    job.status_history = history
    if knowledge_document_id is not None:
        job.knowledge_document_id = knowledge_document_id
    if ragflow_document_id is not None:
        job.ragflow_document_id = ragflow_document_id
    if status in TERMINAL_STATUSES:
        job.completed_at = datetime.now(UTC)

    if commit:
        await session.commit()
        await session.refresh(job)
    return job


def stable_knowledge_document_id(job: KnowledgeIngestionJob) -> uuid.UUID:
    return uuid.uuid5(
        KNOWLEDGE_DOCUMENT_NAMESPACE,
        f"{job.source_app}:{job.source_document_id}:{job.target_dataset or 'default'}",
    )


def stable_knowledge_version_id(
    knowledge_document_id: uuid.UUID,
    source_document_version_id: uuid.UUID,
) -> uuid.UUID:
    return uuid.uuid5(
        KNOWLEDGE_VERSION_NAMESPACE,
        f"{knowledge_document_id}:{source_document_version_id}",
    )


async def complete_ragflow_ingestion(
    session: AsyncSession,
    *,
    job: KnowledgeIngestionJob,
    result: ProviderIngestionResult,
    commit: bool = True,
) -> KnowledgeIngestionJob:
    """Commit the domain identity, provider binding and terminal job atomically."""

    settings = get_settings()
    definition = provider_definition(settings)
    if result.provider != definition.name:
        raise ForbiddenError("provider result does not match configured provider")
    if definition.name != "ragflow":
        raise ForbiddenError(
            "legacy ingestion projection does not support this provider"
        )
    dataset_key = job.target_dataset or "default"
    binding = require_job_binding(job, settings)
    if (
        result.dataset_id != binding.dataset_id
        or result.dataset_name != binding.dataset_name
    ):
        raise ForbiddenError("provider result does not match ingestion binding")
    document_id = stable_knowledge_document_id(job)
    version_id = stable_knowledge_version_id(
        document_id, job.source_document_version_id
    )
    now = datetime.now(UTC)

    await session.execute(
        insert(KnowledgeDocument)
        .values(
            id=document_id,
            source_app=job.source_app,
            source_document_id=job.source_document_id,
            dataset_key=dataset_key,
            status="active",
        )
        .on_conflict_do_nothing(
            index_elements=["source_app", "source_document_id", "dataset_key"]
        )
    )
    await session.execute(
        insert(KnowledgeDocumentVersion)
        .values(
            id=version_id,
            knowledge_document_id=document_id,
            source_app=job.source_app,
            source_document_id=job.source_document_id,
            source_document_version_id=job.source_document_version_id,
            ingestion_id=job.id,
            dataset_key=dataset_key,
            content_hash=job.content_hash or "0" * 64,
            title=job.title,
            source_uri=job.canonical_url,
            source_name=job.source_name,
            access_scope=[f"tenant:{settings.retrieval_default_tenant_id}"],
            status="indexed",
            provider=definition.name,
            provider_dataset_id=result.dataset_id,
            provider_document_id=result.document_id,
            indexed_at=now,
        )
        .on_conflict_do_update(
            index_elements=["knowledge_document_id", "source_document_version_id"],
            set_={
                "ingestion_id": job.id,
                "content_hash": job.content_hash or "0" * 64,
                "title": job.title,
                "source_uri": job.canonical_url,
                "source_name": job.source_name,
                "access_scope": [f"tenant:{settings.retrieval_default_tenant_id}"],
                "status": "indexed",
                "provider": definition.name,
                "provider_dataset_id": result.dataset_id,
                "provider_document_id": result.document_id,
                "indexed_at": now,
                "updated_at": now,
            },
        )
    )

    history = list(job.status_history or [])
    history.append(
        _status_entry(
            "succeeded",
            metadata=build_ragflow_success_metadata(job, result.metadata),
        )
    )
    job.status = "succeeded"
    job.last_error = None
    job.status_history = history
    job.knowledge_document_id = str(document_id)
    job.ragflow_document_id = result.document_id
    job.completed_at = now
    if commit:
        await session.commit()
        await session.refresh(job)
    return job


def classify_ingestion_error(exc: BaseException) -> tuple[str, dict[str, Any]]:
    message = str(exc)
    message_lower = message.lower()
    if any(
        marker in message_lower
        for marker in (
            "artifact",
            "s3 object",
            "s3 endpoint",
            "bucket",
            "object key",
            "sha256",
            "content type",
            "storage version",
        )
    ):
        return "artifact_unreadable", {"error_type": "artifact_unreadable"}
    if "no default embedding model" in message_lower:
        return "ragflow_config_error", {"error_type": "ragflow_config_error"}
    if isinstance(exc, ProviderParseCancelledError):
        # 取消与失败同属可重试的 ragflow_parse_failed，但 error_type 区分开：
        # 取消多为运维动作或 RAGFlow 侧重启，排查方向与解析失败不同。
        return "ragflow_parse_failed", {"error_type": "ragflow_parse_cancelled"}
    if isinstance(exc, ProviderParseError) or (
        "parse timed out" in message_lower or "parse failed" in message_lower
    ):
        return "ragflow_parse_failed", {"error_type": "ragflow_parse_failed"}
    if isinstance(exc, ProviderError):
        return "external_api_error", {
            "error_type": "external_api_error",
            "system": "ragflow",
        }
    if isinstance(exc, OSError):
        return "external_api_error", {"error_type": "external_api_error"}
    return "failed", {"error_type": "unknown"}


async def retry_ingestion_job(
    session: AsyncSession,
    *,
    ingestion_id: uuid.UUID,
    force: bool = False,
    reason: str | None = None,
) -> KnowledgeIngestionJob:
    job = (
        await session.execute(
            select(KnowledgeIngestionJob)
            .where(KnowledgeIngestionJob.id == ingestion_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        raise ValueError(f"ingestion job not found: {ingestion_id}")
    if job.status not in TERMINAL_STATUSES:
        raise ValueError(f"ingestion job is not terminal: {job.status}")
    require_job_binding(job, get_settings())
    if job.status == "succeeded":
        raise ValueError("succeeded ingestion job cannot be retried")
    if job.status in RETRY_BLOCKED_STATUSES and not force:
        raise ValueError(
            f"retry blocked for {job.status}; fix configuration first or force retry"
        )

    metadata_json = dict(job.metadata_json or {})
    # Legacy display metadata is client-extensible, not the execution authority.
    stored_history = metadata_json.get("retry_history")
    retry_history = list(stored_history) if isinstance(stored_history, list) else []
    previous = execution_state(job)
    retry_count = previous.generation + 1
    retry_entry = {
        "attempt": retry_count,
        "at": datetime.now(UTC).isoformat(),
        "from_status": job.status,
        "force": force,
    }
    if reason:
        retry_entry["reason"] = reason
    retry_history.append(retry_entry)
    metadata_json["retry_count"] = retry_count
    metadata_json["retry_history"] = retry_history
    metadata_json["last_retry_at"] = retry_entry["at"]

    history = list(job.status_history or [])
    history.append(
        _status_entry(
            "accepted",
            metadata={
                "retry": True,
                "attempt": retry_count,
                "from_status": job.status,
                "force": force,
                **({"reason": reason} if reason else {}),
            },
        )
    )
    job.status = "accepted"
    job.last_error = None
    job.completed_at = None
    job.status_history = history
    job.metadata_json = metadata_json
    save_execution(
        job, IngestionExecution(generation=retry_count, upload=previous.upload)
    )
    await _enqueue_ingestion(session, job)
    await session.commit()
    await session.refresh(job)
    return job


async def get_ragflow_config_check() -> dict[str, Any]:
    result = await check_ragflow_config(get_settings())
    return {
        "enabled": result.enabled,
        "reachable": result.reachable,
        "has_default_embedding": result.has_default_embedding,
        "ready": result.enabled and result.reachable and result.has_default_embedding,
        "issues": result.issues,
        "details": result.details,
    }


def build_processing_success_metadata(job: KnowledgeIngestionJob) -> dict[str, Any]:
    return {
        "processor": PROCESSOR_NAME,
        "mode": "artifact_verification",
        "artifact_ref_count": len(job.source_artifact_refs or []),
        "ragflow": "not_requested",
    }


def build_ragflow_success_metadata(
    job: KnowledgeIngestionJob, ragflow_metadata: dict[str, Any]
) -> dict[str, Any]:
    return {
        "processor": RAGFLOW_PROCESSOR_NAME,
        "mode": "ragflow",
        "artifact_ref_count": len(job.source_artifact_refs or []),
        "ragflow": "processed",
        **ragflow_metadata,
    }


async def process_ingestion_job(
    session: AsyncSession, *, ingestion_id: uuid.UUID, generation: int, step: int
) -> KnowledgeIngestionJob:
    if "delivery_lease" not in session.info:
        raise RuntimeError("ingestion requires the durable consumer lease")
    job = await get_ingestion_job(session, ingestion_id)
    if job is None:
        raise ValueError(f"ingestion job not found: {ingestion_id}")
    if (
        session.info["delivery_lease"].resource_key
        != f"knowledge.ingest.v1:{upload_identity(job)}"
    ):
        raise ValueError("ingestion command resource does not match the job")
    if job.status in TERMINAL_STATUSES:
        return job

    state = execution_state(job)
    if generation < state.generation or (
        generation == state.generation and step < state.step
    ):
        return job  # Acknowledge obsolete messages, never mutate the current attempt.
    if generation != state.generation or step != state.step:
        raise ValueError("ingestion command is ahead of the persisted execution cursor")
    guard_generation(session, job, generation)

    settings = get_settings()
    processor_name = (
        RAGFLOW_PROCESSOR_NAME
        if provider_definition(settings).enabled
        else PROCESSOR_NAME
    )
    require_job_binding(job, settings)
    if state.upload is not None and not provider_definition(settings).enabled:
        raise ForbiddenError("active parse cannot switch to artifact-only execution")
    if job.status != "running":
        job = await update_ingestion_status(
            session,
            ingestion_id=ingestion_id,
            status="running",
            last_error=None,
            metadata={"processor": processor_name},
            knowledge_document_id=None,
            ragflow_document_id=None,
            commit=True,
        )

    if provider_definition(settings).enabled:
        try:
            result = await ingest_with_receipts(session, job=job, settings=settings)
        except ProviderOutcomeUnknown:
            await update_ingestion_status(
                session,
                ingestion_id=ingestion_id,
                status="reconciliation_required",
                last_error="provider_outcome_unknown",
                metadata={
                    "processor": RAGFLOW_PROCESSOR_NAME,
                    "error_type": "provider_outcome_unknown",
                },
                knowledge_document_id=None,
                ragflow_document_id=None,
            )
            raise
        except (ProviderError, OSError, ValueError) as exc:
            failure_status, failure_metadata = classify_ingestion_error(exc)
            return await update_ingestion_status(
                session,
                ingestion_id=ingestion_id,
                status=failure_status,
                last_error=str(exc),
                metadata={
                    "processor": RAGFLOW_PROCESSOR_NAME,
                    "mode": "ragflow",
                    **failure_metadata,
                },
                knowledge_document_id=None,
                ragflow_document_id=None,
                commit=False,
            )
        if result is None:
            return await _schedule_parse_poll(session, job)
        return await complete_ragflow_ingestion(
            session, job=job, result=result, commit=False
        )

    try:
        artifact = await resolve_artifact_content(
            settings=settings,
            source_artifact_refs=list(job.source_artifact_refs or []),
            title=job.title,
            canonical_url=job.canonical_url,
            metadata_json=dict(job.metadata_json or {}),
            source_document_version_id=str(job.source_document_version_id),
        )
    except (ProviderError, OSError, ValueError) as exc:
        failure_status, failure_metadata = classify_ingestion_error(exc)
        return await update_ingestion_status(
            session,
            ingestion_id=ingestion_id,
            status=failure_status,
            last_error=str(exc),
            metadata={"processor": PROCESSOR_NAME, **failure_metadata},
            knowledge_document_id=None,
            ragflow_document_id=None,
            commit=False,
        )
    return await update_ingestion_status(
        session,
        ingestion_id=ingestion_id,
        status="artifact_verified",
        last_error=None,
        metadata={
            **build_processing_success_metadata(job),
            "verified_size_bytes": len(artifact.content),
            "verified_content_type": artifact.content_type,
        },
        knowledge_document_id=None,
        ragflow_document_id=None,
        commit=False,
    )


async def _schedule_parse_poll(session: AsyncSession, job: KnowledgeIngestionJob):
    """Transport failures must propagate, not become acknowledged provider errors."""
    state = execution_state(job)
    now = await database_now(session)
    if state.deadline is None:
        raise RuntimeError("parse scheduling requires a persisted deadline")
    if now >= state.deadline:
        return await update_ingestion_status(
            session,
            ingestion_id=job.id,
            status="ragflow_parse_failed",
            last_error="RAGFlow parse timed out",
            metadata={
                "processor": RAGFLOW_PROCESSOR_NAME,
                "error_type": "ragflow_parse_failed",
            },
            knowledge_document_id=None,
            ragflow_document_id=None,
            commit=False,
        )
    delay = min(60, state.interval * 2 ** min(state.step, 16))
    when = min(state.deadline, now + timedelta(seconds=delay))
    following = state.model_copy(update={"step": state.step + 1, "ready_at": when})
    save_execution(job, following)
    await enqueue_task(
        session,
        topic="knowledge.ingest.v1",
        key=str(upload_identity(job)),
        payload={
            "ingestion_id": str(job.id),
            "generation": state.generation,
            "step": following.step,
        },
        deduplication_key=f"knowledge:poll:{job.id}:{state.generation}:{following.step}",
        not_before=when,
    )
    return job  # Runtime commits cursor + successor + Inbox together.
