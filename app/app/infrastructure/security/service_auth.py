from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import Header

from app.application.errors.exceptions import (
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.domain.security import Principal
from app.infrastructure.security import OidcProviderClient
from core.config import Settings, get_settings


class ServiceAuthVerifier:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        relation: Literal["ingest", "retrieve"] = "ingest",
    ) -> None:
        self._settings = settings or get_settings()
        self._relation = relation
        if relation == "retrieve":
            application = self._settings.retrieval_auth_casdoor_application
            discovery_url = self._settings.retrieval_auth_discovery_url
            backchannel_endpoint = self._settings.retrieval_auth_backchannel_endpoint
            audience = self._settings.retrieval_auth_audience
        else:
            application = self._settings.internal_auth_casdoor_application
            discovery_url = self._settings.internal_auth_discovery_url
            backchannel_endpoint = self._settings.internal_auth_backchannel_endpoint
            audience = self._settings.internal_auth_audience
        service_discovery_url = discovery_url or self._settings.casdoor_discovery_url
        service_endpoint = self._settings.casdoor_endpoint
        if service_discovery_url:
            parsed = urlsplit(service_discovery_url)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                service_endpoint = urlunsplit(
                    (parsed.scheme, parsed.netloc, "", "", "")
                )
        service_settings = self._settings.model_copy(
            update={
                "casdoor_endpoint": service_endpoint,
                "casdoor_application": application,
                "casdoor_discovery_url": service_discovery_url,
                "casdoor_backchannel_endpoint": backchannel_endpoint,
                "casdoor_client_id": audience or "",
                "casdoor_client_secret": "",
                "casdoor_redirect_uri": "",
            }
        )
        self._oidc = OidcProviderClient(
            service_settings,
            self._settings.browser_profile("admin"),
        )

    async def verify(self, encoded: str) -> Principal:
        if self._relation == "retrieve":
            expected_audience = self._settings.retrieval_auth_audience
            allowed_subjects = self._settings.retrieval_auth_subjects
            required_scope = self._settings.retrieval_auth_required_scope
        else:
            expected_audience = self._settings.internal_auth_audience
            allowed_subjects = self._settings.internal_auth_subjects
            required_scope = self._settings.internal_auth_required_scope
        if not expected_audience or not allowed_subjects:
            raise ServiceUnavailableError(
                "internal service identity binding is not configured"
            )

        claims = await self._oidc.verify_access_token(
            encoded,
            audience=expected_audience,
        )
        subject = claims.get("sub")
        issuer = claims.get("iss")
        if not isinstance(subject, str) or subject not in allowed_subjects:
            raise ForbiddenError("service subject is not bound")
        if not isinstance(issuer, str) or not issuer:
            raise UnauthorizedError("service token issuer invalid")
        self._validate_scope(claims)
        try:
            authenticated_at = datetime.fromtimestamp(int(claims["iat"]), tz=UTC)
            expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
            raise UnauthorizedError("service token timestamps invalid") from exc
        return Principal(
            actor_type="service",
            subject=subject,
            issuer=issuer,
            app="knowbase",
            surface="internal",
            audience=expected_audience,
            roles=(),
            scopes=frozenset({required_scope}),
            authenticated_at=authenticated_at,
            expires_at=expires_at,
            policy_version="service-v1",
        )

    def _validate_scope(self, claims: dict[str, Any]) -> None:
        # Casdoor client-credentials tokens may expose only the provider's
        # `openid` scope. The relation-specific scope is granted by the local
        # subject allowlist and represented on the returned Principal.
        raw_scope = claims.get("scope", claims.get("scp"))
        if raw_scope is None:
            return
        if isinstance(raw_scope, str):
            return
        elif isinstance(raw_scope, list):
            if not all(isinstance(item, str) for item in raw_scope):
                raise UnauthorizedError("service token scope invalid")
            return
        else:
            raise UnauthorizedError("service token scope invalid")


_verifier: ServiceAuthVerifier | None = None
_retrieval_verifier: ServiceAuthVerifier | None = None


def get_service_auth_verifier() -> ServiceAuthVerifier:
    global _verifier
    if _verifier is None:
        _verifier = ServiceAuthVerifier()
    return _verifier


def get_retrieval_service_auth_verifier() -> ServiceAuthVerifier:
    global _retrieval_verifier
    if _retrieval_verifier is None:
        _retrieval_verifier = ServiceAuthVerifier(relation="retrieve")
    return _retrieval_verifier


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise UnauthorizedError("service bearer token required")
    try:
        scheme, encoded = authorization.split(" ", 1)
    except ValueError as exc:
        raise UnauthorizedError("service bearer token required") from exc
    if scheme.lower() != "bearer" or not encoded.strip():
        raise UnauthorizedError("service bearer token required")
    return encoded.strip()


async def require_knowbase_ingest_service(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Principal:
    return await get_service_auth_verifier().verify(_bearer_token(authorization))


async def require_knowbase_retrieve_service(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Principal:
    return await get_retrieval_service_auth_verifier().verify(
        _bearer_token(authorization)
    )
