"""End-to-end management authorization, credential activation and safe recovery."""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.errors import AdminCommitUnknown, AdminConflict, AdminDenied
from app.administration.onboarding_contracts import EmployeeActivationChange, EmployeeActivationReissue
from app.execution.storage import UnitOfWork
from app.identity.service import IdentityService
from app.models.authorization import RoleAssignmentTable
from app.models.identity_foundation import IdentityControlOperationTable
from app.models.native_auth import (
    NativeAccountActivationTable,
    NativeAccountTable,
    NativeAuthEventTable,
    NativeSessionTable,
)
from app.native_auth.crypto import random_secret, verify_password
from app.native_auth.errors import NativeCredentialError
from app.native_auth.keys import KeyRing
from app.native_auth.oauth import NativeOAuth
from tests.support.employee_onboarding import PASSWORD, employee_case
from tests.support.human_administration import signing_key as signing_key
from tests.support.native_oauth import VERIFIER, authorize_values
from tests.support.native_oauth import native_keyring as native_keyring


async def test_create_activate_then_real_native_login_has_no_business_grants(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, native_keyring: KeyRing
) -> None:
    async with employee_case(persistence_sessions, signing_key) as case:
        issued = await case.create()
        target = issued.operation.result
        assert issued.activation_code is not None
        assert (await case.create()).activation_code is None
        oauth = NativeOAuth(persistence_sessions, case.config, native_keyring, credentials=case.activation.credentials)
        login = await oauth.prepare_authorization(**authorize_values())
        with pytest.raises(NativeCredentialError):
            await case.activation.credentials.password(
                login.request_ref, login.browser_secret, login.csrf_token, target.username, PASSWORD
            )
        browser, csrf = random_secret(), random_secret()
        progress = await case.activation.begin(
            activation_ref=target.activation_ref,
            code=issued.activation_code,
            password=PASSWORD,
            browser_secret=browser,
            csrf_token=csrf,
            peer_key="fixture",
        )
        assert progress.totp_secret is not None
        with pytest.raises(NativeCredentialError):
            await case.activation.credentials.password(
                login.request_ref, login.browser_secret, login.csrf_token, target.username, PASSWORD
            )
        final = await case.activation.complete(
            activation_ref=target.activation_ref,
            browser_secret=browser,
            csrf_token=csrf,
            code=pyotp.TOTP(progress.totp_secret).now(),
            peer_key="fixture",
        )
        assert final.status == "completed" and len(final.recovery_codes) == 10
        assert not (
            await case.activation.complete(
                activation_ref=target.activation_ref,
                browser_secret=browser,
                csrf_token=csrf,
                code="000000",
                peer_key="fixture",
            )
        ).recovery_codes
        async with persistence_sessions() as session:
            account = await session.get(NativeAccountTable, target.account_ref)
            assert (
                account is not None
                and account.status == "active"
                and await verify_password(account.password_hash, PASSWORD)
            )
            assert await session.scalar(select(func.count()).select_from(NativeSessionTable)) == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RoleAssignmentTable)
                    .where(RoleAssignmentTable.principal_ref == target.principal_ref)
                )
                == 0
            )
            journal = json.dumps(
                [row.result_json for row in await session.scalars(select(IdentityControlOperationTable))]
            )
            events = json.dumps([row.detail for row in await session.scalars(select(NativeAuthEventTable))])
            assert all(
                secret not in journal + events
                for secret in (issued.activation_code, PASSWORD, progress.totp_secret, *final.recovery_codes)
            )
        login = await oauth.prepare_authorization(**authorize_values())
        pending = await case.activation.credentials.password(
            login.request_ref, login.browser_secret, login.csrf_token, target.username, PASSWORD
        )
        assert pending.status == "mfa"
        # The enrollment OTP was consumed. Use the next accepted time step, not a sleep.
        next_code = pyotp.TOTP(progress.totp_secret).at((int(datetime.now(UTC).timestamp()) // 30 + 1) * 30)
        await case.activation.credentials.otp(login.request_ref, login.browser_secret, login.csrf_token, next_code)
        redirect = await oauth.finish_authorization(
            request_ref=login.request_ref, browser_secret=login.browser_secret, csrf_token=login.csrf_token
        )
        tokens = await oauth.exchange_code(
            client_id="maestro-native",
            redirect_uri=authorize_values()["redirect_uri"],
            code=parse_qs(urlsplit(redirect).query)["code"][0],
            code_verifier=VERIFIER,
        )
        identity = IdentityService(
            persistence_sessions, environment="test", native_keyring=native_keyring, native_issuer=case.config.issuer
        )
        try:
            evidence = await identity.authenticate_human(
                tokens["access_token"], profile_ref="stage-native-human", profile_version=1
            )
            assert evidence.principal_ref == target.principal_ref and evidence.membership_ref == target.membership_ref
        finally:
            await identity.aclose()


async def test_old_manager_revision_cannot_create(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with employee_case(persistence_sessions, signing_key, upgrade=False) as case:
        with pytest.raises(AdminDenied):
            await case.create()
        async with persistence_sessions() as session:
            assert await session.scalar(select(func.count()).select_from(NativeAccountActivationTable)) == 0


@pytest.mark.parametrize("started", [False, True])
async def test_reissue_invalidates_old_proofs_and_revoke_is_terminal(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, started: bool
) -> None:
    async with employee_case(persistence_sessions, signing_key) as case:
        issued = await case.create()
        ref = issued.operation.result.activation_ref
        assert issued.activation_code is not None
        browser, csrf = random_secret(), random_secret()
        if started:
            await case.activation.begin(
                activation_ref=ref,
                code=issued.activation_code,
                password=PASSWORD,
                browser_secret=browser,
                csrf_token=csrf,
                peer_key="fixture",
            )
        replacement = await case.service.change(
            case.manager.evidence,
            assignment_ref=case.manager.assignment_ref,
            activation_ref=ref,
            request=EmployeeActivationReissue(
                expected_authority_version=case.manager.authority_version,
                expected_activation_version=2 if started else 1,
                reason="Delivery replaced",
            ),
            request_id="reissue",
            idempotency_key="reissue",
            reissue=True,
        )
        assert replacement.activation_code and replacement.activation_code != issued.activation_code
        with pytest.raises(NativeCredentialError):
            await case.activation.begin(
                activation_ref=ref,
                code=issued.activation_code,
                password=PASSWORD,
                browser_secret=browser,
                csrf_token=csrf,
                peer_key="fixture",
            )
        with pytest.raises(NativeCredentialError):
            await case.activation.progress(activation_ref=ref, browser_secret=browser)
        revoked = await case.service.change(
            case.manager.evidence,
            assignment_ref=case.manager.assignment_ref,
            activation_ref=ref,
            request=EmployeeActivationChange(
                expected_authority_version=case.manager.authority_version,
                expected_activation_version=replacement.operation.result.version,
                reason="Onboarding cancelled",
            ),
            request_id="cancel",
            idempotency_key="cancel",
            reissue=False,
        )
        assert revoked.operation.result.status == "revoked" and revoked.activation_code is None
        with pytest.raises(NativeCredentialError):
            await case.activation.begin(
                activation_ref=ref,
                code=replacement.activation_code,
                password=PASSWORD,
                browser_secret=browser,
                csrf_token=csrf,
                peer_key="fixture",
            )
        with pytest.raises(AdminConflict):
            await case.create(key="reuse-cancelled-username")
        assert (
            len((await case.service.audit(case.manager.evidence, assignment_ref=case.manager.assignment_ref)).items)
            == 3
        )


async def test_unknown_create_commit_is_recoverable_without_secret_replay(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with employee_case(persistence_sessions, signing_key) as case:
        original = UnitOfWork._commit_by_owner

        async def lost_reply(self: UnitOfWork) -> None:
            await original(self)
            raise ConnectionError("synthetic lost commit acknowledgement")

        with monkeypatch.context() as patch:
            patch.setattr(UnitOfWork, "_commit_by_owner", lost_reply)
            with pytest.raises(AdminCommitUnknown):
                await case.create()
        result = await case.service.operation(
            case.manager.evidence, assignment_ref=case.manager.assignment_ref, idempotency_key="employee-create"
        )
        assert result.replayed and result.result.status == "issued"
        assert (await case.create()).activation_code is None


async def test_stale_authority_and_input_mismatch_fail_without_duplicate_person(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with employee_case(persistence_sessions, signing_key) as case:
        await case.create()
        with pytest.raises(AdminConflict, match="IDEMPOTENCY_CONFLICT"):
            await case.create(username="different.employee")
        case.manager.authority_version -= 1
        with pytest.raises(AdminConflict, match="OBJECT_VERSION_CONFLICT"):
            await case.create(username="another.employee", key="another")
