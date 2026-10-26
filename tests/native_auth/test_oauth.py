"""Protocol and persistence checks for the narrow native issuer."""

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Self

import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import PrincipalTable
from app.models.native_auth import (
    NativeAccountTable,
    NativeAuthEventTable,
    NativeAuthorizationCodeTable,
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeConsentTable,
    NativeLoginRequestTable,
    NativeRefreshTokenTable,
    NativeSessionTable,
)
from app.native_auth.crypto import digest_secret
from app.native_auth.errors import NativeCommitUnknown
from app.native_auth.keys import NativeKeyError
from app.native_auth.oauth import AuthorizationRequest, NativeOAuth, OAuthError, validate_native_session
from tests.support.native_oauth import (
    CLIENT,
    REDIRECT,
    VERIFIER,
    authenticated_request,
    authorization_code,
    authorize_values,
    configure_protocol_client,
    scoped_code,
    tokens,
)
from tests.support.native_oauth import (
    native_keyring as native_keyring,
)
from tests.support.native_oauth import (
    oauth as oauth,
)


async def test_pkce_flow_issues_distinct_audiences_and_only_persists_hashes(oauth: NativeOAuth) -> None:
    code = await authorization_code(oauth)
    response = await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    access = oauth.keyring.verify_access(response["access_token"], oauth.config.issuer, "stage-api")
    jwk = jwt.PyJWK.from_dict(oauth.keyring.jwks()["keys"][0])
    identity = jwt.decode(
        response["id_token"], jwk.key, algorithms=["RS256"], issuer=oauth.config.issuer, audience=CLIENT
    )
    assert access["sid"] == identity["sid"]
    assert access["auth_time"] == identity["auth_time"]
    assert access["amr"] == ["pwd", "otp"]
    assert access["acr"] == "urn:orchestra:acr:pwd-totp"
    assert identity["nonce"] == "caller-nonce"
    assert (
        identity["at_hash"]
        == base64.urlsafe_b64encode(hashlib.sha256(response["access_token"].encode()).digest()[:16])
        .rstrip(b"=")
        .decode()
    )
    assert await oauth.userinfo(response["access_token"]) == {"sub": "account:native"}
    assert response["scope"] == "openid" and 0 < response["expires_in"] <= 300
    with pytest.raises(NativeKeyError):
        oauth.keyring.verify_access(response["id_token"], oauth.config.issuer, "stage-api")
    async with oauth.sessions() as session:
        stored_code = await session.get(NativeAuthorizationCodeTable, digest_secret(code))
        refresh = await session.get(NativeRefreshTokenTable, digest_secret(response["refresh_token"]))
        events = (await session.scalars(select(NativeAuthEventTable))).all()
        assert stored_code is not None and stored_code.consumed_at is not None
        assert refresh is not None and refresh.consumed_at is None
        assert all(
            secret not in json.dumps([row.detail for row in events])
            for secret in response.values()
            if isinstance(secret, str) and len(secret) > 20
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("response_type", "token", "unsupported_response_type"),
        ("scope", "openid profile", "invalid_scope"),
        ("scope", "openid offline_access", "invalid_scope"),
        ("client_id", "unregistered", "invalid_client"),
        ("redirect_uri", "http://attacker.example/callback", "invalid_request"),
        ("redirect_uri", REDIRECT + "/", "invalid_request"),
        ("redirect_uri", REDIRECT + "#fragment", "invalid_request"),
        ("code_challenge_method", "plain", "invalid_request"),
        ("code_challenge", "x" * 42, "invalid_request"),
        ("code_challenge", "x" * 42 + "+", "invalid_request"),
        ("state", "", "invalid_request"),
        ("state", "x\x00y", "invalid_request"),
        ("nonce", "", "invalid_request"),
        ("nonce", "x" * 1025, "invalid_request"),
    ],
)
async def test_rejects_unregistered_or_unsupported_authorization(
    oauth: NativeOAuth, field: str, value: str, error: str
) -> None:
    values = {**authorize_values(), field: value}
    with pytest.raises(OAuthError) as denied:
        await oauth.prepare_authorization(**values)
    assert denied.value.error == error
    async with oauth.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeLoginRequestTable)) == 0


