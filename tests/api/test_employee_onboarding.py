"""Real provider leases and verified human tokens cross the onboarding HTTP boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.administration import AdministrationSecurityMiddleware
from app.api.router import api_router
from app.core.config import Settings
from app.core.errors import register_exception_handlers
from app.core.middleware import RequestIdMiddleware
from app.execution.storage import UnitOfWork
from app.models.identity_foundation import IdentityControlOperationTable
from app.models.native_auth import NativeAccountActivationTable
from app.native_auth.keys import KeyRing
from app.providers.administration import ADMINISTRATION, administration_plugin
from app.providers.identity import IDENTITY, IdentityFacade
from app.providers.native_auth import NATIVE_AUTH, NativeAuthFacade
from app.providers.storage import STORAGE, Storage
from app.runtime import ComponentContext, PluginSpec, RuntimeHost
from tests.support.employee_onboarding import EmployeeCase, employee_case
from tests.support.human_administration import signing_key as signing_key
from tests.support.identity_tokens import token
from tests.support.native_identity import native_keyring as native_keyring

BASE = "/api/v1/administration"
PROFILE: dict[str, str | int] = {"profile_ref": "local-human", "profile_version": 1}


@dataclass
class EmployeeHTTP:
    case: EmployeeCase
    client: httpx.AsyncClient
    bearer: str

    @property
    def selected(self) -> dict[str, str | int]:
        return {**PROFILE, "assignment_ref": self.case.manager.assignment_ref}

    def create_request(self, **changes: Any) -> dict[str, Any]:
        return {
            "username": "employee.http",
            "expected_authority_version": self.case.manager.authority_version,
            "reason": "Explicit HTTP employee onboarding",
            **changes,
        }

    def headers(self, **claims: Any) -> dict[str, str]:
        bearer = token(
            self.case.manager.signing_key,
            claims={"auth_time": int(datetime.now(UTC).timestamp()), "acr": "2", "amr": ["pwd", "otp"], **claims},
        )
        return {"Authorization": "Bearer " + bearer}


@asynccontextmanager
async def employee_http(
    sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    keyring: KeyRing,
    *,
    upgrade: bool = True,
    native_enabled: bool = True,
    mount_native: bool | None = None,
) -> AsyncIterator[EmployeeHTTP]:
    async with employee_case(sessions, signing_key, upgrade=upgrade) as case:
        host = RuntimeHost()
        engine = sessions.kw["bind"]
        assert isinstance(engine, AsyncEngine)

        def storage(ctx: ComponentContext) -> None:
            ctx.provide(STORAGE, Storage(engine, sessions))

        def identity(ctx: ComponentContext) -> None:
            ctx.provide(IDENTITY, IdentityFacade(ctx, case.manager.identity))

        def native(ctx: ComponentContext) -> None:
            ctx.provide(NATIVE_AUTH, NativeAuthFacade(ctx, case.config, keyring, case.encryption_key))

        host.load(PluginSpec("employee-http.storage", storage, provides=(STORAGE,)))
        host.load(PluginSpec("employee-http.identity", identity, requires=(STORAGE,), provides=(IDENTITY,)))
        if native_enabled if mount_native is None else mount_native:
            host.load(PluginSpec("employee-http.native", native, requires=(STORAGE,), provides=(NATIVE_AUTH,)))
        settings = Settings(
            env="test",
            native_auth_enabled=native_enabled,
            native_auth_issuer=case.config.issuer if native_enabled else None,
            native_auth_organization_ref=case.config.organization_ref if native_enabled else None,
            native_auth_key_file="unused-injected-key" if native_enabled else None,
            native_auth_encryption_key_file="unused-injected-encryption-key" if native_enabled else None,
            command_request_timeout_seconds=10,
        )
        host.load(administration_plugin(settings))
        assert (await host.await_ready([ADMINISTRATION], timeout=3)).ready
        app = FastAPI()
        app.state.runtime = SimpleNamespace(host=host, ready=True)
        app.state.settings = settings
        app.add_middleware(RequestIdMiddleware)
        app.add_middleware(AdministrationSecurityMiddleware)
        register_exception_handlers(app)
        app.include_router(api_router)
        bearer = token(
            signing_key, claims={"auth_time": int(datetime.now(UTC).timestamp()), "acr": "2", "amr": ["pwd", "otp"]}
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://stage",
                headers={"Authorization": "Bearer " + bearer, "X-Request-ID": "employee-http-review"},
            ) as client:
                yield EmployeeHTTP(case, client, bearer)
        finally:
            assert (await host.close(timeout=3)).successful


def private_response(response: httpx.Response, bearer: str) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"] == "employee-http-review"
    assert bearer not in response.text


async def test_http_create_inspect_reissue_revoke_result_and_audit_never_replay_secret(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, native_keyring: KeyRing
) -> None:
    async with employee_http(persistence_sessions, signing_key, native_keyring) as http:
        created = await http.client.post(
            BASE + "/people",
            params=http.selected,
            json=http.create_request(),
            headers={"Idempotency-Key": "http-create"},
        )
        assert created.status_code == 200, created.text
        first = created.json()
        first_code = first["activation_code"]
        assert len(first_code) == 43 and first["activation_url"] == http.case.config.issuer + "/oauth/activate"
        ref = first["operation"]["result"]["activation_ref"]
        path = BASE + "/onboarding/activations/" + ref
        inspected = await http.client.get(path, params=http.selected)
        assert inspected.status_code == 200 and inspected.json()["status"] == "issued"
        replay = await http.client.post(
            BASE + "/people",
            params=http.selected,
            json=http.create_request(),
            headers={"Idempotency-Key": "http-create"},
        )
        assert replay.status_code == 200 and replay.json()["operation"]["replayed"]
        assert replay.json()["activation_code"] is None and first_code not in replay.text
        safe_result = await http.client.get(BASE + "/onboarding/operations/http-create", params=http.selected)
        assert safe_result.status_code == 200 and safe_result.json()["replayed"]
        assert "activation_code" not in safe_result.json()
        for protected_path in (path, BASE + "/onboarding/operations/http-create", BASE + "/onboarding/audit"):
            denied_read = await http.client.get(
                protected_path, params=http.selected, headers=http.headers(sub="subject-bob")
            )
            assert denied_read.status_code == 403, denied_read.text
            assert first_code not in denied_read.text
            private_response(denied_read, http.bearer)
        expired_recovery = await http.client.get(
            BASE + "/onboarding/operations/http-create",
            params=http.selected,
            headers=http.headers(auth_time=int(datetime.now(UTC).timestamp()) - 301),
        )
        assert expired_recovery.status_code == 401 and expired_recovery.json()["error"]["code"] == "MFA_REQUIRED"
        assert first_code not in expired_recovery.text
        private_response(expired_recovery, http.bearer)
        wrong_namespace = await http.client.get(BASE + "/operations/http-create", params=http.selected)
        assert wrong_namespace.status_code == 404
        wrong_input = await http.client.post(
            BASE + "/people",
            params=http.selected,
            json=http.create_request(username="another.employee"),
            headers={"Idempotency-Key": "http-create"},
        )
        assert wrong_input.status_code == 409 and wrong_input.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        replaced = await http.client.post(
            path + "/reissue",
            params=http.selected,
            json={
                "expected_authority_version": http.case.manager.authority_version,
                "expected_activation_version": 1,
                "expires_in_seconds": 600,
                "reason": "Replace lost activation delivery",
            },
            headers={"Idempotency-Key": "http-reissue"},
        )
        assert replaced.status_code == 200, replaced.text
        second_code = replaced.json()["activation_code"]
        assert second_code != first_code and replaced.json()["operation"]["result"]["version"] == 2
        revoked = await http.client.post(
            path + "/revoke",
            params=http.selected,
            json={
                "expected_authority_version": http.case.manager.authority_version,
                "expected_activation_version": 2,
                "reason": "Cancel onboarding before employee activation",
            },
            headers={"Idempotency-Key": "http-revoke"},
        )
        assert revoked.status_code == 200 and revoked.json()["activation_code"] is None
        assert revoked.json()["operation"]["result"]["status"] == "revoked"
        after = await http.client.get(path, params=http.selected)
        assert after.status_code == 200 and after.json()["version"] == 3
        audit = await http.client.get(BASE + "/onboarding/audit", params=http.selected)
        assert audit.status_code == 200, audit.text
        assert {item["operation"]["operation_type"] for item in audit.json()["items"]} == {
            "person.create",
            "person.activation.reissue",
            "person.activation.revoke",
        }
        assert all(item["request_id"] == "employee-http-review" for item in audit.json()["items"])
        for response in (
            created,
            inspected,
            replay,
            safe_result,
            wrong_namespace,
            wrong_input,
            replaced,
            revoked,
            after,
            audit,
        ):
            private_response(response, http.bearer)
        for response in (inspected, replay, safe_result, wrong_namespace, wrong_input, revoked, after, audit):
            assert first_code not in response.text and second_code not in response.text
        async with persistence_sessions() as session:
            assert await session.scalar(select(func.count()).select_from(NativeAccountActivationTable)) == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(IdentityControlOperationTable)
                    .where(IdentityControlOperationTable.operation_type == "administration.onboarding.v1")
                )
                == 3
            )


async def test_http_unknown_commit_points_to_safe_onboarding_recovery(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    native_keyring: KeyRing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with employee_http(persistence_sessions, signing_key, native_keyring) as http:
        original = UnitOfWork._commit_by_owner

        async def lose_reply(uow: UnitOfWork) -> None:
            await original(uow)
            raise ConnectionError("synthetic-private-employee-commit")

        with monkeypatch.context() as patch:
            patch.setattr(UnitOfWork, "_commit_by_owner", lose_reply)
            unknown = await http.client.post(
                BASE + "/people",
                params=http.selected,
                json=http.create_request(),
                headers={"Idempotency-Key": "lost-create"},
            )
        assert unknown.status_code == 503, unknown.text
        assert unknown.json()["error"]["code"] == "COMMIT_OUTCOME_UNKNOWN"
        location = urlsplit(unknown.headers["location"])
        assert location.path == BASE + "/onboarding/operations/lost-create"
        assert parse_qs(location.query) == {key: [str(value)] for key, value in http.selected.items()}
        assert "synthetic-private" not in unknown.text
        recovered = await http.client.get(unknown.headers["location"])
        assert recovered.status_code == 200 and recovered.json()["replayed"]
        assert "activation_code" not in recovered.text
        replay = await http.client.post(
            BASE + "/people",
            params=http.selected,
            json=http.create_request(),
            headers={"Idempotency-Key": "lost-create"},
        )
        assert replay.status_code == 200 and replay.json()["activation_code"] is None
        assert replay.json()["operation"]["operation_ref"] == recovered.json()["operation_ref"]
        for response in (unknown, recovered, replay):
            private_response(response, http.bearer)


@pytest.mark.parametrize("failure", ["missing-token", "ordinary-person", "weak-mfa", "stale-mfa", "wrong-assignment"])
async def test_http_requires_current_manager_and_recent_mfa_before_creating(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    native_keyring: KeyRing,
    failure: str,
) -> None:
    async with employee_http(persistence_sessions, signing_key, native_keyring) as http:
        query = http.selected
        headers = {"Idempotency-Key": "denied-create"}
        expected_status, expected_code = 403, "ADMINISTRATION_ACCESS_DENIED"
        if failure == "missing-token":
            del http.client.headers["Authorization"]
            expected_status, expected_code = 401, "AUTHENTICATION_REQUIRED"
        elif failure == "ordinary-person":
            headers.update(http.headers(sub="subject-bob"))
        elif failure == "weak-mfa":
            headers.update(http.headers(amr=["pwd"]))
            expected_status, expected_code = 401, "MFA_REQUIRED"
        elif failure == "stale-mfa":
            headers.update(http.headers(auth_time=int(datetime.now(UTC).timestamp()) - 301))
            expected_status, expected_code = 401, "MFA_REQUIRED"
        else:
            query = {**query, "assignment_ref": "assignment:engineering"}
        denied = await http.client.post(BASE + "/people", params=query, json=http.create_request(), headers=headers)
        assert denied.status_code == expected_status, denied.text
        assert denied.json()["error"]["code"] == expected_code
        private_response(denied, http.bearer)
        async with persistence_sessions() as session:
            assert await session.scalar(select(func.count()).select_from(NativeAccountActivationTable)) == 0


async def test_http_v1_admin_remains_readable_but_cannot_onboard(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, native_keyring: KeyRing
) -> None:
    async with employee_http(persistence_sessions, signing_key, native_keyring, upgrade=False) as http:
        current = await http.client.get(BASE + "/me", params=PROFILE)
        assert current.status_code == 200 and current.json()["authority"]["role_revision"] == 1
        people = await http.client.get(BASE + "/people", params=http.selected)
        assert people.status_code == 200
        denied = await http.client.post(
            BASE + "/people", params=http.selected, json=http.create_request(), headers={"Idempotency-Key": "v1-create"}
        )
        assert denied.status_code == 403
        private_response(denied, http.bearer)


async def test_http_rejects_ambiguous_or_privilege_bearing_create_input(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, native_keyring: KeyRing
) -> None:
    async with employee_http(persistence_sessions, signing_key, native_keyring) as http:
        duplicate = await http.client.post(
            BASE + "/people",
            params=http.selected,
            content='{"username":"employee.one","username":"employee.two","expected_authority_version":3,"reason":"Create"}',
            headers={"Idempotency-Key": "bad-input", "Content-Type": "application/json"},
        )
        privileged = await http.client.post(
            BASE + "/people",
            params=http.selected,
            json=http.create_request(role_ref="role:stage-administrator"),
            headers={"Idempotency-Key": "bad-role"},
        )
        repeated_key = await http.client.post(
            BASE + "/people",
            params=http.selected,
            json=http.create_request(),
            headers=[("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
        )
        media = await http.client.post(
            BASE + "/people",
            params=http.selected,
            content="not-json",
            headers={"Idempotency-Key": "media", "Content-Type": "text/plain"},
        )
        for response in (duplicate, privileged, repeated_key):
            assert response.status_code == 422, response.text
            private_response(response, http.bearer)
        assert media.status_code == 415
        private_response(media, http.bearer)
        async with persistence_sessions() as session:
            assert await session.scalar(select(func.count()).select_from(NativeAccountActivationTable)) == 0


@pytest.mark.parametrize("mount_native", [False, True])
async def test_http_native_disabled_leaves_ordinary_administration_available(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    native_keyring: KeyRing,
    mount_native: bool,
) -> None:
    async with employee_http(
        persistence_sessions, signing_key, native_keyring, native_enabled=False, mount_native=mount_native
    ) as http:
        current = await http.client.get(BASE + "/me", params=PROFILE)
        assert current.status_code == 200, current.text
        unavailable = await http.client.post(
            BASE + "/people",
            params=http.selected,
            json=http.create_request(),
            headers={"Idempotency-Key": "native-disabled"},
        )
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["error"]["code"] == "ADMINISTRATION_UNAVAILABLE"
        private_response(unavailable, http.bearer)
