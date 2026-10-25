"""按关系校验其他应用的服务令牌（SDD 0042，做法同 knowmart、knowlab 的 relation_auth）。

knowbase 的用户资料接口接受四种关系：knowlab 签测试授权（user-data-grant-test）、
运行方凭授权读（user-data-read，现在只有 knowbench）、knowmart 成交复制
（user-data-purchase）、应用把自己原来存的资料搬进自己的分区（user-data-import，
knowlab）。一种关系可以有多个调用方：`SERVICE_AUTH_<关系>_CALLERS` 列出调用方，
每个调用方一组 `SERVICE_AUTH_<关系>_<调用方>_AUDIENCE`（调用方的 client_id，即
Casdoor 给 client_credentials 令牌的 aud）、`_SUBJECT_ALLOWLIST`
（`admin/<调用方服务应用>`）、`_REQUIRED_SCOPE`（`knowbase:<关系>`），都由部署从
调用方配置推出来（`common/backend/service-client`）。关系名带连字符，环境变量里
换成下划线（`SERVICE_AUTH_USER_DATA_READ_CALLERS`），与调用方 service_client 的
`_key` 一致。校验通过后返回调用方是谁：读、导入以这个应用的名义。一组都没配就是
这种关系没开，一律拒绝。

签名、签发方、有效期用模板的 OIDC 客户端核（与浏览器登录同一个 Casdoor、同一套
密钥）。部署下发的 `_CASDOOR_APPLICATION`、`_DISCOVERY_URL`、`_BACKCHANNEL_ENDPOINT`
这里不用。knowfeed 摄入、knowbench 检索仍走老的 INTERNAL_AUTH_*、RETRIEVAL_AUTH_*。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Header

from app.application.audit_context import set_actor
from app.application.errors.exceptions import (
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.domain.security import Principal
from app.infrastructure.security.oidc import OidcProviderClient
from core.config import Settings, get_settings

Relation = Literal[
    "user-data-grant-test", "user-data-read", "user-data-purchase", "user-data-import"
]


CALLER = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def _key(name: str) -> str:
    return name.upper().replace("-", "_")


@dataclass(frozen=True)
class RelationBinding:
    relation: str
    caller: str
    audience: str
    subjects: frozenset[str]
    required_scope: str


@dataclass(frozen=True)
class ServiceCaller:
    """通过校验的调用方：哪个应用、以哪种关系。"""

    caller: str
    principal: Principal


def relation_bindings(
    relation: str, environ: Mapping[str, str] | None = None
) -> list[RelationBinding]:
    env = os.environ if environ is None else environ
    callers = [
        c.strip()
        for c in env.get(f"SERVICE_AUTH_{_key(relation)}_CALLERS", "").split(",")
    ]
    bindings: list[RelationBinding] = []
    for caller in callers:
        if not CALLER.fullmatch(caller):
            continue
        prefix = f"SERVICE_AUTH_{_key(relation)}_{_key(caller)}_"
        audience = env.get(prefix + "AUDIENCE", "").strip()
        subjects = frozenset(
            s.strip()
            for s in env.get(prefix + "SUBJECT_ALLOWLIST", "").split(",")
            if s.strip()
        )
        scope = env.get(prefix + "REQUIRED_SCOPE", "").strip()
        if audience and subjects and scope == f"knowbase:{relation}":
            bindings.append(
                RelationBinding(relation, caller, audience, subjects, scope)
            )
    return bindings


class RelationServiceVerifier:
    def __init__(
        self,
        relation: Relation,
        *,
        settings: Settings | None = None,
        oidc_client: OidcProviderClient | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._relation = relation
        self._settings = settings or get_settings()
        self._environ = environ
        self._oidc = oidc_client or OidcProviderClient(
            self._settings, self._settings.browser_profile("admin")
        )

    async def verify(self, encoded: str) -> ServiceCaller:
        bindings = relation_bindings(self._relation, self._environ)
        if not bindings:
            raise ServiceUnavailableError(
                "This service relation is not configured",
                code="service_relation_unconfigured",
            )
        if not encoded or len(encoded) > 16384:
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            )
        # 受众就是调用方的 client_id：逐个调用方试，签名、签发方、有效期每次都核
        claims: dict[str, Any] | None = None
        binding = bindings[0]
        for candidate in bindings:
            try:
                claims = await self._oidc.verify_access_token(
                    encoded, audience=candidate.audience
                )
            except UnauthorizedError as exc:
                if exc.code == "audience_mismatch":
                    continue
                raise
            binding = candidate
            break
        if claims is None:
            raise UnauthorizedError(
                "The service identity audience is invalid", code="audience_mismatch"
            )
        subject = claims.get("sub")
        issuer = claims.get("iss")
        if not isinstance(subject, str) or not isinstance(issuer, str) or not issuer:
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            )
        if subject not in binding.subjects:
            raise ForbiddenError(
                "The service identity is not bound to this relation",
                code="service_subject_unbound",
            )
        if binding.required_scope not in _scopes(claims):
            raise ForbiddenError(
                "The service identity scope is not authorized",
                code="service_scope_denied",
            )
        return ServiceCaller(
            caller=binding.caller,
            principal=Principal(
                actor_type="service",
                subject=subject,
                issuer=issuer,
                app=self._settings.app_slug,
                surface="internal",
                audience=binding.audience,
                scopes=frozenset({binding.required_scope}),
                authenticated_at=_timestamp(claims.get("iat")),
                expires_at=_timestamp(claims.get("exp")),
                policy_version="service-v1",
            ),
        )


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, (int, float)):
        raise UnauthorizedError(
            "The service identity token is invalid", code="token_invalid"
        )
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise UnauthorizedError(
            "The service identity token is invalid", code="token_invalid"
        ) from exc


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    raw = claims.get("scope", claims.get("scp", ()))
    if isinstance(raw, str):
        return frozenset(re.split(r"\s+", raw.strip())) if raw.strip() else frozenset()
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return frozenset(raw)
    raise UnauthorizedError(
        "The service identity token has invalid scopes", code="token_invalid"
    )


def _bearer(authorization: str | None) -> str:
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not encoded or " " in encoded:
        raise UnauthorizedError(
            "A Bearer service identity is required", code="service_auth_required"
        )
    return encoded


_verifiers: dict[str, RelationServiceVerifier] = {}


def relation_verifier(relation: Relation) -> RelationServiceVerifier:
    if relation not in _verifiers:
        _verifiers[relation] = RelationServiceVerifier(relation)
    return _verifiers[relation]


async def _verify(relation: Relation, authorization: str | None) -> ServiceCaller:
    caller = await relation_verifier(relation).verify(_bearer(authorization))
    set_actor(caller.principal.subject)
    return caller


async def require_grant_test_service(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ServiceCaller:
    return await _verify("user-data-grant-test", authorization)


async def require_read_service(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ServiceCaller:
    return await _verify("user-data-read", authorization)


async def require_purchase_service(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ServiceCaller:
    return await _verify("user-data-purchase", authorization)


async def require_import_service(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ServiceCaller:
    return await _verify("user-data-import", authorization)
