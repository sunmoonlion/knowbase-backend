"""Real transactions; provider/time faults injected only in disposable schemas."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_knowledge_delivery_db import Provider, configure, message, payload, submit

from app.application.errors.exceptions import ForbiddenError
from app.application.services import ingestion_execution as execution
from app.application.services import knowledge_ingestion_service as service
from app.application.services import provider_delivery as provider
from app.infrastructure.external.ragflow import RAGFlowError, _normalise_run
from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
from app.infrastructure.messaging.durable_delivery import DeliveryLeaseLost
from app.infrastructure.messaging.durable_tasks import DurableTasks, enqueue_task
from app.tasks.durable_delivery import pump
from core.config import Settings

APP_ROOT = Path(__file__).resolve().parents[1]


class SlowProvider(Provider):
    def __init__(self):
        super().__init__()
        self.reads = 0
        self.read_fault = False
        self.entered = self.proceed = None

    async def parse_document(self, dataset_id, document_id):
        self.parses += 1
        self.documents[0]["run"] = "RUNNING"
        self.lose("parse")

    async def get_document(self, dataset_id, document_id):
        self.reads += 1
        if self.entered is not None:
            self.entered.set()
            await self.proceed.wait()
        if self.read_fault:
            self.read_fault = False
            raise RAGFlowError("temporary read failure")
        return await super().get_document(dataset_id, document_id)


def configured(monkeypatch):
    fake = SlowProvider()
    configure(monkeypatch, fake)
    settings = service.get_settings().model_copy(
        update={
            "ragflow_parse_timeout_seconds": 120,
            "ragflow_parse_poll_interval_seconds": 1,
        }
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    return fake


def runtime(db):
    return DurableTasks(db, handlers=get_delivery_handlers())


async def state(db):
    value = await sql(
        db,
        "SELECT metadata_json->'ingestion_execution_v1' FROM knowledge_ingestion_job",
    )
    return execution.IngestionExecution.model_validate(value)


async def patch_state(db, **updates):
    await sql(
        db,
        "UPDATE knowledge_ingestion_job SET metadata_json=jsonb_set("
        "metadata_json, '{ingestion_execution_v1}', "
        "(metadata_json->'ingestion_execution_v1') || CAST(:updates AS jsonb))",
        updates=json.dumps(updates),
    )


async def poll_message(db, step, generation=0):
    return await sql(
        db,
        "SELECT id FROM outbox_message WHERE "
        "payload->>'step'=:step AND payload->>'generation'=:generation",
        step=str(step),
        generation=str(generation),
    )


async def command_key(db):
    return await sql(
        db,
        "SELECT aggregate_key FROM outbox_message "
        "WHERE deduplication_key LIKE 'knowbase:ingest:%' "
        "ORDER BY created_at,id LIMIT 1",
    )


async def make_due(db, mid):
    # Advance ONLY this test transport timer, not the persistent parse deadline.
    # B6a separately verifies the actual PostgreSQL not-before predicate.
    await sql(
        db,
        "UPDATE outbox_message SET available_at='2000-01-01', "
        "headers=jsonb_set(headers, '{sunmoonai.not_before.v1}', "
        "to_jsonb('2000-01-01T00:00:00Z'::text)) WHERE id=:id",
        id=mid,
    )


async def begun(db, monkeypatch):
    fake = configured(monkeypatch)
    job_id = await submit(db)
    assert await runtime(db).consume(await message(db))
    assert (await state(db)).step == 1
    return fake, job_id


@pytest.mark.parametrize(
    "run,expected",
    [
        (0, "UNSTART"),
        (1, "RUNNING"),
        (2, "CANCEL"),
        (3, "DONE"),
        (4, "FAIL"),
        (5, "SCHEDULE"),
    ],
)
def test_numeric_provider_states_include_integer_zero(run, expected):
    assert _normalise_run(run) == expected


@pytest.mark.parametrize(
    "values",
    [
        {"RAGFLOW_PARSE_TIMEOUT_SECONDS": 0},
        {"RAGFLOW_PARSE_TIMEOUT_SECONDS": 86401},
        {"RAGFLOW_PARSE_POLL_INTERVAL_SECONDS": 0},
        {"RAGFLOW_PARSE_POLL_INTERVAL_SECONDS": 0.001},
        {"RAGFLOW_PARSE_POLL_INTERVAL_SECONDS": 61},
        {"RAGFLOW_PARSE_POLL_INTERVAL_SECONDS": float("nan")},
        {"RAGFLOW_PARSE_POLL_INTERVAL_SECONDS": float("inf")},
    ],
)
def test_invalid_parse_scheduling_fails_at_configuration(values):
    with pytest.raises(ValueError):
        Settings(**values)


async def test_poll_releases_worker_queries_once_and_reuses_verified_upload(
    db, monkeypatch
):
    fake, _ = await begun(db, monkeypatch)
    before = await state(db)
    first = await poll_message(db, 1)
    assert not await runtime(db).consume(first)
    assert (
        await sql(
            db,
            "SELECT count(*) FROM outbox_execution WHERE expires_at>clock_timestamp()",
        )
        == 0
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("poll must not re-read source, upload, or use the wait loop")

    monkeypatch.setattr(provider, "prepare_artifact", forbidden)
    monkeypatch.setattr(fake, "upload_document", forbidden)
    from app.infrastructure.external import ragflow

    monkeypatch.setattr(ragflow, "_sleep", forbidden)
    for step in (1, 2):
        mid = await poll_message(db, step)
        await make_due(db, mid)
        reads = fake.reads
        assert await asyncio.wait_for(runtime(db).consume(mid), timeout=2)
        assert fake.reads == reads + 1
        assert not await runtime(db).consume(mid)
        after = await state(db)
        assert after.deadline == before.deadline
        assert after.step == step + 1
        assert after.ready_at <= after.deadline
    fake.documents[0]["run"] = "DONE"
    mid = await poll_message(db, 3)
    await make_due(db, mid)
    reads = fake.reads
    assert await runtime(db).consume(mid)
    assert fake.reads == reads + 1
    assert await sql(db, "SELECT status FROM knowledge_ingestion_job") == "succeeded"
    assert await sql(db, "SELECT count(*) FROM knowledge_document_version") == 1
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 4
    assert fake.uploads == fake.parses == 1


async def test_poll_backoff_is_bounded_and_never_moves_deadline(db, monkeypatch):
    _, _ = await begun(db, monkeypatch)
    first = await state(db)
    for step in range(1, 8):
        mid = await poll_message(db, step)
        await make_due(db, mid)
        before = await sql(db, "SELECT clock_timestamp()")
        assert await runtime(db).consume(mid)
        current = await state(db)
        delay = (current.ready_at - before).total_seconds()
        assert min(60, 2**step) <= delay < min(60, 2**step) + 1
        assert current.deadline == first.deadline


async def test_deadline_expiry_retries_with_same_upload_and_obsoletes_old_message(
    db, monkeypatch
):
    fake, job_id = await begun(db, monkeypatch)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    await patch_state(db, deadline="2000-01-01T00:00:00Z")
    reads = fake.reads
    assert await runtime(db).consume(mid)
    assert fake.reads == reads
    assert (
        await sql(db, "SELECT status FROM knowledge_ingestion_job")
        == "ragflow_parse_failed"
    )
    async with db() as s:
        await service.retry_ingestion_job(s, ingestion_id=job_id)
    current = await state(db)
    assert current.generation == 1 and current.deadline is None

    async def forbidden(*args, **kwargs):
        pytest.fail("retry of verified upload must not read the source again")

    monkeypatch.setattr(provider, "prepare_artifact", forbidden)
    # A distinct stale envelope also cannot reset the new generation.
    async with db() as s, s.begin():
        stale = await enqueue_task(
            s,
            topic="knowledge.ingest.v1",
            key=await command_key(db),
            payload={"ingestion_id": str(job_id), "generation": 0, "step": 1},
            deduplication_key="stale-copy",
        )
    assert await runtime(db).consume(stale)
    assert await state(db) == current
    assert fake.reads == reads
    fake.documents[0]["run"] = "DONE"
    assert await runtime(db).consume(await poll_message(db, 0, generation=1))
    assert fake.uploads == fake.parses == 1
    assert await sql(db, "SELECT status FROM knowledge_ingestion_job") == "succeeded"


@pytest.mark.parametrize(
    "run,error_type",
    [("FAIL", "ragflow_parse_failed"), ("CANCEL", "ragflow_parse_cancelled")],
)
async def test_parse_terminal_failure_is_not_indexed_and_can_be_explicitly_retried(
    db, monkeypatch, run, error_type
):
    fake, job_id = await begun(db, monkeypatch)
    fake.documents[0]["run"] = run
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    assert await runtime(db).consume(mid)
    assert await sql(db, "SELECT count(*) FROM knowledge_document_version") == 0
    assert (
        await sql(
            db,
            "SELECT status_history->-1->'metadata'->>'error_type' "
            "FROM knowledge_ingestion_job",
        )
        == error_type
    )
    async with db() as s:
        await service.retry_ingestion_job(s, ingestion_id=job_id)
    assert await runtime(db).consume(await poll_message(db, 0, generation=1))
    assert fake.uploads == 1 and fake.parses == 2
    assert (await state(db)).generation == 1


async def test_read_failure_schedules_bounded_recovery_without_resubmission(
    db, monkeypatch
):
    fake, _ = await begun(db, monkeypatch)
    before = await state(db)
    fake.read_fault = True
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    assert await runtime(db).consume(mid)
    after = await state(db)
    assert after.step == 2 and after.deadline == before.deadline
    assert after.read_error == "RAGFlowError"
    fake.documents[0]["run"] = "DONE"
    mid = await poll_message(db, 2)
    await make_due(db, mid)
    assert await runtime(db).consume(mid)
    assert fake.uploads == fake.parses == 1


@pytest.mark.parametrize("done", [False, True])
async def test_progress_next_message_and_final_result_roll_back_with_inbox(
    db, monkeypatch, done
):
    fake, _ = await begun(db, monkeypatch)
    before = await state(db)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    if done:
        fake.documents[0]["run"] = "DONE"
    real_handler = get_delivery_handlers()["knowledge.ingest.v1"]

    async def fail(s, payload):
        await real_handler(s, payload)

        def reject(sync_session):
            raise RuntimeError("final commit failure")

        event.listen(s.sync_session, "before_commit", reject, once=True)

    with pytest.raises(RuntimeError, match="final commit failure"):
        await DurableTasks(db, handlers={"knowledge.ingest.v1": fail}).consume(mid)
    assert await state(db) == before
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 2
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 1
    assert await sql(db, "SELECT count(*) FROM knowledge_document_version") == 0
    assert await runtime(db).consume(mid)
    assert fake.uploads == fake.parses == 1


@pytest.mark.parametrize("failure", ["cancel", "lease", "retry"])
async def test_suspended_poll_can_be_recovered_but_old_worker_cannot_commit(
    db, monkeypatch, failure
):
    fake, job_id = await begun(db, monkeypatch)
    before = await state(db)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    fake.entered, fake.proceed = asyncio.Event(), asyncio.Event()
    worker = asyncio.create_task(runtime(db).consume(mid))
    await asyncio.wait_for(fake.entered.wait(), timeout=2)
    try:
        if failure == "cancel":
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        else:
            if failure == "lease":
                await sql(db, "UPDATE outbox_execution SET expires_at='2000-01-01'")
            else:
                # This must finish while the HTTP query is paused: no row lock
                # may be held across the read. Then autoflush must see generation 1.
                async def retry():
                    await sql(db, "UPDATE knowledge_ingestion_job SET status='failed'")
                    async with db() as s:
                        await service.retry_ingestion_job(s, ingestion_id=job_id)

                await asyncio.wait_for(retry(), timeout=2)
            fake.proceed.set()
            with pytest.raises(DeliveryLeaseLost):
                await worker
        current = await state(db)
        assert current.generation == (1 if failure == "retry" else 0)
        if failure != "retry":
            assert current == before
        assert await sql(db, "SELECT count(*) FROM inbox_message") == 1
        assert await sql(db, "SELECT count(*) FROM knowledge_document_version") == 0
    finally:
        fake.proceed.set()
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_parse_submission_response_loss_keeps_deadline_and_upload_cursor(
    db, monkeypatch
):
    fake = configured(monkeypatch)
    fake.fault = "parse"
    await submit(db)
    mid = await message(db)
    with pytest.raises(provider.ProviderOutcomeUnknown):
        await runtime(db).consume(mid)
    before = await state(db)
    assert before.deadline is not None
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert await runtime(db).consume(mid)
    after = await state(db)
    assert after.deadline == before.deadline and after.step == 1
    assert fake.uploads == fake.parses == 1


@pytest.mark.parametrize("change", ["scope", "document", "binding"])
async def test_poll_rechecks_provider_identity_and_authorization(
    db, monkeypatch, change
):
    fake, _ = await begun(db, monkeypatch)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    if change == "scope":

        async def changed():
            return {"tenant_id": "another-tenant"}

        monkeypatch.setattr(fake, "get_tenant_models", changed)
    elif change == "document":
        fake.documents[0]["name"] = "another-file.md"
    else:
        changed_settings = service.get_settings().model_copy(
            update={"ingestion_dataset_bindings": "{}"}
        )
        monkeypatch.setattr(service, "get_settings", lambda: changed_settings)
    if change == "binding":
        with pytest.raises(ForbiddenError):
            await runtime(db).consume(mid)
    else:
        assert await runtime(db).consume(mid)
    assert await sql(db, "SELECT count(*) FROM knowledge_document_version") == 0
    assert fake.uploads == fake.parses == 1


async def test_client_retry_metadata_is_not_an_execution_generation(db, monkeypatch):
    configured(monkeypatch)
    request = payload().model_dump(mode="json")
    request["document"]["metadata"]["retry_count"] = 999
    request["document"]["metadata"]["retry_history"] = 42
    from app.application.dto.knowledge import KnowledgeIngestionCreate

    job_id = await submit(db, KnowledgeIngestionCreate.model_validate(request))
    assert (await state(db)).generation == 0
    assert await poll_message(db, 0) is not None
    await sql(db, "UPDATE knowledge_ingestion_job SET status='failed'")
    async with db() as s:
        await service.retry_ingestion_job(s, ingestion_id=job_id)
    assert (await state(db)).generation == 1
    assert (
        await sql(
            db,
            "SELECT payload->'document'->'metadata'->>'retry_count' "
            "FROM knowledge_ingestion_job",
        )
        == "999"
    )


async def test_legacy_marker_cannot_be_forged_by_status_metadata_or_dispatch(
    db, monkeypatch
):
    configured(monkeypatch)
    job_id = await submit(db)
    await sql(db, "UPDATE knowledge_ingestion_job SET status_history='[]'")
    async with db() as s:
        with pytest.raises(ForbiddenError):
            await service.update_ingestion_status(
                s,
                ingestion_id=job_id,
                status="accepted",
                last_error=None,
                metadata={execution.EXECUTION_KEY: execution.EXECUTION_MARKER},
                knowledge_document_id=None,
                ragflow_document_id=None,
            )
    with pytest.raises(ForbiddenError):
        await runtime(db).consume(await message(db))
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0


@pytest.mark.parametrize(
    "payload_fields",
    [
        {},
        {"generation": True, "step": 0},
        {"generation": 0, "step": -1},
        {"generation": 1, "step": 0},
    ],
)
async def test_legacy_or_ahead_messages_do_not_drive_current_job(
    db, monkeypatch, payload_fields
):
    configured(monkeypatch)
    job_id = await submit(db)
    async with db() as s, s.begin():
        mid = await enqueue_task(
            s,
            topic="knowledge.ingest.v1",
            key=await command_key(db),
            payload={"ingestion_id": str(job_id), **payload_fields},
            deduplication_key="injected",
        )
    with pytest.raises(ValueError):
        await runtime(db).consume(mid)
    assert await sql(db, "SELECT status FROM knowledge_ingestion_job") == "accepted"
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0


@pytest.mark.parametrize("error", [RuntimeError, ValueError])
async def test_enqueue_failure_does_not_ack_or_persist_advanced_cursor(
    db, monkeypatch, error
):
    fake, _ = await begun(db, monkeypatch)
    before = await state(db)
    mid = await poll_message(db, 1)
    await make_due(db, mid)

    async def reject(*args, **kwargs):
        raise error("injected enqueue failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(service, "enqueue_task", reject)
        with pytest.raises(error, match="injected enqueue failure"):
            await runtime(db).consume(mid)
    assert await state(db) == before
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 1
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 2
    assert await runtime(db).consume(mid)
    assert fake.uploads == fake.parses == 1


async def test_changed_settings_do_not_reset_persisted_parse_policy(db, monkeypatch):
    _, _ = await begun(db, monkeypatch)
    before = await state(db)
    settings = service.get_settings().model_copy(
        update={
            "ragflow_parse_timeout_seconds": 3600,
            "ragflow_parse_poll_interval_seconds": 60,
        }
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    assert await runtime(db).consume(mid)
    after = await state(db)
    assert after.deadline == before.deadline and after.interval == before.interval == 1


async def test_hung_provider_read_is_cancelled_at_deadline(db, monkeypatch):
    fake, _ = await begun(db, monkeypatch)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    when = await sql(db, "SELECT clock_timestamp()") + timedelta(seconds=0.2)
    await patch_state(db, deadline=when.isoformat())
    fake.entered, fake.proceed = asyncio.Event(), asyncio.Event()
    assert await asyncio.wait_for(runtime(db).consume(mid), timeout=2)
    assert fake.entered.is_set()
    assert (
        await sql(db, "SELECT status FROM knowledge_ingestion_job")
        == "ragflow_parse_failed"
    )
    assert (
        await sql(
            db,
            "SELECT count(*) FROM outbox_execution WHERE expires_at>clock_timestamp()",
        )
        == 0
    )


async def test_lost_poll_broker_hint_dead_letters_and_replays_without_resubmission(
    db, monkeypatch
):
    fake, _ = await begun(db, monkeypatch)
    before = await state(db)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    # Initial command was directly consumed in this fixture; suppress its hint.
    await sql(
        db,
        "UPDATE outbox_message SET status='published',published_at=clock_timestamp() "
        "WHERE payload->>'step'='0'",
    )
    delivery = DurableTasks(db, handlers=get_delivery_handlers(), max_attempts=1)

    async def lost(message):
        assert message["id"] == mid

    assert await pump(delivery, lost) == 1
    await sql(
        db, "UPDATE outbox_message SET published_at='2000-01-01' WHERE id=:id", id=mid
    )
    await delivery.reconcile()
    assert not await delivery.consume(mid)
    await delivery.replay(mid)
    assert await delivery.consume(mid)
    assert (await state(db)).deadline == before.deadline
    assert fake.uploads == fake.parses == 1


async def test_missing_poll_deadline_fails_closed_without_reset(db, monkeypatch):
    fake, _ = await begun(db, monkeypatch)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    await patch_state(db, deadline=None)
    reads = fake.reads
    with pytest.raises(ValueError, match="deadline"):
        await runtime(db).consume(mid)
    assert fake.reads == reads
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 1


async def test_actual_worker_process_death_replays_persisted_poll(db, monkeypatch):
    fake, _ = await begun(db, monkeypatch)
    before = await state(db)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    schema = await sql(db, "SELECT current_schema()")
    # Only the disposable database URL goes to the child; no business credentials.
    url = db.kw["bind"].url.render_as_string(hide_password=False)
    script = """
import asyncio, sys, uuid
sys.path.insert(0, 'tests')
from sqlalchemy.ext.asyncio import create_async_engine
from app.infrastructure.storage.postgres import make_session_factory
from test_knowledge_delivery_db import authorized_settings
from app.application.services import knowledge_ingestion_service as service
from app.application.services import provider_delivery as provider
from app.infrastructure.messaging.durable_tasks import DurableTasks
from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
settings = authorized_settings(RAGFLOW_API_BASE='https://provider.example.test',
    RAGFLOW_API_KEY='test-only', RAGFLOW_PARSE_TIMEOUT_SECONDS=120)
service.get_settings = lambda: settings
class Client:
    async def get_tenant_models(self): return {'tenant_id': 'test-tenant'}
    async def get_document(self, *args):
        print('POLL_ENTERED', flush=True)
        await asyncio.Event().wait()
    async def close(self): pass
from app.infrastructure.external.ragflow_provider import RAGFlowProvider
provider.create_provider = lambda settings: RAGFlowProvider(settings, client=Client())
async def main():
    engine = create_async_engine(sys.argv[1],
        connect_args={'server_settings': {'search_path': sys.argv[2] + ',public'}})
    sessions = make_session_factory(engine)
    runtime = DurableTasks(sessions, handlers=get_delivery_handlers())
    await runtime.consume(uuid.UUID(sys.argv[3]))
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        url,
        schema,
        str(mid),
        cwd=APP_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        assert line == b"POLL_ENTERED\n"
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=5)
        assert process.returncode < 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert await state(db) == before
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 2
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 1
    assert not await runtime(db).consume(mid)  # Lease still belongs to the dead worker.
    await sql(db, "UPDATE outbox_execution SET expires_at='2000-01-01'")
    fake.documents[0]["run"] = "DONE"
    assert await runtime(db).consume(mid)
    assert fake.uploads == fake.parses == 1
    assert await sql(db, "SELECT status FROM knowledge_ingestion_job") == "succeeded"


