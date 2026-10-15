from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import jsonschema
import pytest
from pydantic import ValidationError

from app.application.ports.knowledge_provider import ArtifactError
from app.application.services.knowledge_ingestion_service import (
    PROCESSOR_NAME,
    RAGFLOW_PROCESSOR_NAME,
    build_idempotency_key,
    build_processing_success_metadata,
    build_ragflow_success_metadata,
    classify_ingestion_error,
    get_ragflow_config_check,
)
from app.infrastructure.external.artifact_content import (
    _s3_sigv4_headers,
    resolve_artifact_content,
)
from app.infrastructure.external.ragflow import (
    ArtifactContent,
    RAGFlowClient,
    RAGFlowConfigCheck,
    RAGFlowError,
    RAGFlowParseCancelledError,
    RAGFlowParseError,
    _wait_for_document_parse,
    check_ragflow_config,
    ingest_into_ragflow,
)
from app.infrastructure.messaging.celery_producer import CeleryProducer
from app.interfaces.schemas.knowledge import KnowledgeIngestionCreate
from core.config import Settings

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "contracts/artifact/v1/knowfeed-knowbase-artifact.schema.json"
)
CONTRACT_MANIFEST_PATH = CONTRACT_PATH.with_name("contract-manifest.json")
CONTRACT_EXAMPLE_PATH = CONTRACT_PATH.parent / "examples/upsert.json"


def _contract_payload() -> dict[str, Any]:
    document_id = uuid.UUID("00000000-0000-0000-0000-000000000010")
    version_id = uuid.UUID("00000000-0000-0000-0000-000000000011")
    distribution_id = uuid.UUID("00000000-0000-0000-0000-000000000012")
    return {
        "contract_version": 1,
        "operation": "upsert",
        "distribution_id": str(distribution_id),
        "source_app": "knowfeed-app",
        "source_document_id": str(document_id),
        "source_document_version_id": str(version_id),
        "artifact": {
            "artifact_type": "clean_markdown",
            "uri": "s3://development-knowfeed-originals/knowfeed/original/doc/clean.md",
            "storage_version": "version-1",
            "sha256": "a" * 64,
            "size_bytes": 7,
            "content_type": "text/markdown; charset=utf-8",
        },
        "dataset_key": "market-news",
        "idempotency_key": f"knowfeed-app:{version_id}:market-news:artifact-v1",
        "correlation_id": str(distribution_id),
        "causation_id": str(version_id),
        "document": {
            "title": "Example",
            "canonical_url": "https://example.com/news",
            "content_hash": "b" * 64,
            "source_name": "Example Source",
            "published_at": None,
            "metadata": {},
        },
    }


def _patch_s3_http(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.infrastructure.external.artifact_content.httpx.AsyncClient",
        lambda *args, **kwargs: real_async_client(transport=transport),
    )


def _s3_settings() -> Settings:
    return Settings(
        S3_ENDPOINT="http://minio:9000",
        S3_ACCESS_KEY_ID="access",
        S3_SECRET_ACCESS_KEY="secret",
    )


def test_build_idempotency_key_defaults_dataset() -> None:
    payload = KnowledgeIngestionCreate.model_validate(_contract_payload())

    assert build_idempotency_key(payload) == payload.idempotency_key


def test_build_idempotency_key_uses_explicit_value() -> None:
    raw = _contract_payload()
    raw["idempotency_key"] = "custom-key"
    payload = KnowledgeIngestionCreate.model_validate(raw)

    assert build_idempotency_key(payload) == "custom-key"


def test_ingestion_payload_uses_standard_contract_fields() -> None:
    raw = _contract_payload()
    schema_bytes = CONTRACT_PATH.read_bytes()
    manifest = json.loads(CONTRACT_MANIFEST_PATH.read_text())
    assert manifest["major_version"] == 1
    assert hashlib.sha256(schema_bytes).hexdigest() == manifest["sha256"]
    jsonschema.Draft202012Validator(
        json.loads(schema_bytes),
        format_checker=jsonschema.FormatChecker(),
    ).validate(raw)
    payload = KnowledgeIngestionCreate.model_validate(raw)

    assert payload.contract_version == 1
    assert payload.dataset_key == "market-news"
    assert payload.document.canonical_url == "https://example.com/news"
    assert payload.artifact.storage_version == "version-1"


