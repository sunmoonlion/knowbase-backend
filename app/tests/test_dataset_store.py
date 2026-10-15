"""数据集来源：本地文件优先；否则按钉住的 sha256 从对象存储取回，不匹配不用。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from app.infrastructure.external.dataset_store import DatasetUnavailable, ensure_dataset
from core.config import Settings

BODY = b"SQLite format 3\x00" + b"x" * 100
SHA = hashlib.sha256(BODY).hexdigest()


def settings(tmp_path: Path, **extra) -> Settings:
    values = {
        "knowbase_dataset_path": str(tmp_path / "ds" / "mini.sqlite"),
        "knowbase_dataset_object": (
            "s3://development-knowbase-datasets/lesson23/mini.sqlite"
        ),
        "knowbase_dataset_sha256": SHA,
        "S3_ENDPOINT": "http://minio.test",
        "S3_ACCESS_KEY_ID": "AKIA",
        "S3_SECRET_ACCESS_KEY": "secret",
        **extra,
    }
    return Settings(_env_file=None, **values)


def transport(body: bytes = BODY, status: int = 200, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def test_fetches_signed_object_and_pins_sha(tmp_path: Path):
    seen: list[httpx.Request] = []
    path = ensure_dataset(settings(tmp_path), transport(seen=seen))
    assert path.read_bytes() == BODY
    request = seen[0]
    assert request.url.path == "/development-knowbase-datasets/lesson23/mini.sqlite"
    assert request.headers["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIA/"
    )
    # second call: local file, no request
    ensure_dataset(settings(tmp_path), transport(seen=seen))
    assert len(seen) == 1


def test_mismatched_or_failed_fetch_leaves_nothing(tmp_path: Path):
    with pytest.raises(DatasetUnavailable, match="sha256"):
        ensure_dataset(settings(tmp_path), transport(body=b"tampered"))
    assert not (tmp_path / "ds" / "mini.sqlite").exists()
    assert not (tmp_path / "ds" / "mini.sqlite.partial").exists()
    with pytest.raises(DatasetUnavailable, match="HTTP 403"):
        ensure_dataset(settings(tmp_path), transport(status=403))
    with pytest.raises(DatasetUnavailable, match="sha256 is required"):
        ensure_dataset(settings(tmp_path, knowbase_dataset_sha256=None), transport())
    with pytest.raises(DatasetUnavailable, match="s3://"):
        ensure_dataset(
            settings(tmp_path, knowbase_dataset_object="http://x/y"), transport()
        )


def test_local_file_must_match_pin_when_pinned(tmp_path: Path):
    target = tmp_path / "ds" / "mini.sqlite"
    target.parent.mkdir()
    target.write_bytes(b"other")
    with pytest.raises(DatasetUnavailable, match="local dataset"):
        ensure_dataset(settings(tmp_path), transport())
    target.write_bytes(BODY)
    assert ensure_dataset(settings(tmp_path), transport()) == target
