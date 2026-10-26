"""Native credentials use the existing human identity and business transaction gates."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.authentication import AuthenticationRejected, AuthenticationUnavailable
from app.identity.service import IdentityDenied, IdentityService
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    OrganizationMembershipTable,
)
from app.models.native_auth import NativeAccountTable, NativeClientTable, NativeSessionTable
from app.native_auth.keys import KeyRing
from tests.support.authenticated_commands import (
    audit_snapshot,
    authenticated_context,
    authenticated_kernel,
)
from tests.support.authorization import ORG, PERSON
from tests.support.native_identity import (
    PASSWORD,
    native_context,
)
from tests.support.native_identity import (
    native_keyring as native_keyring,
)
from tests.support.work_details import assert_unchanged, command


async def test_native_identity_edits_and_replays_through_existing_permission_kernel(
    persistence_sessions: async_sessionmaker[AsyncSession], native_keyring: KeyRing
) -> None:
    identity, oauth, context, tokens = await native_context(persistence_sessions, native_keyring)
    try:
        assert context.actor_ref == PERSON
        assert context.authentication and context.authentication.credential.native_session_ref
        kernel = authenticated_kernel(persistence_sessions, identity)
        committed = await kernel.execute(context, command())
        assert committed.result["version"] == 2
        refresh = await oauth.exchange_refresh(client_id="maestro", refresh_token=tokens["refresh_token"])
        refreshed = await authenticated_context(identity, refresh["access_token"])
        replay = await kernel.execute(refreshed, command())
        assert replay.replayed and replay.execution_id == committed.execution_id
        audit = str(await audit_snapshot(persistence_sessions))
        assert tokens["access_token"] not in audit and tokens["refresh_token"] not in audit
        await oauth.revoke(client_id="maestro", token=refresh["refresh_token"])
        with pytest.raises(AuthenticationRejected):
            await kernel.execute(refreshed, command())
        with pytest.raises(AuthenticationRejected):
            await identity.read_self(refresh["access_token"], profile_ref="local-human", profile_version=1)
    finally:
        await identity.aclose()


@pytest.mark.parametrize(
    "mutation",
    ["logout", "password", "admin_reset", "disable", "membership", "principal", "mapping", "client", "expiry"],
)
async def test_native_state_change_invalidates_already_minted_business_evidence(
    persistence_sessions: async_sessionmaker[AsyncSession], native_keyring: KeyRing, mutation: str
) -> None:
    identity, oauth, context, tokens = await native_context(persistence_sessions, native_keyring)
    try:
        assert context.authentication
        sid = context.authentication.credential.native_session_ref
        assert sid
        if mutation == "logout":
            await oauth.revoke(client_id="maestro", token=tokens["refresh_token"])
        elif mutation == "password":
            await oauth.credentials.change_password(sid, PASSWORD, PASSWORD + "-changed")
        elif mutation == "admin_reset":
            await oauth.credentials.admin_reset("native:alice", 2, reset_totp=True)
        elif mutation == "disable":
            await oauth.credentials.disable_account("native:alice", 2)
        else:
            async with persistence_sessions.begin() as session:
                if mutation == "membership":
                    member = await session.get(OrganizationMembershipTable, (ORG, "membership:alice"))
                    assert member
                    member.status = "left"
                    member.ended_at = datetime.now(UTC)
                elif mutation == "principal":
                    principal = await session.scalar(
                        select(PrincipalTable).where(PrincipalTable.principal_ref == PERSON)
                    )
                    assert principal
                    principal.disabled_at = datetime.now(UTC)
                elif mutation == "mapping":
                    session.add(PrincipalTable(principal_ref="person:other", principal_type="human"))
                    await session.flush()
                    account = await session.get(NativeAccountTable, "native:alice")
                    assert account
                    account.principal_ref = "person:other"
                elif mutation == "client":
                    client = await session.get(NativeClientTable, "maestro")
                    assert client
                    client.enabled = False
                else:
                    login = await session.get(NativeSessionTable, sid)
                    assert login
                    login.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    login.auth_time = datetime.now(UTC) - timedelta(minutes=10)
        with pytest.raises((AuthenticationRejected, IdentityDenied)):
            await authenticated_kernel(persistence_sessions, identity).execute(context, command())
        await assert_unchanged(persistence_sessions)
    finally:
        await identity.aclose()


async def test_native_id_token_and_disabled_local_verifier_cannot_authenticate(
    persistence_sessions: async_sessionmaker[AsyncSession], native_keyring: KeyRing
) -> None:
    identity, _, _, tokens = await native_context(persistence_sessions, native_keyring)
    try:
        with pytest.raises(AuthenticationRejected):
            await authenticated_context(identity, tokens["id_token"])
        disabled = IdentityService(persistence_sessions, environment="test")
        try:
            with pytest.raises(AuthenticationUnavailable):
                await authenticated_context(disabled, tokens["access_token"])
        finally:
            await disabled.aclose()
    finally:
        await identity.aclose()