def test_published_artifact_contract_example_is_valid() -> None:
    schema = json.loads(CONTRACT_PATH.read_text())
    example = json.loads(CONTRACT_EXAMPLE_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    ).validate(example)
    KnowledgeIngestionCreate.model_validate(example)


def test_ingestion_payload_rejects_legacy_and_unversioned_artifacts() -> None:
    legacy = {
        "source_app": "knowfeed-app",
        "source_document_id": str(uuid.uuid4()),
        "source_document_version_id": str(uuid.uuid4()),
        "source_artifact_refs": [{"uri": "info-artifact:123"}],
    }
    with pytest.raises(ValidationError):
        KnowledgeIngestionCreate.model_validate(legacy)

    raw = _contract_payload()
    del raw["artifact"]["storage_version"]
    with pytest.raises(ValidationError):
        KnowledgeIngestionCreate.model_validate(raw)

    raw = _contract_payload()
    raw["artifact"]["size_bytes"] = 52_428_801
    with pytest.raises(ValidationError):
        KnowledgeIngestionCreate.model_validate(raw)


def test_database_url_uses_asyncpg_without_sslmode() -> None:
    settings = Settings(
        database_url=(
            "postgresql://knowbase:secret@postgresql:5432/knowbase"
            "?sslmode=require&connect_timeout=10"
        )
    )

    assert settings.database_url == (
        "postgresql+asyncpg://knowbase:secret@postgresql:5432/knowbase"
        "?connect_timeout=10"
    )


def test_build_processing_success_metadata_marks_artifact_verification() -> None:
    job = SimpleNamespace(
        source_artifact_refs=[
            {"artifact_type": "clean"},
            {"artifact_type": "text"},
        ]
    )

    metadata = build_processing_success_metadata(cast(Any, job))

    assert metadata["processor"] == PROCESSOR_NAME
    assert metadata["mode"] == "artifact_verification"
    assert metadata["artifact_ref_count"] == 2
    assert metadata["ragflow"] == "not_requested"


def test_build_ragflow_success_metadata_marks_processed() -> None:
    job = SimpleNamespace(source_artifact_refs=[{"artifact_type": "text"}])

    metadata = build_ragflow_success_metadata(
        cast(Any, job),
        {
            "ragflow_dataset_id": "dataset-1",
            "ragflow_document_name": "doc.txt",
            "ragflow_chunk_count": 3,
        },
    )

    assert metadata["processor"] == RAGFLOW_PROCESSOR_NAME
    assert metadata["mode"] == "ragflow"
    assert metadata["ragflow"] == "processed"
    assert metadata["ragflow_dataset_id"] == "dataset-1"
    assert metadata["ragflow_chunk_count"] == 3


async def test_resolve_artifact_content_verifies_version_size_type_and_hash(
    monkeypatch,
) -> None:
    content = b"# hello"
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        headers = {
            "content-length": str(len(content)),
            "content-type": "text/markdown; charset=utf-8",
            "x-amz-version-id": "version-1",
        }
        return httpx.Response(
            200, headers=headers, content=b"" if request.method == "HEAD" else content
        )

    _patch_s3_http(monkeypatch, handler)
    artifact = await resolve_artifact_content(
        settings=_s3_settings(),
        source_artifact_refs=[
            {
                "artifact_type": "clean_markdown",
                "uri": "s3://development-knowfeed-originals/knowfeed/original/doc/clean.md",
                "storage_version": "version-1",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "content_type": "text/markdown; charset=utf-8",
            }
        ],
        title="Contract Smoke",
        canonical_url=None,
        metadata_json={"markdown": "this inline value must be ignored"},
        source_document_version_id="version-1",
    )

    assert isinstance(artifact, ArtifactContent)
    assert artifact.filename == "clean.md"
    assert artifact.content == content
    assert artifact.content_type == "text/markdown; charset=utf-8"
    assert [method for method, _ in calls] == ["HEAD", "GET"]
    assert all("versionId=version-1" in url for _, url in calls)


