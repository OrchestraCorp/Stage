"""Exercise an actual loopback Keycloak authorization-code/PKCE and client-credentials flow.

This local fixture client keeps passwords/tokens in memory, never logs them and never
starts a callback listener. It stops at the exact registered loopback redirect.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import stat
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.identity.authentication import AuthenticationRejected, KeycloakAuthenticator, KeycloakProfile  # noqa: E402


class LoginForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and values.get("id") == "kc-form-login":
            self._inside = True
            self.action = values.get("action")
        if tag == "input" and self._inside and values.get("type") == "hidden" and values.get("name"):
            self.fields[str(values["name"])] = str(values.get("value") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._inside = False


def assert_local_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
        raise ValueError("only the explicitly configured loopback development fixture is supported")


async def authorization_code(client: httpx.AsyncClient, config: dict[str, Any]) -> tuple[str, str, str]:
    verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    response = await client.get(
        config["issuer"]
        + "/protocol/openid-connect/auth?"
        + urlencode(
            {
                "client_id": config["human_client"],
                "redirect_uri": config["redirect_uri"],
                "response_type": "code",
                "scope": "openid",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
            }
        )
    )
    if response.status_code != 200:
        raise RuntimeError("authorization page unavailable")
    # Keycloak marks loopback cookies Secure because browsers treat loopback as a
    # potentially trustworthy origin. HTTPX does not make that exception. This
    # fixture-only adjustment is restricted to the already checked loopback host;
    # no redirects are followed and credentials never leave that origin.
    for cookie in client.cookies.jar:
        if cookie.domain == "127.0.0.1":
            cookie.secure = False
    form = LoginForm()
    form.feed(response.text)
    if form.action is None:
        raise RuntimeError("expected Keycloak login form")
    action = urljoin(config["issuer"], form.action)
    if urlsplit(action)[:2] != urlsplit(config["issuer"])[:2]:
        raise RuntimeError("login form escaped the configured IdP")
    response = await client.post(
        action,
        data={
            **form.fields,
            "username": config["human_username"],
            "password": config["human_password"],
            "credentialId": "",
        },
    )
    if response.status_code == 200 and config.get("otp_secret"):
        # Both the original password-only fixture and its explicitly upgraded
        # MFA configuration use this entry. A real OTP form must be completed;
        # merely finding an OTP secret in local configuration grants no assurance.
        from scripts.identity_mfa_smoke import LoginAttempt, complete_otp

        attempt = LoginAttempt(response, config["human_client"], verifier, state, nonce)
        response = (await complete_otp(client, config, attempt)).response
    if response.status_code != 302:
        raise RuntimeError("fixture login did not produce authorization code")
    location = response.headers["location"]
    expected = urlsplit(config["redirect_uri"])
    actual = urlsplit(location)
    if actual[:3] != expected[:3]:
        raise RuntimeError("unexpected OAuth redirect")
    query = parse_qs(actual.query)
    if query.get("state") != [state] or len(query.get("code", [])) != 1:
        raise RuntimeError("OAuth state or code mismatch")
    return query["code"][0], verifier, nonce


async def run(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    assert_local_url(config["issuer"])
    assert_local_url(config["redirect_uri"])
    endpoint = config["issuer"] + "/protocol/openid-connect/token"
    profile_configs = {
        "human": dict(
            profile_ref="local-keycloak-human",
            profile_version=1,
            issuer=config["issuer"],
            jwks_url=config["issuer"] + "/protocol/openid-connect/certs",
            audience=config["audience"],
            credential_kind="human_oidc",
            allowed_clients=(config["human_client"],),
            subject_namespace="keycloak-human-v1",
            environment="dev",
            allow_loopback_http=True,
        ),
        "service": dict(
            profile_ref="local-keycloak-service",
            profile_version=1,
            issuer=config["issuer"],
            jwks_url=config["issuer"] + "/protocol/openid-connect/certs",
            audience=config["audience"],
            credential_kind="workload_oauth2",
            allowed_clients=(config["service_client"],),
            subject_namespace="keycloak-service-v1",
            environment="dev",
            allow_loopback_http=True,
        ),
    }
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        code, verifier, nonce = await authorization_code(client, config)
        response = await client.post(
            endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config["human_client"],
                "code_verifier": verifier,
                "redirect_uri": config["redirect_uri"],
            },
        )
        if response.status_code != 200:
            raise RuntimeError("authorization-code exchange failed")
        human_tokens = response.json()
        human_verifier = KeycloakAuthenticator(KeycloakProfile(**profile_configs["human"]), http_client=client)
        human = await human_verifier.authenticate(human_tokens["access_token"])
        if human.subject != config["human_subject"]:
            raise RuntimeError("unexpected human identity")
        # The login client independently verifies its ID token and nonce; Stage rejects this token type.
        id_header = jwt.get_unverified_header(human_tokens["id_token"])
        keys_response = await client.get(profile_configs["human"]["jwks_url"])
        keys_response.raise_for_status()
        id_keys = [j for j in keys_response.json()["keys"] if j.get("kid") == id_header["kid"]]
        if len(id_keys) != 1:
            raise RuntimeError("ambiguous ID token key")
        id_claims = jwt.decode(
            human_tokens["id_token"],
            jwt.PyJWK.from_dict(id_keys[0]).key,
            algorithms=["RS256"],
            audience=config["human_client"],
            issuer=config["issuer"],
            options={"require": ["exp", "iat", "sub", "nonce"]},
        )
        if id_claims["nonce"] != nonce or id_claims["sub"] != human.subject or id_claims.get("typ") != "ID":
            raise RuntimeError("ID token nonce, subject or usage mismatch")
        try:
            await human_verifier.authenticate(human_tokens["id_token"])
        except AuthenticationRejected:
            pass
        else:
            raise RuntimeError("API accepted ID token")
        replay = await client.post(
            endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config["human_client"],
                "code_verifier": verifier,
                "redirect_uri": config["redirect_uri"],
            },
        )
        if replay.status_code != 400:
            raise RuntimeError("authorization code replay accepted")
        wrong_code, _, _ = await authorization_code(client, config)
        wrong_pkce = await client.post(
            endpoint,
            data={
                "grant_type": "authorization_code",
                "code": wrong_code,
                "client_id": config["human_client"],
                "code_verifier": secrets.token_urlsafe(48),
                "redirect_uri": config["redirect_uri"],
            },
        )
        if wrong_pkce.status_code != 400:
            raise RuntimeError("incorrect PKCE verifier accepted")
        missing_pkce_state = secrets.token_urlsafe(32)
        missing_pkce = await client.get(
            config["issuer"] + "/protocol/openid-connect/auth",
            params={
                "client_id": config["human_client"],
                "redirect_uri": config["redirect_uri"],
                "response_type": "code",
                "scope": "openid",
                "state": missing_pkce_state,
            },
        )
        missing_pkce_redirect = urlsplit(missing_pkce.headers.get("location", ""))
        missing_pkce_error = parse_qs(missing_pkce_redirect.query)
        if not (
            missing_pkce.status_code == 302
            and missing_pkce_redirect[:3] == urlsplit(config["redirect_uri"])[:3]
            and missing_pkce_error.get("state") == [missing_pkce_state]
            and missing_pkce_error.get("error") == ["invalid_request"]
            and "code" not in missing_pkce_error
        ):
            raise RuntimeError("authorization request without PKCE accepted")
        service_response = await client.post(
            endpoint,
            data={"grant_type": "client_credentials"},
            auth=(config["service_client"], config["service_secret"]),
        )
        if service_response.status_code != 200:
            raise RuntimeError("client credentials exchange failed")
        service_token = service_response.json()["access_token"]
        service_verifier = KeycloakAuthenticator(KeycloakProfile(**profile_configs["service"]), http_client=client)
        service = await service_verifier.authenticate(service_token)
        try:
            await human_verifier.authenticate(service_token)
        except AuthenticationRejected:
            pass
        else:
            raise RuntimeError("service credential accepted as human")
        password_grant = await client.post(
            endpoint,
            data={
                "grant_type": "password",
                "client_id": config["human_client"],
                "username": config["human_username"],
                "password": config["human_password"],
            },
        )
        if password_grant.status_code not in {400, 401}:
            raise RuntimeError("public client accepted password grant")
        result: dict[str, Any] = {
            "issuer": config["issuer"],
            "audience": config["audience"],
            "human_subject": human.subject,
            "human_client": human.client_ref,
            "service_subject": service.subject,
            "service_client": service.client_ref,
            "profiles": profile_configs,
            "checks": [
                "authorization_code_pkce",
                "id_token_nonce",
                "id_token_rejected_by_api",
                "authorization_code_replay_rejected",
                "wrong_pkce_rejected",
                "missing_pkce_rejected",
                "client_credentials",
                "service_as_human_rejected",
                "password_grant_disabled",
            ],
        }
        if args.stage_url:
            assert_local_url(args.stage_url)
            for kind, credential in (("human", human_tokens["access_token"]), ("service", service_token)):
                request = await client.get(
                    args.stage_url.rstrip("/") + "/api/v1/identity/me",
                    params={
                        "profile_ref": profile_configs[kind]["profile_ref"],
                        "profile_version": 1,
                    },
                    headers={"Authorization": "Bearer " + credential},
                )
                if request.status_code != 200:
                    raise RuntimeError("Stage " + kind + " identity request failed: " + str(request.status_code))
                projection = request.json()
                if (
                    request.headers.get("cache-control") != "no-store"
                    or projection.get("principal_type") != kind
                    or projection.get("organization_ref") != args.organization_ref
                    or "tenant_ref" in projection
                    or not isinstance(projection.get("principal_ref"), str)
                    or not projection["principal_ref"]
                    or not isinstance(projection.get("membership_ref"), str)
                    or not projection["membership_ref"]
                    or "authenticated_workload_ref" not in projection
                    or (kind == "human" and projection["authenticated_workload_ref"] is not None)
                    or (
                        kind == "service"
                        and (
                            not isinstance(projection["authenticated_workload_ref"], str)
                            or not projection["authenticated_workload_ref"]
                        )
                    )
                ):
                    raise RuntimeError("Stage " + kind + " identity projection mismatch")
            result["checks"].append("stage_human_and_service_membership")
            path = args.stage_url.rstrip("/") + "/api/v1/identity/me"
            human_params = {
                "profile_ref": profile_configs["human"]["profile_ref"],
                "profile_version": 1,
            }
            for name, expected, headers, parameters in (
                ("stage_missing_bearer_rejected", 401, {}, human_params),
                (
                    "stage_forged_identity_headers_rejected",
                    401,
                    {"X-Principal-ID": human.subject, "X-Roles": "admin", "X-Tenant-ID": args.organization_ref},
                    human_params,
                ),
                ("stage_id_token_rejected", 401, {"Authorization": "Bearer " + human_tokens["id_token"]}, human_params),
                ("stage_service_as_human_rejected", 401, {"Authorization": "Bearer " + service_token}, human_params),
                (
                    "stage_tenant_selection_rejected",
                    422,
                    {"Authorization": "Bearer " + human_tokens["access_token"]},
                    {**human_params, "tenant_ref": "unregistered-" + secrets.token_hex(16)},
                ),
                (
                    "stage_organization_selection_rejected",
                    422,
                    {"Authorization": "Bearer " + human_tokens["access_token"]},
                    {**human_params, "organization_ref": "unregistered-" + secrets.token_hex(16)},
                ),
            ):
                rejected = await client.get(path, params=parameters, headers=headers)
                if rejected.status_code != expected or rejected.headers.get("cache-control") != "no-store":
                    raise RuntimeError(name + " failed")
                if any(
                    secret in rejected.text
                    for secret in (human_tokens["access_token"], human_tokens["id_token"], service_token)
                ):
                    raise RuntimeError("Stage error response exposed a credential")
                result["checks"].append(name)
            forged_headers = await client.get(
                path,
                params=human_params,
                headers={
                    "Authorization": "Bearer " + human_tokens["access_token"],
                    "X-Tenant-ID": "forged-organization",
                    "X-Organization-ID": "forged-organization",
                },
            )
            if (
                forged_headers.status_code != 200
                or forged_headers.json().get("organization_ref") != args.organization_ref
                or forged_headers.headers.get("cache-control") != "no-store"
            ):
                raise RuntimeError("Stage identity headers changed the deployment organization")
            result["checks"].append("stage_identity_headers_cannot_select_organization")
            for prefix in ("/api", "/api/v2"):
                unsupported = await client.get(
                    args.stage_url.rstrip("/") + prefix + "/identity/me", params=human_params
                )
                if unsupported.status_code != 404:
                    raise RuntimeError("Stage exposed an unsupported identity API version")
            result["checks"].append("stage_unsupported_api_versions_rejected")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--stage-url")
    parser.add_argument(
        "--organization-ref",
        default="stage-development",
        help="Expected deployment organization in the response; never sent as a request selector.",
    )
    args = parser.parse_args()
    info = args.credentials.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
        raise SystemExit("credentials must belong to current operator and have mode 0600")
    config = json.loads(args.credentials.read_text())
    try:
        result = asyncio.run(run(config, args))
    except Exception as exc:
        # Do not emit HTTP errors, redirects or response bodies which might carry credentials.
        raise SystemExit("Identity protocol smoke failed: " + type(exc).__name__) from None
    if args.metadata:
        args.metadata.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["checks"], "metadata_written": bool(args.metadata)}))


if __name__ == "__main__":
    main()
