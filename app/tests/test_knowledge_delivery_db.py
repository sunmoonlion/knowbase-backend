"""Real DB transactions and simulated provider response-loss/fencing faults."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_knowledge_ingestion import _contract_payload

from app.application.dto.knowledge import KnowledgeIngestionCreate
from app.application.ports.knowledge_provider import ProviderDataset, ProviderError
from app.application.services import knowledge_ingestion_service as service
from app.application.services import provider_delivery as provider
from app.infrastructure.external.ragflow import ArtifactContent, RAGFlowError
from app.infrastructure.external.ragflow_provider import RAGFlowProvider
from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
from app.infrastructure.messaging.durable_delivery import DeliveryLeaseLost
from app.infrastructure.messaging.durable_tasks import DurableTasks
from app.infrastructure.models.knowledge import (
    KnowledgeIngestionJob,
    KnowledgeProviderOperation,
)
from core.config import Settings

CONTENT = b"# durable knowledge"
ROOT = Path(__file__).resolve().parents[1]


def authorized_settings(**kwargs):
    return Settings(
        INGESTION_DATASET_BINDINGS=json.dumps(
            {
                "market-news": {
                    "dataset_id": "dataset-1",
                    "dataset_name": "market-news",
                },
                "different": {"dataset_id": "dataset-2", "dataset_name": "different"},
            }
        ),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def ingestion_policy(monkeypatch):
    monkeypatch.setattr(service, "get_settings", authorized_settings)


def payload():
    raw = _contract_payload()
    raw["artifact"]["sha256"] = hashlib.sha256(CONTENT).hexdigest()
    raw["artifact"]["size_bytes"] = len(CONTENT)
    return KnowledgeIngestionCreate.model_validate(raw)


async def submit(db, request=None):
    async with db() as session:
        job = await service.submit_ingestion(session, request or payload())
        return job.id


class Provider:
    def __init__(self, fault=None):
        self.fault = fault
        self.datasets = [{"id": "dataset-1", "name": "market-news"}]
        self.documents = []
        self.creates = self.uploads = self.parses = 0
        self.hide_upload = False
        self.bytes_valid = True

    async def close(self):
        pass

    async def get_tenant_models(self):
        return {"tenant_id": "test-tenant"}

    async def find_datasets(self, name):
        return [dict(d) for d in self.datasets if d["name"] == name]

    async def create_dataset(self, name):
        self.creates += 1
        result = {"id": "dataset-1", "name": name}
        self.datasets.append(result)
        self.lose("dataset")
        return dict(result)

    async def find_documents(self, dataset_id, filename):
        return (
            []
            if self.hide_upload
            else [
                dict(d)
                for d in self.documents
                if d["dataset_id"] == dataset_id and d["name"] == filename
            ]
        )

    async def upload_document(self, dataset_id, artifact):
        self.uploads += 1
        result = {
            "id": "document-1",
            "dataset_id": dataset_id,
            "name": artifact.filename,
            "run": "UNSTART",
        }
        self.documents.append(result)
        self.lose("upload")
        return dict(result)

    async def get_document(self, dataset_id, document_id):
        return dict(next(d for d in self.documents if d["id"] == document_id))

    async def verify_document_content(self, dataset_id, document_id, *, size, sha256):
        assert size == len(CONTENT)
        assert sha256 == hashlib.sha256(CONTENT).hexdigest()
        if not self.bytes_valid:
            raise RAGFlowError("remote document content does not match source version")

    async def parse_document(self, dataset_id, document_id):
        self.parses += 1
        self.documents[0]["run"] = "DONE"
        self.lose("parse")

    def lose(self, phase):
        if self.fault == phase:
            self.fault = None
            raise RAGFlowError("response lost after provider committed")


def configure(monkeypatch, fake):
    settings = authorized_settings(
        RAGFLOW_API_BASE="https://provider.example.test",
        RAGFLOW_API_KEY="test-only",
        RAGFLOW_PARSE_TIMEOUT_SECONDS=1,
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        provider,
        "create_provider",
        lambda settings: RAGFlowProvider(settings, client=fake),
    )

    async def artifact(**kwargs):
        return ArtifactContent("clean.md", CONTENT, "text/markdown")

    monkeypatch.setattr(provider, "resolve_artifact_content", artifact)


async def message(db):
    return await sql(
        db, "SELECT id FROM outbox_message WHERE topic='knowledge.ingest.v1'"
    )


async def test_concurrent_acceptance_and_changed_intent(db):
    request = payload()
    ids = await asyncio.gather(*(submit(db, request) for _ in range(5)))
    assert len(set(ids)) == 1
    assert await sql(db, "SELECT count(*) FROM knowledge_ingestion_job") == 1
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 1
    changed = request.model_copy(update={"dataset_key": "different"})
    with pytest.raises(ValueError, match="another ingestion intent"):
        await submit(db, changed)


async def test_operator_recovery_verifies_receipt_without_remote_writes(
    db, monkeypatch
):
    fake = Provider("upload")
    configure(monkeypatch, fake)
    job_id = await submit(db)
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    mid = await message(db)
    with pytest.raises(provider.ProviderOutcomeUnknown):
        await runtime.consume(mid)
    fake.hide_upload = True
    async with db() as session:
        job = await service.get_ingestion_job(session, job_id)
        document = await provider.recover_upload_receipt(
            session, job=job, settings=service.get_settings(), document_id="document-1"
        )
    assert document.id == "document-1"
    assert fake.uploads == 1 and fake.parses == 0
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert (
        await sql(db, "SELECT status FROM knowledge_ingestion_job")
        == "reconciliation_required"
    )
    assert await runtime.consume(mid)
    assert fake.uploads == 1


@pytest.mark.parametrize(
    "field,value", [("name", "another-version.md"), ("dataset_id", "another-dataset")]
)
async def test_operator_cannot_claim_an_unrelated_document(
    db, monkeypatch, field, value
):
    fake = Provider("upload")
    configure(monkeypatch, fake)
    job_id = await submit(db)
    with pytest.raises(provider.ProviderOutcomeUnknown):
        await DurableTasks(db, handlers=get_delivery_handlers()).consume(
            await message(db)
        )
    fake.documents[0][field] = value
    async with db() as session:
        job = await service.get_ingestion_job(session, job_id)
        with pytest.raises(ProviderError, match="source version and dataset"):
            await provider.recover_upload_receipt(
                session,
                job=job,
                settings=service.get_settings(),
                document_id="document-1",
            )
    assert fake.uploads == 1 and fake.parses == 0


async def test_migration_backfills_only_inflight_and_preserves_unknowns(db):
    filename = ROOT / "alembic/versions/20260911_0006_durable_delivery.py"
    spec = importlib.util.spec_from_file_location(
        "knowledge_delivery_migration", filename
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    async def run(direction):
        def invoke(connection):
            with Operations.context(MigrationContext.configure(connection)):
                getattr(migration, direction)()

        async with db.kw["bind"].begin() as connection:
            await connection.run_sync(invoke)

    await run("downgrade")
    jobs = []
    async with db() as session:
        for status in ("accepted", "running", "succeeded"):
            job = KnowledgeIngestionJob(
                source_app="knowfeed-app",
                source_document_id=uuid.uuid4(),
                source_document_version_id=uuid.uuid4(),
                target_dataset="market-news",
                idempotency_key=str(uuid.uuid4()),
                status=status,
            )
            session.add(job)
            jobs.append(job)
        await session.commit()
    await run("upgrade")
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 2
    assert await sql(db, "SELECT count(*) FROM knowledge_provider_operation") == 1
    assert (
        await sql(db, "SELECT operation_key FROM knowledge_provider_operation")
        == f"upload:{service.upload_identity(jobs[1])}"
    )
    assert (
        await sql(db, "SELECT state FROM knowledge_provider_operation")
        == "legacy_unknown"
    )
    with pytest.raises(RuntimeError, match="drain Knowledge"):
        await run("downgrade")


async def test_acceptance_and_command_roll_back_together(db, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("command insert failed")

    monkeypatch.setattr(service, "enqueue_task", fail)
    with pytest.raises(RuntimeError):
        await submit(db)
    assert await sql(db, "SELECT count(*) FROM knowledge_ingestion_job") == 0


@pytest.mark.parametrize("fault", ["upload", "parse"])
async def test_response_loss_recovers_without_repeating_remote_write(
    db, monkeypatch, fault
):
    fake = Provider(fault)
    configure(monkeypatch, fake)
    await submit(db)
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    mid = await message(db)
    with pytest.raises(provider.ProviderOutcomeUnknown):
        await runtime.consume(mid)
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert await runtime.consume(mid)
    assert not await runtime.consume(mid)
    assert (fake.creates, fake.uploads, fake.parses) == (0, 1, 1)
    assert await sql(db, "SELECT status FROM knowledge_ingestion_job") == "succeeded"
    assert await sql(db, "SELECT count(*) FROM knowledge_document_version") == 1
    assert (
        await sql(
            db,
            "SELECT count(*) FROM knowledge_provider_operation WHERE state='confirmed'",
        )
        == 3
    )


async def test_missing_unknown_receipt_never_triggers_another_upload(db, monkeypatch):
    fake = Provider("upload")
    configure(monkeypatch, fake)
    await submit(db)
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    mid = await message(db)
    with pytest.raises(provider.ProviderOutcomeUnknown):
        await runtime.consume(mid)
    fake.hide_upload = True
    for _ in range(3):
        with pytest.raises(provider.ProviderOutcomeUnknown):
            await runtime.consume(mid)
    assert fake.uploads == 1
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0


async def test_new_retry_generation_cannot_bypass_unknown_parse(db, monkeypatch):
    fake = Provider("parse")
    configure(monkeypatch, fake)
    job_id = await submit(db)
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    with pytest.raises(provider.ProviderOutcomeUnknown):
        await runtime.consume(await message(db))
    # A stale provider read and an explicit force retry are not proof that the
    # previous POST never executed. The old generation remains authoritative.
    fake.documents[0]["run"] = "UNSTART"
    with pytest.raises(ValueError, match="not terminal: reconciliation_required"):
        async with db() as session:
            await service.retry_ingestion_job(session, ingestion_id=job_id, force=True)
    # Even if an operator reclassifies the domain status, the independent
    # side-effect ledger must still prevent a duplicate parse submission.
    await sql(db, "UPDATE knowledge_ingestion_job SET status='failed'")
    async with db() as session:
        await service.retry_ingestion_job(
            session, ingestion_id=job_id, force=True, reason="test unknown outcome"
        )
    next_message = await sql(
        db,
        "SELECT id FROM outbox_message WHERE deduplication_key LIKE :pattern",
        pattern="%:1",
    )
    with pytest.raises(provider.ProviderOutcomeUnknown, match="prior parse"):
        await runtime.consume(next_message)
    assert fake.parses == fake.uploads == 1
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0


async def test_remote_content_mismatch_does_not_confirm_upload(db, monkeypatch):
    fake = Provider()
    fake.bytes_valid = False
    configure(monkeypatch, fake)
    await submit(db)
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    assert await runtime.consume(await message(db))
    assert await sql(db, "SELECT count(*) FROM knowledge_document_version") == 0
    assert (
        await sql(
            db,
            "SELECT state FROM knowledge_provider_operation "
            "WHERE operation_key LIKE 'upload:%'",
        )
        == "executing"
    )
    assert fake.parses == 0


async def test_lost_upload_receipt_commit_is_recovered_without_reupload(
    db, monkeypatch
):
    fake = Provider()
    configure(monkeypatch, fake)
    await submit(db)
    real_confirm = provider.confirm
    fired = False

    async def crash(session, key, receipt):
        nonlocal fired
        if key.startswith("upload:") and not fired:
            fired = True

            def reject(sync_session):
                raise DeliveryLeaseLost(
                    "simulated lease revocation before receipt commit"
                )

            event.listen(session.sync_session, "before_commit", reject)
            try:
                await real_confirm(session, key, receipt)
            finally:
                event.remove(session.sync_session, "before_commit", reject)
        else:
            await real_confirm(session, key, receipt)

    monkeypatch.setattr(provider, "confirm", crash)
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    mid = await message(db)
    with pytest.raises(DeliveryLeaseLost):
        await runtime.consume(mid)
    assert (
        await sql(
            db,
            "SELECT state FROM knowledge_provider_operation "
            "WHERE operation_key LIKE 'upload:%'",
        )
        == "executing"
    )
    assert await runtime.consume(mid)
    assert fake.uploads == 1


async def test_missing_dataset_is_not_created_by_ingestion(db, monkeypatch):
    fake = Provider()
    fake.datasets = []
    configure(monkeypatch, fake)
    await submit(db)
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    assert await runtime.consume(await message(db))
    assert fake.creates == fake.uploads == fake.parses == 0
    assert (
        await sql(db, "SELECT status FROM knowledge_ingestion_job")
        == "external_api_error"
    )


async def test_legacy_unknown_upload_is_blocked_before_provider_calls(db, monkeypatch):
    fake = Provider()
    configure(monkeypatch, fake)
    job_id = await submit(db)
    async with db() as session:
        job = await service.get_ingestion_job(session, job_id)
        session.add(
            KnowledgeProviderOperation(
                operation_key=f"upload:{service.upload_identity(job)}",
                intent={"migration": "20260911_0006"},
                state="legacy_unknown",
                receipt={},
            )
        )
        await session.commit()
    with pytest.raises(provider.ProviderOutcomeUnknown):
        await DurableTasks(db, handlers=get_delivery_handlers()).consume(
            await message(db)
        )
    assert fake.creates == fake.uploads == fake.parses == 0


async def test_expired_worker_cannot_write_receipt_after_replacement_completes(
    db, monkeypatch
):
    started, resume = asyncio.Event(), asyncio.Event()

    class DelayedProvider(Provider):
        async def upload_document(self, dataset_id, artifact):
            result = await super().upload_document(dataset_id, artifact)
            started.set()
            await resume.wait()
            return result

    fake = DelayedProvider()
    configure(monkeypatch, fake)
    await submit(db)
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    mid = await message(db)
    old = asyncio.create_task(runtime.consume(mid))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await sql(
            db,
            "UPDATE outbox_execution "
            "SET expires_at=clock_timestamp()-interval '1 second'",
        )
        assert await runtime.consume(mid)
    finally:
        resume.set()
    with pytest.raises(DeliveryLeaseLost):
        await old
    assert (fake.creates, fake.uploads, fake.parses) == (0, 1, 1)
    assert await sql(db, "SELECT status FROM knowledge_ingestion_job") == "succeeded"
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 1


async def test_concurrent_dataset_requests_only_confirm_existing_target(db):
    fake = Provider()

    async def request():
        async with db() as session:
            return await provider.dataset(
                session,
                RAGFlowProvider(authorized_settings(), client=fake),
                "market-news",
                "scope",
                authorized_settings(),
            )

    results = await asyncio.gather(
        *(request() for _ in range(5)), return_exceptions=True
    )
    assert all(
        isinstance(result, (ProviderDataset, provider.ProviderOutcomeUnknown))
        for result in results
    )
    assert (await request()).id == "dataset-1"
    assert fake.creates == 0


async def test_retry_and_command_failure_restore_original_terminal_state(
    db, monkeypatch
):
    job_id = await submit(db)
    await sql(db, "UPDATE knowledge_ingestion_job SET status='failed'")

    async def fail(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(service, "enqueue_task", fail)
    with pytest.raises(RuntimeError):
        async with db() as session:
            await service.retry_ingestion_job(session, ingestion_id=job_id)
    assert await sql(db, "SELECT status FROM knowledge_ingestion_job") == "failed"
    assert (
        await sql(
            db, "SELECT metadata_json->>'retry_count' FROM knowledge_ingestion_job"
        )
        is None
    )
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 1
