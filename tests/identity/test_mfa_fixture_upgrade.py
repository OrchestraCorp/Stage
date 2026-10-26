"""The local IdP upgrade must preserve identities and resume without duplicate grants."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import identity_mfa_upgrade as upgrade
from scripts.identity_idp_prepare import write_private


class AdminFixture:
    def __init__(self) -> None:
        self.realm: dict[str, Any] = {
            "browserFlow": "browser",
            "otpPolicyType": "totp",
            "otpPolicyAlgorithm": "HmacSHA1",
            "otpPolicyDigits": 6,
            "otpPolicyPeriod": 30,
            "otpPolicyCodeReusable": False,
        }
        self.user = {"id": "human-existing", "username": "stage-dev-human", "enabled": True}
        self.credentials = [{"id": "password-existing", "type": "password"}]
        self.human: dict[str, Any] = {
            "id": "client-existing",
            "clientId": "maestro-stage-dev",
            "publicClient": True,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "enabled": True,
            "redirectUris": ["http://127.0.0.1:18765/callback"],
            "attributes": {"pkce.code.challenge.method": "S256"},
            "protocolMappers": [
                {
                    "id": "audience-existing",
                    "protocolMapper": "oidc-audience-mapper",
                    "config": {"included.client.audience": "stage-api", "access.token.claim": "true"},
                },
                {
                    "id": "kind-existing",
                    "protocolMapper": "oidc-hardcoded-claim-mapper",
                    "config": {
                        "claim.name": "stage_credential_kind",
                        "claim.value": "human_oidc",
                        "access.token.claim": "true",
                    },
                },
            ],
        }
        self.service: dict[str, Any] = {
            "id": "service-existing",
            "clientId": "stage-worker-dev",
            "serviceAccountsEnabled": True,
        }
        self.objects: dict[str, dict[str, dict[str, Any]]] = {"flows": {}, "config": {}, "executions": {}}
        self.mutations: list[str] = []
        self.fail_after_mutation: int | None = None

    def response(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(upgrade.REALM)
        if request.method == "POST" and request.url.path == "/realms/master/protocol/openid-connect/token":
            return httpx.Response(200, json={"access_token": "fixture-admin-token"})
        if request.method == "GET":
            if path == "":
                return httpx.Response(200, json=self.realm)
            if path == "/clients":
                result = self.human if request.url.params["clientId"] == self.human["clientId"] else self.service
                return httpx.Response(200, json=[result])
            if path in {"/clients/client-existing", "/clients/service-existing"}:
                return httpx.Response(200, json=self.human if path.endswith("client-existing") else self.service)
            if path == "/clients/client-existing/default-client-scopes":
                return httpx.Response(200, json=[{"id": "acr-scope-existing"}])
            if path == "/client-scopes/acr-scope-existing/protocol-mappers/models":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "acr-existing",
                            "protocolMapper": "oidc-acr-mapper",
                            "config": {"access.token.claim": "true"},
                        }
                    ],
                )
            if path == "/users/human-existing":
                return httpx.Response(200, json=self.user)
            if path == "/users/human-existing/credentials":
                return httpx.Response(200, json=self.credentials)
            if path.endswith("/executions") and path.startswith("/authentication/flows/"):
                return httpx.Response(200, json=list(self.objects["executions"].values()))
            if path.startswith("/authentication/"):
                _, _, collection, identity = path.split("/")
                found = self.objects[collection].get(identity)
                return httpx.Response(404) if found is None else httpx.Response(200, json=found)
            raise AssertionError("unexpected mock read")

        body = json.loads(request.content)
        if request.method == "POST" and path.startswith("/authentication/"):
            collection = path.split("/")[-1]
            if "authenticatorConfig" in body:
                body["authenticatorConfig"] = next(
                    item["id"]
                    for item in self.objects["config"].values()
                    if item["alias"] == body["authenticatorConfig"]
                )
            assert body["id"] not in self.objects[collection]
            self.objects[collection][body["id"]] = body
            status = 201
        elif request.method == "PUT" and path == "/users/human-existing":
            assert body["id"] == self.user["id"]
            for credential in body["credentials"]:
                assert credential["type"] == "otp"
                self.credentials.append({"id": credential["id"], "type": "otp"})
            status = 204
        elif request.method == "PUT" and path == "/clients/client-existing":
            self.human = body
            status = 204
        else:
            raise AssertionError("unexpected mock mutation")
        self.mutations.append(path)
        if self.fail_after_mutation == len(self.mutations):
            raise httpx.ReadTimeout("simulated lost response after committed IdP mutation")
        return httpx.Response(status)


@pytest.fixture
def setup_upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AdminFixture, Path, Path, Path]:
    config = {
        "issuer": upgrade.ISSUER,
        "human_client": "maestro-stage-dev",
        "service_client": "stage-worker-dev",
        "human_subject": "human-existing",
        "human_username": "stage-dev-human",
        "human_password": "retained-private-password",
        "redirect_uri": "http://127.0.0.1:18765/callback",
        "audience": "stage-api",
    }
    credentials, operator, state = tmp_path / "credentials.json", tmp_path / "operator.json", tmp_path / "state"
    write_private(credentials, json.dumps(config))
    write_private(operator, json.dumps({"admin_username": "temp", "admin_password": "private-operator-password"}))
    fixture = AdminFixture()
    client_type = httpx.Client
    monkeypatch.setattr(
        upgrade.httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(fixture.response), **kwargs)
    )
    return fixture, credentials, operator, state


def test_upgrade_preserves_identity_and_replay_does_not_mutate(
    setup_upgrade: tuple[AdminFixture, Path, Path, Path],
) -> None:
    fixture, credentials, operator, state = setup_upgrade
    original_service = copy.deepcopy(fixture.service)
    upgrade.upgrade(credentials, operator, state)
    mutations = len(fixture.mutations)
    upgrade.upgrade(credentials, operator, state)
    assert len(fixture.mutations) == mutations
    assert len(fixture.credentials) == 2
    assert fixture.credentials[0] == {"id": "password-existing", "type": "password"}
    assert fixture.service == original_service
    assert fixture.realm["browserFlow"] == "browser"
    saved = json.loads(credentials.read_text())
    assert saved["human_subject"] == "human-existing" and saved["human_password"] == "retained-private-password"
    assert credentials.stat().st_mode & 0o077 == 0
    assert len(fixture.objects["flows"]) == 3 and len(fixture.objects["executions"]) == 6
    assert not any(item["protocolMapper"] == "oidc-acr-mapper" for item in fixture.human["protocolMappers"])


@pytest.mark.parametrize("mutation", [1, 4, 9, 13, 14, 15])
def test_lost_admin_response_resumes_without_duplicate_credentials(
    setup_upgrade: tuple[AdminFixture, Path, Path, Path], mutation: int
) -> None:
    fixture, credentials, operator, state = setup_upgrade
    fixture.fail_after_mutation = mutation
    with pytest.raises(httpx.ReadTimeout):
        upgrade.upgrade(credentials, operator, state)
    fixture.fail_after_mutation = None
    upgrade.upgrade(credentials, operator, state)
    assert len(fixture.credentials) == 2
    assert len(fixture.objects["flows"]) == 3
    assert len(fixture.objects["executions"]) == 6


def test_foreign_otp_is_rejected_before_any_write(setup_upgrade: tuple[AdminFixture, Path, Path, Path]) -> None:
    fixture, credentials, operator, state = setup_upgrade
    fixture.credentials.append({"id": "unknown-otp", "type": "otp"})
    with pytest.raises(RuntimeError, match="unowned OTP"):
        upgrade.upgrade(credentials, operator, state)
    assert not fixture.mutations and "otp_secret" not in json.loads(credentials.read_text())


def test_completed_plan_rejects_authentication_drift(setup_upgrade: tuple[AdminFixture, Path, Path, Path]) -> None:
    fixture, credentials, operator, state = setup_upgrade
    upgrade.upgrade(credentials, operator, state)
    mutations = len(fixture.mutations)
    fixture.human["attributes"]["minimum.acr.value"] = "pwd"
    with pytest.raises(RuntimeError, match="does not match"):
        upgrade.upgrade(credentials, operator, state)
    assert len(fixture.mutations) == mutations