async def test_cached_terminal_job_cannot_repeat_or_reset_an_existing_retry(
    db, monkeypatch
):
    _, job_id = await begun(db, monkeypatch)
    await sql(db, "UPDATE knowledge_ingestion_job SET status='failed'")
    async with db() as stale:
        old = await service.get_ingestion_job(stale, job_id)
        assert old.status == "failed"
        async with db() as fresh:
            await service.retry_ingestion_job(fresh, ingestion_id=job_id)
        before = await state(db)
        with pytest.raises(ValueError, match="not terminal: accepted"):
            await service.retry_ingestion_job(stale, ingestion_id=job_id)
    assert await state(db) == before
    assert before.generation == 1
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 3


async def test_distinct_old_cursor_message_is_acknowledged_without_changing_progress(
    db, monkeypatch
):
    fake, job_id = await begun(db, monkeypatch)
    before, reads = await state(db), fake.reads
    async with db() as s, s.begin():
        old = await enqueue_task(
            s,
            topic="knowledge.ingest.v1",
            key=await command_key(db),
            payload={"ingestion_id": str(job_id), "generation": 0, "step": 0},
            deduplication_key="copied-old-cursor",
        )
    assert await runtime(db).consume(old)
    assert await state(db) == before and fake.reads == reads
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 2


