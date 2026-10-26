"""Signed human credentials must meet the current deployment MFA policy everywhere."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.contracts import CommandEffect, CommandEnvelope, ExecutionContext
from app.execution.errors import ContextExpired
from app.execution.storage import UnitOfWork
from app.features.work.details import update_work_details
from app.identity.authentication import (
    AuthenticationAssuranceRequired,
    AuthenticationRejected,
    AuthenticationUnavailable,
)
from app.identity.service import IdentityDenied, content_digest, profile_content
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    OrganizationAuthenticationBindingTable,
    OrganizationHumanAuthenticationPolicyTable,
)
from tests.support.assignment_http import assignment_client as assignment_client
from tests.support.authenticated_commands import (
    audit_snapshot,
    authenticated_context,
    authenticated_kernel,
    identity_service,
    jwt_client,
    seed_authenticated_command,
)
from tests.support.authorization import ORG, PERSON
from tests.support.database_commands import counts
from tests.support.identity_assurance import (
    configure,
)
from tests.support.identity_service import seed as seed_identity
from tests.support.identity_tokens import token
from tests.support.work_details import assert_unchanged, command
from tests.support.work_http import assert_private


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def strong_claims(**changes: Any) -> dict[str, Any]:
    return {"auth_time": int(datetime.now(UTC).timestamp()), "acr": "2", "amr": ["pwd", "otp"], **changes}


async def test_strong_signed_identity_pins_policy_and_audit_without_persisting_token(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    await configure(persistence_sessions, max_auth_age_seconds=30)
    claims = strong_claims(amr=["otp", "pwd", "hwk"])
    bearer = token(signing_key, claims=claims)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, bearer)
        evidence = context.authentication
        assert evidence is not None
        assert evidence.assurance_policy_version == 1 and len(evidence.assurance_policy_digest) == 64
        assert evidence.valid_until == datetime.fromtimestamp(claims["auth_time"], UTC) + timedelta(seconds=30)
        result = await authenticated_kernel(persistence_sessions, identity).execute(context, command())
        assert result.result["version"] == 2
    audit = await audit_snapshot(persistence_sessions)
    auth = audit["authentication"]
    assert auth["principal_ref"] == PERSON
    assert auth["assurance_policy_version"] == evidence.assurance_policy_version
    assert auth["assurance_policy_digest"] == evidence.assurance_policy_digest
    assert auth["acr"] == "2" and auth["amr"] == ["otp", "pwd", "hwk"]
    assert auth["authenticated_at"] == datetime.fromtimestamp(claims["auth_time"], UTC).isoformat()
    assert bearer not in str(audit) and "subject-1" not in str(audit)


@pytest.mark.parametrize(
    "case",
    [
        "no-strength",
        "weak-acr",
        "greater-acr",
        "padded-acr",
        "missing-amr",
        "only-password",
        "missing-auth-time",
        "stale",
        "boundary",
    ],
)
async def test_signed_token_must_prove_exact_strength_and_original_authentication_freshness(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, case: str
) -> None:
    await seed_authenticated_command(persistence_sessions)
    await configure(persistence_sessions)
    claims = strong_claims()
    if case == "no-strength":
        claims = {}
    elif case in {"weak-acr", "greater-acr", "padded-acr"}:
        claims["acr"] = {"weak-acr": "1", "greater-acr": "3", "padded-acr": "02"}[case]
    elif case == "missing-amr":
        claims.pop("amr")
    elif case == "only-password":
        claims["amr"] = ["pwd"]
    elif case == "missing-auth-time":
        claims.pop("auth_time")
    elif case in {"stale", "boundary"}:
        claims["auth_time"] -= 601 if case == "stale" else 600
        # A newly issued/refreshed access token cannot renew authentication time.
        claims["iat"] = int(datetime.now(UTC).timestamp())
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        with pytest.raises(AuthenticationAssuranceRequired):
            await identity.authenticate_human(
                token(signing_key, claims=claims), profile_ref="local-human", profile_version=1
            )
    await assert_unchanged(persistence_sessions)


async def test_required_profile_allowlist_prevents_bypass_through_still_active_older_profile(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    async with persistence_sessions.begin() as session:
        original = await session.get(AuthenticationProfileTable, ("local-human", 1))
        assert original is not None
        current = AuthenticationProfileTable(
            profile_ref=original.profile_ref,
            profile_version=2,
            provider_ref=original.provider_ref,
            subject_namespace=original.subject_namespace,
            credential_kind=original.credential_kind,
            stage_audience=original.stage_audience,
            config_schema=original.config_schema,
            config_json=deepcopy(original.config_json),
        )
        current.digest = content_digest(profile_content(current))
        session.add(current)
        await session.flush()
        session.add(
            OrganizationAuthenticationBindingTable(
                organization_ref=ORG,
                binding_ref="binding:new-profile",
                profile_ref="local-human",
                profile_version=2,
                environment="test",
                constraints_json={"allowed_clients": ["maestro"]},
                valid_from=datetime.now(UTC),
            )
        )
    await configure(persistence_sessions, profile_version=2)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        bearer = token(signing_key, claims=strong_claims())
        with pytest.raises(AuthenticationAssuranceRequired):
            await identity.authenticate_human(bearer, profile_ref="local-human", profile_version=1)
        assert (
            await identity.authenticate_human(bearer, profile_ref="local-human", profile_version=2)
        ).principal_ref == PERSON


@pytest.mark.parametrize("mutation", ["version", "content"])
async def test_policy_change_after_authentication_invalidates_evidence_even_if_new_policy_allows_token(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, mutation: str
) -> None:
    await seed_authenticated_command(persistence_sessions)
    await configure(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key, claims=strong_claims()))
        if mutation == "version":
            await configure(persistence_sessions, key="republish", expected_version=1)
        else:
            # Simulate corrupt/out-of-band facts without changing the version.
            async with persistence_sessions.begin() as session:
                row = await session.get(OrganizationHumanAuthenticationPolicyTable, ORG)
                assert row is not None
                row.max_auth_age_seconds = 601
        with pytest.raises(AuthenticationRejected, match="authentication_policy_changed"):
            await authenticated_kernel(persistence_sessions, identity).execute(context, command())
    await assert_unchanged(persistence_sessions)


async def test_policy_corruption_is_unavailable_instead_of_disabled_compatibility(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    await configure(persistence_sessions)
    async with persistence_sessions.begin() as session:
        row = await session.get(OrganizationHumanAuthenticationPolicyTable, ORG)
        assert row is not None
        row.rules_json = [{"profile_ref": "local-human", "profile_version": 1, "required_acr": "2", "unexpected": True}]
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        with pytest.raises(AuthenticationUnavailable, match="authentication_policy_unavailable"):
            await identity.authenticate_human(
                token(signing_key, claims=strong_claims()), profile_ref="local-human", profile_version=1
            )


async def test_policy_change_inside_trusted_handler_rolls_back_business_and_audit(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    await configure(persistence_sessions)

    async def corrupt_policy(
        uow: UnitOfWork, context: ExecutionContext, envelope: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        effect = await update_work_details(uow, context, envelope, payload)
        row = await uow.session.get(OrganizationHumanAuthenticationPolicyTable, ORG)
        assert row is not None
        row.version += 1
        await uow.flush()
        return effect

    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key, claims=strong_claims()))
        with pytest.raises(AuthenticationRejected, match="authentication_policy_changed"):
            await authenticated_kernel(persistence_sessions, identity, handler=corrupt_policy).execute(
                context, command()
            )
    await assert_unchanged(persistence_sessions)
    async with persistence_sessions() as session:
        row = await session.get(OrganizationHumanAuthenticationPolicyTable, ORG)
        assert row is not None and row.version == 1


async def test_pre_mfa_result_requires_current_assurance_for_original_key_replay(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        engine = authenticated_kernel(persistence_sessions, identity)
        weak = await authenticated_context(identity, token(signing_key))
        original = await engine.execute(weak, command())
        before = await audit_snapshot(persistence_sessions)
        await configure(persistence_sessions)
        with pytest.raises(AuthenticationRejected, match="authentication_policy_changed"):
            await engine.execute(weak, command())
        with pytest.raises(AuthenticationAssuranceRequired):
            await authenticated_context(identity, token(signing_key, claims={"jti": "refreshed-but-still-weak"}))
        strong = await authenticated_context(identity, token(signing_key, claims=strong_claims()))
        replayed = await engine.execute(strong, command())
        assert replayed.replayed and replayed.execution_id == original.execution_id
        assert (await engine.get_result(strong, original.execution_id)).result == original.result
        assert await audit_snapshot(persistence_sessions) == before
    assert await counts(persistence_sessions) == (2, 1, 1, 0)


async def test_assurance_deadline_rejects_an_inflight_command_before_commit(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    await configure(persistence_sessions, max_auth_age_seconds=30)
    clock = [datetime.now(UTC)]

    async def work_past_authentication_freshness(
        uow: UnitOfWork, context: ExecutionContext, envelope: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        effect = await update_work_details(uow, context, envelope, payload)
        clock[0] += timedelta(seconds=31)
        return effect

    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key, claims=strong_claims()))
        clock[0] = datetime.now(UTC)
        engine = authenticated_kernel(
            persistence_sessions, identity, handler=work_past_authentication_freshness, clock=lambda: clock[0]
        )
        with pytest.raises((AuthenticationRejected, ContextExpired)):
            await engine.execute(context, command())
    await assert_unchanged(persistence_sessions)


async def test_human_mfa_policy_does_not_turn_technical_service_authentication_into_human_authority(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_identity(persistence_sessions, service=True)
    async with persistence_sessions.begin() as session:
        # A synthetic policy belongs to the human population. This deliberately
        # does not configure the unrelated service profile as an MFA provider.
        session.add(
            OrganizationHumanAuthenticationPolicyTable(
                organization_ref="tenant-a",
                mode="required",
                rules_json=[{"profile_ref": "human-only-profile", "profile_version": 1, "required_acr": "2"}],
                max_auth_age_seconds=600,
                version=1,
            )
        )
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        bearer = token(signing_key, claims={"stage_credential_kind": "workload_oauth2"})
        result = await identity.read_self(bearer, profile_ref="local-human", profile_version=1)
        assert result.principal_type == "service" and result.authenticated_workload_ref == "workload"
        with pytest.raises(IdentityDenied):
            await identity.authenticate_human(bearer, profile_ref="local-human", profile_version=1)


@pytest.mark.parametrize("path", ["/api/v1/identity/me", "/api/v1/identity/assignments", "/api/v1/work-items/1"])
async def test_all_human_http_reads_challenge_weak_tokens_and_accept_current_strong_tokens(
    assignment_client: httpx.AsyncClient,
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    path: str,
) -> None:
    await configure(persistence_sessions)
    query = {"assignment_ref": "assignment:engineering"} if path.startswith("/api/v1/work-items") else {}
    response = await assignment_client.get(path, params=query)
    assert response.status_code == 401 and response.json()["error"]["code"] == "MFA_REQUIRED"
    assert response.headers["www-authenticate"] == 'Bearer error="insufficient_user_authentication"'
    assert_private(response)
    assignment_client.headers["Authorization"] = f"Bearer {token(signing_key, claims=strong_claims())}"
    assert (await assignment_client.get(path, params=query)).status_code == 200


async def test_work_write_and_result_recovery_require_current_mfa_at_http_boundary(
    assignment_client: httpx.AsyncClient,
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    query = {"assignment_ref": "assignment:engineering"}
    payload = {"title": "Before MFA", "description": None, "expected_version": 1}
    headers = {"Idempotency-Key": "pre-mfa-operation"}
    original = await assignment_client.put("/api/v1/work-items/1/details", params=query, headers=headers, json=payload)
    assert original.status_code == 200
    await configure(persistence_sessions)
    response = await assignment_client.put("/api/v1/work-items/1/details", params=query, headers=headers, json=payload)
    assert response.status_code == 401 and response.json()["error"]["code"] == "MFA_REQUIRED"
    response = await assignment_client.get("/api/v1/work-items/1/operations/pre-mfa-operation", params=query)
    assert response.status_code == 401 and response.json()["error"]["code"] == "MFA_REQUIRED"
    assignment_client.headers["Authorization"] = f"Bearer {token(signing_key, claims=strong_claims())}"
    recovered = await assignment_client.get("/api/v1/work-items/1/operations/pre-mfa-operation", params=query)
    assert recovered.status_code == 200 and recovered.json()["execution_id"] == original.json()["execution_id"]
