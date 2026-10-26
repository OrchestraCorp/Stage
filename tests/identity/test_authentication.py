"""Protocol boundaries: signatures do not imply the right issuer, usage or subject kind."""

import base64
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.identity.authentication import (
    AuthenticationRejected,
    AuthenticationUnavailable,
    KeycloakAuthenticator,
)
from tests.support.identity_tokens import (
    jwks,
    profile,
    token,
)
from tests.support.identity_tokens import (
    key as key,
)


async def test_human_evidence_omits_unknown_authentication_strength(key: rsa.RSAPrivateKey) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=jwks(key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verifier = KeycloakAuthenticator(profile(), http_client=client)
        first = await verifier.authenticate(token(key))
        second = await verifier.authenticate(token(key))
    assert first.subject_kind == "human"
    assert first.authenticated_at is None and first.acr is None and first.amr == ()
    assert first.evidence_valid_until <= first.credential_expires_at
    assert first.evidence_valid_until > datetime.now(UTC)
    assert first.evidence_ref != second.evidence_ref
    assert len(requests) == 1
    assert requests[0].url == profile().jwks_url
    assert "Authorization" not in requests[0].headers
    assert "access_token" not in repr(first)


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://attacker.example"},
        {"aud": "maestro"},
        {"azp": "unregistered"},
        {"typ": "ID"},
        {"stage_credential_kind": "workload_oauth2"},
        {"exp": 1},
        {"exp": True},
        {"exp": str(2**32)},
        {"iat": 2**32},
        {"nbf": 2**32},
        {"sub": ""},
        {"sub": 123},
        {"sub": "subject\x00suffix"},
        {"sub": "subject\ud800suffix"},
        {"auth_time": 2**32},
        {"amr": "mfa"},
        {"acr": 2},
        {"acr": "level\x00suffix"},
        {"amr": ["pwd\x00suffix"]},
        {"scope": ["openid"]},
    ],
)
async def test_wrong_profile_or_malformed_claims_rejected(key: rsa.RSAPrivateKey, claims: dict[str, Any]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        verifier = KeycloakAuthenticator(profile(), http_client=client)
        with pytest.raises(AuthenticationRejected, match="invalid_credential"):
            await verifier.authenticate(token(key, claims=claims))


@pytest.mark.parametrize(
    "headers",
    [
        {"jku": "https://attacker.example"},
        {"x5u": "https://attacker.example"},
        {"jwk": {}},
        {"crit": ["exp"]},
        {"typ": "at+jwt"},
    ],
)
async def test_token_cannot_select_trust_or_other_profile(key: rsa.RSAPrivateKey, headers: dict[str, Any]) -> None:
    def no_fetch(_: httpx.Request) -> httpx.Response:
        pytest.fail("untrusted header must not cause network access")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_fetch)) as client:
        with pytest.raises(AuthenticationRejected):
            await KeycloakAuthenticator(profile(), http_client=client).authenticate(token(key, headers=headers))


async def test_service_profile_cannot_reuse_human_namespace(key: rsa.RSAPrivateKey) -> None:
    service = profile(
        credential_kind="workload_oauth2", allowed_clients=("worker",), subject_namespace="keycloak-service-v1"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        verifier = KeycloakAuthenticator(service, http_client=client)
        with pytest.raises(AuthenticationRejected):
            await verifier.authenticate(token(key))
        evidence = await verifier.authenticate(
            token(key, claims={"azp": "worker", "stage_credential_kind": "workload_oauth2"})
        )
    assert evidence.subject_kind == "service" and evidence.subject_namespace == "keycloak-service-v1"
    assert evidence.subject == "subject-1"  # Same sub never supplies a human interpretation.


async def test_signature_and_algorithm_are_verified(key: rsa.RSAPrivateKey) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        verifier = KeycloakAuthenticator(profile(), http_client=client)
        with pytest.raises(AuthenticationRejected):
            await verifier.authenticate(token(other))
        with pytest.raises(AuthenticationRejected):
            await verifier.authenticate(
                jwt.encode({"sub": "x"}, "not-an-rsa-key-but-long-enough-for-hmac", algorithm="HS256")
            )


async def test_unknown_kid_refresh_is_bounded_and_rotation_is_supported(key: rsa.RSAPrivateKey) -> None:
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=jwks(key, "key-1" if request_count == 1 else "key-2"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verifier = KeycloakAuthenticator(profile(), http_client=client)
        await verifier.authenticate(token(key))
        for _ in range(10):
            with pytest.raises(AuthenticationRejected):
                await verifier.authenticate(token(key, headers={"kid": "unknown"}))
        assert request_count == 1
        verifier._last_attempt -= 6
        rotated = await verifier.authenticate(token(key, headers={"kid": "key-2"}))
        assert rotated.key_id == "key-2" and request_count == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://attacker.example"}),
        httpx.Response(503),
        httpx.Response(200, content=b"x" * 131073),
        httpx.Response(200, json={"keys": []}),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"keys": [None]}),
    ],
)
async def test_jwks_failure_is_unavailable(key: rsa.RSAPrivateKey, response: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(AuthenticationUnavailable):
            await KeycloakAuthenticator(profile(), http_client=client).authenticate(token(key))


async def test_expired_cache_does_not_allow_verification_during_outage(key: rsa.RSAPrivateKey) -> None:
    online = True
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)) if online else httpx.Response(503))
    ) as client:
        verifier = KeycloakAuthenticator(profile(), http_client=client)
        await verifier.authenticate(token(key))
        online = False
        verifier._expires_at = 0
        verifier._last_attempt -= 6
        with pytest.raises(AuthenticationUnavailable):
            await verifier.authenticate(token(key))


@pytest.mark.parametrize(
    "changes",
    [
        {"issuer": "http://idp.example"},
        {"jwks_url": "https://idp.example/keys#fragment"},
        {"jwks_url": "https://user:password@idp.example/keys"},
        {"environment": "production", "issuer": "http://127.0.0.1:18080", "allow_loopback_http": True},
        {"allowed_clients": ()},
        {"evidence_ttl_seconds": 301},
    ],
)
def test_insecure_profile_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        profile(**changes)


@pytest.mark.parametrize("header", ["{", "[]", '{"alg":"RS256","typ":"JWT","kid":"key-1","x":' + "1" * 5000 + "}"])
async def test_malformed_json_never_causes_server_error(key: rsa.RSAPrivateKey, header: str) -> None:
    encoded = base64.urlsafe_b64encode(header.encode()).rstrip(b"=").decode()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        with pytest.raises(AuthenticationRejected):
            await KeycloakAuthenticator(profile(), http_client=client).authenticate(encoded + ".e30.c2ln")


async def test_deep_or_oversized_token_is_rejected(key: rsa.RSAPrivateKey) -> None:
    header = '{"alg":"RS256","typ":"JWT","kid":"key-1","x":' + "[" * 5000 + "0" + "]" * 5000 + "}"
    encoded = base64.urlsafe_b64encode(header.encode()).rstrip(b"=").decode() + ".e30.c2ln"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        verifier = KeycloakAuthenticator(profile(), http_client=client)
        for invalid in (encoded, "x" * 16385):
            with pytest.raises(AuthenticationRejected):
                await verifier.authenticate(invalid)