async def test_resolve_artifact_content_rejects_non_s3_and_disallowed_bucket() -> None:
    settings = Settings()
    with pytest.raises(ArtifactError, match="only accepts s3"):
        await resolve_artifact_content(
            settings=settings,
            source_artifact_refs=[{"uri": "https://example.com/doc"}],
            title=None,
            canonical_url=None,
            metadata_json={},
            source_document_version_id="version-1",
        )

    ref = _contract_payload()["artifact"]
    ref["uri"] = "s3://private-bucket/knowfeed/original/doc/clean.md"
    with pytest.raises(ArtifactError, match="bucket is not allowed"):
        await resolve_artifact_content(
            settings=settings,
            source_artifact_refs=[ref],
            title=None,
            canonical_url=None,
            metadata_json={},
            source_document_version_id="version-1",
        )

    ref = _contract_payload()["artifact"]
    ref["uri"] = "s3://development-knowfeed-originals/private/doc/clean.md"
    with pytest.raises(ArtifactError, match="outside the allowed prefixes"):
        await resolve_artifact_content(
            settings=settings,
            source_artifact_refs=[ref],
            title=None,
            canonical_url=None,
            metadata_json={},
            source_document_version_id="version-1",
        )


@pytest.mark.parametrize("status_code", [403, 404])
async def test_resolve_artifact_content_classifies_s3_access_failures(
    monkeypatch, status_code: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    _patch_s3_http(monkeypatch, handler)
    with pytest.raises(ArtifactError, match=f"HTTP {status_code}"):
        await resolve_artifact_content(
            settings=_s3_settings(),
            source_artifact_refs=[_contract_payload()["artifact"]],
            title=None,
            canonical_url=None,
            metadata_json={},
            source_document_version_id="version-1",
        )


@pytest.mark.parametrize(
    ("header_version", "expected_hash", "message"),
    [
        (
            "wrong-version",
            hashlib.sha256(b"# hello").hexdigest(),
            "storage version mismatch",
        ),
        ("version-1", "0" * 64, "sha256 mismatch"),
    ],
)
async def test_resolve_artifact_content_rejects_version_and_hash_mismatch(
    monkeypatch, header_version: str, expected_hash: str, message: str
) -> None:
    content = b"# hello"

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {
            "content-length": str(len(content)),
            "content-type": "text/markdown",
            "x-amz-version-id": header_version,
        }
        return httpx.Response(
            200,
            request=request,
            headers=headers,
            content=b"" if request.method == "HEAD" else content,
        )

    _patch_s3_http(monkeypatch, handler)
    ref = _contract_payload()["artifact"]
    ref["sha256"] = expected_hash
    ref["content_type"] = "text/markdown"
    with pytest.raises(ArtifactError, match=message):
        await resolve_artifact_content(
            settings=_s3_settings(),
            source_artifact_refs=[ref],
            title=None,
            canonical_url=None,
            metadata_json={},
            source_document_version_id="version-1",
        )


async def test_resolve_artifact_content_rejects_body_larger_than_declared(
    monkeypatch,
) -> None:
    declared = b"1234567"
    actual = declared + b"8"

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {
            "content-length": str(len(declared)),
            "content-type": "text/markdown",
            "x-amz-version-id": "version-1",
        }
        return httpx.Response(
            200,
            request=request,
            headers=headers,
            content=b"" if request.method == "HEAD" else actual,
        )

    _patch_s3_http(monkeypatch, handler)
    ref = _contract_payload()["artifact"]
    ref["sha256"] = hashlib.sha256(declared).hexdigest()
    ref["content_type"] = "text/markdown"
    with pytest.raises(ArtifactError, match="exceeded the declared artifact size"):
        await resolve_artifact_content(
            settings=_s3_settings(),
            source_artifact_refs=[ref],
            title=None,
            canonical_url=None,
            metadata_json={},
            source_document_version_id="version-1",
        )


def test_s3_sigv4_headers_include_signed_authorization() -> None:
    headers = _s3_sigv4_headers(
        method="GET",
        host="minio.example.test",
        canonical_uri="/bucket/path/doc.txt",
        region="us-east-1",
        access_key="access",
        secret_key="secret",
    )

    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=access/")
    assert (
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in headers["Authorization"]
    )
    assert headers["x-amz-content-sha256"]
    assert headers["x-amz-date"]


def test_ragflow_enabled_requires_base_and_key() -> None:
    assert not Settings(RAGFLOW_API_BASE="http://ragflow:9380").ragflow_enabled
    assert not Settings(RAGFLOW_API_KEY="token").ragflow_enabled
    assert Settings(
        RAGFLOW_API_BASE="http://ragflow:9380", RAGFLOW_API_KEY="token"
    ).ragflow_enabled


async def test_bare_ragflow_ingestion_is_disabled() -> None:
    with pytest.raises(RuntimeError, match="bare RAGFlow ingestion disabled"):
        await ingest_into_ragflow(
            settings=Settings(),
            target_dataset="target",
            title="Smoke",
            canonical_url=None,
            source_artifact_refs=[],
            metadata_json={},
            source_document_version_id="version-1",
        )


async def test_ragflow_client_wraps_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": 503, "message": "unavailable"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ragflow = RAGFlowClient(
        Settings(RAGFLOW_API_BASE="http://ragflow:9380", RAGFLOW_API_KEY="token"),
        client=client,
    )

    with pytest.raises(RAGFlowError, match="RAGFlow HTTP request failed"):
        await ragflow.list_datasets()

    await client.aclose()