@pytest.mark.parametrize("change", ["browser", "csrf", "password_only", "expired", "version", "no_totp", "client"])
async def test_completion_requires_current_factors_and_browser_proof(oauth: NativeOAuth, change: str) -> None:
    request = await authenticated_request(oauth)
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        account = await session.get(NativeAccountTable, "account:native")
        client = await session.get(NativeClientTable, CLIENT)
        assert row is not None and account is not None and client is not None
        if change == "password_only":
            row.status = "mfa"
        elif change == "expired":
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "version":
            account.credential_version += 1
        elif change == "no_totp":
            account.totp_secret = None
        elif change == "client":
            client.enabled = False
    with pytest.raises(OAuthError):
        await oauth.finish_authorization(
            request_ref=request.request_ref,
            browser_secret="wrong" if change == "browser" else request.browser_secret,
            csrf_token="wrong" if change == "csrf" else request.csrf_token,
        )
    async with oauth.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeSessionTable)) == 0


async def test_browser_authorization_can_only_complete_once(oauth: NativeOAuth) -> None:
    request = await authenticated_request(oauth)
    values = {
        "request_ref": request.request_ref,
        "browser_secret": request.browser_secret,
        "csrf_token": request.csrf_token,
    }
    await oauth.finish_authorization(**values)
    with pytest.raises(OAuthError):
        await oauth.finish_authorization(**values)
    async with oauth.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeSessionTable)) == 1


@pytest.mark.parametrize("change", ["client", "redirect", "verifier", "plain", "code"])
async def test_code_exchange_bound_to_original_client_redirect_and_verifier(oauth: NativeOAuth, change: str) -> None:
    code = await authorization_code(oauth)
    values = {"client_id": CLIENT, "redirect_uri": REDIRECT, "code": code, "code_verifier": VERIFIER}
    values[
        {
            "client": "client_id",
            "redirect": "redirect_uri",
            "verifier": "code_verifier",
            "plain": "code_verifier",
            "code": "code",
        }[change]
    ] = "short" if change == "plain" else "z" * 64
    with pytest.raises(OAuthError) as denied:
        await oauth.exchange_code(**values)
    assert denied.value.error == ("invalid_client" if change == "client" else "invalid_grant")
    # An attacker who lacks PKCE proof cannot burn or revoke the genuine code.
    await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)


async def test_code_replay_revokes_previously_issued_session(oauth: NativeOAuth) -> None:
    code = await authorization_code(oauth)
    first = await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    with pytest.raises(OAuthError, match="invalid_grant"):
        await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    with pytest.raises(OAuthError, match="invalid_token"):
        await oauth.userinfo(first["access_token"])


async def test_refresh_rotation_preserves_original_assurance_and_reuse_revokes_family(oauth: NativeOAuth) -> None:
    first = await tokens(oauth)
    second = await oauth.exchange_refresh(client_id=CLIENT, refresh_token=first["refresh_token"])
    a = oauth.keyring.verify_access(first["access_token"], oauth.config.issuer, "stage-api")
    b = oauth.keyring.verify_access(second["access_token"], oauth.config.issuer, "stage-api")
    assert first["refresh_token"] != second["refresh_token"]
    assert a["jti"] != b["jti"]
    assert (a["sid"], a["auth_time"], a["acr"], a["amr"]) == (b["sid"], b["auth_time"], b["acr"], b["amr"])
    with pytest.raises(OAuthError, match="invalid_grant"):
        await oauth.exchange_refresh(client_id=CLIENT, refresh_token=first["refresh_token"])
    with pytest.raises(OAuthError, match="invalid_grant"):
        await oauth.exchange_refresh(client_id=CLIENT, refresh_token=second["refresh_token"])
    with pytest.raises(OAuthError, match="invalid_token"):
        await oauth.userinfo(second["access_token"])
    async with oauth.sessions() as session:
        events = (await session.scalars(select(NativeAuthEventTable.event))).all()
        assert "refresh_reused" in events


