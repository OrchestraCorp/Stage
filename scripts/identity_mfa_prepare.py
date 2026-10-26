"""Prepare an isolated password + TOTP Keycloak protocol fixture, never Stage grants.

The output contains test credentials. Import it only into a separate disposable
Keycloak data directory; do not import it into the standing development realm.
"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.identity_idp_prepare import write_private


def fixture_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    config = {
        "issuer": "http://127.0.0.1:18082/realms/stage-mfa-development",
        "audience": "stage-api",
        "human_client": "stage-mfa-desktop",
        "weak_client": "stage-weak-negative-fixture",
        "human_username": "stage-mfa-human",
        "human_password": secrets.token_urlsafe(32),
        "human_subject": str(uuid4()),
        "otp_secret": secrets.token_urlsafe(32),
        "redirect_uri": "http://127.0.0.1:18767/callback",
        "required_acr": "mfa",
        "required_amr": ["pwd", "otp"],
    }
    mappers: list[dict[str, Any]] = [
        {
            "name": "Stage audience",
            "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": "stage-api", "access.token.claim": "true"},
        },
        {
            "name": "Stage credential kind",
            "protocolMapper": "oidc-hardcoded-claim-mapper",
            "config": {
                "claim.name": "stage_credential_kind",
                "claim.value": "human_oidc",
                "jsonType.label": "String",
                "access.token.claim": "true",
            },
        },
        {"name": "Actual achieved ACR", "protocolMapper": "oidc-acr-mapper", "config": {}},
        {"name": "Actual completed AMR", "protocolMapper": "oidc-amr-mapper", "config": {}},
    ]
    for mapper in mappers:
        mapper["protocol"] = "openid-connect"
        mapper["consentRequired"] = False
        mapper["config"].update({"access.token.claim": "true", "id.token.claim": "true"})
    clients = []
    for client_key, minimum in (("human_client", "mfa"), ("weak_client", "pwd")):
        clients.append(
            {
                "clientId": config[client_key],
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": True,
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": [config["redirect_uri"]],
                "webOrigins": [],
                "defaultClientScopes": ["basic"],
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    "acr.loa.map": json.dumps({"pwd": 1, "mfa": 2}),
                    "minimum.acr.value": minimum,
                    "default.acr.values": minimum,
                },
                "protocolMappers": mappers,
            }
        )
    realm = {
        "realm": "stage-mfa-development",
        "enabled": True,
        "sslRequired": "none",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "rememberMe": False,
        "accessTokenLifespan": 300,
        "ssoSessionIdleTimeout": 600,
        "ssoSessionMaxLifespan": 1800,
        "otpPolicyType": "totp",
        "otpPolicyAlgorithm": "HmacSHA1",
        "otpPolicyDigits": 6,
        "otpPolicyPeriod": 30,
        "otpPolicyLookAheadWindow": 1,
        "otpPolicyCodeReusable": False,
        "users": [
            {
                "id": config["human_subject"],
                "username": config["human_username"],
                "enabled": True,
                "emailVerified": True,
                "email": "stage-mfa-human@example.invalid",
                "firstName": "Stage",
                "lastName": "MFA Fixture",
                "requiredActions": [],
                "credentials": [
                    {"type": "password", "value": config["human_password"], "temporary": False},
                    {
                        "type": "otp",
                        "userLabel": "isolated protocol fixture",
                        "secretData": json.dumps({"value": config["otp_secret"]}),
                        "credentialData": json.dumps(
                            {"subType": "totp", "digits": 6, "counter": 0, "period": 30, "algorithm": "HmacSHA1"}
                        ),
                    },
                ],
            }
        ],
        "clients": [{"clientId": "stage-api", "enabled": True, "bearerOnly": True}, *clients],
        "browserFlow": "stage-fixture-browser",
        "authenticationFlows": [
            {
                "alias": "stage-fixture-browser",
                "providerId": "basic-flow",
                "topLevel": True,
                "builtIn": False,
                "authenticationExecutions": [
                    {
                        "flowAlias": "stage-password",
                        "authenticatorFlow": True,
                        "requirement": "CONDITIONAL",
                        "priority": 10,
                    },
                    {
                        "flowAlias": "stage-totp",
                        "authenticatorFlow": True,
                        "requirement": "CONDITIONAL",
                        "priority": 20,
                    },
                ],
            },
            *[
                {
                    "alias": flow,
                    "providerId": "basic-flow",
                    "topLevel": False,
                    "builtIn": False,
                    "authenticationExecutions": [
                        {
                            "authenticator": "conditional-level-of-authentication",
                            "authenticatorConfig": loa_config,
                            "authenticatorFlow": False,
                            "requirement": "REQUIRED",
                            "priority": 10,
                        },
                        {
                            "authenticator": authenticator,
                            "authenticatorConfig": method_config,
                            "authenticatorFlow": False,
                            "requirement": "REQUIRED",
                            "priority": 20,
                        },
                    ],
                }
                for flow, loa_config, authenticator, method_config in (
                    ("stage-password", "stage-loa-1", "auth-username-password-form", "stage-method-pwd"),
                    ("stage-totp", "stage-loa-2", "auth-otp-form", "stage-method-otp"),
                )
            ],
        ],
        "authenticatorConfig": [
            {"alias": "stage-loa-1", "config": {"loa-condition-level": "1", "loa-max-age": "0"}},
            {"alias": "stage-loa-2", "config": {"loa-condition-level": "2", "loa-max-age": "0"}},
            *[
                {
                    "alias": "stage-method-" + method,
                    "config": {"default.reference.value": method, "default.reference.maxAge": "600"},
                }
                for method in ("pwd", "otp")
            ],
        ],
    }
    return config, realm


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise SystemExit("state directory must have mode 0700")
    config, realm = fixture_documents()
    write_private(directory / "credentials.json", json.dumps(config, indent=2) + "\n")
    write_private(directory / "realm-import.json", json.dumps(realm, indent=2) + "\n")
    print("Created private isolated MFA fixture files; no Stage grants or standing IdP configuration changed.")


if __name__ == "__main__":
    main()
