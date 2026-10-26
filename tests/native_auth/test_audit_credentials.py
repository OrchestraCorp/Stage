"""Security regression cases found by an independent native-credential audit."""

from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Self

import pyotp
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.native_auth import NativeAccountTable, NativeLoginRequestTable, NativeSessionTable
from app.native_auth.credentials import LoginProgress
from app.native_auth.errors import NativeCredentialError
from tests.support.migrations import migrate
from tests.support.native_credentials import PASSWORD, credentials, enroll, request, strong_session


@pytest.mark.parametrize("reset", ["recovery", "self-service"])
async def test_password_alone_cannot_claim_factor_replacement(
    reset: str,
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A second browser with only the leaked password lacks the recovery proof."""
    sessions = persistence_sessions
    service = await credentials(sessions)
    secret, enrolled = await enroll(service, sessions)
    if reset == "recovery":
        legitimate_request = await request(sessions)
        await service.password(*legitimate_request, "alice", PASSWORD)
        replacement = await service.recover(*legitimate_request, enrolled.recovery_codes[0])
        assert replacement.status == "enroll"
    else:
        login = await strong_session(sessions)
        next_step = int(datetime.now(UTC).timestamp()) // 30 + 1
        await service.reset_totp(login, PASSWORD, pyotp.TOTP(secret).at(next_step * 30))

    attacker_request = await request(sessions)
    try:
        attacker_progress = await service.password(*attacker_request, "alice", PASSWORD)
    except NativeCredentialError:
        return
    assert attacker_progress.enrollment_secret is None, (
        "Factor replacement must stay bound to the browser/recovery proof that authorized it; "
        "a password-only second browser must not receive a fresh seed."
    )


@pytest.mark.parametrize("operation", ["enrollment", "recovery"])
async def test_factor_change_rechecks_authorization_deadline_before_commit(
    operation: str,
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database wait must not commit an expired browser's factor replacement."""
    from app.native_auth import credentials as credentials_module

    sessions = persistence_sessions
    service = await credentials(sessions)
    recovery_code = None
    if operation == "recovery":
        _, enrolled = await enroll(service, sessions)
        recovery_code = enrolled.recovery_codes[0]
    old_session = await strong_session(sessions)
    pending = await request(sessions)
    progress = await service.password(*pending, "alice", PASSWORD)
    async with sessions.begin() as session:
        account = await session.get(NativeAccountTable, "account-a")
        browser_request = await session.get(NativeLoginRequestTable, pending[0])
        assert account is not None
        assert browser_request is not None
        browser_request.expires_at = datetime.now(UTC) + timedelta(seconds=1)
        previous_version = account.credential_version
        previous_factor = account.totp_secret

    original_revoke = service._revoke_sessions

    class Clock(datetime):
        offset = timedelta(0)

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp((datetime.now(UTC) + cls.offset).timestamp(), tz)

    async def delayed_revoke(session: AsyncSession, account_ref: str, now: datetime) -> None:
        await original_revoke(session, account_ref, now)
        # Cross the browser-request deadline while remaining inside the normal
        # HTTP request timeout; this need not involve a five-minute stalled call.
        Clock.offset = timedelta(seconds=2)

    monkeypatch.setattr(credentials_module, "datetime", Clock)
    monkeypatch.setattr(service, "_revoke_sessions", delayed_revoke)
    with pytest.raises(NativeCredentialError):
        if operation == "recovery":
            assert recovery_code is not None
            await service.recover(*pending, recovery_code)
        else:
            assert progress.enrollment_secret is not None
            await service.otp(*pending, pyotp.TOTP(progress.enrollment_secret).now())

    async with sessions() as session:
        current = await session.get(NativeAccountTable, "account-a")
        login = await session.get(NativeSessionTable, old_session)
        assert current is not None
        assert current.credential_version == previous_version and current.totp_secret == previous_factor
        assert login is not None and login.revoked_at is None


async def test_interrupted_recovery_requires_an_unused_proof_and_invalidates_old_browser(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    service = await credentials(sessions)
    _, enrolled = await enroll(service, sessions)
    first = await request(sessions)
    await service.password(*first, "alice", PASSWORD)
    first_progress = await service.recover(*first, enrolled.recovery_codes[0])
    assert first_progress.enrollment_secret is not None

    restarted = await request(sessions)
    assert (await service.password(*restarted, "alice", PASSWORD)).status == "mfa"
    with pytest.raises(NativeCredentialError):
        await service.recover(*restarted, enrolled.recovery_codes[0])
    second_progress = await service.recover(*restarted, enrolled.recovery_codes[1])
    assert second_progress.enrollment_secret is not None
    with pytest.raises(NativeCredentialError):
        await service.otp(*first, pyotp.TOTP(first_progress.enrollment_secret).now())
    complete = await service.otp(*restarted, pyotp.TOTP(second_progress.enrollment_secret).now())
    assert complete.status == "authenticated" and len(complete.recovery_codes) == 10
    assert set(complete.recovery_codes).isdisjoint(enrolled.recovery_codes)
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None and not account.totp_bootstrap_allowed
        assert len(account.recovery_hashes) == 10


async def test_operator_reset_requires_the_returned_single_use_enrollment_proof(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from app.native_auth.crypto import digest_secret

    sessions = persistence_sessions
    service = await credentials(sessions)
    await enroll(service, sessions)
    reset = await service.admin_reset("account-a", 2, reset_totp=True, actor_ref="operator:test")
    assert reset.credential_version == 3 and reset.enrollment_code is not None
    async with sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        assert account is not None and not account.totp_bootstrap_allowed
        assert account.recovery_hashes == [digest_secret(reset.enrollment_code)]
    pending = await request(sessions)
    assert (await service.password(*pending, "alice", PASSWORD)).status == "mfa"
    with pytest.raises(NativeCredentialError):
        await service.recover(*pending, "incorrect-proof")
    progress = await service.recover(*pending, reset.enrollment_code)
    assert progress.enrollment_secret is not None
    complete = await service.otp(*pending, pyotp.TOTP(progress.enrollment_secret).now())
    assert complete.status == "authenticated"
    password_reset = await service.admin_reset("account-a", 5, new_password=PASSWORD + "-new")
    assert password_reset.credential_version == 6 and password_reset.enrollment_code is None


async def test_postgresql_password_only_race_cannot_claim_recovery_enrollment(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    import asyncio

    sessions = persistence_sessions
    async with sessions() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("Concurrent credential authority requires PostgreSQL row locks")
    service = await credentials(sessions)
    _, enrolled = await enroll(service, sessions)
    legitimate = await request(sessions)
    await service.password(*legitimate, "alice", PASSWORD)
    attacker = await request(sessions)
    replacement, attacker_progress = await asyncio.gather(
        service.recover(*legitimate, enrolled.recovery_codes[0], client_address="legitimate"),
        service.password(*attacker, "alice", PASSWORD, client_address="attacker"),
        return_exceptions=True,
    )
    assert isinstance(replacement, LoginProgress) and replacement.enrollment_secret is not None
    if isinstance(attacker_progress, LoginProgress):
        assert attacker_progress.status == "mfa" and attacker_progress.enrollment_secret is None
    else:
        assert isinstance(attacker_progress, NativeCredentialError)
    complete = await service.otp(*legitimate, pyotp.TOTP(replacement.enrollment_secret).now())
    assert complete.status == "authenticated"
    with pytest.raises(NativeCredentialError):
        await service.otp(*attacker, "invalid-otp", client_address="attacker")


def test_migration_preserves_existing_factor_authority_and_refuses_loss(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "native-enrollment-authority.db"
    migrate(database, "upgrade", "e8a1c6d9f240")
    states = [("initial", 1, None), ("enrolled", 1, "encrypted"), ("reset", 3, None)]
    with sqlite3.connect(database) as connection:
        for name, version, factor in states:
            connection.execute(
                "INSERT INTO principal (principal_ref, principal_type, status) VALUES (?, 'human', 'active')",
                (name,),
            )
            connection.execute(
                "INSERT INTO native_account (account_ref, principal_ref, username, password_hash, "
                "credential_version, totp_secret, recovery_hashes) VALUES (?, ?, ?, 'unchanged', ?, ?, '[]')",
                (name, name, name, version, factor),
            )
    migrate(database, "upgrade", "b7f0d3a9216c")
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT account_ref, credential_version, totp_secret, totp_bootstrap_allowed, password_hash "
            "FROM native_account ORDER BY account_ref"
        ).fetchall()
        assert rows == [
            ("enrolled", 1, "encrypted", 0, "unchanged"),
            ("initial", 1, None, 1, "unchanged"),
            ("reset", 3, None, 0, "unchanged"),
        ]
    downgrade = migrate(database, "downgrade", "e8a1c6d9f240", successful=False)
    assert "cannot discard native factor enrollment authorization" in downgrade.stderr
    migrate(database, "upgrade", "head")
    migrate(database, "check")
