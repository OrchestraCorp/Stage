"""Real native MFA/PKCE token drives the current-person management HTTP boundary."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.administration.bootstrap import NominationPlan, nominate
from app.api.administration import AdministrationSecurityMiddleware, router
from app.api.work import router as work_router
from app.authz.management import GrantRule
from app.core.config import Settings
from app.core.errors import register_exception_handlers
from app.core.middleware import RequestIdMiddleware
from app.execution.storage import UnitOfWork
from app.identity.assurance_administration import ConfigureHumanAuthenticationRequest, configure_human_authentication
from app.identity.native import NATIVE_ACR
from app.models.authorization import RoleVersionTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import OrganizationMembershipTable
from app.native_auth.keys import KeyRing
from app.native_auth.oauth import OAuthError
from app.providers.administration import ADMINISTRATION, administration_plugin
from app.providers.identity import IDENTITY, IdentityFacade
from app.providers.storage import STORAGE, Storage
from app.providers.work import WORK, human_work_plugin
from app.runtime import ComponentContext, PluginSpec, RuntimeHost
from tests.support.authorization import ORG, PERSON
from tests.support.native_identity import native_context
from tests.support.native_identity import native_keyring as native_keyring

BASE = "/api/v1/administration"
PROFILE: dict[str, str | int] = {"profile_ref": "local-human", "profile_version": 1}
TARGET = "person:native-admin-target"
MEMBERSHIP = "membership:native-admin-target"


async def nominate_native_administrator(sessions: async_sessionmaker[AsyncSession]) -> None:
    await configure_human_authentication(
        sessions,
        ConfigureHumanAuthenticationRequest.model_validate(
            {
                "environment": "test",
                "organization_ref": ORG,
                "expected_version": 0,
                "policy": {
                    "mode": "required",
                    "max_auth_age_seconds": 900,
                    "rules": [
                        {
                            "profile_ref": "local-human",
                            "profile_version": 1,
                            "required_acr": NATIVE_ACR,
                            "required_amr": ["pwd", "otp"],
                        }
                    ],
                },
            }
        ),
        actor_ref="operator:native-administration-test",
        idempotency_key="require-native-management-mfa",
    )
    async with sessions.begin() as session:
        session.add(PrincipalTable(principal_ref=TARGET, principal_type="human"))
        await session.flush()
        session.add(
            OrganizationMembershipTable(
                organization_ref=ORG,
                membership_ref=MEMBERSHIP,
                principal_ref=TARGET,
                principal_type="human",
                source_ref="fixture:existing-native-managed-person",
                valid_from=datetime.now(UTC) - timedelta(days=1),
            )
        )
        role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
        assert role is not None
        rule = GrantRule(
            role_ref="role:engineering",
            role_revision=1,
            content_digest=role.content_digest,
            scope_ref="scope:engineering",
            max_duration_seconds=7 * 86400,
        )
    await nominate(
        sessions,
        NominationPlan(
            environment="test",
            organization_ref=ORG,
            principal_ref=PERSON,
            membership_ref="membership:alice",
            profile_ref="local-human",
            profile_version=1,
            external_identity_ref="identity",
            expected_version=0,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            reason="Explicit native human nomination",
            grant_rules=(rule,),
        ),
        actor_ref="operator:native-administration-test",
        idempotency_key="nominate-native-administrator",
    )


@pytest.mark.parametrize("lost_grant_reply", [False, True])
async def test_native_mfa_management_activate_grant_recovery_revoke_audit_and_logout(
    persistence_sessions: async_sessionmaker[AsyncSession],
    native_keyring: KeyRing,
    monkeypatch: pytest.MonkeyPatch,
    lost_grant_reply: bool,
) -> None:
    # This fixture performs password and actual TOTP enrollment, then consumes a
    # single-use PKCE authorization code. Neither claims nor evidence are forged.
    identity, oauth, _execution_context, issued = await native_context(persistence_sessions, native_keyring)
    host = RuntimeHost()
    engine = persistence_sessions.kw["bind"]
    assert isinstance(engine, AsyncEngine)

    def storage(ctx: ComponentContext) -> None:
        ctx.provide(STORAGE, Storage(engine, persistence_sessions))

    def identity_provider(ctx: ComponentContext) -> None:
        ctx.effect(lambda: identity.aclose, label="native-admin.identity")
        ctx.provide(IDENTITY, IdentityFacade(ctx, identity))

    host.load(PluginSpec("native-admin.storage", storage, provides=(STORAGE,)))
    host.load(PluginSpec("native-admin.identity", identity_provider, requires=(STORAGE,), provides=(IDENTITY,)))
    settings = Settings(env="test", command_request_timeout_seconds=10)
    host.load(administration_plugin(settings))
    host.load(human_work_plugin(settings))
    assert (await host.await_ready([ADMINISTRATION, WORK], timeout=3)).ready
    app = FastAPI()
    app.state.runtime = SimpleNamespace(host=host, ready=True)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(AdministrationSecurityMiddleware)
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")
    app.include_router(work_router, prefix="/api/v1")
    try:
        await nominate_native_administrator(persistence_sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://stage",
            headers={
                "Authorization": "Bearer " + issued["access_token"],
                "X-Request-ID": "native-admin.http",
            },
        ) as client:
            current = await client.get(BASE + "/me", params=PROFILE)
            assert current.status_code == 200, current.text
            assert current.json()["authority"]["status"] == "pending"
            management_assignment = current.json()["authority"]["assignment_ref"]
            selected = {**PROFILE, "assignment_ref": management_assignment}
            activation = await client.post(
                BASE + "/activation",
                params=PROFILE,
                json={"expected_authority_version": 1},
                headers={"Idempotency-Key": "native-activate"},
            )
            assert activation.status_code == 200, activation.text
            assert activation.json()["result"]["assignment_ref"] == management_assignment
            people = await client.get(BASE + "/people", params=selected)
            assert people.status_code == 200 and {row["principal_ref"] for row in people.json()["items"]} == {
                PERSON,
                TARGET,
            }
            wrong_selection = await client.get(
                BASE + "/people", params={**PROFILE, "assignment_ref": "assignment:engineering"}
            )
            assert wrong_selection.status_code == 403
            management_work = await client.get("/api/v1/work-items/1", params=selected)
            ordinary_work = await client.get(
                "/api/v1/work-items/1", params={**PROFILE, "assignment_ref": "assignment:engineering"}
            )
            assert management_work.status_code == 403 and ordinary_work.status_code == 200
            grant_input: dict[str, Any] = {
                "membership_ref": MEMBERSHIP,
                "expected_membership_version": 1,
                "expected_authority_version": 2,
                "role_ref": "role:engineering",
                "role_revision": 1,
                "scope_ref": "scope:engineering",
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "reason": "Assign ordinary work responsibilities",
            }
            original_commit = UnitOfWork._commit_by_owner

            async def lose_acknowledgement(uow: UnitOfWork) -> None:
                await original_commit(uow)
                raise OSError("synthetic-private-lost-management-commit")

            with monkeypatch.context() as patch:
                if lost_grant_reply:
                    patch.setattr(UnitOfWork, "_commit_by_owner", lose_acknowledgement)
                granted = await client.post(
                    BASE + f"/people/{TARGET}/assignments",
                    params=selected,
                    json=grant_input,
                    headers={"Idempotency-Key": "native-grant"},
                )
            if lost_grant_reply:
                assert granted.status_code == 503 and granted.json()["error"]["code"] == "COMMIT_OUTCOME_UNKNOWN"
                assert "synthetic-private" not in granted.text
                granted = await client.get(granted.headers["location"])
                assert granted.json()["replayed"]
            assert granted.status_code == 200, granted.text
            target_assignment = granted.json()["result"]["assignment_ref"]
            replayed = await client.post(
                BASE + f"/people/{TARGET}/assignments",
                params=selected,
                json=grant_input,
                headers={"Idempotency-Key": "native-grant"},
            )
            assert replayed.status_code == 200 and replayed.json()["replayed"]
            listed = await client.get(BASE + f"/people/{TARGET}/assignments", params=selected)
            assert listed.status_code == 200 and [row["assignment_ref"] for row in listed.json()["items"]] == [
                target_assignment
            ]
            revoked = await client.post(
                BASE + f"/people/{TARGET}/assignments/{target_assignment}/revoke",
                params=selected,
                json={
                    "expected_assignment_version": 1,
                    "expected_authority_version": 2,
                    "reason": "Responsibilities ended",
                },
                headers={"Idempotency-Key": "native-revoke"},
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["result"]["status"] == "revoked" and revoked.json()["result"]["version"] == 2
            recovered = await client.get(BASE + "/operations/native-revoke", params=selected)
            assert recovered.status_code == 200 and recovered.json()["replayed"]
            audit = await client.get(BASE + "/audit", params=selected)
            assert audit.status_code == 200, audit.text
            assert {row["operation_type"] for row in audit.json()["items"]} == {
                "activation",
                "assignment.grant",
                "assignment.revoke",
            }
            assert len(audit.json()["items"]) == 3
            for response in (
                current,
                activation,
                people,
                wrong_selection,
                granted,
                replayed,
                listed,
                revoked,
                recovered,
                audit,
            ):
                assert response.headers["cache-control"] == "no-store"
                assert issued["access_token"] not in response.text
            await oauth.revoke(client_id="maestro", token=issued["refresh_token"])
            for path, query in (("/me", PROFILE), ("/people", selected), ("/operations/native-revoke", selected)):
                denied = await client.get(BASE + path, params=query)
                assert denied.status_code == 401, denied.text
                assert denied.json()["error"]["code"] == "INVALID_CREDENTIAL"
            with pytest.raises(OAuthError):
                await oauth.exchange_refresh(client_id="maestro", refresh_token=issued["refresh_token"])
    finally:
        assert (await host.close(timeout=3)).successful