@pytest.mark.parametrize(
    "change", ["account_disabled", "version", "principal", "disabled_at", "client", "expired", "revoked"]
)
async def test_issued_access_and_refresh_check_current_identity(oauth: NativeOAuth, change: str) -> None:
    issued = await tokens(oauth)
    async with oauth.sessions.begin() as session:
        account = await session.get(NativeAccountTable, "account:native")
        login = await session.scalar(select(NativeSessionTable))
        principal = await session.scalar(select(PrincipalTable))
        client = await session.get(NativeClientTable, CLIENT)
        assert account is not None and login is not None and principal is not None and client is not None
        if change == "account_disabled":
            account.status = "disabled"
        elif change == "version":
            account.credential_version += 1
        elif change == "principal":
            principal.status = "disabled"
        elif change == "disabled_at":
            principal.disabled_at = datetime.now(UTC)
        elif change == "client":
            client.enabled = False
        elif change == "expired":
            login.auth_time = datetime.now(UTC) - timedelta(hours=2)
            login.expires_at = datetime.now(UTC) - timedelta(hours=1)
        elif change == "revoked":
            login.revoked_at = datetime.now(UTC)
    with pytest.raises(OAuthError):
        await oauth.userinfo(issued["access_token"])
    with pytest.raises(OAuthError):
        await oauth.exchange_refresh(client_id=CLIENT, refresh_token=issued["refresh_token"])


@pytest.mark.parametrize("token_kind", ["access_token", "refresh_token"])
async def test_revocation_is_idempotent_and_invalidates_entire_session(oauth: NativeOAuth, token_kind: str) -> None:
    issued = await tokens(oauth)
    await oauth.revoke(client_id=CLIENT, token=issued[token_kind], token_type_hint="unrecognized-hint")
    await oauth.revoke(client_id=CLIENT, token=issued[token_kind])
    await oauth.revoke(client_id=CLIENT, token="random-unknown-token")
    with pytest.raises(OAuthError):
        await oauth.userinfo(issued["access_token"])
    with pytest.raises(OAuthError):
        await oauth.exchange_refresh(client_id=CLIENT, refresh_token=issued["refresh_token"])


async def test_other_client_cannot_revoke_or_refresh_a_session(oauth: NativeOAuth) -> None:
    async with oauth.sessions.begin() as session:
        session.add(NativeClientTable(client_id="other-client", redirect_uris=[REDIRECT], enabled=True))
    issued = await tokens(oauth)
    for token_kind in ("access_token", "refresh_token"):
        await oauth.revoke(client_id="other-client", token=issued[token_kind])
    with pytest.raises(OAuthError):
        await oauth.exchange_refresh(client_id="other-client", refresh_token=issued["refresh_token"])
    assert await oauth.userinfo(issued["access_token"]) == {"sub": "account:native"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("credential_version", True),
        ("auth_time", 1),
        ("amr", ["pwd"]),
        ("scope", "admin"),
        ("sid", "another-session"),
        ("sub", "another-person"),
    ],
)
async def test_valid_signature_cannot_replace_persisted_native_facts(
    oauth: NativeOAuth, field: str, value: Any
) -> None:
    issued = await tokens(oauth)
    claims = oauth.keyring.verify_access(issued["access_token"], oauth.config.issuer, "stage-api")
    claims[field] = value
    async with oauth.sessions() as session:
        with pytest.raises(OAuthError):
            await validate_native_session(session, claims, now=datetime.now(UTC))


async def test_sensitive_account_access_uses_original_authentication_time(oauth: NativeOAuth) -> None:
    issued = await tokens(oauth)
    assert (await oauth.authenticate_account(issued["access_token"]))["sub"] == "account:native"
    with pytest.raises(OAuthError, match="login_required"):
        await oauth.authenticate_account(issued["access_token"], max_age_seconds=1)


