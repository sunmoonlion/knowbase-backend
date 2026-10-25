"""用户资料的对象存储（S3 兼容，SigV4 签名，SDD 0042）。

凭据单独一套 `USER_DATA_S3_*`（`external/artifact_content.py` 用的 `S3_*` 是知识源
只读身份）。那边只取不写，这里要放、取、删、复制，所以单写一份带请求体摘要与复制头
的签名，不动那边的代码。对象存储的证书按 `AWS_CA_BUNDLE` 核：`SSL_CERT_FILE` 是给
知识源那套连接的。
"""

from __future__ import annotations

import hashlib
import hmac
import ssl
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from app.domain.user_data import UserDataError
from core.config import Settings


class S3UserObjects:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if (
            not settings.user_data_s3_endpoint
            or not settings.user_data_s3_access_key_id
            or not settings.user_data_s3_secret_access_key
            or not settings.user_data_bucket
        ):
            raise UserDataError(
                503, "storage_unconfigured", "user data storage is not configured"
            )
        parsed = urlsplit(settings.user_data_s3_endpoint.rstrip("/"))
        if not parsed.scheme or not parsed.netloc or parsed.path not in ("", "/"):
            raise UserDataError(
                503, "storage_unconfigured", "USER_DATA_S3_ENDPOINT is invalid"
            )
        self._scheme = parsed.scheme
        self._endpoint_host = parsed.netloc
        self._bucket = settings.user_data_bucket
        self._path_style = settings.user_data_s3_force_path_style
        self._region = settings.user_data_s3_region
        self._access_key = settings.user_data_s3_access_key_id
        self._secret_key = settings.user_data_s3_secret_access_key
        self._timeout = settings.user_data_storage_timeout_seconds
        self._transport = transport
        self._verify: ssl.SSLContext | bool = True
        if parsed.scheme == "https" and settings.aws_ca_bundle:
            try:
                self._verify = ssl.create_default_context(cafile=settings.aws_ca_bundle)
            except (OSError, ssl.SSLError):
                raise UserDataError(
                    503, "storage_unconfigured", "AWS_CA_BUNDLE cannot be loaded"
                ) from None

    async def put(self, key: str, content: bytes, media_type: str) -> None:
        response = await self._request(
            "PUT", key, body=content, extra={"content-type": media_type}
        )
        self._ok(response)

    async def get(self, key: str) -> bytes:
        response = await self._request("GET", key)
        self._ok(response)
        return response.content

    async def delete(self, key: str) -> None:
        response = await self._request("DELETE", key)
        if response.status_code not in (200, 204, 404):
            self._ok(response)

    async def copy(self, source_key: str, target_key: str) -> None:
        source = "/" + quote(f"{self._bucket}/{source_key}", safe="/")
        response = await self._request(
            "PUT", target_key, extra={"x-amz-copy-source": source}
        )
        self._ok(response)
        # S3 复制出错时可能回 200、错误写在正文里
        if b"<Error>" in response.content[:200]:
            raise UserDataError(503, "storage_failed", "object copy failed")

    async def _request(
        self,
        method: str,
        key: str,
        *,
        body: bytes = b"",
        extra: dict[str, str] | None = None,
    ) -> httpx.Response:
        parts = [quote(p, safe="") for p in key.split("/")]
        if self._path_style:
            host = self._endpoint_host
            path = "/" + "/".join([quote(self._bucket, safe=""), *parts])
        else:
            host = f"{self._bucket}.{self._endpoint_host}"
            path = "/" + "/".join(parts)
        headers = sign(
            method=method,
            host=host,
            path=path,
            body=body,
            extra=extra or {},
            region=self._region,
            access_key=self._access_key,
            secret_key=self._secret_key,
        )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, verify=self._verify
            ) as client:
                return await client.request(
                    method,
                    f"{self._scheme}://{host}{path}",
                    content=body,
                    headers=headers,
                )
        except httpx.HTTPError:
            raise UserDataError(
                503, "storage_failed", "object storage is unreachable"
            ) from None

    @staticmethod
    def _ok(response: httpx.Response) -> None:
        if response.status_code == 404:
            raise UserDataError(503, "storage_missing", "stored object is missing")
        if response.status_code >= 300:
            raise UserDataError(
                503, "storage_failed", f"object storage answered {response.status_code}"
            )


def sign(
    *,
    method: str,
    host: str,
    path: str,
    body: bytes,
    extra: dict[str, str],
    region: str,
    access_key: str,
    secret_key: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """AWS SigV4：签 host、请求体摘要、时间与额外的头（内容类型、复制来源）。"""
    moment = now or datetime.now(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    datestamp = moment.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        **{k.lower(): v.strip() for k, v in extra.items()},
    }
    names = sorted(headers)
    signed_headers = ";".join(names)
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in names)
    canonical_request = "\n".join(
        [method, path, "", canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = hmac.new(f"AWS4{secret_key}".encode(), datestamp.encode(), hashlib.sha256)
    key = hmac.new(key.digest(), region.encode(), hashlib.sha256)
    key = hmac.new(key.digest(), b"s3", hashlib.sha256)
    key = hmac.new(key.digest(), b"aws4_request", hashlib.sha256)
    signature = hmac.new(
        key.digest(), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers
