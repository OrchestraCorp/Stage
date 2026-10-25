"""Create private, disposable Keycloak realm import and local protocol-test credentials.

Run as the deployment operator. This never calls Stage or provisions its identities.
The generated files contain credentials and MUST NOT be committed or printed.
"""

import argparse
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4


def write_private(path: Path, contents: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(contents)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise SystemExit("state directory must be private (mode 0700)")
    credentials = {
        "issuer": "http://127.0.0.1:18080/realms/stage-development",
        "audience": "stage-api",
        "human_client": "maestro-stage-dev",
        "service_client": "stage-worker-dev",
        "human_username": "stage-dev-human",
        "human_password": secrets.token_urlsafe(32),
        "human_subject": str(uuid4()),
        "service_secret": secrets.token_urlsafe(48),
        "admin_username": "stage-local-admin",
        "admin_password": secrets.token_urlsafe(48),
        "redirect_uri": "http://127.0.0.1:18765/callback",
    }

    def mappers(kind: str) -> list[dict]:
        return [
            {
                "name": "Stage API audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {
                    "included.client.audience": "stage-api",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            },
            {
                "name": "Stage credential kind",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-hardcoded-claim-mapper",
                "consentRequired": False,
                "config": {
                    "claim.name": "stage_credential_kind",
                    "claim.value": kind,
                    "jsonType.label": "String",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            },
        ]

    realm = {
        "realm": "stage-development",
        "enabled": True,
        "sslRequired": "none",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "rememberMe": False,
        "accessTokenLifespan": 300,
        "ssoSessionIdleTimeout": 600,
        "ssoSessionMaxLifespan": 1800,
        "users": [
            {
                "id": credentials["human_subject"],
                "username": credentials["human_username"],
                "enabled": True,
                "emailVerified": True,
                "email": "stage-dev-human@example.invalid",
                "firstName": "Stage",
                "lastName": "Fixture",
                "requiredActions": [],
                "credentials": [{"type": "password", "value": credentials["human_password"], "temporary": False}],
            }
        ],
        "clients": [
            {"clientId": "stage-api", "enabled": True, "bearerOnly": True},
            {
                "clientId": credentials["human_client"],
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": True,
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "redirectUris": [credentials["redirect_uri"]],
                "webOrigins": [],
                "attributes": {"pkce.code.challenge.method": "S256"},
                "protocolMappers": mappers("human_oidc"),
            },
            {
                "clientId": credentials["service_client"],
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "secret": credentials["service_secret"],
                "standardFlowEnabled": False,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": True,
                "protocolMappers": mappers("workload_oauth2"),
            },
        ],
    }
    write_private(directory / "credentials.json", json.dumps(credentials, indent=2) + "\n")
    write_private(directory / "realm-import.json", json.dumps(realm, indent=2) + "\n")
    write_private(
        directory / "keycloak.env",
        (
            "KC_BOOTSTRAP_ADMIN_USERNAME=" + credentials["admin_username"] + "\n"
            "KC_BOOTSTRAP_ADMIN_PASSWORD=" + credentials["admin_password"] + "\n"
        ),
    )
    print("Created private local IdP fixture files; no Stage identities were granted.")


if __name__ == "__main__":
    main()