async def test_native_session_expiry_is_checked_again_after_database_reads(
    oauth: NativeOAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.native_auth import current_session as module

    issued = await tokens(oauth)
    claims = oauth.keyring.verify_access(issued["access_token"], oauth.config.issuer, "stage-api")
    before_reads = datetime.now(UTC)

    class AfterReads(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp((datetime.now(tz) + timedelta(minutes=10)).timestamp(), tz)

    monkeypatch.setattr(module, "datetime", AfterReads)
    async with oauth.sessions() as session:
        with pytest.raises(OAuthError, match="invalid_token"):
            await validate_native_session(session, claims, now=before_reads)


@pytest.mark.parametrize("grant", ["code", "refresh"])
async def test_expired_one_time_credentials_cannot_be_consumed(oauth: NativeOAuth, grant: str) -> None:
    code = await authorization_code(oauth)
    if grant == "code":
        async with oauth.sessions.begin() as session:
            row = await session.get(NativeAuthorizationCodeTable, digest_secret(code))
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(OAuthError, match="invalid_grant"):
            await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    else:
        issued = await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
        async with oauth.sessions.begin() as session:
            refresh = await session.get(NativeRefreshTokenTable, digest_secret(issued["refresh_token"]))
            assert refresh is not None
            refresh.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(OAuthError, match="invalid_grant"):
            await oauth.exchange_refresh(client_id=CLIENT, refresh_token=issued["refresh_token"])


async def test_lost_commit_acknowledgement_never_returns_unconfirmed_tokens(
    oauth: NativeOAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = await authorization_code(oauth)
    commit = AsyncSession.commit

    async def committed_but_lost_reply(session: AsyncSession) -> None:
        await commit(session)
        raise ConnectionError("synthetic lost COMMIT acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", committed_but_lost_reply)
        with pytest.raises(NativeCommitUnknown):
            await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    async with oauth.sessions() as session:
        row = await session.get(NativeAuthorizationCodeTable, digest_secret(code))
        assert row is not None and row.consumed_at is not None
        assert await session.scalar(select(func.count()).select_from(NativeRefreshTokenTable)) == 1
    with pytest.raises(OAuthError, match="invalid_grant"):
        await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)


@pytest.mark.parametrize("grant", ["code", "refresh"])
async def test_concurrent_single_use_consumption_revokes_reused_family(oauth: NativeOAuth, grant: str) -> None:
    async with oauth.sessions() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires actual PostgreSQL row locking")
    if grant == "code":
        code = await authorization_code(oauth)

        async def exchange() -> dict[str, Any]:
            return await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    else:
        initial = await tokens(oauth)

        async def exchange() -> dict[str, Any]:
            return await oauth.exchange_refresh(client_id=CLIENT, refresh_token=initial["refresh_token"])

    outcomes = await asyncio.gather(exchange(), exchange(), return_exceptions=True)
    successful = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, OAuthError)]
    assert len(successful) == len(rejected) == 1
    assert rejected[0].error == "invalid_grant"
    with pytest.raises(OAuthError):
        await oauth.userinfo(successful[0]["access_token"])


