"""Independent activation checks for authority changes and uncertain consumption."""

from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.native_auth.activation as activation_module
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    ExternalIdentityTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationMembershipTable,
    TrustStateTable,
)
from app.models.native_auth import (
    NativeAccountActivationTable,
    NativeAccountTable,
    NativeBrowserSessionTable,
    NativeSessionTable,
)
from app.native_auth.activation import ActivationProgress, NativeActivation
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials
from app.native_auth.crypto import digest_secret
from app.native_auth.errors import NativeCommitUnknown, NativeCredentialError
from app.native_auth.onboarding_source import load_onboarding_source
from app.storage.coordination import LockMode, lock_organization
from tests.support.identity_administration import install as install_identity
from tests.support.native_administration import install_native

CONFIG = NativeAuthConfig(issuer="http://127.0.0.1:8080", organization_ref="tenant-a", environment="test")
ACTIVATION = "activation:audit"
PERSON = "person:pending-audit"
MEMBER = "member:pending-audit"
EXTERNAL = "external:pending-audit"
ACCOUNT = "account:pending-audit"
CODE = "synthetic-activation-code-audit-only-2026"
BROWSER = "synthetic-browser-secret-audit-only-2026"
CSRF = "synthetic-csrf-secret-audit-only-2026"
PASSWORD = "synthetic-password-audit-only-2026"


@pytest.fixture
async def activation(persistence_sessions: async_sessionmaker[AsyncSession]) -> NativeActivation:
    await install_identity(persistence_sessions)
    await install_native(persistence_sessions)
    async with persistence_sessions.begin() as session:
        policy = await session.get(OrganizationHumanAuthenticationPolicyTable, CONFIG.organization_ref)
        if policy is None:
            policy = OrganizationHumanAuthenticationPolicyTable(organization_ref=CONFIG.organization_ref)
            session.add(policy)
        policy.mode = "required"
        policy.max_auth_age_seconds = 900
        policy.rules_json = [
            {
                "profile_ref": "stage-native-human",
                "profile_version": 1,
                "required_acr": "urn:orchestra:acr:pwd-totp",
                "required_amr": ["pwd", "otp"],
            }
        ]
        session.add(PrincipalTable(principal_ref=PERSON, principal_type="human", status="suspended"))
        await session.flush()
        session.add(
            OrganizationMembershipTable(
                organization_ref=CONFIG.organization_ref,
                membership_ref=MEMBER,
                principal_ref=PERSON,
                principal_type="human",
                status="suspended",
                source_ref=ACTIVATION,
                valid_from=datetime.now(UTC),
            )
        )
        session.add(
            ExternalIdentityTable(
                external_identity_ref=EXTERNAL,
                principal_ref=PERSON,
                principal_type="human",
                provider_ref="provider:native-auth",
                subject_namespace="stage-native-human-v1",
                subject_exact=ACCOUNT,
                link_evidence_ref=ACTIVATION,
                status="suspended",
            )
        )
        session.add(
            NativeAccountTable(
                account_ref=ACCOUNT,
                principal_ref=PERSON,
                username="pending-audit",
                password_hash=None,
                status="pending",
                totp_bootstrap_allowed=False,
            )
        )
        await session.flush()
        await session.scalar(
            select(TrustStateTable).where(TrustStateTable.trust_ref == CONFIG.trust_ref).with_for_update(read=True)
        )
        await lock_organization(session, CONFIG.organization_ref, mode=LockMode.EXCLUSIVE)
        source = await load_onboarding_source(session, CONFIG)
        session.add(
            NativeAccountActivationTable(
                activation_ref=ACTIVATION,
                organization_ref=CONFIG.organization_ref,
                principal_ref=PERSON,
                membership_ref=MEMBER,
                external_identity_ref=EXTERNAL,
                account_ref=ACCOUNT,
                source_pin=source,
                code_hash=digest_secret(CODE),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    credentials = NativeCredentials(
        persistence_sessions,
        fernet_key=Fernet.generate_key(),
        organization_ref=CONFIG.organization_ref,
        trust_ref=CONFIG.trust_ref,
    )
    return NativeActivation(persistence_sessions, CONFIG, credentials=credentials)


async def begin(activation: NativeActivation) -> ActivationProgress:
    return await activation.begin(
        activation_ref=ACTIVATION,
        code=CODE,
        password=PASSWORD,
        browser_secret=BROWSER,
        csrf_token=CSRF,
        peer_key="audit-local",
    )


async def complete(activation: NativeActivation, secret: str) -> ActivationProgress:
    return await activation.complete(
        activation_ref=ACTIVATION,
        browser_secret=BROWSER,
        csrf_token=CSRF,
        code=pyotp.TOTP(secret).now(),
        peer_key="audit-local",
    )


async def assert_cleared(activation: NativeActivation) -> None:
    async with activation.sessions() as session:
        row = await session.get(NativeAccountActivationTable, ACTIVATION)
        assert row is not None
        assert (
            row.code_hash is row.browser_hash is row.csrf_hash is row.pending_totp is row.pending_password_hash is None
        )


async def test_pending_account_cannot_login_and_completion_creates_no_session(activation: NativeActivation) -> None:
    async with activation.sessions() as session:
        with pytest.raises(NativeCredentialError):
            await activation.credentials.lock_account(session, ACCOUNT)
    started = await begin(activation)
    assert started.totp_secret and started.totp_secret not in repr(started)
    assert started.totp_uri and started.totp_uri not in repr(started)
    finished = await complete(activation, started.totp_secret)
    assert finished.status == "completed" and len(finished.recovery_codes) == 10
    assert all(value not in repr(finished) for value in finished.recovery_codes)
    replay = await complete(activation, started.totp_secret)
    assert replay.status == "completed" and not replay.recovery_codes and replay.totp_secret is None
    async with activation.sessions() as session:
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == PERSON))
        member = await session.get(OrganizationMembershipTable, (CONFIG.organization_ref, MEMBER))
        external = await session.get(ExternalIdentityTable, EXTERNAL)
        account = await session.get(NativeAccountTable, ACCOUNT)
        assert principal and member and external and account
        assert principal.status == member.status == external.status == account.status == "active"
        assert principal.version == member.version == external.version == account.credential_version == 2
        assert account.recovery_hashes == [digest_secret(value) for value in finished.recovery_codes]
        assert await session.scalar(select(func.count()).select_from(NativeSessionTable)) == 0
        assert await session.scalar(select(func.count()).select_from(NativeBrowserSessionTable)) == 0


