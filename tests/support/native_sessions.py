"""Shared synthetic builders for native sessions."""

import pytest

from app.native_auth.oauth import NativeOAuth
from app.native_auth.sessions import AccountSessions


@pytest.fixture
async def manager(oauth: NativeOAuth) -> AccountSessions:
    return AccountSessions(oauth.sessions, oauth.config, credentials=oauth.credentials, oauth=oauth)
