from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ArtifactRef(BaseModel):
    artifact_type: Literal["clean_markdown", "text_plain"]
    uri: str = Field(min_length=8, max_length=8192)
    storage_version: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1, le=52_428_800)
    content_type: str = Field(
        pattern=r"^(text/markdown|text/plain)(;\s*charset=[A-Za-z0-9._-]+)?$"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("uri")
    @classmethod
    def validate_s3_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        key = unquote(parsed.path.lstrip("/"))
        if parsed.scheme != "s3" or not parsed.netloc or not key:
            raise ValueError("artifact uri must be an s3://bucket/key reference")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError(
                "artifact uri must not contain query, fragment or userinfo"
            )
        if any(part in {"", ".", ".."} for part in key.split("/")):
            raise ValueError("artifact object key contains an invalid path segment")
        return value


class ArtifactDocument(BaseModel):
    title: str = Field(min_length=1, max_length=4096)
    canonical_url: str = Field(min_length=1, max_length=8192)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_name: str | None = Field(default=None, max_length=255)
    published_at: datetime | None = None
    metadata: dict = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class KnowledgeIngestionCreate(BaseModel):
    contract_version: Literal[1]
    operation: Literal["upsert"]
    distribution_id: uuid.UUID
    source_app: Literal["knowfeed-app"]
    source_document_id: uuid.UUID
    source_document_version_id: uuid.UUID
    artifact: ArtifactRef
    dataset_key: str = Field(
        min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$"
    )
    idempotency_key: str = Field(min_length=1, max_length=255)
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None
    document: ArtifactDocument

    model_config = ConfigDict(extra="forbid")


class KnowledgeIngestionStatusUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=30)
    last_error: str | None = None
    metadata: dict = Field(default_factory=dict)
    knowledge_document_id: str | None = Field(default=None, max_length=255)
    ragflow_document_id: str | None = Field(default=None, max_length=255)


class KnowledgeIngestionRetryRequest(BaseModel):
    force: bool = False
    reason: str | None = Field(default=None, max_length=500)


class RAGFlowConfigCheckRead(BaseModel):
    enabled: bool
    reachable: bool
    has_default_embedding: bool
    ready: bool
    issues: list[str]
    details: dict = Field(default_factory=dict)


class KnowledgeIngestionRead(BaseModel):
    id: uuid.UUID
    source_app: str
    source_document_id: uuid.UUID
    source_document_version_id: uuid.UUID
    target_dataset: str | None
    profile_key: str
    idempotency_key: str
    title: str | None
    canonical_url: str | None
    source_name: str | None
    content_hash: str | None
    source_artifact_refs: list[dict]
    metadata_json: dict
    payload: dict
    status: str
    last_error: str | None
    status_history: list[dict]
    knowledge_document_id: str | None
    ragflow_document_id: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)
