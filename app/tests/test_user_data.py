"""用户资料的公共件（SDD 0042 起，第四版沿用）：本人令牌、对象存储、读格式。

v1 的资料、同意依据、运行授权与 `user-data/v1` 接口 10-28 已删（decisions ⑰），它们的
测试一并删了。这里还放着第四版各测试共用的替身（`CSV`、`MemoryObjects`、`Clock`）。
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime

import httpx
import pytest

from app.domain.user_data import (
    Owner,
    UserDataError,
    check_size,
    clean_name,
    format_view,
    kind_of,
)
from app.infrastructure.security.user_token import UserTokenVerifier
from app.infrastructure.storage.user_objects import S3UserObjects, sign
from core.config import Settings

ALICE_LAB = Owner("alice", "knowlab")
CSV = b"year,revenue\n2023,10\n"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx(*sheets: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        names = "".join(
            f'<sheet name="{n}" sheetId="{i}"/>' for i, n in enumerate(sheets)
        )
        book.writestr(
            "xl/workbook.xml", f"<workbook><sheets>{names}</sheets></workbook>"
        )
    return buffer.getvalue()


class MemoryObjects:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes, media_type: str) -> None:
        self.data[key] = content

    async def get(self, key: str) -> bytes:
        return self.data[key]

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def copy(self, source_key: str, target_key: str) -> None:
        self.data[target_key] = self.data[source_key]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


# —— 本人令牌 ——


class FakeOidc:
    def __init__(self, claims: dict) -> None:
        self.claims = claims
        self.audience: str | None = None

    async def verify_access_token(self, encoded: str, *, audience: str) -> dict:
        self.audience = audience
        return self.claims


def verifier(claims: dict, **settings) -> tuple[UserTokenVerifier, FakeOidc]:
    base = {
        "user_token_audience": "knowbase-client",
        "user_token_apps_json": json.dumps(
            {"bench-client": "knowbench", "lab-client": "knowlab"}
        ),
    }
    oidc = FakeOidc(claims)
    return UserTokenVerifier(Settings(**{**base, **settings}), oidc_client=oidc), oidc  # type: ignore[arg-type]


async def test_a_user_token_names_the_user_and_the_acting_app():
    v, oidc = verifier(
        {"sub": "alice", "azp": "lab-client", "tokenType": "access-token"}
    )
    assert await v.verify("token") == ALICE_LAB
    assert oidc.audience == "knowbase-client"


@pytest.mark.parametrize(
    ("claims", "settings", "status", "code"),
    [
        ({"sub": "alice", "azp": "mart-client"}, {}, 403, "user_token_app_denied"),
        (
            {"sub": "alice", "azp": "bench-client", "tokenType": "id-token"},
            {},
            401,
            "token_invalid",
        ),
        ({"sub": " ", "azp": "bench-client"}, {}, 401, "token_invalid"),
        (
            {"sub": "alice", "azp": "bench-client"},
            {"user_token_audience": ""},
            503,
            "user_token_unconfigured",
        ),
    ],
)
async def test_user_tokens_that_are_refused(claims, settings, status, code):
    v, _ = verifier(claims, **settings)
    from app.application.errors.exceptions import AppException

    with pytest.raises(AppException) as caught:
        await v.verify("token")
    assert (caught.value.status_code, caught.value.code) == (status, code)


# —— 对象存储 ——


def test_sigv4_matches_the_aws_get_object_example():
    """AWS 文档「GET Object」示例（examplebucket、Range 头、2013-05-24）。"""
    headers = sign(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        body=b"",
        extra={"Range": "bytes=0-9"},
        region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert headers["Authorization"].endswith(
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


async def test_object_store_puts_gets_copies_and_deletes_in_its_bucket():
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"body" if request.method == "GET" else b"")

    store = S3UserObjects(
        Settings(
            **{
                "user_data_s3_endpoint": "http://minio:9000",
                "user_data_s3_access_key_id": "k",
                "user_data_s3_secret_access_key": "s",
                "user_data_bucket": "user-data",
            }
        ),
        transport=httpx.MockTransport(answer),
    )
    await store.put("users/ab/x/knowbench/1", b"abc", "text/csv")
    assert await store.get("users/ab/x/knowbench/1") == b"body"
    await store.copy("users/ab/x/knowmart/2", "users/cd/y/knowlab/3")
    await store.delete("users/ab/x/knowbench/1")
    assert [(r.method, r.url.path) for r in seen] == [
        ("PUT", "/user-data/users/ab/x/knowbench/1"),
        ("GET", "/user-data/users/ab/x/knowbench/1"),
        ("PUT", "/user-data/users/cd/y/knowlab/3"),
        ("DELETE", "/user-data/users/ab/x/knowbench/1"),
    ]
    assert seen[0].headers["x-amz-content-sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert seen[2].headers["x-amz-copy-source"] == "/user-data/users/ab/x/knowmart/2"
    assert "x-amz-copy-source" in seen[2].headers["authorization"]


def test_object_store_refuses_to_start_without_configuration():
    with pytest.raises(UserDataError) as caught:
        S3UserObjects(Settings(user_data_s3_endpoint="http://minio:9000"))
    assert caught.value.code == "storage_unconfigured"


def test_object_store_does_not_borrow_the_knowledge_source_credentials():
    """S3_* 是知识源只读身份（只读 knowfeed 的桶）：只配它，用户资料照样未配置。"""
    with pytest.raises(UserDataError) as caught:
        S3UserObjects(
            Settings(
                **{
                    "S3_ENDPOINT": "http://minio:9000",
                    "S3_ACCESS_KEY_ID": "k",
                    "S3_SECRET_ACCESS_KEY": "s",
                    "user_data_bucket": "user-data",
                }
            )
        )
    assert caught.value.code == "storage_unconfigured"


def test_object_store_checks_tls_with_the_object_storage_ca(tmp_path):
    settings = {
        "user_data_s3_endpoint": "https://object-storage:9000",
        "user_data_s3_access_key_id": "k",
        "user_data_s3_secret_access_key": "s",
        "user_data_bucket": "knowbase-originals",
    }
    missing = str(tmp_path / "missing.crt")
    with pytest.raises(UserDataError) as caught:
        S3UserObjects(Settings(**settings, AWS_CA_BUNDLE=missing))
    assert caught.value.code == "storage_unconfigured"
    assert S3UserObjects(Settings(**settings))._verify is True


# —— 收文件的检查与读格式 ——


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("", CSV, "name_invalid"),
        ("a/b.csv", CSV, "name_invalid"),
        ("run.exe", b"MZ", "type_not_allowed"),
        ("fake.pdf", b"hello", "content_mismatch"),
        ("gbk.csv", "营收".encode("gbk"), "not_utf8"),
    ],
)
def test_bad_files_are_refused(name, content, code):
    with pytest.raises(UserDataError) as caught:
        kind_of(clean_name(name), content)
    assert caught.value.code == code


def test_size_and_quota():
    with pytest.raises(UserDataError) as caught:
        check_size(0, 0)
    assert caught.value.code == "empty"
    with pytest.raises(UserDataError) as caught:
        check_size(10, 1024 * 1024 * 1024 - 5)
    assert (caught.value.status, caught.value.code) == (413, "quota_exceeded")


def test_format_view_gives_sheet_names_or_the_first_line_only():
    view = format_view(XLSX, xlsx("资产负债表", "利润表"))
    assert view.sheets == ("资产负债表", "利润表") and view.header is None
    assert format_view("text/csv; charset=utf-8", CSV).header == "year,revenue"
    assert format_view(XLSX, b"PK\x03\x04 broken").sheets == ()
    pdf = format_view("application/pdf", b"%PDF-1.7")
    assert pdf.sheets == () and pdf.header is None
