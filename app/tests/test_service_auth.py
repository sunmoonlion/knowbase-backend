from __future__ import annotations

import time

import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from app.application.errors.exceptions import ForbiddenError, UnauthorizedError
from app.infrastructure.security.oidc import OidcMetadata
from app.infrastructure.security.service_auth import ServiceAuthVerifier
from core.config import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        casdoor_endpoint="https://identity.example.test",
        internal_auth_casdoor_application="sunmoonai-knowfeed-knowbase-ingest",
        internal_auth_audience="service-client",
        internal_auth_subject_allowlist="service-subject",
        internal_auth_required_scope="knowbase:ingest",
    )


def _retrieval_settings() -> Settings:
    return _settings().model_copy(
        update={
            "retrieval_auth_casdoor_application": (
                "sunmoonai-investment-knowbase-retrieve"
            ),
            "retrieval_auth_audience": "retrieval-client",
            "retrieval_auth_subject_allowlist": "investment-agent-worker",
            "retrieval_auth_required_scope": "knowbase:retrieve",
        }
    )


def test_service_verifier_uses_explicit_service_discovery_url() -> None:
    settings = _settings().model_copy(
        update={
            "casdoor_discovery_url": "https://identity.example.test/.well-known/browser/openid-configuration",
            "internal_auth_discovery_url": "https://identity.example.test/.well-known/openid-configuration",
        }
    )
    verifier = ServiceAuthVerifier(settings)

    assert verifier._oidc._settings.casdoor_discovery_url == (
        "https://identity.example.test/.well-known/openid-configuration"
    )


def test_internal_discovery_uses_only_explicit_service_backchannel() -> None:
    settings = _settings().model_copy(
        update={
            "casdoor_endpoint": "https://browser-identity.example.test",
            "casdoor_backchannel_endpoint": "http://casdoor-sunmoonai:8000",
            "internal_auth_discovery_url": (
                "https://identity.example.test/.well-known/openid-configuration"
            ),
            "internal_auth_backchannel_endpoint": "http://casdoor-service:8000",
        }
    )
    verifier = ServiceAuthVerifier(settings)

    assert verifier._oidc._settings.casdoor_endpoint == "https://identity.example.test"
    assert verifier._oidc._settings.casdoor_backchannel_endpoint == (
        "http://casdoor-service:8000"
    )


def test_internal_discovery_does_not_inherit_browser_backchannel() -> None:
    settings = _settings().model_copy(
        update={
            "casdoor_backchannel_endpoint": "http://browser-casdoor:8000",
            "internal_auth_discovery_url": (
                "https://identity.example.test/.well-known/openid-configuration"
            ),
        }
    )
    verifier = ServiceAuthVerifier(settings)

    assert verifier._oidc._settings.casdoor_backchannel_endpoint is None


def _bind_test_key(verifier: ServiceAuthVerifier, key: RSAKey) -> None:
    now = time.monotonic()
    verifier._oidc._metadata = OidcMetadata(
        issuer="https://identity.example.test",
        authorization_endpoint="https://identity.example.test/authorize",
        token_endpoint="https://identity.example.test/token",
        jwks_uri="https://identity.example.test/jwks",
    )
    verifier._oidc._metadata_loaded_at = now
    verifier._oidc._key_set = KeySet.import_key_set(
        {"keys": [key.as_dict(private=False)]}
    )
    verifier._oidc._keys_loaded_at = now


def _token(key: RSAKey, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://identity.example.test",
        "sub": "service-subject",
        "aud": "service-client",
        "iat": now,
        "exp": now + 300,
        "scope": "knowbase:ingest",
    }
    claims.update(overrides)
    return jwt.encode(
        {"alg": "RS256", "kid": "test-key"},
        claims,
        key,
        algorithms=["RS256"],
    )


@pytest.mark.asyncio
async def test_service_token_is_verified_and_bound_to_relation() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    verifier = ServiceAuthVerifier(_settings())
    _bind_test_key(verifier, key)

    principal = await verifier.verify(_token(key))

    assert principal.actor_type == "service"
    assert principal.subject == "service-subject"
    assert principal.app == "knowbase"
    assert principal.surface == "internal"
    assert principal.scopes == frozenset({"knowbase:ingest"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides, error",
    [
        ({"aud": "wrong-client"}, UnauthorizedError),
        ({"sub": "other-service"}, ForbiddenError),
        ({"scope": 42}, UnauthorizedError),
        ({"exp": int(time.time()) - 60}, UnauthorizedError),
    ],
)
async def test_service_token_negative_matrix(
    overrides: dict, error: type[Exception]
) -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    verifier = ServiceAuthVerifier(_settings())
    _bind_test_key(verifier, key)

    with pytest.raises(error):
        await verifier.verify(_token(key, **overrides))


@pytest.mark.asyncio
async def test_retrieval_relation_is_independent_from_ingestion_relation() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    verifier = ServiceAuthVerifier(_retrieval_settings(), relation="retrieve")
    _bind_test_key(verifier, key)
    retrieval_token = _token(
        key,
        sub="investment-agent-worker",
        aud="retrieval-client",
        scope="openid",
    )

    principal = await verifier.verify(retrieval_token)

    assert principal.subject == "investment-agent-worker"
    assert principal.audience == "retrieval-client"
    assert principal.scopes == frozenset({"knowbase:retrieve"})


@pytest.mark.asyncio
async def test_ingestion_credential_cannot_call_retrieval_relation() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    verifier = ServiceAuthVerifier(_retrieval_settings(), relation="retrieve")
    _bind_test_key(verifier, key)

    with pytest.raises(UnauthorizedError, match="audience"):
        await verifier.verify(_token(key))
