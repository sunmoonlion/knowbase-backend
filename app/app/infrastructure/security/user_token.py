"""用户本人的凭据（SDD 0042 第六节）。

应用后端拿用户登录时 Casdoor 发给本应用的访问令牌，按 RFC 8693 换一张
`aud` = knowbase 的令牌，连同请求一起送来。这里验：签名（JWKS）、`iss`、`aud`、
`exp`、`iat`，再按 `azp` 认是哪个应用代办；只认配置里登记的三个应用。
光有服务身份加一个用户名不算本人（数据拓扑第 5.1 节）。
2026-10-23 在临时 Casdoor 4.12.0 上实测过换出来的令牌长什么样
（k8s 仓 scripts/results/opus-0042-token-exchange-lab）。
"""

from __future__ import annotations

from fastapi import Header

from app.application.errors.exceptions import (
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.domain.user_data import Owner
from app.infrastructure.security.oidc import OidcProviderClient
from core.config import Settings, get_settings


class UserTokenVerifier:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        oidc_client: OidcProviderClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._oidc = oidc_client or OidcProviderClient(
            self._settings, self._settings.browser_profile("admin")
        )

    async def verify(self, encoded: str) -> Owner:
        audience = self._settings.user_token_audience.strip()
        apps = self._settings.user_token_apps
        if not audience or not apps:
            raise ServiceUnavailableError(
                "User tokens are not configured", code="user_token_unconfigured"
            )
        if not encoded or len(encoded) > 16384:
            raise UnauthorizedError("The user token is invalid", code="token_invalid")
        claims = await self._oidc.verify_access_token(encoded, audience=audience)
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
            raise UnauthorizedError("The user token is invalid", code="token_invalid")
        # Casdoor 的访问令牌带 tokenType；有就必须是访问令牌，挡住拿别的令牌冒充
        token_type = claims.get("tokenType")
        if token_type is not None and token_type != "access-token":
            raise UnauthorizedError("The user token is invalid", code="token_invalid")
        app = apps.get(claims.get("azp") or "")
        if app is None:
            raise ForbiddenError(
                "The acting application is not allowed", code="user_token_app_denied"
            )
        return Owner(subject=subject, app=app)  # type: ignore[arg-type]


_verifier: UserTokenVerifier | None = None


def _get_verifier() -> UserTokenVerifier:
    global _verifier
    if _verifier is None:
        _verifier = UserTokenVerifier(get_settings())
    return _verifier


async def require_user_owner(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Owner:
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not encoded or " " in encoded:
        raise UnauthorizedError(
            "A Bearer user token is required", code="user_auth_required"
        )
    return await _get_verifier().verify(encoded)
