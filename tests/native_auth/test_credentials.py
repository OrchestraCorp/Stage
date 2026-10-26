"""Credential transitions prove single-use factors and revocation, not only hashing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyotp
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.identity import PrincipalTable
from app.models.native_auth import (
    NativeAccountTable,
    NativeAuthEventTable,
    NativeLoginRequestTable,
    NativeLoginThrottleTable,
    NativeSessionTable,
)
from app.native_auth.credentials import LoginProgress
from app.native_auth.crypto import TotpCipher, digest_secret, hash_password, verify_password
from app.native_auth.errors import NativeCredentialError
from app.native_auth.keys import KeyRing
from tests.support import native_oauth as oauth_support
from tests.support.migrations import migrate
from tests.support.native_credentials import (
    PASSWORD,
    credentials,
    enroll,
    request,
    strong_session,
)

native_keyring = oauth_support.native_keyring


async def test_password_hash_is_salted_argon2id_and_wrong_password_fails() -> None:
    one = await hash_password(PASSWORD)
    two = await hash_password(PASSWORD)
    assert one.startswith("$argon2id$") and one != two
    assert await verify_password(one, PASSWORD)
    assert not await verify_password(one, PASSWORD + "wrong")
    assert not await verify_password(None, PASSWORD)
    assert not await verify_password("broken", PASSWORD)


@pytest.mark.parametrize("password", ["short", "a" * 257, "a" * 13 + "\0", "a" * 13 + "\ud800"])
async def test_invalid_new_password_rejected(password: str) -> None:
    with pytest.raises(NativeCredentialError, match="invalid_password"):
        await hash_password(password)


async def test_enrollment_requires_password_actual_otp_and_returns_recovery_only_once(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    ref, browser, csrf = await request(sessions)
    before = await service.progress(ref, browser)
    assert before.status == "password" and before.enrollment_secret is None
    with pytest.raises(NativeCredentialError):
        await service.otp(ref, browser, csrf, "000000")
    progress = await service.password(ref, browser, csrf, "alice", PASSWORD)
    assert progress.status == "enroll" and progress.enrollment_secret is not None
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        pending = await session.get(NativeLoginRequestTable, ref)
        assert account is not None and pending is not None
        assert account.totp_secret is None and pending.pending_totp != progress.enrollment_secret
    final = await service.otp(ref, browser, csrf, pyotp.TOTP(progress.enrollment_secret).now())
    assert len(final.recovery_codes) == 10 and len(set(final.recovery_codes)) == 10
    assert not (await service.progress(ref, browser)).recovery_codes
    with pytest.raises(NativeCredentialError):
        await service.otp(ref, browser, csrf, pyotp.TOTP(progress.enrollment_secret).now())
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None and account.credential_version == 2
        assert account.recovery_hashes == [digest_secret(code) for code in final.recovery_codes]
        assert all(code not in repr(account.recovery_hashes) for code in final.recovery_codes)
        events = (await session.scalars(select(NativeAuthEventTable))).all()
        assert all(
            PASSWORD not in repr(row.detail) and progress.enrollment_secret not in repr(row.detail) for row in events
        )


@pytest.mark.parametrize("tamper", ["browser", "csrf", "expiry"])
async def test_browser_binding_csrf_and_deadline(
    tamper: str, persistence_sessions: async_sessionmaker[AsyncSession]
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    ref, browser, csrf = await request(sessions)
    if tamper == "expiry":
        async with sessions.begin() as session:
            row = await session.get(NativeLoginRequestTable, ref)
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(NativeCredentialError, match="invalid_request"):
        await service.password(
            ref,
            browser + "x" if tamper == "browser" else browser,
            csrf + "x" if tamper == "csrf" else csrf,
            "alice",
            PASSWORD,
        )
    async with sessions() as session:
        row = await session.get(NativeLoginRequestTable, ref)
        assert row is not None and row.status == "password"


async def test_failed_password_persists_budget_and_audit(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    ref, browser, csrf = await request(sessions)
    for _ in range(5):
        with pytest.raises(NativeCredentialError, match="invalid_credentials"):
            await service.password(ref, browser, csrf, "alice", PASSWORD + "wrong")
    with pytest.raises(NativeCredentialError):
        await service.password(ref, browser, csrf, "alice", PASSWORD)
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None and account.locked_until is not None
        assert account.locked_until > datetime.now(UTC)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NativeAuthEventTable)
                .where(NativeAuthEventTable.event == "login.failed")
            )
            == 6
        )


async def test_unknown_username_has_same_failure_and_transport_budget(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    ref, browser, csrf = await request(sessions)
    with pytest.raises(NativeCredentialError, match="invalid_credentials"):
        await service.password(ref, browser, csrf, "does-not-exist", PASSWORD)
    async with sessions.begin() as session:
        bucket = await session.get(NativeLoginThrottleTable, digest_secret("native-login-peer:local"))
        assert bucket is not None
        bucket.failures = 30
    with pytest.raises(NativeCredentialError, match="rate_limited"):
        await service.password(ref, browser, csrf, "alice", PASSWORD)


async def test_same_totp_step_cannot_authenticate_another_request(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    secret, _ = await enroll(service, sessions)
    ref, browser, csrf = await request(sessions)
    assert (await service.password(ref, browser, csrf, "alice", PASSWORD)).status == "mfa"
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None
        consumed_step = account.last_totp_step
    with pytest.raises(NativeCredentialError):
        await service.otp(ref, browser, csrf, pyotp.TOTP(secret).at(consumed_step * 30))
    next_step = int(datetime.now(UTC).timestamp()) // 30 + 1
    result = await service.otp(ref, browser, csrf, pyotp.TOTP(secret).at(next_step * 30))
    assert result.status == "authenticated" and not result.recovery_codes


async def test_recovery_revokes_old_sessions_and_requires_new_factor(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    old_secret, enrollment = await enroll(service, sessions)
    old_session = await strong_session(sessions)
    first = await request(sessions)
    second = await request(sessions)
    await service.password(*first, "alice", PASSWORD)
    await service.password(*second, "alice", PASSWORD)
    progress = await service.recover(*first, enrollment.recovery_codes[0])
    assert progress.status == "enroll" and progress.enrollment_secret not in (None, old_secret)
    async with sessions() as session:
        old = await session.get(NativeSessionTable, old_session)
        account = await session.get(NativeAccountTable, "account-a")
        assert old is not None and old.revoked_at is not None
        assert account is not None and account.totp_secret is None and not account.totp_bootstrap_allowed
        assert account.recovery_hashes == [digest_secret(code) for code in enrollment.recovery_codes[1:]]
    with pytest.raises(NativeCredentialError):
        await service.recover(*second, enrollment.recovery_codes[0])
    assert progress.enrollment_secret is not None
    finished = await service.otp(*first, pyotp.TOTP(progress.enrollment_secret).now())
    assert finished.status == "authenticated" and len(finished.recovery_codes) == 10
    assert set(finished.recovery_codes).isdisjoint(enrollment.recovery_codes)


async def test_password_change_revokes_all_sessions_and_pending_logins(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    await enroll(service, sessions)
    first = await strong_session(sessions)
    second = await strong_session(sessions)
    pending = await request(sessions)
    await service.password(*pending, "alice", PASSWORD)
    version = await service.change_password(first, PASSWORD, PASSWORD + "-changed")
    assert version == 3
    with pytest.raises(NativeCredentialError):
        await service.change_password(second, PASSWORD + "-changed", PASSWORD)
    with pytest.raises(NativeCredentialError):
        await service.otp(*pending, "000000")
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None and await verify_password(account.password_hash, PASSWORD + "-changed")
        assert not await verify_password(account.password_hash, PASSWORD)
        rows = (await session.scalars(select(NativeSessionTable))).all()
        assert all(row.revoked_at is not None for row in rows)


async def test_security_change_requires_recent_strong_session(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    await enroll(service, sessions)
    expired_proof = await strong_session(sessions, age=301)
    with pytest.raises(NativeCredentialError):
        await service.change_password(expired_proof, PASSWORD, PASSWORD + "-changed")
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None and account.credential_version == 2


async def test_reset_factor_requires_new_otp_and_revokes_session(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    secret, _ = await enroll(service, sessions)
    reference = await strong_session(sessions)
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None
        consumed_step = account.last_totp_step
    with pytest.raises(NativeCredentialError):
        await service.reset_totp(reference, PASSWORD, pyotp.TOTP(secret).at(consumed_step * 30))
    next_step = int(datetime.now(UTC).timestamp()) // 30 + 1
    reset = await service.reset_totp(reference, PASSWORD, pyotp.TOTP(secret).at(next_step * 30))
    assert reset.credential_version == 3 and reset.enrollment_code not in repr(reset)
    pending = await request(sessions)
    assert (await service.password(*pending, "alice", PASSWORD)).status == "mfa"
    replacement = await service.recover(*pending, reset.enrollment_code)
    assert replacement.status == "enroll" and replacement.enrollment_secret is not None
    assert (await service.otp(*pending, pyotp.TOTP(replacement.enrollment_secret).now())).status == "authenticated"


async def test_disable_and_os_recovery_use_expected_version(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    await enroll(service, sessions)
    current = await strong_session(sessions)
    with pytest.raises(NativeCredentialError, match="version_conflict"):
        await service.admin_reset("account-a", 1, reset_totp=True)
    reset = await service.admin_reset("account-a", 2, new_password=PASSWORD + "-reset", reset_totp=True)
    assert reset.credential_version == 3 and reset.enrollment_code is not None
    assert reset.enrollment_code not in repr(reset)
    async with sessions() as session:
        old = await session.get(NativeSessionTable, current)
        assert old is not None and old.revoked_at is not None
    assert await service.disable_account("account-a", 3) == 4
    pending = await request(sessions)
    with pytest.raises(NativeCredentialError):
        await service.password(*pending, "alice", PASSWORD + "-reset")


@pytest.mark.parametrize("mutation", ["status", "disabled_at", "version"])
async def test_current_principal_and_credential_state_rechecked_between_factors(
    mutation: str, persistence_sessions: async_sessionmaker[AsyncSession]
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    pending = await request(sessions)
    progress = await service.password(*pending, "alice", PASSWORD)
    assert progress.enrollment_secret is not None
    async with sessions.begin() as session:
        if mutation == "version":
            account = await session.get(NativeAccountTable, "account-a")
            assert account is not None
            account.credential_version += 1
        else:
            principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == "human-a"))
            assert principal is not None
            if mutation == "status":
                principal.status = "disabled"
            else:
                principal.disabled_at = datetime.now(UTC)
    with pytest.raises(NativeCredentialError):
        await service.otp(*pending, pyotp.TOTP(progress.enrollment_secret).now())


async def test_native_account_cannot_bind_service_principal(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    async with sessions.begin() as session:
        session.add(PrincipalTable(principal_ref="service", principal_type="service"))
    with pytest.raises(NativeCredentialError):
        await service.create_account("service-account", "service", "service", PASSWORD)
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(
                NativeAccountTable(
                    account_ref="bad",
                    principal_ref="service",
                    principal_type="service",
                    username="bad",
                    password_hash="bad",
                )
            )


def test_totp_cipher_refuses_wrong_key() -> None:
    one = TotpCipher(Fernet.generate_key())
    two = TotpCipher(Fernet.generate_key())
    secret = one.new_secret()
    encrypted = one.encrypt(secret)
    assert encrypted != secret and one.decrypt(encrypted) == secret
    with pytest.raises(NativeCredentialError, match="authentication_unavailable"):
        two.decrypt(encrypted)


def test_native_migration_roundtrip_and_no_silent_loss(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "native-migration.db"
    migrate(database, "upgrade", "head")
    migrate(database, "check")
    migrate(database, "downgrade", "9c2e7a4b6d10")
    migrate(database, "upgrade", "b7f0d3a9216c")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO native_client(client_id, redirect_uris) VALUES('maestro','[]')")
    result = migrate(database, "downgrade", "9c2e7a4b6d10", successful=False)
    assert "cannot discard native authentication facts" in result.stderr


async def test_valid_password_cannot_reset_failed_otp_budget(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    await enroll(service, sessions)
    for index in range(5):
        pending = await request(sessions)
        await service.password(*pending, "alice", PASSWORD, client_address=f"peer-{index}")
        with pytest.raises(NativeCredentialError):
            await service.otp(*pending, "not-an-otp", client_address=f"peer-{index}")
    pending = await request(sessions)
    with pytest.raises(NativeCredentialError):
        await service.password(*pending, "alice", PASSWORD, client_address="another-peer")
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None and account.locked_until is not None and account.locked_until > datetime.now(UTC)


@pytest.mark.parametrize("factor", ["otp", "recovery"])
async def test_postgresql_concurrent_factor_consumption_has_one_winner(
    factor: str,
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    import asyncio

    sessions = persistence_sessions
    async with sessions() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("Real row-lock serialization is PostgreSQL-only")
    service = await credentials(sessions)
    secret, enrolled = await enroll(service, sessions)
    one, two = await request(sessions), await request(sessions)
    await service.password(*one, "alice", PASSWORD)
    await service.password(*two, "alice", PASSWORD)
    next_step = int(datetime.now(UTC).timestamp()) // 30 + 1
    value = pyotp.TOTP(secret).at(next_step * 30) if factor == "otp" else enrolled.recovery_codes[0]
    call = service.otp if factor == "otp" else service.recover
    results = await asyncio.gather(call(*one, value), call(*two, value), return_exceptions=True)
    assert sum(isinstance(item, LoginProgress) for item in results) == 1
    assert sum(isinstance(item, NativeCredentialError) for item in results) == 1
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None
        if factor == "otp":
            assert account.last_totp_step == next_step
        else:
            assert account.credential_version == 3
            assert account.recovery_hashes == [digest_secret(code) for code in enrolled.recovery_codes[1:]]


@pytest.mark.parametrize("operation", ["password", "totp"])
async def test_security_change_rolls_back_if_proof_expires_during_transaction(
    operation: str,
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.native_auth import credentials as credentials_module

    sessions = persistence_sessions
    service = await credentials(sessions)
    secret, _ = await enroll(service, sessions)
    reference = await strong_session(sessions)
    original = service._revoke_sessions

    class Clock(datetime):
        offset = timedelta(0)

        @classmethod
        def now(cls, tz: object = None) -> Clock:
            return cls.fromtimestamp((datetime.now(UTC) + cls.offset).timestamp(), UTC)

    async def slow_revoke(session: AsyncSession, account_ref: str, now: datetime) -> None:
        await original(session, account_ref, now)
        Clock.offset = timedelta(minutes=6)

    monkeypatch.setattr(credentials_module, "datetime", Clock)
    monkeypatch.setattr(service, "_revoke_sessions", slow_revoke)
    with pytest.raises(NativeCredentialError):
        if operation == "password":
            await service.change_password(reference, PASSWORD, PASSWORD + "changed")
        else:
            next_step = int(datetime.now(UTC).timestamp()) // 30 + 1
            await service.reset_totp(reference, PASSWORD, pyotp.TOTP(secret).at(next_step * 30))
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        login = await session.get(NativeSessionTable, reference)
        assert account is not None and account.credential_version == 2 and account.totp_secret is not None
        assert await verify_password(account.password_hash, PASSWORD)
        assert login is not None and login.revoked_at is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NativeAuthEventTable)
                .where(NativeAuthEventTable.event.in_(["password.changed", "factor.reset"]))
            )
            == 0
        )


async def test_invalid_strong_session_acr_cannot_change_credentials(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    await enroll(service, sessions)
    reference = await strong_session(sessions)
    async with sessions.begin() as session:
        login = await session.get(NativeSessionTable, reference)
        assert login is not None
        login.acr = "weak"
    with pytest.raises(NativeCredentialError):
        await service.change_password(reference, PASSWORD, PASSWORD + "changed")


async def test_account_change_rechecks_access_token_deadline_after_database_work(
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    native_keyring: KeyRing,
) -> None:
    from app.native_auth import credentials as credentials_module
    from tests.support.native_identity import native_context

    keyring = native_keyring
    identity, oauth, context, tokens = await native_context(persistence_sessions, keyring)
    try:
        claims = keyring.verify_access(tokens["access_token"], oauth.config.issuer, oauth.config.audience)
        claims["exp"] = int(datetime.now(UTC).timestamp()) + 20
        proof = await oauth.authenticate_account(keyring.sign(claims, typ="at+jwt"))
        original = oauth.credentials._revoke_sessions

        class Clock(datetime):
            offset = timedelta(0)

            @classmethod
            def now(cls, tz: object = None) -> Clock:
                return cls.fromtimestamp((datetime.now(UTC) + cls.offset).timestamp(), UTC)

        async def slow_revoke(session: AsyncSession, account_ref: str, now: datetime) -> None:
            await original(session, account_ref, now)
            Clock.offset = timedelta(seconds=21)

        monkeypatch.setattr(credentials_module, "datetime", Clock)
        monkeypatch.setattr(oauth.credentials, "_revoke_sessions", slow_revoke)
        from tests.support.native_identity import PASSWORD as INTEGRATION_PASSWORD

        with pytest.raises(NativeCredentialError):
            await oauth.credentials.change_password(
                proof["sid"],
                INTEGRATION_PASSWORD,
                INTEGRATION_PASSWORD + "changed",
                access_claims=proof,
            )
        async with persistence_sessions() as session:
            account = await session.get(NativeAccountTable, "native:alice")
            login = await session.get(NativeSessionTable, proof["sid"])
            assert account is not None and account.credential_version == 2
            assert login is not None and login.revoked_at is None
    finally:
        await identity.aclose()


async def test_cancelled_password_work_retains_capacity_until_worker_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    from app.native_auth import crypto

    started = threading.Event()
    release = threading.Event()
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(crypto, "_PASSWORD_SLOTS", slots)

    def work() -> bool:
        started.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test_worker_timeout")
        return True

    task = asyncio.create_task(crypto._password_work(work))
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert slots.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not slots.locked()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
