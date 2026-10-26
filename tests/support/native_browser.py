"""Shared synthetic builders for native browser."""

from app.native_auth.browser import BrowserAuthorization
from app.native_auth.oauth import NativeOAuth
from tests.support import native_oauth as oauth_support


def browser_service(oauth: NativeOAuth) -> BrowserAuthorization:
    return BrowserAuthorization(oauth.sessions, oauth.config, credentials=oauth.credentials, oauth=oauth)


async def established(oauth: NativeOAuth) -> tuple[BrowserAuthorization, str]:
    browser = browser_service(oauth)
    request = await oauth_support.authenticated_request(oauth)
    outcome = await browser.advance(request.request_ref, request.browser_secret, request.csrf_token)
    assert outcome.kind == "complete" and outcome.cookie_secret
    return browser, outcome.cookie_secret
