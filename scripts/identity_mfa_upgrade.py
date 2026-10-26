"""Upgrade only the standing loopback Keycloak human fixture to password + TOTP.

Requires a separately provisioned temporary operator in a private JSON file. The
script never stops Keycloak, resets passwords, recreates users, or writes Stage.
Its private persisted UUID plan supports resuming interrupted REST operations.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from scripts.identity_idp_prepare import write_private
from scripts.identity_mfa_prepare import fixture_documents

BASE = "http://127.0.0.1:18080"
REALM = "/admin/realms/stage-development"
ISSUER = BASE + "/realms/stage-development"


def _read_private(path: Path) -> dict[str, Any]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("configuration must be a private regular file")
    result: dict[str, Any] = json.loads(path.read_text())
    return result


def _replace_private(path: Path, contents: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".mfa-update-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(contents, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _request(client: httpx.Client, method: str, path: str, *, body: Any = None) -> httpx.Response:
    response = client.request(method, BASE + path, json=body)
    allowed = {200, 201, 204, 404} if method == "GET" else {200, 201, 204}
    if response.status_code not in allowed:
        raise RuntimeError(f"IdP operation failed ({method} {path}: {response.status_code})")
    return response


def _get(client: httpx.Client, path: str) -> Any:
    response = _request(client, "GET", path)
    if response.status_code != 200:
        raise RuntimeError("required IdP object was not found")
    return response.json()


def _exact(actual: dict[str, Any], expected: dict[str, Any], fields: tuple[str, ...]) -> None:
    if any(actual.get(field) != expected.get(field) for field in fields):
        raise RuntimeError("existing IdP object does not match the owned upgrade plan")


def _check_mappers(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> None:
    if sorted(actual, key=lambda item: item["id"]) != sorted(expected, key=lambda item: item["id"]):
        raise RuntimeError("authentication protocol mappers differ from the owned plan")


def _client(client: httpx.Client, client_id: str) -> dict[str, Any]:
    response = client.get(BASE + REALM + "/clients", params={"clientId": client_id})
    if response.status_code != 200:
        raise RuntimeError("client lookup failed")
    matches = [item for item in response.json() if item.get("clientId") == client_id]
    if len(matches) != 1:
        raise RuntimeError("ambiguous or missing fixture client")
    result: dict[str, Any] = _get(client, REALM + "/clients/" + matches[0]["id"])
    return result


def _plan(config: dict[str, Any], before_client: dict[str, Any], before_user: dict[str, Any]) -> dict[str, Any]:
    _, fixture = fixture_documents()
    aliases = {
        "stage-fixture-browser": "stage-human-mfa-v1",
        "stage-password": "stage-human-mfa-password-v1",
        "stage-totp": "stage-human-mfa-totp-v1",
        "stage-loa-1": "stage-human-mfa-loa-1-v1",
        "stage-loa-2": "stage-human-mfa-loa-2-v1",
        "stage-method-pwd": "stage-human-mfa-method-pwd-v1",
        "stage-method-otp": "stage-human-mfa-method-otp-v1",
    }
    flows = []
    configs = []
    executions = []
    flow_ids = {flow["alias"]: str(uuid4()) for flow in fixture["authenticationFlows"]}
    for flow in fixture["authenticationFlows"]:
        flows.append(
            {
                "id": flow_ids[flow["alias"]],
                "alias": aliases[flow["alias"]],
                "providerId": flow["providerId"],
                "topLevel": flow["topLevel"],
                "builtIn": False,
            }
        )
        for execution in flow["authenticationExecutions"]:
            desired = {
                "id": str(uuid4()),
                "parentFlow": flow_ids[flow["alias"]],
                "authenticatorFlow": execution["authenticatorFlow"],
                "requirement": execution["requirement"],
                "priority": execution["priority"],
            }
            if execution["authenticatorFlow"]:
                desired["flowId"] = flow_ids[execution["flowAlias"]]
            else:
                desired["authenticator"] = execution["authenticator"]
                desired["authenticatorConfig"] = aliases[execution["authenticatorConfig"]]
            executions.append(desired)
    for config_item in fixture["authenticatorConfig"]:
        configs.append({"id": str(uuid4()), "alias": aliases[config_item["alias"]], "config": config_item["config"]})
    return {
        "issuer": config["issuer"],
        "human_subject": config["human_subject"],
        "human_client_id": before_client["id"],
        "otp_secret": secrets.token_urlsafe(32),
        "otp_credential_id": str(uuid4()),
        "before_client": before_client,
        "before_user": before_user,
        "flows": flows,
        "configs": configs,
        "executions": executions,
        "acr_mapper_id": str(uuid4()),
        "amr_mapper_id": str(uuid4()),
        "completed": False,
    }


def _ensure_owned(client: httpx.Client, collection: str, desired: dict[str, Any], fields: tuple[str, ...]) -> None:
    response = _request(client, "GET", REALM + "/authentication/" + collection + "/" + desired["id"])
    if response.status_code == 404:
        response = _request(client, "POST", REALM + "/authentication/" + collection, body=desired)
        if response.status_code != 201:
            raise RuntimeError("owned object creation failed")
        response = _request(client, "GET", REALM + "/authentication/" + collection + "/" + desired["id"])
    if response.status_code != 200:
        raise RuntimeError("owned object could not be verified")
    _exact(response.json(), desired, fields)


def _all_mappers(client: httpx.Client, representation: dict[str, Any]) -> list[dict[str, Any]]:
    mappers = list(representation.get("protocolMappers", []))
    scopes = _get(client, REALM + "/clients/" + representation["id"] + "/default-client-scopes")
    for scope in scopes:
        mappers.extend(_get(client, REALM + "/client-scopes/" + scope["id"] + "/protocol-mappers/models"))
    return mappers


def _check_client(representation: dict[str, Any], config: dict[str, Any]) -> None:
    expected = {
        "clientId": "maestro-stage-dev",
        "publicClient": True,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "enabled": True,
    }
    _exact(representation, expected, tuple(expected))
    if representation.get("attributes", {}).get("pkce.code.challenge.method") != "S256":
        raise RuntimeError("existing fixture does not require PKCE S256")
    if representation.get("redirectUris") != [config["redirect_uri"]]:
        raise RuntimeError("existing fixture callback changed")
    mappers = representation.get("protocolMappers", [])
    if not any(
        mapper["protocolMapper"] == "oidc-audience-mapper"
        and mapper["config"].get("included.client.audience") == "stage-api"
        and mapper["config"].get("access.token.claim") == "true"
        for mapper in mappers
    ):
        raise RuntimeError("existing fixture audience mapper mismatch")
    if not any(
        mapper["protocolMapper"] == "oidc-hardcoded-claim-mapper"
        and mapper["config"].get("claim.name") == "stage_credential_kind"
        and mapper["config"].get("claim.value") == "human_oidc"
        and mapper["config"].get("access.token.claim") == "true"
        for mapper in mappers
    ):
        raise RuntimeError("existing fixture kind mapper mismatch")


def upgrade(credentials: Path, operator_credentials: Path, state_dir: Path) -> list[str]:
    config, operator = _read_private(credentials), _read_private(operator_credentials)
    if (
        config.get("issuer") != ISSUER
        or config.get("human_client") != "maestro-stage-dev"
        or config.get("service_client") != "stage-worker-dev"
        or config.get("audience") != "stage-api"
    ):
        raise ValueError("only the standing loopback development fixture is supported")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_dir.stat().st_mode & 0o077:
        raise ValueError("upgrade state directory must have mode 0700")
    state_file = state_dir / "owned-upgrade.json"
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = client.post(
            BASE + "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": operator["admin_username"],
                "password": operator["admin_password"],
            },
        )
        if response.status_code != 200:
            raise RuntimeError("temporary operator authentication failed")
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        realm = _get(client, REALM)
        expected_otp = {
            "otpPolicyType": "totp",
            "otpPolicyAlgorithm": "HmacSHA1",
            "otpPolicyDigits": 6,
            "otpPolicyPeriod": 30,
        }
        _exact(realm, expected_otp, tuple(expected_otp))
        if realm.get("otpPolicyCodeReusable", False):
            raise RuntimeError("OTP reuse must remain disabled")
        before = _client(client, config["human_client"])
        _check_client(before, config)
        service_before = _client(client, config["service_client"])
        user_path = REALM + "/users/" + config["human_subject"]
        user = _get(client, user_path)
        _exact(
            user,
            {"id": config["human_subject"], "username": config["human_username"], "enabled": True},
            ("id", "username", "enabled"),
        )
        current_credentials = _get(client, user_path + "/credentials")
        password_ids = {item["id"] for item in current_credentials if item["type"] == "password"}
        if len(password_ids) != 1:
            raise RuntimeError("expected one existing password credential")
        if state_file.exists():
            plan = _read_private(state_file)
            _exact(
                plan,
                {"issuer": ISSUER, "human_subject": user["id"], "human_client_id": before["id"]},
                ("issuer", "human_subject", "human_client_id"),
            )
        else:
            if any(item["type"] == "otp" for item in current_credentials) or "otp_secret" in config:
                raise RuntimeError("unowned OTP already exists; refusing to replace it")
            plan = _plan(config, before, user)
            plan["password_ids"] = sorted(password_ids)
            plan["service_before"] = service_before
            plan["realm_browser_flow"] = realm["browserFlow"]
            write_private(state_dir / "credentials-before.json", json.dumps(config, indent=2) + "\n")
            write_private(state_file, json.dumps(plan, indent=2) + "\n")
        if sorted(password_ids) != plan["password_ids"] or service_before != plan["service_before"]:
            raise RuntimeError("existing password or service client drifted from the upgrade snapshot")
        if realm["browserFlow"] != plan["realm_browser_flow"]:
            raise RuntimeError("realm browser flow drifted")
        if plan["completed"]:
            expected_client = plan["after_client"]
            _exact(before, expected_client, ("attributes", "authenticationFlowBindingOverrides"))
            _check_mappers(before.get("protocolMappers", []), expected_client.get("protocolMappers", []))
        if config.get("otp_secret", plan["otp_secret"]) != plan["otp_secret"]:
            raise RuntimeError("private OTP configuration differs from the owned plan")
        if any(item["type"] == "otp" and item["id"] != plan["otp_credential_id"] for item in current_credentials):
            raise RuntimeError("unowned OTP exists; refusing to replace it")

        for desired in plan["flows"]:
            _ensure_owned(client, "flows", desired, ("id", "alias", "providerId", "topLevel", "builtIn"))
        for desired in plan["configs"]:
            _ensure_owned(client, "config", desired, ("id", "alias", "config"))
        for desired in plan["executions"]:
            _ensure_owned(
                client,
                "executions",
                desired,
                ("id", "parentFlow", "flowId", "authenticatorFlow", "authenticator", "requirement", "priority"),
            )
            actual = _get(client, REALM + "/authentication/executions/" + desired["id"])
            if desired.get("authenticatorConfig") is not None:
                expected_config = next(
                    item["id"] for item in plan["configs"] if item["alias"] == desired["authenticatorConfig"]
                )
                if actual.get("authenticatorConfig") != expected_config:
                    raise RuntimeError("execution configuration binding mismatch")
        top = next(flow for flow in plan["flows"] if flow["topLevel"])
        actual_executions = _get(client, REALM + "/authentication/flows/" + top["alias"] + "/executions")
        if {item["id"] for item in actual_executions} != {item["id"] for item in plan["executions"]}:
            raise RuntimeError("unexpected executions in the owned MFA flow")

        # Persist the new fixture secret before any client can start requiring OTP.
        if config.get("otp_secret") != plan["otp_secret"] or config.get("mfa_owned_state") != str(state_file.resolve()):
            config["otp_secret"] = plan["otp_secret"]
            config["mfa_owned_state"] = str(state_file.resolve())
            _replace_private(credentials, config)
        if not any(item["id"] == plan["otp_credential_id"] for item in current_credentials):
            changed_user = copy.deepcopy(user)
            changed_user["credentials"] = [
                {
                    "id": plan["otp_credential_id"],
                    "type": "otp",
                    "userLabel": "stage-owned-mfa-development-v1",
                    "secretData": json.dumps({"value": plan["otp_secret"]}),
                    "credentialData": json.dumps(
                        {"subType": "totp", "digits": 6, "counter": 0, "period": 30, "algorithm": "HmacSHA1"}
                    ),
                }
            ]
            _request(client, "PUT", user_path, body=changed_user)
        after_credentials = _get(client, user_path + "/credentials")
        if {item["id"] for item in after_credentials if item["type"] == "password"} != password_ids:
            raise RuntimeError("existing password credential changed")
        if [item["id"] for item in after_credentials if item["type"] == "otp"] != [plan["otp_credential_id"]]:
            raise RuntimeError("owned OTP credential was not installed exactly once")

        changed = copy.deepcopy(before)
        changed.setdefault("authenticationFlowBindingOverrides", {})["browser"] = top["id"]
        changed.setdefault("attributes", {}).update(
            {"acr.loa.map": json.dumps({"pwd": 1, "mfa": 2}), "minimum.acr.value": "mfa", "default.acr.values": "mfa"}
        )
        mappers = _all_mappers(client, before)
        for claim in ("acr", "amr", "auth_time"):
            if any(
                mapper["protocolMapper"] == "oidc-hardcoded-claim-mapper"
                and mapper.get("config", {}).get("claim.name") == claim
                for mapper in mappers
            ):
                raise RuntimeError("fixed authentication assurance claim is forbidden")
        for kind in ("acr", "amr"):
            existing = [mapper for mapper in mappers if mapper["protocolMapper"] == "oidc-" + kind + "-mapper"]
            if len(existing) > 1 or existing and existing[0].get("config", {}).get("access.token.claim") != "true":
                raise RuntimeError("ambiguous or inactive authentication assurance mapper")
            if not existing:
                changed.setdefault("protocolMappers", []).append(
                    {
                        "id": plan[kind + "_mapper_id"],
                        "name": "stage-owned-actual-" + kind + "-v1",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-" + kind + "-mapper",
                        "consentRequired": False,
                        "config": {"access.token.claim": "true", "id.token.claim": "true"},
                    }
                )
        if changed != before:
            _request(client, "PUT", REALM + "/clients/" + before["id"], body=changed)
        after = _client(client, config["human_client"])
        _check_client(after, config)
        _exact(after, changed, ("authenticationFlowBindingOverrides", "attributes"))
        _check_mappers(after.get("protocolMappers", []), changed.get("protocolMappers", []))
        if _client(client, config["service_client"]) != service_before:
            raise RuntimeError("service client unexpectedly changed")
        if _get(client, REALM)["browserFlow"] != realm["browserFlow"]:
            raise RuntimeError("realm browser flow unexpectedly changed")
        if not plan["completed"]:
            plan["completed"] = True
            plan["after_client"] = after
            _replace_private(state_file, plan)
    return [
        "existing_subject_and_password_preserved",
        "owned_mfa_flow_verified",
        "single_owned_otp_installed",
        "human_client_requires_actual_mfa",
        "service_client_and_realm_flow_preserved",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--operator-credentials", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        checks = upgrade(args.credentials, args.operator_credentials, args.state_dir)
    except (ValueError, RuntimeError, KeyError, httpx.HTTPError) as exc:
        # Deliberately omit response bodies and exception chaining: admin request
        # representations can contain password or OTP secret fields.
        raise SystemExit(
            f"MFA fixture upgrade failed ({type(exc).__name__}); inspect the private plan before retrying."
        ) from None
    print(json.dumps({"issuer": ISSUER, "checks": checks}, indent=2))


if __name__ == "__main__":
    main()
