"""Logout hints identify an RP; only a confirmed browser proof can remove its login."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.identity import PrincipalTable
from app.models.native_auth import (
    NativeAccountTable,
    NativeAuthEventTable,
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeLogoutRequestTable,
    NativeSessionTable,
)
from app.native_auth.crypto import digest_secret, random_secret
from app.native_auth.errors import NativeCommitUnknown
from app.native_auth.logout import LogoutChallenge, NativeLogout
from app.native_auth.oauth import NativeOAuth, OAuthError
from tests.support.native_oauth import CLIENT, native_keyring, oauth  # noqa: F401

LOGOUT_REDIRECT = "https://application.example/signed-out?from=stage"


@pytest.fixture
async def logout(oauth: NativeOAuth) -> NativeLogout:  # noqa: F811
    async with oauth.sessions.begin() as session:
        client = await session.get(NativeClientTable, CLIENT)
        assert client is not None
        client.post_logout_redirect_uris = [LOGOUT_REDIRECT]
        client.display_name = "Maestro"
    return NativeLogout(oauth.sessions, oauth.config, credentials=oauth.credentials, oauth=oauth)


async def browser(
    logout: NativeLogout, *, account_ref: str = "account:native", prefix: str = "first"
) -> tuple[str, str]:
    secret = random_secret()
    now = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=2)
    async with logout.sessions.begin() as session:
        session.add(
            NativeBrowserSessionTable(
                session_hash=digest_secret(secret),
                account_ref=account_ref,
                credential_version=1,
                auth_time=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        await session.flush()
        session.add(
            NativeSessionTable(
                session_ref=prefix + "-rp",
                account_ref=account_ref,
                client_id=CLIENT,
                credential_version=1,
                auth_time=now,
                expires_at=now + timedelta(hours=1),
                acr="urn:orchestra:acr:pwd-totp",
                amr=["pwd", "otp"],
                browser_session_hash=digest_secret(secret),
            )
        )
    hint = logout.oauth.keyring.sign(
        {
            "iss": logout.config.issuer,
            "sub": account_ref,
            "aud": CLIENT,
            "azp": CLIENT,
            "sid": prefix + "-rp",
            "auth_time": int(now.timestamp()),
            "iat": int(now.timestamp()),
            "exp": int(now.timestamp()) + 300,
            "acr": "urn:orchestra:acr:pwd-totp",
            "amr": ["pwd", "otp"],
        },
        typ="JWT",
    )
    return secret, hint


async def confirm(
    logout: NativeLogout, challenge: LogoutChallenge, sso: str | None, *, allow: bool = True, **changes: Any
) -> Any:
    return await logout.confirm(
        **{
            "request_ref": challenge.request_ref,
            "browser_secret": challenge.browser_secret,
            "csrf_token": challenge.csrf_token,
            "sso_cookie": sso,
            "confirm": allow,
            **changes,
        }
    )


async def test_get_confirmation_changes_no_login_then_post_revokes_only_this_browser(logout: NativeLogout) -> None:
    current, hint = await browser(logout)
    other, _ = await browser(logout, prefix="other")
    async with logout.sessions.begin() as session:
        source = await session.get(NativeSessionTable, "first-rp")
        assert source is not None
        session.add(NativeClientTable(client_id="other-client", redirect_uris=["https://other.example/callback"]))
        await session.flush()
        session.add(
            NativeSessionTable(
                session_ref="first-other-client",
                account_ref=source.account_ref,
                client_id="other-client",
                credential_version=1,
                auth_time=source.auth_time,
                expires_at=source.expires_at,
                acr=source.acr,
                amr=source.amr,
                browser_session_hash=digest_secret(current),
            )
        )
    challenge = await logout.prepare(
        id_token_hint=hint, post_logout_redirect_uri=LOGOUT_REDIRECT, state="opaque-state", sso_cookie=current
    )
    assert current not in repr(challenge) and hint not in repr(challenge)
    assert challenge.client_name == "Maestro" and challenge.redirect_uri == LOGOUT_REDIRECT
    async with logout.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(NativeSessionTable).where(NativeSessionTable.revoked_at.is_not(None))
            )
            == 0
        )
    result = await confirm(logout, challenge, current)
    assert result.logged_out and result.redirect_uri == LOGOUT_REDIRECT and result.state == "opaque-state"
    async with logout.sessions() as session:
        first = await session.get(NativeBrowserSessionTable, digest_secret(current))
        second = await session.get(NativeBrowserSessionTable, digest_secret(other))
        first_rp = await session.get(NativeSessionTable, "first-rp")
        other_rp = await session.get(NativeSessionTable, "first-other-client")
        second_rp = await session.get(NativeSessionTable, "other-rp")
        assert first is not None and first.revoked_at is not None
        assert second is not None and second.revoked_at is None
        assert first_rp is not None and first_rp.revoked_at is not None
        assert other_rp is not None and other_rp.revoked_at is not None
        assert second_rp is not None and second_rp.revoked_at is None
        events = (
            await session.scalars(
                select(NativeAuthEventTable).where(NativeAuthEventTable.event == "browser.logout_confirmed")
            )
        ).all()
        assert len(events) == 1
        data = json.dumps(events[0].detail)
        assert all(
            secret not in data for secret in (current, hint, challenge.csrf_token, "opaque-state", LOGOUT_REDIRECT)
        )


async def test_hint_without_browser_cookie_never_revokes_a_session(logout: NativeLogout) -> None:
    sso, hint = await browser(logout)
    challenge = await logout.prepare(id_token_hint=hint, post_logout_redirect_uri=LOGOUT_REDIRECT, state="caller")
    result = await confirm(logout, challenge, None)
    assert not result.logged_out and result.redirect_uri == LOGOUT_REDIRECT and result.state == "caller"
    async with logout.sessions() as session:
        saved = await session.get(NativeBrowserSessionTable, digest_secret(sso))
        login = await session.get(NativeSessionTable, "first-rp")
        assert saved is not None and saved.revoked_at is None
        assert login is not None and login.revoked_at is None


async def test_declining_logout_consumes_confirmation_without_redirect_or_revocation(logout: NativeLogout) -> None:
    sso, _ = await browser(logout)
    challenge = await logout.prepare(client_id=CLIENT, post_logout_redirect_uri=LOGOUT_REDIRECT, sso_cookie=sso)
    result = await confirm(logout, challenge, sso, allow=False)
    assert not result.logged_out and result.redirect_uri is None and result.state is None
    async with logout.sessions() as session:
        request = await session.get(NativeLogoutRequestTable, challenge.request_ref)
        login = await session.get(NativeBrowserSessionTable, digest_secret(sso))
        assert request is not None and request.consumed_at is not None
        assert login is not None and login.revoked_at is None
    with pytest.raises(OAuthError, match="invalid_request"):
        await confirm(logout, challenge, sso)


async def test_expired_id_token_is_only_a_hint_and_requires_confirmation(logout: NativeLogout) -> None:
    sso, hint = await browser(logout)
    claims = logout.oauth.keyring.verify_id_token_hint(hint, logout.config.issuer)
    async with logout.sessions.begin() as session:
        login = await session.get(NativeSessionTable, "first-rp")
        assert login is not None
        login.auth_time -= timedelta(minutes=10)
        claims["auth_time"] = int(login.auth_time.timestamp())
    claims["iat"] -= 600
    claims["exp"] -= 600
    expired = logout.oauth.keyring.sign(claims, typ="JWT")
    challenge = await logout.prepare(id_token_hint=expired, sso_cookie=sso)
    assert (await confirm(logout, challenge, sso)).logged_out


@pytest.mark.parametrize(
    "change",
    [
        {"client_id": "different-client"},
        {"id_token_hint": "untrusted-token"},
        {"post_logout_redirect_uri": "https://attacker.example/"},
        {"post_logout_redirect_uri": LOGOUT_REDIRECT + "&extra=1"},
        {"state": "bad\x00state"},
        {"client_id": "unknown"},
    ],
)
async def test_invalid_hint_or_redirect_is_rejected_without_creating_confirmation(
    logout: NativeLogout, change: dict[str, Any]
) -> None:
    sso, hint = await browser(logout)
    with pytest.raises(OAuthError, match="invalid_request"):
        await logout.prepare(
            **{"id_token_hint": hint, "sso_cookie": sso, "post_logout_redirect_uri": LOGOUT_REDIRECT, **change}
        )
    async with logout.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeLogoutRequestTable)) == 0


async def test_cookie_and_hint_from_different_accounts_are_rejected(logout: NativeLogout) -> None:
    current, _ = await browser(logout)
    async with logout.sessions.begin() as session:
        session.add(PrincipalTable(principal_ref="person:other", principal_type="human"))
        await session.flush()
        session.add(
            NativeAccountTable(
                account_ref="account:other",
                principal_ref="person:other",
                username="other",
                password_hash="not-used",
                totp_secret="placeholder",
            )
        )
    _, other_hint = await browser(logout, account_ref="account:other", prefix="other")
    with pytest.raises(OAuthError, match="invalid_request"):
        await logout.prepare(id_token_hint=other_hint, sso_cookie=current)


async def test_same_account_hint_from_another_browser_does_not_control_target_browser(logout: NativeLogout) -> None:
    first, first_hint = await browser(logout)
    second, _ = await browser(logout, prefix="second")
    challenge = await logout.prepare(id_token_hint=first_hint, sso_cookie=second)
    assert (await confirm(logout, challenge, second)).logged_out
    async with logout.sessions() as session:
        untouched = await session.get(NativeBrowserSessionTable, digest_secret(first))
        closed = await session.get(NativeBrowserSessionTable, digest_secret(second))
        assert untouched is not None and untouched.revoked_at is None
        assert closed is not None and closed.revoked_at is not None


@pytest.mark.parametrize("change", ["browser", "csrf", "missing_sso", "changed_sso", "expired"])
async def test_confirmation_requires_both_browser_proofs_and_current_challenge(
    logout: NativeLogout, change: str
) -> None:
    sso, _ = await browser(logout)
    other, _ = await browser(logout, prefix="other")
    challenge = await logout.prepare(client_id=CLIENT, sso_cookie=sso)
    changes: dict[str, Any] = {}
    if change == "expired":
        async with logout.sessions.begin() as session:
            request = await session.get(NativeLogoutRequestTable, challenge.request_ref)
            assert request is not None
            request.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        choices: dict[str, dict[str, Any]] = {
            "browser": {"browser_secret": "wrong"},
            "csrf": {"csrf_token": "wrong"},
            "missing_sso": {"sso_cookie": None},
            "changed_sso": {"sso_cookie": other},
        }
        changes = choices[change]
    with pytest.raises(OAuthError, match="invalid_request"):
        await confirm(logout, challenge, sso, **changes)
    async with logout.sessions() as session:
        request = await session.get(NativeLogoutRequestTable, challenge.request_ref)
        browser_row = await session.get(NativeBrowserSessionTable, digest_secret(sso))
        assert request is not None and request.consumed_at is None
        assert browser_row is not None and browser_row.revoked_at is None


async def test_no_cookie_challenge_cannot_clear_a_browser_that_signed_in_later(logout: NativeLogout) -> None:
    challenge = await logout.prepare(client_id=CLIENT)
    sso, _ = await browser(logout)
    with pytest.raises(OAuthError, match="invalid_request"):
        await confirm(logout, challenge, sso)


@pytest.mark.parametrize("change", ["disabled", "redirect_removed"])
async def test_confirm_revalidates_current_logout_registration(logout: NativeLogout, change: str) -> None:
    sso, _ = await browser(logout)
    challenge = await logout.prepare(client_id=CLIENT, post_logout_redirect_uri=LOGOUT_REDIRECT, sso_cookie=sso)
    async with logout.sessions.begin() as session:
        client = await session.get(NativeClientTable, CLIENT)
        assert client is not None
        if change == "disabled":
            client.enabled = False
        else:
            client.post_logout_redirect_uris = []
    with pytest.raises(OAuthError, match="invalid_request"):
        await confirm(logout, challenge, sso)
    async with logout.sessions() as session:
        browser_row = await session.get(NativeBrowserSessionTable, digest_secret(sso))
        assert browser_row is not None and browser_row.revoked_at is None


async def test_disabled_principal_can_still_revoke_the_browser_it_possesses(logout: NativeLogout) -> None:
    sso, _ = await browser(logout)
    challenge = await logout.prepare(sso_cookie=sso)
    async with logout.sessions.begin() as session:
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == "person:native"))
        assert principal is not None
        principal.disabled_at = datetime.now(UTC)
    assert (await confirm(logout, challenge, sso)).logged_out


async def test_competing_confirmations_consume_once(logout: NativeLogout) -> None:
    sso, _ = await browser(logout)
    challenge = await logout.prepare(sso_cookie=sso)
    results = await asyncio.gather(
        confirm(logout, challenge, sso), confirm(logout, challenge, sso), return_exceptions=True
    )
    assert sum(isinstance(value, OAuthError) for value in results) == 1
    assert sum(not isinstance(value, BaseException) and value.logged_out for value in results) == 1
    async with logout.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(NativeAuthEventTable)
                .where(NativeAuthEventTable.event == "browser.logout_confirmed")
            )
            == 1
        )


async def test_expiry_during_flush_rolls_back_all_revocation_and_consumption(
    logout: NativeLogout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.native_auth.logout as module

    sso, _ = await browser(logout)
    challenge = await logout.prepare(sso_cookie=sso)
    advanced = False

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Self:
            instant = datetime.now(tz) + (timedelta(seconds=1000) if advanced else timedelta())
            return cls.fromtimestamp(instant.timestamp(), tz=tz)

    class DelayedFlush(AsyncSession):
        async def flush(self, objects: Any = None) -> None:
            nonlocal advanced
            await super().flush(objects)
            advanced = True

    monkeypatch.setattr(module, "datetime", Clock)
    delayed_sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
        logout.sessions.kw["bind"], class_=DelayedFlush, expire_on_commit=False
    )
    delayed = NativeLogout(delayed_sessions, logout.config, credentials=logout.credentials, oauth=logout.oauth)
    with pytest.raises(OAuthError, match="invalid_request"):
        await confirm(delayed, challenge, sso)
    async with logout.sessions() as session:
        request = await session.get(NativeLogoutRequestTable, challenge.request_ref)
        browser_row = await session.get(NativeBrowserSessionTable, digest_secret(sso))
        login = await session.get(NativeSessionTable, "first-rp")
        assert request is not None and request.consumed_at is None
        assert browser_row is not None and browser_row.revoked_at is None
        assert login is not None and login.revoked_at is None


async def test_audit_failure_rolls_back_logout(logout: NativeLogout) -> None:
    sso, _ = await browser(logout)
    challenge = await logout.prepare(sso_cookie=sso)
    engine = logout.sessions.kw["bind"]

    def fail_audit(*args: Any) -> None:
        if "INSERT INTO native_auth_event" in args[2]:
            raise RuntimeError("audit-unavailable")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="audit-unavailable"):
            await confirm(logout, challenge, sso)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_audit)
    async with logout.sessions() as session:
        browser_row = await session.get(NativeBrowserSessionTable, digest_secret(sso))
        assert browser_row is not None and browser_row.revoked_at is None


async def test_commit_response_loss_is_unknown_and_never_replays_logout(logout: NativeLogout) -> None:
    sso, _ = await browser(logout)
    challenge = await logout.prepare(sso_cookie=sso)

    class LostCommit(AsyncSession):
        async def commit(self) -> None:
            await super().commit()
            raise SQLAlchemyError("response lost after commit")

    uncertain_sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
        logout.sessions.kw["bind"], class_=LostCommit, expire_on_commit=False
    )
    uncertain = NativeLogout(uncertain_sessions, logout.config, credentials=logout.credentials, oauth=logout.oauth)
    with pytest.raises(NativeCommitUnknown):
        await confirm(uncertain, challenge, sso)
    with pytest.raises(OAuthError, match="invalid_request"):
        await confirm(logout, challenge, sso)
    async with logout.sessions() as session:
        browser_row = await session.get(NativeBrowserSessionTable, digest_secret(sso))
        assert browser_row is not None and browser_row.revoked_at is not None


@pytest.mark.parametrize("parameter", ["state", "iss", "error", "error_description", "error_uri", "%73tate"])
async def test_even_legacy_registered_logout_callback_cannot_override_protocol_fields(
    logout: NativeLogout,
    parameter: str,
) -> None:
    unsafe = "https://application.example/signed-out?" + parameter + "=injected"
    async with logout.sessions.begin() as session:
        client = await session.get(NativeClientTable, CLIENT)
        assert client is not None
        client.post_logout_redirect_uris = [unsafe]
    with pytest.raises(OAuthError, match="invalid_request"):
        await logout.prepare(client_id=CLIENT, post_logout_redirect_uri=unsafe, state="correct-state")
