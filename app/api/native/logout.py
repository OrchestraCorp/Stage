"""RP-initiated logout relay and browser-bound confirmation."""

import secrets
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.api.native.common import (
    NativeHTTPError,
    callback_origin,
    clear_browser_cookie,
    cookie_secret,
    hidden_input,
    login_cookie_name,
    logout_cookie_name,
    make_oauth_router,
    native_services,
    parse_pairs,
    read_form,
    require_fields,
    require_same_origin,
    set_browser_cookie,
    sso_cookie_name,
)

router = make_oauth_router()


_LOGOUT_FIELDS = {"id_token_hint", "client_id", "post_logout_redirect_uri", "state"}


@router.get(
    "/oauth/logout",
    operation_id="native_logout_confirmation",
    response_class=HTMLResponse,
    openapi_extra={
        "parameters": [
            {"name": key, "in": "query", "required": False, "schema": {"type": "string"}}
            for key in sorted(_LOGOUT_FIELDS)
        ]
    },
)
async def logout_confirmation(request: Request) -> Response:
    values = parse_pairs(request.scope.get("query_string", b""))
    require_fields(values, set(), _LOGOUT_FIELDS)
    return await _begin_logout(request, values)


async def _begin_logout(request: Request, values: dict[str, str]) -> Response:
    async with native_services(request) as (facade, services):
        issuer = facade.config.issuer
        await services.credentials.throttle_logout(request.client.host if request.client is not None else "unavailable")
        origins = request.headers.getlist("origin")
        if len(origins) > 1:
            raise NativeHTTPError()
        cross_site_post = request.method == "POST" and (
            (origins and origins[0] != issuer) or request.headers.get("sec-fetch-site") == "cross-site"
        )
        if cross_site_post:
            # SameSite=Lax omits the browser SSO cookie on an RP's cross-site
            # POST. Re-enter this endpoint from an issuer document before
            # creating the browser-bound challenge. The relay never revokes.
            return _logout_relay(issuer, values)
        challenge = await services.logout.prepare(
            **values, sso_cookie=cookie_secret(request, sso_cookie_name(issuer), required=False)
        )
        client_name = (
            f"应用 {escape(challenge.client_name)} 请求退出登录。" if challenge.client_name else "确认退出登录。"
        )
        response = HTMLResponse(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>确认退出登录</title></head><body><main><h1>确认退出登录</h1>"
            f"<p>{client_name}</p><p>确认后，本浏览器的登录状态及由此建立的应用会话将退出。</p>"
            f'<form method="post" action="{escape(issuer + "/oauth/logout", quote=True)}">'
            + hidden_input("request_ref", challenge.request_ref)
            + hidden_input("csrf_token", challenge.csrf_token)
            + '<button type="submit" name="decision" value="allow">退出登录</button>'
            '<button type="submit" name="decision" value="deny">取消</button></form></main></body></html>',
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; form-action 'self'"
                    + (" " + callback_origin(challenge.redirect_uri) if challenge.redirect_uri else "")
                    + "; frame-ancestors 'none'; base-uri 'none'"
                )
            },
        )
        set_browser_cookie(response, logout_cookie_name(issuer), challenge.browser_secret, issuer, 300)
        return response


def _logout_relay(issuer: str, values: dict[str, str]) -> HTMLResponse:
    nonce = secrets.token_urlsafe(24)
    controls = "".join(hidden_input(name, value) for name, value in values.items())
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>继续退出登录</title></head><body><main><h1>继续退出登录</h1>"
        f'<form id="logout-init" method="post" action="{escape(issuer + "/oauth/logout", quote=True)}">'
        f'{controls}<button type="submit">继续</button></form>'
        "<noscript><p>请选择继续，以确认是否退出登录。</p></noscript>"
        f'<script nonce="{nonce}">document.getElementById("logout-init").submit();</script>'
        "</main></body></html>",
        headers={
            "Referrer-Policy": "same-origin",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; form-action 'self'; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )


_LOGOUT_POST_CONTRACT = {
    "requestBody": {
        "required": True,
        "content": {
            "application/x-www-form-urlencoded": {
                "schema": {
                    "oneOf": [
                        {
                            "type": "object",
                            "description": (
                                "Start RP-initiated logout and display confirmation; does not revoke a session."
                            ),
                            "properties": {name: {"type": "string"} for name in sorted(_LOGOUT_FIELDS)},
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "description": "Confirm or cancel the browser-bound logout challenge.",
                            "required": ["request_ref", "csrf_token", "decision"],
                            "properties": {
                                "request_ref": {"type": "string"},
                                "csrf_token": {"type": "string"},
                                "decision": {"type": "string", "enum": ["allow", "deny"]},
                            },
                            "additionalProperties": False,
                        },
                    ]
                }
            }
        },
    }
}


@router.post(
    "/oauth/logout",
    operation_id="native_logout",
    response_class=HTMLResponse,
    description="Accept RP-initiated logout parameters or the subsequent same-origin CSRF-protected confirmation.",
    openapi_extra=_LOGOUT_POST_CONTRACT,
)
async def logout(request: Request) -> Response:
    values = await read_form(request)
    if "request_ref" not in values:
        require_fields(values, set(), _LOGOUT_FIELDS)
        return await _begin_logout(request, values)
    require_fields(values, {"request_ref", "csrf_token", "decision"})
    if values["decision"] not in {"allow", "deny"}:
        raise NativeHTTPError()
    async with native_services(request) as (facade, services):
        issuer = facade.config.issuer
        require_same_origin(request, issuer)
        logout_cookie = cookie_secret(request, logout_cookie_name(issuer), required=True)
        assert logout_cookie is not None
        result = await services.logout.confirm(
            request_ref=values["request_ref"],
            browser_secret=logout_cookie,
            csrf_token=values["csrf_token"],
            sso_cookie=cookie_secret(request, sso_cookie_name(issuer), required=False),
            confirm=values["decision"] == "allow",
        )
        if result.redirect_uri:
            parts = urlsplit(result.redirect_uri)
            query = parse_qsl(parts.query, keep_blank_values=True)
            if result.state is not None:
                query.append(("state", result.state))
            response: Response = RedirectResponse(
                urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")), status_code=303
            )
        else:
            message = "已退出登录" if result.logged_out else "已取消退出登录"
            response = HTMLResponse(
                f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{message}</title></head>'
                f"<body><main><h1>{message}</h1><p>可以关闭此页面。</p></main></body></html>"
            )
        clear_browser_cookie(response, logout_cookie_name(issuer), issuer)
        if result.logged_out:
            clear_browser_cookie(response, sso_cookie_name(issuer), issuer)
            clear_browser_cookie(response, login_cookie_name(issuer), issuer)
        return response
