"""Compose native HTTP adapters while retaining their established application imports."""

from fastapi import APIRouter

from app.api.native.account import account_router as account_router
from app.api.native.browser import router as browser_router
from app.api.native.common import NativeAuthSecurityMiddleware as NativeAuthSecurityMiddleware
from app.api.native.common import NativeHTTPError as NativeHTTPError
from app.api.native.common import authorization_response as _authorization_response
from app.api.native.common import require_same_origin as _origin
from app.api.native.logout import router as logout_router
from app.api.native.oauth import client_authentication as _client_authentication
from app.api.native.oauth import router as oauth_router

# Helpers remain available for older internal integration tests; new adapters
# import their named boundary from app.api.native.common or app.api.native.oauth.
__all__ = [
    "NativeAuthSecurityMiddleware",
    "NativeHTTPError",
    "account_router",
    "router",
    "_authorization_response",
    "_client_authentication",
    "_origin",
]

router = APIRouter()
router.include_router(oauth_router)
router.include_router(browser_router)
router.include_router(logout_router)