async def test_code_flow_accepts_optional_nonce_and_state_and_returns_issuer(oauth: NativeOAuth) -> None:
    values = authorize_values()
    values.pop("nonce")
    values.pop("state")
    request = await oauth.prepare_authorization(**values)
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        assert row is not None
        row.account_ref = "account:native"
        row.credential_version = 1
        row.password_verified_at = datetime.now(UTC)
        row.status = "authenticated"
    response = await oauth.finish_authorization_response(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    assert response.parameters["iss"] == oauth.config.issuer
    assert "state" not in response.parameters
    issued = await oauth.exchange_code(
        client_id=CLIENT, redirect_uri=REDIRECT, code=response.parameters["code"], code_verifier=VERIFIER
    )
    identity = oauth.keyring.verify_id_token_hint(issued["id_token"], oauth.config.issuer, CLIENT)
    assert "nonce" not in identity


@pytest.mark.parametrize("response_mode", ["query", "form_post"])
async def test_authorization_error_only_targets_registered_callback(oauth: NativeOAuth, response_mode: str) -> None:
    with pytest.raises(OAuthError) as result:
        await oauth.prepare_authorization(
            **{**authorize_values(), "response_type": "token"}, response_mode=response_mode
        )
    assert result.value.response is not None
    assert result.value.response.redirect_uri == REDIRECT
    assert result.value.response.response_mode == response_mode
    assert result.value.response.parameters == {
        "error": "unsupported_response_type",
        "state": "caller-state",
        "iss": oauth.config.issuer,
    }
    with pytest.raises(OAuthError) as unsafe:
        await oauth.prepare_authorization(
            **{**authorize_values(), "redirect_uri": "https://evil.test/cb", "response_type": "token"}
        )
    assert unsafe.value.response is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", "none login"),
        ("prompt", "login login"),
        ("prompt", "unknown"),
        ("max_age", "-1"),
        ("max_age", "1.1"),
        ("max_age", True),
        ("max_age", 2147483648),
        ("response_mode", "fragment"),
        ("login_hint", "\x00"),
    ],
)
async def test_authorization_rejects_invalid_oidc_parameters(oauth: NativeOAuth, field: str, value: Any) -> None:
    with pytest.raises(OAuthError, match="invalid_request") as error:
        await oauth.prepare_authorization(**authorize_values(), **{field: value})
    assert error.value.response is not None


async def test_authorization_acr_cannot_claim_an_unsupported_method(oauth: NativeOAuth) -> None:
    with pytest.raises(OAuthError, match="unmet_authentication_requirements"):
        await oauth.prepare_authorization(**authorize_values(), acr_values="urn:unsupported:passkey")