async def test_used_code_and_wrong_browser_cannot_rebind_ceremony(activation: NativeActivation) -> None:
    started = await begin(activation)
    with pytest.raises(NativeCredentialError):
        await begin(activation)
    with pytest.raises(NativeCredentialError):
        await activation.progress(activation_ref=ACTIVATION, browser_secret="different-browser-proof-audit-only")
    current = await activation.progress(activation_ref=ACTIVATION, browser_secret=BROWSER)
    assert current.totp_secret == started.totp_secret


async def test_totp_budget_is_persistent_and_clears_abandoned_secrets(activation: NativeActivation) -> None:
    started = await begin(activation)
    assert started.totp_secret
    for _ in range(5):
        with pytest.raises(NativeCredentialError):
            await activation.complete(
                activation_ref=ACTIVATION,
                browser_secret=BROWSER,
                csrf_token=CSRF,
                code="invalid-otp",
                peer_key="audit-local",
            )
    await assert_cleared(activation)
    with pytest.raises(NativeCredentialError):
        await complete(activation, started.totp_secret)
    async with activation.sessions() as session:
        account = await session.get(NativeAccountTable, ACCOUNT)
        assert account and account.status == "pending" and account.password_hash is None
        row = await session.get(NativeAccountActivationTable, ACTIVATION)
        assert row and row.failures == 5


@pytest.mark.parametrize("completed", [False, True])
async def test_ceremony_and_completed_recovery_proof_expire(activation: NativeActivation, completed: bool) -> None:
    started = await begin(activation)
    assert started.totp_secret
    if completed:
        await complete(activation, started.totp_secret)
    async with activation.sessions.begin() as session:
        row = await session.get(NativeAccountActivationTable, ACTIVATION)
        assert row
        row.ceremony_expires_at = datetime.now(UTC)
    with pytest.raises(NativeCredentialError):
        await activation.progress(activation_ref=ACTIVATION, browser_secret=BROWSER)
    await assert_cleared(activation)


@pytest.mark.parametrize("kind", ["principal", "membership", "external", "source"])
async def test_changed_authority_clears_ceremony_and_cannot_be_restored_implicitly(
    activation: NativeActivation, kind: str
) -> None:
    await begin(activation)
    async with activation.sessions.begin() as session:
        row: PrincipalTable | OrganizationMembershipTable | ExternalIdentityTable | AuthenticationProfileTable | None
        if kind == "principal":
            row = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == PERSON))
        elif kind == "membership":
            row = await session.get(OrganizationMembershipTable, (CONFIG.organization_ref, MEMBER))
        elif kind == "external":
            row = await session.get(ExternalIdentityTable, EXTERNAL)
        else:
            row = await session.get(AuthenticationProfileTable, ("stage-native-human", 1))
        assert row
        row.version += 1
    with pytest.raises(NativeCredentialError):
        await activation.progress(activation_ref=ACTIVATION, browser_secret=BROWSER)
    await assert_cleared(activation)


@pytest.mark.parametrize("at_completion", [False, True])
async def test_unknown_commit_uses_bound_status_without_repeating_secrets(
    activation: NativeActivation, monkeypatch: pytest.MonkeyPatch, at_completion: bool
) -> None:
    started = await begin(activation) if at_completion else None
    real_commit = activation_module.commit_authentication

    async def committed_but_response_lost(session: AsyncSession) -> None:
        await real_commit(session)
        raise NativeCommitUnknown()

    monkeypatch.setattr(activation_module, "commit_authentication", committed_but_response_lost)
    with pytest.raises(NativeCommitUnknown):
        if at_completion:
            assert started and started.totp_secret
            await complete(activation, started.totp_secret)
        else:
            await begin(activation)
    monkeypatch.setattr(activation_module, "commit_authentication", real_commit)
    recovered = await activation.progress(activation_ref=ACTIVATION, browser_secret=BROWSER)
    assert recovered.status == ("completed" if at_completion else "enrolling")
    assert not recovered.recovery_codes
    assert (recovered.totp_secret is None) == at_completion


async def test_expiration_while_hashing_cannot_start_ceremony(
    activation: NativeActivation, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_hash = activation_module.hash_password

    async def delayed_password(password: str) -> str:
        hashed = await real_hash(password)
        async with activation.sessions.begin() as session:
            row = await session.get(NativeAccountActivationTable, ACTIVATION)
            assert row
            row.expires_at = datetime.now(UTC)
        return hashed

    monkeypatch.setattr(activation_module, "hash_password", delayed_password)
    with pytest.raises(NativeCredentialError):
        await begin(activation)
    await assert_cleared(activation)
