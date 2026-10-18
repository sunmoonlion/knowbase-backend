"""B5 policy unit/HTTP/DB tests; provider calls are injected, DB is real."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_knowledge_delivery_db import (
    Provider,
    authorized_settings,
    configure,
    message,
    payload,
    submit,
)

from app.application.errors.exceptions import ForbiddenError
from app.application.services import knowledge_ingestion_service as service
from app.application.services import provider_delivery as provider
from app.application.services.ingestion_authorization import (
    SNAPSHOT_KEY,
    binding_snapshot,
    require_job_binding,
    resolve_binding,
)
from app.infrastructure.external.ragflow import RAGFlowClient, RAGFlowProtocolError
from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
from app.infrastructure.messaging.durable_tasks import DurableTasks
from app.infrastructure.models.knowledge import KnowledgeIngestionJob
from app.infrastructure.security.service_auth import require_knowbase_ingest_service
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.knowledge_routes import internal_router, router
from app.interfaces.errors.exception_handlers import register_exception_handlers
from core.config import Settings

KNOWFEED_APP = Path(__file__).resolve().parents[4] / "knowfeed-app/knowfeed-backend/app"


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(service, "get_settings", authorized_settings)


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "null",
        "not json",
        '{"x":{}}',
        '{"x":{"dataset_id":"id","dataset_name":"name","extra":true}}',
        '{"x":{"dataset_id":"../id","dataset_name":"name"}}',
        '{"X":{"dataset_id":"id","dataset_name":"name"}}',
        '{"x":{"dataset_id":"id","dataset_name":" name"}}',
        '{"x":{"dataset_id":"id","dataset_name":"name"},"x":{}}',
        '{"x":{"dataset_id":"id","dataset_id":"other","dataset_name":"name"}}',
        '{"x":{"dataset_id":"id","dataset_name":"name"},"y":{"dataset_id":"id","dataset_name":"name"}}',
    ],
)
def test_malformed_policy_fails_at_settings_construction(raw):
    with pytest.raises(ValueError):
        Settings(INGESTION_DATASET_BINDINGS=raw)


def test_empty_policy_denies_even_if_retrieval_is_allowed():
    settings = Settings(
        INGESTION_DATASET_BINDINGS="{}", RETRIEVAL_DATASET_ALLOWLIST="market-news"
    )
    with pytest.raises(ForbiddenError):
        resolve_binding(settings, "market-news")


async def test_unknown_dataset_creates_no_job_command_or_provider_operation(db):
    with pytest.raises(ForbiddenError):
        await submit(db, payload().model_copy(update={"dataset_key": "unconfigured"}))
    for table in (
        "knowledge_ingestion_job",
        "outbox_message",
        "knowledge_provider_operation",
    ):
        assert await sql(db, f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize(
    "path", ["/api/knowledge/ingestions", "/api/internal/v1/knowledge/ingestions"]
)
@pytest.mark.parametrize(
    "key,expected",
    [("market-news", 202), ("unconfigured", 403), ("reserved-execution-metadata", 403)],
)
async def test_admin_and_internal_share_dataset_authorization(db, path, key, expected):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(internal_router, prefix="/api")
    register_exception_handlers(app)

    async def session():
        async with db() as s:
            yield s

    # This test injects an already-authenticated service principal. Existing
    # identity/CSRF suites cover authentication; it is not a live OIDC test.
    principal = SimpleNamespace(
        actor_type="service",
        subject="knowfeed-service",
        issuer="test",
        audience="knowbase",
        app="knowfeed-app",
        surface="internal",
        scopes={"knowbase:ingest"},
        policy_version="test",
    )
    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[require_knowbase_ingest_service] = lambda: principal
    request = payload().model_copy(update={"dataset_key": key})
    if key == "reserved-execution-metadata":
        from app.application.services.ingestion_execution import EXECUTION_KEY

        request = payload()
        request.document.metadata[EXECUTION_KEY] = {"generation": 42}
    # 内部接口只认集群内地址（审读第 1 条）
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://knowbase-tls.app-platform-dev.svc.cluster.local",
    ) as client:
        response = await client.post(path, json=request.model_dump(mode="json"))
    assert response.status_code == expected
    if "/internal/" in path:
        await exercise_info_consumer(response)
    assert await sql(db, "SELECT count(*) FROM knowledge_ingestion_job") == int(
        expected == 202
    )
    assert await sql(db, "SELECT count(*) FROM outbox_message") == int(expected == 202)


async def exercise_info_consumer(response):
    """Actual sibling Info client consumes this provider's real ASGI response.

    Isolate its app/core imports in a subprocess. Transport and token acquisition
    are injected; no live HTTP or credentials. No Info source changes needed.
    """
    app_path = KNOWFEED_APP
    interpreter = app_path / ".venv/bin/python"
    if not await asyncio.to_thread(interpreter.is_file):
        pytest.skip("initialize the sibling Info consumer and its test venv")
    script = """
import asyncio, json, sys
import httpx
from app.infrastructure.external.knowbase_app import KnowbaseAppClient
data = json.load(sys.stdin)
original = httpx.AsyncClient
transport = httpx.MockTransport(
    lambda request: httpx.Response(data["status"], json=data["body"])
)
httpx.AsyncClient = lambda **kwargs: original(transport=transport, **kwargs)
class Token:
    async def get_token(self): return "test-only"
async def run():
    client = KnowbaseAppClient("https://test/ingestions", Token(), 1)
    try:
        result = await client.ingest_document({"fixture": True})
    except httpx.HTTPStatusError as exc:
        assert data["status"] == exc.response.status_code == 403
    else:
        assert data["status"] == result["status_code"] == 202
asyncio.run(run())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [str(interpreter), "-c", script],
        cwd=app_path,
        input=json.dumps({"status": response.status_code, "body": response.json()}),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


async def test_client_metadata_cannot_supply_policy_snapshot(db):
    request = payload()
    request.document.metadata[SNAPSHOT_KEY] = {"dataset_id": "attacker"}
    job_id = await submit(db, request)
    async with db() as s:
        job = await s.get(KnowledgeIngestionJob, job_id)
        assert job.metadata_json[SNAPSHOT_KEY]["dataset_id"] == "attacker"
        assert require_job_binding(job, authorized_settings()).dataset_id == "dataset-1"
        assert (
            job.status_history[0]["metadata"][SNAPSHOT_KEY]["dataset_id"] == "dataset-1"
        )


@pytest.mark.parametrize("change", ["remove", "id", "endpoint"])
async def test_worker_rechecks_revocation_and_rebinding_before_any_work(
    db, monkeypatch, change
):
    fake = Provider()
    configure(monkeypatch, fake)
    await submit(db)
    old = service.get_settings()
    fields = {}
    if change == "remove":
        fields["ingestion_dataset_bindings"] = "{}"
    elif change == "id":
        fields["ingestion_dataset_bindings"] = json.dumps(
            {"market-news": {"dataset_id": "new-id", "dataset_name": "market-news"}}
        )
    else:
        fields["ragflow_api_base"] = "https://different.example.test"
    monkeypatch.setattr(service, "get_settings", lambda: old.model_copy(update=fields))

    async def forbidden(*args, **kwargs):
        raise AssertionError("artifact/provider access preceded policy check")

    monkeypatch.setattr(provider, "prepare_artifact", forbidden)
    with pytest.raises(ForbiddenError):
        await DurableTasks(db, handlers=get_delivery_handlers()).consume(
            await message(db)
        )
    assert fake.creates == fake.uploads == fake.parses == 0
    assert await sql(db, "SELECT status FROM knowledge_ingestion_job") == "accepted"
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert await sql(db, "SELECT count(*) FROM knowledge_provider_operation") == 0


async def test_force_retry_and_dispatch_cannot_bypass_revocation(db, monkeypatch):
    job_id = await submit(db)
    await sql(db, "UPDATE knowledge_ingestion_job SET status='failed'")
    monkeypatch.setattr(
        service, "get_settings", lambda: Settings(INGESTION_DATASET_BINDINGS="{}")
    )
    with pytest.raises(ForbiddenError):
        async with db() as s:
            await service.retry_ingestion_job(s, ingestion_id=job_id, force=True)
    await sql(db, "UPDATE knowledge_ingestion_job SET status='accepted'")
    with pytest.raises(ForbiddenError):
        async with db() as s:
            await service.request_ingestion_job(s, ingestion_id=job_id)
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 1
    assert (
        await sql(
            db, "SELECT metadata_json->>'retry_count' FROM knowledge_ingestion_job"
        )
        is None
    )


async def test_legacy_job_and_later_status_metadata_cannot_gain_authority(db):
    job_id = await submit(db)
    snapshot = binding_snapshot(authorized_settings(), "market-news")
    history = [
        {"status": "accepted"},
        {"status": "accepted", "metadata": {SNAPSHOT_KEY: snapshot}},
    ]
    await sql(
        db,
        "UPDATE knowledge_ingestion_job SET status_history=CAST(:history AS jsonb)",
        history=json.dumps(history),
    )
    with pytest.raises(ForbiddenError):
        await DurableTasks(db, handlers=get_delivery_handlers()).consume(
            await message(db)
        )
    async with db() as s:
        job = await s.get(KnowledgeIngestionJob, job_id)
        with pytest.raises(ForbiddenError):
            require_job_binding(job, authorized_settings())


async def test_recovery_cannot_access_artifact_after_policy_revocation(db, monkeypatch):
    fake = Provider("upload")
    configure(monkeypatch, fake)
    job_id = await submit(db)
    with pytest.raises(provider.ProviderOutcomeUnknown):
        await DurableTasks(db, handlers=get_delivery_handlers()).consume(
            await message(db)
        )
    old = service.get_settings()
    revoked = old.model_copy(update={"ingestion_dataset_bindings": "{}"})

    async def forbidden(*args, **kwargs):
        raise AssertionError("revoked recovery fetched artifact")

    monkeypatch.setattr(provider, "prepare_artifact", forbidden)
    async with db() as s:
        job = await s.get(KnowledgeIngestionJob, job_id)
        with pytest.raises(ForbiddenError):
            await provider.recover_upload_receipt(
                s, job=job, settings=revoked, document_id="document-1"
            )
    assert fake.uploads == 1 and fake.parses == 0


async def test_same_name_wrong_id_does_not_upload_or_create(db, monkeypatch):
    fake = Provider()
    fake.datasets = [{"id": "wrong-id", "name": "market-news"}]
    configure(monkeypatch, fake)
    await submit(db)
    assert await DurableTasks(db, handlers=get_delivery_handlers()).consume(
        await message(db)
    )
    assert fake.creates == fake.uploads == fake.parses == 0
    assert await sql(db, "SELECT count(*) FROM knowledge_document_version") == 0


async def test_final_domain_binding_rejects_wrong_provider_id(db):
    job_id = await submit(db)
    async with db() as s:
        job = await s.get(KnowledgeIngestionJob, job_id)
        with pytest.raises(ForbiddenError):
            await service.complete_ragflow_ingestion(
                s,
                job=job,
                result=SimpleNamespace(
                    provider="ragflow", dataset_id="wrong", dataset_name="market-news"
                ),
            )
    assert await sql(db, "SELECT count(*) FROM knowledge_document") == 0


async def test_compatibility_create_dataset_entry_never_issues_http():
    def forbidden(request):
        raise AssertionError("data plane attempted dataset creation")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as transport:
        client = RAGFlowClient(
            Settings(RAGFLOW_API_BASE="https://test", RAGFLOW_API_KEY="test-only"),
            client=transport,
        )
        with pytest.raises(RAGFlowProtocolError, match="disabled"):
            await client.create_dataset("any-name")


async def test_status_endpoint_cannot_authorize_an_empty_legacy_history(db):
    job_id = await submit(db)
    await sql(db, "UPDATE knowledge_ingestion_job SET status_history='[]'::jsonb")
    async with db() as s:
        with pytest.raises(ForbiddenError):
            await service.update_ingestion_status(
                s,
                ingestion_id=job_id,
                status="accepted",
                last_error=None,
                metadata={
                    SNAPSHOT_KEY: binding_snapshot(authorized_settings(), "market-news")
                },
                knowledge_document_id=None,
                ragflow_document_id=None,
            )
    assert await sql(db, "SELECT status_history FROM knowledge_ingestion_job") == []
