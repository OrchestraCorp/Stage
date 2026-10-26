"""Current-session ownership and the single-transaction UserInfo read boundary."""

import ast
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.authentication import AccessTokenProfile, KeycloakProfile
from app.models.native_auth import NativeAccountTable
from app.native_auth.oauth import NativeOAuth
from app.native_auth.protocol import OAuthError
from tests.support.identity_tokens import profile
from tests.support.native_oauth import (
    CLIENT,
    REDIRECT,
    VERIFIER,
    configure_protocol_client,
    scoped_code,
)
from tests.support.native_oauth import native_keyring as native_keyring
from tests.support.native_oauth import oauth as oauth


def test_shared_profile_name_preserves_historical_callers() -> None:
    assert KeycloakProfile is AccessTokenProfile
    assert isinstance(profile(), AccessTokenProfile)


@pytest.mark.parametrize(
    "relative",
    ["identity/native.py", "native_auth/credentials.py", "native_auth/current_session.py"],
)
def test_current_identity_and_credential_facts_do_not_depend_on_the_issuer_service(relative: str) -> None:
    source = Path(__file__).resolve().parents[2] / "app" / relative
    imports = [node for node in ast.walk(ast.parse(source.read_text())) if isinstance(node, ast.ImportFrom)]
    assert all(node.module != "app.native_auth.oauth" for node in imports)


def test_shared_protocol_values_do_not_load_persistence_or_service_implementations() -> None:
    source = Path(__file__).resolve().parents[2] / "app/native_auth/protocol.py"
    imports = [node.module for node in ast.walk(ast.parse(source.read_text())) if isinstance(node, ast.ImportFrom)]
    assert not any(name and name.startswith(("app.", "sqlalchemy", "fastapi")) for name in imports)


@pytest.mark.parametrize("scope", ["openid", "openid profile"])
async def test_userinfo_reads_one_current_authority_snapshot_and_rejects_revoked_tokens(
    oauth: NativeOAuth, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    await configure_protocol_client(oauth, allowed_scopes=["openid", "profile"])
    issued = await oauth.exchange_code(
        client_id=CLIENT,
        redirect_uri=REDIRECT,
        code=await scoped_code(oauth, scope),
        code_verifier=VERIFIER,
    )
    locked_accounts: list[str] = []
    lock_account = oauth.credentials.lock_account

    async def observe_authority_lock(session: AsyncSession, account_ref: str) -> NativeAccountTable:
        locked_accounts.append(account_ref)
        return await lock_account(session, account_ref)

    monkeypatch.setattr(oauth.credentials, "lock_account", observe_authority_lock)
    expected = {"sub": "account:native"}
    if "profile" in scope.split():
        expected["preferred_username"] = "native-test"
    assert await oauth.userinfo(issued["access_token"]) == expected
    assert locked_accounts == ["account:native"]

    await oauth.revoke(client_id=CLIENT, token=issued["access_token"])
    with pytest.raises(OAuthError, match="invalid_token"):
        await oauth.userinfo(issued["access_token"])
