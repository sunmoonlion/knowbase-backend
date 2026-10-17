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

from app.application.errors.exceptions import ForbiddenError
from app.application.services.knowledge_ingestion_service import (
    stable_knowledge_document_id,
    stable_knowledge_version_id,
)
from app.application.services.knowledge_retrieval_service import _assemble_response
from app.domain.security import Principal
from app.infrastructure.external.ragflow import RAGFlowClient, RAGFlowRetrievalResult
from app.infrastructure.external.ragflow_provider import normalize_retrieval
from app.interfaces.schemas.retrieval import (
    Citation,
    KnowledgeRetrievalRequest,
    KnowledgeRetrievalResponse,
)
from core.config import Settings

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts/retrieval/v1"
MANIFEST_PATH = CONTRACT_DIR / "contract-manifest.json"


def _example(name: str) -> dict[str, Any]:
    return json.loads((CONTRACT_DIR / "examples" / name).read_text())


def _request(**updates: Any) -> KnowledgeRetrievalRequest:
    raw = _example("request.json")
    raw.update(updates)
    return KnowledgeRetrievalRequest.model_validate(raw)


def _version(**updates: Any) -> Any:
    values = {
        "id": uuid.UUID("50a7f1ae-0b4f-4dc4-b33c-604df996334a"),
        "knowledge_document_id": uuid.UUID("417070e0-403f-4dd5-a3c8-ae8f16e0c6c4"),
        "source_app": "knowfeed-app",
        "source_document_id": uuid.UUID("83bb7926-47f0-4262-8215-27da1cc1e681"),
        "source_document_version_id": uuid.UUID("359ea32d-90b3-47e4-9168-930bfb768d0d"),
        "dataset_key": "market-news",
        "content_hash": "d" * 64,
        "title": "Market policy update",
        "source_uri": "https://example.com/policy/update#fragment",
        "access_scope": ["tenant:sunmoonai"],
        "provider_dataset_id": "ragflow-dataset-1",
        "provider_document_id": "ragflow-document-1",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def test_retrieval_contract_manifest_and_examples_are_valid() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert manifest["major"] == 1
    for relative_path, expected_digest in manifest["files"].items():
        path = CONTRACT_DIR / relative_path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_digest

    pairs = (
        ("knowledge-retrieval-request.schema.json", "request.json"),
        ("knowledge-retrieval-response.schema.json", "response.json"),
        ("citation.schema.json", "citation.json"),
    )
    for schema_name, example_name in pairs:
        schema = json.loads((CONTRACT_DIR / schema_name).read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(_example(example_name))

    KnowledgeRetrievalRequest.model_validate(_example("request.json"))
    KnowledgeRetrievalResponse.model_validate(_example("response.json"))
    Citation.model_validate(_example("citation.json"))


def test_request_rejects_blank_query_duplicate_dataset_and_provider_filter() -> None:
    with pytest.raises(ValidationError):
        _request(query="   ")
    with pytest.raises(ValidationError):
        _request(dataset_keys=["market-news", "market-news"])
    raw = _example("request.json")
    raw["filters"]["ragflow_document_ids"] = ["provider-id"]
    with pytest.raises(ValidationError):
        KnowledgeRetrievalRequest.model_validate(raw)


def test_domain_identity_is_stable_and_provider_independent() -> None:
    job = cast(
        Any,
        SimpleNamespace(
            source_app="knowfeed-app",
            source_document_id=uuid.UUID("83bb7926-47f0-4262-8215-27da1cc1e681"),
            source_document_version_id=uuid.UUID(
                "359ea32d-90b3-47e4-9168-930bfb768d0d"
            ),
            target_dataset="market-news",
        ),
    )
    first = stable_knowledge_document_id(job)
    second = stable_knowledge_document_id(job)
    version = stable_knowledge_version_id(first, job.source_document_version_id)

    assert first == second
    assert version != first
    assert "ragflow" not in str(first)


def test_response_drops_unmapped_provider_chunks_and_hides_provider_ids() -> None:
    result = RAGFlowRetrievalResult(
        chunks=[
            {
                "id": "private-chunk-1",
                "dataset_id": "ragflow-dataset-1",
                "document_id": "ragflow-document-1",
                "content": "verified evidence",
                "similarity": 0.9,
                "term_similarity": 0.8,
                "vector_similarity": 0.95,
            },
            {
                "id": "private-chunk-2",
                "dataset_id": "other-dataset",
                # A globally unique document ID is not sufficient proof of
                # dataset ownership.  The full provider binding must match.
                "document_id": "ragflow-document-1",
                "content": "must not leak",
                "similarity": 1.0,
            },
        ],
        total=2,
    )

    response = _assemble_response(
        _request(), normalize_retrieval(result), [cast(Any, _version())]
    )
    raw = response.model_dump(mode="json")

    assert len(response.evidence) == 1
    assert response.evidence[0].content == "verified evidence"
    assert response.evidence[0].source_uri == "https://example.com/policy/update"
    serialized = json.dumps(raw)
    assert "private-chunk" not in serialized
    assert "ragflow-dataset" not in serialized
    assert "ragflow-document" not in serialized


def test_token_budget_is_enforced_and_marked_truncated() -> None:
    result = RAGFlowRetrievalResult(
        chunks=[
            {
                "id": "chunk-1",
                "dataset_id": "ragflow-dataset-1",
                "document_id": "ragflow-document-1",
                "content": "abcdefghij",
                "similarity": 0.8,
            }
        ],
        total=1,
    )

    response = _assemble_response(
        _request(token_budget=4),
        normalize_retrieval(result),
        [cast(Any, _version())],
    )

    assert response.evidence[0].content == "abcd"
    assert response.evidence[0].token_estimate == 4
    assert response.evidence[0].truncated
    assert response.truncated


def test_citation_projection_contains_only_safe_relative_source_link() -> None:
    evidence = KnowledgeRetrievalResponse.model_validate(
        _example("response.json")
    ).evidence[0]
    citation = Citation.from_evidence(evidence)
    raw = citation.model_dump(mode="json")

    assert citation.source_href == (
        f"/api/web/v1/citations/{evidence.evidence_id}/source"
    )
    assert "source_uri" not in raw
    assert "provider_metadata" not in raw
    assert "ragflow" not in json.dumps(raw)


async def test_ragflow_retrieval_uses_only_resolved_provider_bindings() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "chunks": [
                        {
                            "id": "chunk-1",
                            "dataset_id": "dataset-1",
                            "document_id": "document-1",
                            "content": "answer",
                        }
                    ],
                    "total": 1,
                },
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RAGFlowClient(
        Settings(RAGFLOW_API_BASE="http://ragflow:9380", RAGFLOW_API_KEY="secret"),
        client=http_client,
    )
    result = await client.retrieve(
        question="question",
        dataset_ids=["dataset-1"],
        document_ids=["document-1"],
        top_k=5,
    )
    await http_client.aclose()

    assert result.total == 1
    assert captured["dataset_ids"] == ["dataset-1"]
    assert captured["document_ids"] == ["document-1"]
    assert captured["question"] == "question"


def test_dataset_allowlist_configuration_has_no_wildcard_semantics() -> None:
    settings = Settings(RETRIEVAL_DATASET_ALLOWLIST="market-news,policy")
    assert settings.retrieval_datasets == frozenset({"market-news", "policy"})
    assert "*" not in settings.retrieval_datasets


def _principal(scope: str = "knowbase:retrieve") -> Principal:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return Principal(
        actor_type="service",
        subject="knowbench-agent-worker",
        issuer="https://identity.example.test",
        app="knowbase",
        surface="internal",
        audience="retrieve-client",
        roles=(),
        scopes=frozenset({scope}),
        authenticated_at=now,
        expires_at=now + timedelta(minutes=5),
        policy_version="knowledge-v1",
    )


@pytest.mark.asyncio
async def test_unknown_dataset_is_forbidden_before_provider_call(monkeypatch) -> None:
    from app.application.services.knowledge_retrieval_service import retrieve_knowledge
    from core.config import get_settings

    monkeypatch.setenv("RETRIEVAL_DATASET_ALLOWLIST", "allowed")
    get_settings.cache_clear()
    with pytest.raises(ForbiddenError, match="dataset"):
        await retrieve_knowledge(
            cast(Any, None),
            _request(dataset_keys=["unknown"]),
            service_principal=_principal(),
        )
    get_settings.cache_clear()


def test_citation_source_href_resolves_to_a_real_route() -> None:
    """契约声明的 source_href 必须真的能 GET 到。

    O6 的成因：`citation.schema.json` 与两侧 DTO 都写 `/api/citations/{id}/source`，
    而真实路由挂在 `/api/web/v1/` 下——照契约字面拼路径去 GET 一律 404。
    三处各自断言"字符串等于某个常量"，谁也没有和路由表比对过，所以三处一起错、
    测试全绿。这里改为拿契约的正则去**真实路由表**里找。
    """
    import re
    import uuid as _uuid

    from fastapi.routing import APIRoute

    from app.application.dto.retrieval import Citation
    from app.bootstrap.api import create_app

    pattern = Citation.model_fields["source_href"].metadata[-1].pattern
    sample = f"/api/web/v1/citations/{_uuid.uuid4()}/source"
    assert re.match(pattern, sample), "样例须先满足契约正则"

    # 把真实路由的 {param} 占位换成 uuid，再看契约正则能否匹配上任何一条
    routes = [r.path for r in create_app().routes if isinstance(r, APIRoute)]
    concrete = [re.sub(r"\{[^}]+\}", str(_uuid.uuid4()), p) for p in routes]
    assert any(re.match(pattern, p) for p in concrete), (
        f"契约 source_href 的 pattern {pattern!r} 匹配不到任何真实路由；"
        f"含 citations 的路由有：{[p for p in routes if 'citations' in p]}"
    )