def test_celery_delivery_options_use_platform_queue(monkeypatch) -> None:
    monkeypatch.setenv("CELERY_QUEUE", "knowbase.admin.default")
    from core.config import get_settings

    get_settings.cache_clear()
    options = CeleryProducer()._delivery_options()

    assert options == {
        "queue": "knowbase.admin.default",
        "exchange": "knowbase.admin.default",
        "routing_key": "knowbase.admin.default",
    }
    get_settings.cache_clear()


def test_classify_ingestion_error_marks_ragflow_config_error() -> None:
    status, metadata = classify_ingestion_error(
        RAGFlowError("No default embedding model is set.")
    )

    assert status == "ragflow_config_error"
    assert metadata == {"error_type": "ragflow_config_error"}


def test_classify_ingestion_error_marks_artifact_unreadable() -> None:
    status, metadata = classify_ingestion_error(
        RAGFlowError("No readable artifact content found for ingestion job")
    )

    assert status == "artifact_unreadable"
    assert metadata == {"error_type": "artifact_unreadable"}


def test_classify_ingestion_error_marks_external_ragflow_error() -> None:
    status, metadata = classify_ingestion_error(RAGFlowError("upstream unavailable"))

    assert status == "external_api_error"
    assert metadata == {"error_type": "external_api_error", "system": "ragflow"}


def test_classify_ingestion_error_marks_explicit_parse_failure() -> None:
    status, metadata = classify_ingestion_error(
        RAGFlowParseError("embedding provider returned a transient TLS failure")
    )

    assert status == "ragflow_parse_failed"
    assert metadata == {"error_type": "ragflow_parse_failed"}