async def test_form_post_response_preserves_callback_and_does_not_preencode_parameters(oauth: NativeOAuth) -> None:
    request = await oauth.prepare_authorization(**authorize_values(), response_mode="form_post")
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        assert row is not None
        row.account_ref, row.credential_version = "account:native", 1
        row.password_verified_at, row.status = datetime.now(UTC), "authenticated"
    response = await oauth.finish_authorization_response(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    assert response.response_mode == "form_post"
    assert response.redirect_uri == REDIRECT
    assert response.parameters["state"] == "caller-state"
    assert response.session_ref


async def test_profile_scope_exposes_only_account_name_and_stays_bound_to_grant(oauth: NativeOAuth) -> None:
    await configure_protocol_client(oauth, allowed_scopes=["openid", "profile"])
    issued = await oauth.exchange_code(
        client_id=CLIENT, redirect_uri=REDIRECT, code=await scoped_code(oauth, "profile openid"), code_verifier=VERIFIER
    )
    assert issued["scope"] == "openid profile"
    assert await oauth.userinfo(issued["access_token"]) == {
        "sub": "account:native",
        "preferred_username": "native-test",
    }
    claims = oauth.keyring.verify_access(issued["access_token"], oauth.config.issuer, "stage-api")
    claims["scope"] = "openid offline_access"
    async with oauth.sessions() as session:
        with pytest.raises(OAuthError, match="invalid_token"):
            await validate_native_session(session, claims, now=datetime.now(UTC))
    refreshed = await oauth.exchange_refresh(
        client_id=CLIENT, refresh_token=issued["refresh_token"], scope="profile openid"
    )
    assert refreshed["scope"] == "openid profile"
    with pytest.raises(OAuthError, match="invalid_scope"):
        await oauth.exchange_refresh(
            client_id=CLIENT, refresh_token=refreshed["refresh_token"], scope="openid offline_access"
        )
    # Invalid requested scope does not consume the existing refresh token.
    await oauth.exchange_refresh(client_id=CLIENT, refresh_token=refreshed["refresh_token"])


async def test_userinfo_audience_cannot_reach_account_security(oauth: NativeOAuth) -> None:
    await configure_protocol_client(oauth, resource_audience="stage-userinfo")
    issued = await tokens(oauth)
    assert await oauth.userinfo(issued["access_token"]) == {"sub": "account:native"}
    with pytest.raises(OAuthError, match="invalid_token"):
        await oauth.authenticate_account(issued["access_token"])
    with pytest.raises(NativeKeyError):
        oauth.keyring.verify_access(issued["access_token"], oauth.config.issuer, "stage-api")
    await oauth.revoke(client_id=CLIENT, token=issued["access_token"])
    with pytest.raises(OAuthError):
        await oauth.userinfo(issued["access_token"])


@pytest.mark.parametrize("grant", ["code", "refresh", "revoke"])
async def test_confidential_authentication_precedes_single_use_token_mutation(oauth: NativeOAuth, grant: str) -> None:
    secret = "confidential-client-secret-with-high-entropy"
    await configure_protocol_client(
        oauth, token_endpoint_auth_method="client_secret_basic", client_secret_hash=digest_secret(secret)
    )
    code = await authorization_code(oauth)
    auth = {"client_id": CLIENT, "client_secret": secret, "client_auth_method": "client_secret_basic"}
    if grant == "code":
        with pytest.raises(OAuthError, match="invalid_client"):
            await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    issued = await oauth.exchange_code(**auth, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    if grant == "refresh":
        with pytest.raises(OAuthError, match="invalid_client"):
            await oauth.exchange_refresh(
                client_id=CLIENT,
                client_secret="wrong",
                client_auth_method="client_secret_basic",
                refresh_token=issued["refresh_token"],
            )
        issued = await oauth.exchange_refresh(**auth, refresh_token=issued["refresh_token"])
    elif grant == "revoke":
        with pytest.raises(OAuthError, match="invalid_client"):
            await oauth.revoke(client_id=CLIENT, token=issued["refresh_token"])
    assert await oauth.userinfo(issued["access_token"])
    await oauth.revoke(**auth, token=issued["refresh_token"])
    with pytest.raises(OAuthError):
        await oauth.userinfo(issued["access_token"])


async def test_public_client_cannot_claim_basic_authentication(oauth: NativeOAuth) -> None:
    code = await authorization_code(oauth)
    with pytest.raises(OAuthError, match="invalid_client"):
        await oauth.exchange_code(
            client_id=CLIENT,
            redirect_uri=REDIRECT,
            code=code,
            code_verifier=VERIFIER,
            client_secret="anything",
            client_auth_method="client_secret_basic",
        )
    await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)


@pytest.mark.parametrize("action", ["finish", "code", "access", "refresh"])
async def test_changed_client_version_invalidates_pending_and_issued_authority(oauth: NativeOAuth, action: str) -> None:
    request = await authenticated_request(oauth)
    code = None
    issued = None
    if action != "finish":
        result = await oauth.finish_authorization_response(
            request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
        )
        code = result.parameters["code"]
    if action in {"access", "refresh"}:
        assert code is not None
        issued = await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    await configure_protocol_client(oauth, version=2)
    with pytest.raises(OAuthError):
        if action == "finish":
            await oauth.finish_authorization_response(
                request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
            )
        elif action == "code":
            assert code is not None
            await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
        elif action == "access":
            assert issued is not None
            await oauth.userinfo(issued["access_token"])
        else:
            assert issued is not None
            await oauth.exchange_refresh(client_id=CLIENT, refresh_token=issued["refresh_token"])


async def test_offline_access_and_explicit_consent_cannot_bypass_consent(oauth: NativeOAuth) -> None:
    await configure_protocol_client(oauth, allowed_scopes=["openid", "offline_access"])
    with pytest.raises(OAuthError, match="consent_required"):
        await scoped_code(oauth, "openid offline_access")
    code = await scoped_code(oauth, "openid offline_access", consent=True)
    issued = await oauth.exchange_code(client_id=CLIENT, redirect_uri=REDIRECT, code=code, code_verifier=VERIFIER)
    assert issued["scope"] == "openid offline_access"


async def test_id_token_hint_is_distinct_from_access_and_checks_client(oauth: NativeOAuth) -> None:
    issued = await tokens(oauth)
    claims = oauth.keyring.verify_id_token_hint(issued["id_token"], oauth.config.issuer, CLIENT)
    assert claims["sub"] == "account:native"
    assert oauth.keyring.verify_id_token_hint(issued["id_token"], oauth.config.issuer)["aud"] == CLIENT
    for token, client in [(issued["access_token"], CLIENT), (issued["id_token"], "other")]:
        with pytest.raises(NativeKeyError):
            oauth.keyring.verify_id_token_hint(token, oauth.config.issuer, client)
    old = datetime.now(UTC) - timedelta(hours=1)
    claims.update(auth_time=int(old.timestamp()) - 2, iat=int(old.timestamp()), exp=int(old.timestamp()) + 300)
    expired_hint = oauth.keyring.sign(claims, typ="JWT")
    assert oauth.keyring.verify_id_token_hint(expired_hint, oauth.config.issuer, CLIENT)["sid"] == claims["sid"]


async def reused_request(oauth: NativeOAuth, **parameters: Any) -> tuple[AuthorizationRequest, str]:
    request = await oauth.prepare_authorization(**authorize_values(), **parameters)
    browser_hash = digest_secret("a-browser-session-secret")
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        assert row is not None
        auth_time = datetime.now(UTC) - timedelta(hours=1)
        session.add(
            NativeBrowserSessionTable(
                session_hash=browser_hash,
                account_ref="account:native",
                credential_version=1,
                auth_time=auth_time,
                expires_at=datetime.now(UTC) + timedelta(minutes=20),
            )
        )
        await session.flush()
        row.account_ref, row.credential_version = "account:native", 1
        row.password_verified_at, row.status = auth_time, "authenticated"
        row.browser_session_hash = browser_hash
        row.authentication_reused = True
    return request, browser_hash


async def test_reused_authentication_preserves_original_assurance_and_browser_expiry(oauth: NativeOAuth) -> None:
    request, browser_hash = await reused_request(oauth)
    result = await oauth.finish_authorization_response(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    issued = await oauth.exchange_code(
        client_id=CLIENT, redirect_uri=REDIRECT, code=result.parameters["code"], code_verifier=VERIFIER
    )
    claims = oauth.keyring.verify_access(issued["access_token"], oauth.config.issuer, "stage-api")
    async with oauth.sessions() as session:
        browser = await session.get(NativeBrowserSessionTable, browser_hash)
        login = await session.get(NativeSessionTable, result.session_ref)
        assert browser is not None and login is not None
        assert login.expires_at == browser.expires_at
        assert login.auth_time == browser.auth_time
        assert claims["auth_time"] == int(browser.auth_time.timestamp())
    # A linked browser logout immediately invalidates both resource access and refresh.
    async with oauth.sessions.begin() as session:
        browser = await session.get(NativeBrowserSessionTable, browser_hash)
        assert browser is not None
        browser.revoked_at = datetime.now(UTC)
    with pytest.raises(OAuthError):
        await oauth.userinfo(issued["access_token"])
    with pytest.raises(OAuthError):
        await oauth.exchange_refresh(client_id=CLIENT, refresh_token=issued["refresh_token"])


@pytest.mark.parametrize(
    "parameters", [{"max_age": 0}, {"max_age": 60}, {"prompt": "login"}, {"prompt": "select_account"}]
)
async def test_protocol_cannot_bypass_required_reauthentication_using_reused_state(
    oauth: NativeOAuth, parameters: dict[str, Any]
) -> None:
    request, _ = await reused_request(oauth, **parameters)
    with pytest.raises(OAuthError, match="login_required"):
        await oauth.finish_authorization_response(
            request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
        )


async def test_fresh_mfa_with_new_browser_session_satisfies_max_age_zero(oauth: NativeOAuth) -> None:
    request, browser_hash = await reused_request(oauth, max_age=0)
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        browser = await session.get(NativeBrowserSessionTable, browser_hash)
        assert row is not None and browser is not None
        row.authentication_reused = False
        row.password_verified_at = datetime.now(UTC)
        browser.auth_time = row.password_verified_at
    await oauth.finish_authorization_response(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )


@pytest.mark.parametrize("change", ["revoked", "expired", "version", "time"])
async def test_final_authorization_checks_browser_session_facts(oauth: NativeOAuth, change: str) -> None:
    request, browser_hash = await reused_request(oauth)
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeBrowserSessionTable, browser_hash)
        assert row is not None
        if change == "revoked":
            row.revoked_at = datetime.now(UTC)
        elif change == "expired":
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "version":
            row.credential_version += 1
        else:
            row.auth_time -= timedelta(seconds=1)
    with pytest.raises(OAuthError, match="login_required"):
        await oauth.finish_authorization_response(
            request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
        )


@pytest.mark.parametrize(
    "version,scopes,accepted",
    [(1, ["openid", "profile"], True), (1, ["openid"], False), (2, ["openid", "profile"], False)],
)
async def test_consent_is_bound_to_current_client_version_and_requested_scope(
    oauth: NativeOAuth, version: int, scopes: list[str], accepted: bool
) -> None:
    await configure_protocol_client(oauth, require_consent=True, allowed_scopes=["openid", "profile"])
    async with oauth.sessions.begin() as session:
        session.add(
            NativeConsentTable(
                account_ref="account:native",
                client_id=CLIENT,
                client_version=version,
                scopes=scopes,
                granted_at=datetime.now(UTC),
            )
        )
    if accepted:
        await scoped_code(oauth, "openid profile")
    else:
        with pytest.raises(OAuthError, match="consent_required"):
            await scoped_code(oauth, "openid profile")


async def test_refresh_scope_only_narrows_each_generation_and_reuse_revokes_the_entire_grant(
    oauth: NativeOAuth,
) -> None:
    await configure_protocol_client(oauth, allowed_scopes=["openid", "profile", "offline_access"])
    first = await oauth.exchange_code(
        client_id=CLIENT,
        redirect_uri=REDIRECT,
        code=await scoped_code(oauth, "openid profile offline_access", consent=True),
        code_verifier=VERIFIER,
    )
    second = await oauth.exchange_refresh(
        client_id=CLIENT, refresh_token=first["refresh_token"], scope="openid profile"
    )
    third = await oauth.exchange_refresh(client_id=CLIENT, refresh_token=second["refresh_token"], scope="openid")
    assert first["scope"] == "openid profile offline_access"
    assert second["scope"] == "openid profile" and third["scope"] == "openid"
    assert (await oauth.userinfo(first["access_token"]))["preferred_username"] == "native-test"
    assert await oauth.userinfo(third["access_token"]) == {"sub": "account:native"}
    with pytest.raises(OAuthError, match="invalid_scope"):
        await oauth.exchange_refresh(client_id=CLIENT, refresh_token=third["refresh_token"], scope="openid profile")
    fourth = await oauth.exchange_refresh(client_id=CLIENT, refresh_token=third["refresh_token"])
    assert fourth["scope"] == "openid"
    with pytest.raises(OAuthError, match="invalid_grant"):
        await oauth.exchange_refresh(client_id=CLIENT, refresh_token=third["refresh_token"])
    for issued in (first, second, third, fourth):
        with pytest.raises(OAuthError):
            await oauth.userinfo(issued["access_token"])


async def test_legacy_refresh_without_stored_scope_uses_original_grant(oauth: NativeOAuth) -> None:
    issued = await tokens(oauth)
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeRefreshTokenTable, digest_secret(issued["refresh_token"]))
        assert row is not None
        row.scope = None
    refreshed = await oauth.exchange_refresh(client_id=CLIENT, refresh_token=issued["refresh_token"])
    assert refreshed["scope"] == "openid"
