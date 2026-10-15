"""数据集文件的来源（0006 F-KNOW-07）：本地路径，或按 sha256 钉住的一个 S3 对象。

对象存储里的数据集是版本化的：配置写 `s3://bucket/key` 与 sha256，
取回后必须匹配，否则不用。
下载到本地路径（容器里是 /tmp 的 emptyDir）后复用；不缓存跨进程状态。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from app.infrastructure.external.artifact_content import _s3_sigv4_headers
from core.config import Settings

MAX_DATASET_BYTES = 512 * 1024 * 1024


class DatasetUnavailable(RuntimeError):
    """数据集拿不到或校验不过；消息可对外，不含端点与凭据。"""


def parse_object(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise DatasetUnavailable("dataset object must be s3://bucket/key")
    key = parsed.path.lstrip("/")
    if ".." in key.split("/") or key.startswith("/"):
        raise DatasetUnavailable("dataset object key is invalid")
    return parsed.netloc, key


def ensure_dataset(
    settings: Settings, transport: httpx.BaseTransport | None = None
) -> Path:
    """返回可用的本地文件路径；需要时从对象存储取回并校验。"""
    path = Path(settings.knowbase_dataset_path)
    expected = (settings.knowbase_dataset_sha256 or "").lower()
    if path.is_file():
        if expected and _sha256(path) != expected:
            raise DatasetUnavailable("local dataset does not match the pinned sha256")
        return path
    if not settings.knowbase_dataset_object:
        raise DatasetUnavailable(
            "dataset file missing and no dataset object configured"
        )
    bucket, key = parse_object(settings.knowbase_dataset_object)
    return fetch_pinned_object(
        settings,
        bucket=bucket,
        key=key,
        sha256=expected,
        path=path,
        transport=transport,
    )


def fetch_pinned_object(
    settings: Settings,
    *,
    bucket: str,
    key: str,
    sha256: str,
    path: Path,
    version_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """从对象存储取一个对象到 path，必须与给定的 sha256 一致，否则不留文件。"""
    expected = sha256.lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise DatasetUnavailable(
            "a pinned sha256 is required to fetch a dataset object"
        )
    if (
        not settings.s3_endpoint
        or not settings.s3_access_key_id
        or not settings.s3_secret_access_key
    ):
        raise DatasetUnavailable("S3 credentials are not configured")
    if ".." in key.split("/") or key.startswith("/") or not key:
        raise DatasetUnavailable("dataset object key is invalid")
    endpoint = urlsplit(settings.s3_endpoint.rstrip("/"))
    if settings.s3_force_path_style:
        uri = "/" + "/".join(quote(p, safe="") for p in [bucket, *key.split("/")])
        host = endpoint.netloc
    else:
        uri = "/" + "/".join(quote(p, safe="") for p in key.split("/"))
        host = f"{bucket}.{endpoint.netloc}"
    query = f"versionId={quote(version_id, safe='')}" if version_id else ""
    url = f"{endpoint.scheme}://{host}{uri}" + (f"?{query}" if query else "")
    headers = _s3_sigv4_headers(
        method="GET",
        host=host,
        canonical_uri=uri,
        region=settings.s3_region,
        access_key=settings.s3_access_key_id,
        secret_key=settings.s3_secret_access_key,
        **({"canonical_query": query} if query else {}),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    hasher = hashlib.sha256()
    received = 0
    try:
        with httpx.Client(timeout=60, transport=transport) as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    code = response.status_code
                    raise DatasetUnavailable(
                        f"dataset object request failed with HTTP {code}"
                    )
                with open(partial, "wb") as out:
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > MAX_DATASET_BYTES:
                            raise DatasetUnavailable(
                                "dataset object exceeds the size limit"
                            )
                        hasher.update(chunk)
                        out.write(chunk)
    except httpx.HTTPError as exc:
        _discard(partial)
        raise DatasetUnavailable("dataset object request failed") from exc
    except DatasetUnavailable:
        _discard(partial)
        raise
    if hasher.hexdigest() != expected:
        _discard(partial)
        raise DatasetUnavailable("fetched dataset does not match the pinned sha256")
    os.replace(partial, path)
    return path


class ObjectDatasetFiles:
    """登记的数据集文件：按 sha256 放在缓存目录里，没有就取回。

    文件名就是校验值，所以缓存里的文件要么不存在，要么是对的那一份；
    已有文件用之前再核一次，被改动过就丢掉重取。
    """

    def __init__(
        self, settings: Settings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    def _path(self, dataset) -> Path:
        return (
            Path(self._settings.knowbase_dataset_cache_dir)
            / f"{dataset.sha256}.sqlite"
        )

    def fetched(self, dataset) -> bool:
        """文件在不在缓存里。文件名就是校验值，取回时核对过才会落到这个名字上。"""
        return self._path(dataset).is_file()

    def ensure(self, dataset) -> Path:
        allowed = {
            b.strip()
            for b in self._settings.knowbase_dataset_allowed_buckets.split(",")
            if b.strip()
        }
        if dataset.bucket not in allowed:
            raise DatasetUnavailable("dataset bucket is not allowed")
        path = self._path(dataset)
        if path.is_file():
            if _sha256(path) == dataset.sha256:
                return path
            _discard(path)
        return fetch_pinned_object(
            self._settings,
            bucket=dataset.bucket,
            key=dataset.object_key,
            sha256=dataset.sha256,
            path=path,
            version_id=dataset.object_version_id,
            transport=self._transport,
        )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
