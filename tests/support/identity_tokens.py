"""Shared synthetic builders for identity tokens."""

import json
import time
from dataclasses import replace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.identity.authentication import (
    KeycloakProfile,
)


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def profile(**changes: Any) -> KeycloakProfile:
    return replace(
        KeycloakProfile(
            profile_ref="local-human",
            profile_version=1,
            issuer="https://idp.example/realms/stage",
            jwks_url="https://idp.example/realms/stage/protocol/openid-connect/certs",
            audience="stage-api",
            credential_kind="human_oidc",
            allowed_clients=("maestro",),
            subject_namespace="keycloak-human-v1",
            environment="test",
        ),
        **changes,
    )


def token(
    key: rsa.RSAPrivateKey, *, claims: dict[str, Any] | None = None, headers: dict[str, Any] | None = None
) -> str:
    now = int(time.time())
    payload = {
        "iss": profile().issuer,
        "sub": "subject-1",
        "aud": "stage-api",
        "azp": "maestro",
        "iat": now,
        "exp": now + 300,
        "typ": "Bearer",
        "stage_credential_kind": "human_oidc",
    }
    payload.update(claims or {})
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "key-1", "typ": "JWT", **(headers or {})})


def jwks(key: rsa.RSAPrivateKey, kid: str = "key-1") -> dict[str, Any]:
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**public, "kid": kid, "alg": "RS256", "use": "sig"}]}