async def test_incorrect_resource_key_cannot_bypass_upload_serialization(
    db, monkeypatch
):
    configured(monkeypatch)
    job_id = await submit(db)
    async with db() as s, s.begin():
        mid = await enqueue_task(
            s,
            topic="knowledge.ingest.v1",
            key="wrong-resource",
            payload={"ingestion_id": str(job_id), "generation": 0, "step": 0},
            deduplication_key="wrong-resource",
        )
    with pytest.raises(ValueError, match="resource"):
        await runtime(db).consume(mid)
    assert await sql(db, "SELECT count(*) FROM knowledge_provider_operation") == 0
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0


async def test_missing_credentials_cannot_downgrade_poll_to_artifact_verification(
    db, monkeypatch
):
    fake, _ = await begun(db, monkeypatch)
    before = await state(db)
    mid = await poll_message(db, 1)
    await make_due(db, mid)
    settings = service.get_settings().model_copy(update={"ragflow_api_key": None})

    async def forbidden(*args, **kwargs):
        pytest.fail("active parse cannot fall back to source verification")

    with monkeypatch.context() as scoped:
        scoped.setattr(service, "get_settings", lambda: settings)
        scoped.setattr(service, "resolve_artifact_content", forbidden)
        with pytest.raises(ForbiddenError, match="artifact-only"):
            await runtime(db).consume(mid)
    assert await state(db) == before
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 1
    fake.documents[0]["run"] = "DONE"
    assert await runtime(db).consume(mid)
