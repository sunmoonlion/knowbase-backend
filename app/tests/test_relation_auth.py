"""按关系校验用户资料接口的服务令牌（SDD 0042；做法同 knowmart）。

环境变量按平台实际下发的样子写：关系名带连字符，变量里换成下划线。
"""

from __future__ import annotations

import time

import pytest

from app.application.errors.exceptions import (
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.infrastructure.security.relation_auth import (
    RelationServiceVerifier,
    relation_bindings,
)
from core.config import Settings

READ_AUD = "sunmoon-knowbench-knowbase-user-data-read"
READ_SUB = "admin/sunmoonai-knowbench-knowbase-user-data-read"

ENV = {
    "SERVICE_AUTH_USER_DATA_READ_CALLERS": "knowbench",
    "SERVICE_AUTH_USER_DATA_READ_KNOWBENCH_AUDIENCE": READ_AUD,
    "SERVICE_AUTH_USER_DATA_READ_KNOWBENCH_SUBJECT_ALLOWLIST": READ_SUB,
    "SERVICE_AUTH_USER_DATA_READ_KNOWBENCH_REQUIRED_SCOPE": "knowbase:user-data-read",
    "SERVICE_AUTH_USER_DATA_GRANT_TEST_CALLERS": "knowlab",
    "SERVICE_AUTH_USER_DATA_GRANT_TEST_KNOWLAB_AUDIENCE": (
        "sunmoon-knowlab-knowbase-user-data-grant-test"
    ),
    "SERVICE_AUTH_USER_DATA_GRANT_TEST_KNOWLAB_SUBJECT_ALLOWLIST": (
        "admin/sunmoonai-knowlab-knowbase-user-data-grant-test"
    ),
    "SERVICE_AUTH_USER_DATA_GRANT_TEST_KNOWLAB_REQUIRED_SCOPE": (
        "knowbase:user-data-grant-test"
    ),
}


def settings() -> Settings:
    return Settings(_env_file=None, casdoor_endpoint="https://identity.example.test")


class FakeOidc:
    def __init__(self, aud: str = READ_AUD, **claims: object) -> None:
        now = int(time.time())
        self.claims = {
            "iss": "https://identity.example.test",
            "sub": READ_SUB,
            "scope": "knowbase:user-data-read",
            "iat": now,
            "exp": now + 900,
        } | claims
        self.aud = aud
        self.tried: list[str] = []

    async def verify_access_token(self, encoded: str, *, audience: str) -> dict:
        self.tried.append(audience)
        if audience != self.aud:
            raise UnauthorizedError("wrong audience", code="audience_mismatch")
        return dict(self.claims)


def verifier(
    oidc: FakeOidc, env: dict | None = None, relation: str = "user-data-read"
) -> RelationServiceVerifier:
    return RelationServiceVerifier(
        relation,  # type: ignore[arg-type]
        settings=settings(),
        oidc_client=oidc,  # type: ignore[arg-type]
        environ=ENV if env is None else env,
    )


def test_bindings_read_the_hyphenated_relation_with_underscores() -> None:
    assert [b.caller for b in relation_bindings("user-data-read", ENV)] == ["knowbench"]
    assert [b.caller for b in relation_bindings("user-data-grant-test", ENV)] == [
        "knowlab"
    ]
    assert relation_bindings("user-data-purchase", ENV) == []


def test_bindings_need_all_three_and_the_matching_scope() -> None:
    bad_scope = ENV | {
        "SERVICE_AUTH_USER_DATA_READ_KNOWBENCH_REQUIRED_SCOPE": (
            "knowbase:user-data-purchase"
        )
    }
    assert relation_bindings("user-data-read", bad_scope) == []
    no_subject = ENV | {"SERVICE_AUTH_USER_DATA_READ_KNOWBENCH_SUBJECT_ALLOWLIST": " "}
    assert relation_bindings("user-data-read", no_subject) == []
    no_callers = ENV | {"SERVICE_AUTH_USER_DATA_READ_CALLERS": ""}
    assert relation_bindings("user-data-read", no_callers) == []


@pytest.mark.asyncio
async def test_bound_subject_with_scope_is_accepted_as_that_caller() -> None:
    oidc = FakeOidc()
    caller = await verifier(oidc).verify("token")
    assert caller.caller == "knowbench"
    assert oidc.tried == [READ_AUD]
    assert caller.principal.subject == READ_SUB
    assert caller.principal.scopes == frozenset({"knowbase:user-data-read"})


@pytest.mark.asyncio
async def test_a_token_for_one_relation_does_not_open_another() -> None:
    # knowbench 读的令牌拿去签测试授权：受众对不上 knowlab 那一组
    with pytest.raises(UnauthorizedError):
        await verifier(FakeOidc(), relation="user-data-grant-test").verify("token")


@pytest.mark.asyncio
async def test_unconfigured_relation_is_unavailable() -> None:
    with pytest.raises(ServiceUnavailableError):
        await verifier(FakeOidc(), relation="user-data-purchase").verify("token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "error"),
    [
        (
            {"sub": "admin/sunmoonai-knowlab-knowbase-user-data-grant-test"},
            ForbiddenError,
        ),
        ({"scope": "knowbase:retrieve"}, ForbiddenError),
        ({"scope": ""}, ForbiddenError),
        ({"scope": 7}, UnauthorizedError),
        ({"iat": "soon"}, UnauthorizedError),
        ({"iss": ""}, UnauthorizedError),
    ],
)
async def test_wrong_subject_scope_or_claims_are_rejected(claims: dict, error) -> None:
    with pytest.raises(error):
        await verifier(FakeOidc(**claims)).verify("token")


@pytest.mark.asyncio
async def test_empty_or_oversized_tokens_are_rejected() -> None:
    for token in ("", "x" * 16385):
        with pytest.raises(UnauthorizedError):
            await verifier(FakeOidc()).verify(token)


@pytest.mark.asyncio
async def test_unknown_audience_is_rejected() -> None:
    with pytest.raises(UnauthorizedError):
        await verifier(FakeOidc(aud="someone-else")).verify("token")