async def test_ragflow_config_check_reports_missing_default_embedding() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/datasets":
            return httpx.Response(200, json={"code": 0, "data": []})
        if request.method == "GET" and request.url.path == "/api/v1/users/me/models":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "tenant_id": "tenant-1",
                        "name": "tenant",
                        "llm_id": "chat-model",
                    },
                },
            )
        return httpx.Response(404, json={"code": 404, "message": "unexpected"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    class TestRAGFlowClient(RAGFlowClient):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings, client=client)

    import app.infrastructure.external.ragflow as ragflow_module

    original_client = ragflow_module.RAGFlowClient
    ragflow_module.RAGFlowClient = TestRAGFlowClient
    try:
        result = await check_ragflow_config(
            Settings(RAGFLOW_API_BASE="http://ragflow:9380", RAGFLOW_API_KEY="token")
        )
    finally:
        ragflow_module.RAGFlowClient = original_client
        await client.aclose()

    assert result.enabled
    assert result.reachable
    assert not result.has_default_embedding
    assert result.issues == ["RAGFlow tenant has no default embedding model"]
    assert result.details["dataset_list_accessible"] is True
    assert result.details["tenant_id"] == "tenant-1"


async def test_service_ragflow_config_check_masks_secret(monkeypatch) -> None:
    async def fake_check(settings: Settings) -> RAGFlowConfigCheck:
        return RAGFlowConfigCheck(
            enabled=True,
            reachable=True,
            has_default_embedding=True,
            issues=[],
            details={"api_key_configured": True, "tenant_id": "tenant-1"},
        )

    monkeypatch.setattr(
        "app.application.services.knowledge_ingestion_service.check_ragflow_config",
        fake_check,
    )

    result = await get_ragflow_config_check()

    assert result["ready"] is True
    assert result["details"] == {"api_key_configured": True, "tenant_id": "tenant-1"}
    assert "api_key" not in result["details"]


class _StubRAGFlowClient:
    """按序返回若干份 document 快照，用尽后重复最后一份。"""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.calls = 0

    async def get_document(self, dataset_id: str, document_id: str) -> dict[str, Any]:
        del dataset_id, document_id
        doc = self._docs[min(self.calls, len(self._docs) - 1)]
        self.calls += 1
        return doc


async def _wait(
    docs: list[dict[str, Any]],
    *,
    timeout_seconds: int = 5,
    interval_seconds: float = 0,
):
    return await _wait_for_document_parse(
        client=_StubRAGFlowClient(docs),  # type: ignore[arg-type]
        dataset_id="dataset-1",
        document_id="document-1",
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )


@pytest.mark.anyio
async def test_cancelled_parse_is_not_success() -> None:
    """被取消的解析必须报错。

    此前 CANCEL 与 DONE 同在 terminal 集合、只有 FAIL 抛错，于是取消的文档
    走完 complete_ragflow_ingestion：version.status 落 "indexed"、job 落
    "succeeded"。前者会被检索侧 _eligible_versions 选中（内容残缺却照常返回），
    后者被 retry_ingestion_job 明确拒绝重试，连补救入口都没有。
    """
    with pytest.raises(RAGFlowParseCancelledError):
        await _wait([{"id": "document-1", "run": "CANCEL", "progress": 0}])


@pytest.mark.anyio
async def test_cancelled_parse_is_retryable_but_distinguishable() -> None:
    status, metadata = classify_ingestion_error(
        RAGFlowParseCancelledError("cancelled by operator")
    )
    # 复用可重试的 ragflow_parse_failed（它不在 RETRY_BLOCKED_STATUSES 里）
    assert status == "ragflow_parse_failed"
    # 但 error_type 要能区分：取消多为运维动作，排查方向与解析失败不同
    assert metadata["error_type"] == "ragflow_parse_cancelled"


@pytest.mark.anyio
async def test_progress_alone_does_not_signal_completion() -> None:
    """progress 不再单独构成成功条件。

    RAGFlow 在同一次更新里写 {"run": DONE, "progress": 1}
    （见其 api/db/services/document_service.py），progress 不会先于 run 到 1。
    因此以 progress 抢跑只有坏处：某些路径下 run 尚未落定就被当成完成。
    """
    # timeout 必须 > 0：否则循环一次都不进，新旧实现都走超时分支，测试会空转。
    with pytest.raises(RAGFlowParseError):
        await _wait(
            [{"id": "document-1", "run": "RUNNING", "progress": 1.0}],
            timeout_seconds=1,
            interval_seconds=0.01,
        )


@pytest.mark.anyio
async def test_numeric_run_codes_are_recognised() -> None:
    """RAGFlow 库里 run 是数字，HTTP 端点映射为文本；两种都要认。

    只认文本时，若某版本直接吐数字，所有取值都落不进终态，
    只会一路轮询到超时——失效是静默的。
    """
    done = await _wait([{"id": "document-1", "run": "3"}])
    assert done["run"] == "3"

    with pytest.raises(RAGFlowParseCancelledError):
        await _wait([{"id": "document-1", "run": "2"}])

    with pytest.raises(RAGFlowParseError):
        await _wait([{"id": "document-1", "run": "4"}])


@pytest.mark.anyio
async def test_parse_polls_until_terminal() -> None:
    doc = await _wait(
        [
            {"id": "document-1", "run": "UNSTART"},
            {"id": "document-1", "run": "RUNNING", "progress": 0.5},
            {"id": "document-1", "run": "DONE", "chunk_count": 2},
        ]
    )
    assert doc["chunk_count"] == 2
