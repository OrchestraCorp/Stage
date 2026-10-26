"""Browser authorization, password/TOTP ceremony and application consent."""

from html import escape
from typing import Any

from fastapi import Request
from starlette.responses import HTMLResponse, Response

from app.api.native.common import (
    NativeHTTPError,
    authorization_response,
    callback_origin,
    clear_browser_cookie,
    cookie_secret,
    form_contract,
    get_browser_secret,
    hidden_input,
    login_cookie_name,
    make_oauth_router,
    native_services,
    parse_pairs,
    read_form,
    require_fields,
    require_same_origin,
    set_browser_cookie,
    sso_cookie_name,
)
from app.native_auth.credentials import LoginProgress
from app.native_auth.errors import NativeCredentialError
from app.native_auth.protocol import AuthorizationResponse
from app.providers.native_auth import NativeAuthFacade, NativeAuthServices

router = make_oauth_router()


def _login_page(
    issuer: str,
    request_ref: str,
    csrf_token: str,
    progress: LoginProgress,
    *,
    invalid: bool = False,
) -> HTMLResponse:
    common = hidden_input("request_ref", request_ref) + hidden_input("csrf_token", csrf_token)
    target = escape(issuer + "/oauth/login", quote=True)
    callback_target = callback_origin(progress.redirect_uri)

    def form(stage: str, content: str, submit: str) -> str:
        return (
            f'<form method="post" action="{target}">'
            + common
            + hidden_input("stage", stage)
            + content
            + f'<button type="submit">{submit}</button></form>'
        )

    warning = '<p role="alert">验证未通过，请核对信息后重试。</p>' if invalid else ""
    if progress.status == "password":
        title = "登录 OrchestraCorp"
        content = form(
            "password",
            '<p><label>用户名 <input name="username" autocomplete="username" maxlength="128" required></label></p>'
            '<p><label>密码 <input type="password" name="password" autocomplete="current-password" '
            'maxlength="256" required></label></p>',
            "继续",
        )
    elif progress.status in {"mfa", "enroll"}:
        title = "绑定认证器" if progress.status == "enroll" else "验证动态码"
        enrollment = ""
        if progress.status == "enroll":
            if not progress.enrollment_secret or not progress.enrollment_uri:
                raise NativeHTTPError()
            enrollment = (
                "<p>在认证器中添加基于时间的六位动态码账号，保存下方密钥，然后输入认证器生成的动态码。</p>"
                f'<p>密钥：<code id="enrollment-secret">{escape(progress.enrollment_secret)}</code></p>'
                f"<details><summary>认证器导入 URI</summary><code>{escape(progress.enrollment_uri)}</code></details>"
            )
        content = enrollment + form(
            "otp",
            '<p><label>六位动态码 <input name="code" inputmode="numeric" pattern="[0-9]{6}" '
            'autocomplete="one-time-code" minlength="6" maxlength="6" required></label></p>',
            "验证",
        )
        if progress.status == "mfa":
            content += (
                "<details><summary>无法使用认证器</summary>"
                + form(
                    "recovery",
                    "<p>使用恢复码或重置时保存的重绑码后，已有会话将退出，并需要重新绑定认证器。</p>"
                    '<p><label>恢复码或重绑码 <input type="password" name="recovery_code" autocomplete="off" '
                    'maxlength="128" required></label></p>',
                    "恢复并重新绑定",
                )
                + "</details>"
            )
    elif progress.status == "authenticated":
        title = "保存恢复码" if progress.recovery_codes else "认证已完成"
        content = ""
        if progress.recovery_codes:
            codes = "".join(f"<li><code>{escape(code)}</code></li>" for code in progress.recovery_codes)
            content = (
                "<p>恢复码仅显示这一次。请保存在安全位置；每个恢复码只能使用一次。</p>"
                f'<ul id="recovery-codes">{codes}</ul>'
            )
        else:
            content = "<p>如果刚才显示了恢复码，请确认已安全保存；刷新页面不会再次显示。</p>"
        content += form("continue", "", "返回应用")
    else:
        raise NativeHTTPError()
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title></head><body><main><h1>{title}</h1>{warning}{content}</main></body></html>",
        status_code=400 if invalid else 200,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; form-action 'self' {callback_target}; frame-ancestors 'none'; base-uri 'none'"
            )
        },
    )


def _consent_page(issuer: str, request_ref: str, csrf_token: str, outcome: Any) -> HTMLResponse:
    labels = {
        "openid": "确认你的账号身份",
        "profile": "读取你的用户名",
        "offline_access": "关闭浏览器后，允许应用在会话有效期内续期",
    }
    scopes = "".join(f"<li>{escape(labels.get(scope, scope))}</li>" for scope in outcome.scopes)
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>确认应用授权</title></head><body><main><h1>确认应用授权</h1>"
        f"<p>应用 <strong>{escape(outcome.client_name)}</strong> 请求：</p><ul>{scopes}</ul>"
        "<p>此授权用于登录和账号信息，不会赋予应用组织成员或业务操作权限。</p>"
        f'<form method="post" action="{escape(issuer + "/oauth/login", quote=True)}">'
        + hidden_input("request_ref", request_ref)
        + hidden_input("csrf_token", csrf_token)
        + hidden_input("stage", "consent")
        + '<button type="submit" name="decision" value="allow">允许</button>'
        '<button type="submit" name="decision" value="deny">拒绝</button>'
        "</form></main></body></html>",
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; form-action 'self' {callback_origin(outcome.redirect_uri)}; "
                "frame-ancestors 'none'; base-uri 'none'"
            )
        },
    )


async def _advance_login(
    facade: NativeAuthFacade,
    services: NativeAuthServices,
    context: tuple[str, str, str],
    *,
    consent: bool | None = None,
) -> tuple[Response, bool]:
    outcome = await services.browser.advance(*context, consent=consent)
    issuer = facade.config.issuer
    if outcome.kind == "consent":
        response: Response = _consent_page(issuer, context[0], context[2], outcome)
        completed = False
    elif outcome.kind == "complete":
        destination = await services.oauth.finish_authorization_response(
            request_ref=context[0], browser_secret=context[1], csrf_token=context[2]
        )
        response = authorization_response(destination)
        clear_browser_cookie(response, login_cookie_name(issuer), issuer)
        completed = True
    else:
        raise NativeHTTPError()
    if outcome.cookie_secret is not None:
        set_browser_cookie(response, sso_cookie_name(issuer), outcome.cookie_secret, issuer, outcome.cookie_max_age)
    return response, completed


_AUTHORIZE_FIELDS = [
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "code_challenge",
    "code_challenge_method",
]


_AUTHORIZE_OPTIONAL_FIELDS = ["state", "nonce", "response_mode", "prompt", "max_age", "acr_values", "login_hint"]


@router.get(
    "/oauth/authorize",
    operation_id="native_authorize",
    response_class=HTMLResponse,
    openapi_extra={
        "parameters": [
            {"name": key, "in": "query", "required": key in _AUTHORIZE_FIELDS, "schema": {"type": "string"}}
            for key in [*_AUTHORIZE_FIELDS, *_AUTHORIZE_OPTIONAL_FIELDS]
        ]
    },
)
async def authorize(request: Request) -> Response:
    values = parse_pairs(request.scope.get("query_string", b""))
    require_fields(values, set(_AUTHORIZE_FIELDS), set(_AUTHORIZE_OPTIONAL_FIELDS))
    async with native_services(request) as (facade, services):
        await services.credentials.throttle_authorization(
            request.client.host if request.client is not None else "unavailable"
        )
        authorization = await services.oauth.prepare_authorization(**values)
        issuer = facade.config.issuer
        context = (authorization.request_ref, authorization.browser_secret, authorization.csrf_token)
        resumed = await services.browser.resume(
            *context, cookie_secret(request, sso_cookie_name(issuer), required=False)
        )
        if resumed:
            response, completed = await _advance_login(facade, services, context)
            if completed:
                return response
        elif values.get("prompt") == "none":
            parameters = {"error": "login_required", "iss": issuer}
            if "state" in values:
                parameters["state"] = values["state"]
            return authorization_response(
                AuthorizationResponse(values["redirect_uri"], parameters, values.get("response_mode", "query"))
            )
        else:
            response = _login_page(
                issuer,
                authorization.request_ref,
                authorization.csrf_token,
                LoginProgress("password", redirect_uri=values["redirect_uri"]),
            )
        set_browser_cookie(
            response,
            login_cookie_name(issuer),
            authorization.browser_secret,
            issuer,
            facade.config.login_request_seconds,
        )
        return response


@router.post(
    "/oauth/login",
    operation_id="native_browser_login",
    response_class=HTMLResponse,
    openapi_extra=form_contract(
        ["request_ref", "csrf_token", "stage"], ["username", "password", "code", "recovery_code", "decision"]
    ),
)
async def login(request: Request) -> Response:
    values = await read_form(request)
    common = {"request_ref", "csrf_token", "stage"}
    stages = {
        "password": {"username", "password"},
        "otp": {"code"},
        "recovery": {"recovery_code"},
        "continue": set(),
        "consent": {"decision"},
    }
    stage = values.get("stage", "")
    if stage not in stages:
        raise NativeHTTPError()
    require_fields(values, common | stages[stage])
    async with native_services(request) as (facade, services):
        issuer = facade.config.issuer
        require_same_origin(request, issuer)
        browser_secret = get_browser_secret(request, issuer)
        context = (values["request_ref"], browser_secret, values["csrf_token"])
        client_address = request.client.host if request.client is not None else "unavailable"
        if stage == "consent":
            if values["decision"] not in {"allow", "deny"}:
                raise NativeHTTPError()
            response, _completed = await _advance_login(
                facade, services, context, consent=values["decision"] == "allow"
            )
            return response
        try:
            if stage == "password":
                progress = await services.credentials.password(
                    *context, values["username"], values["password"], client_address=client_address
                )
            elif stage == "otp":
                progress = await services.credentials.otp(*context, values["code"], client_address=client_address)
            elif stage == "recovery":
                progress = await services.credentials.recover(
                    *context, values["recovery_code"], client_address=client_address
                )
            else:
                progress = LoginProgress("authenticated")
        except NativeCredentialError as exc:
            if exc.code != "invalid_credentials":
                raise
            progress = await services.credentials.progress(values["request_ref"], browser_secret)
            return _login_page(issuer, values["request_ref"], values["csrf_token"], progress, invalid=True)
        if progress.status == "authenticated" and not progress.recovery_codes:
            response, _completed = await _advance_login(facade, services, context)
            return response
        return _login_page(issuer, values["request_ref"], values["csrf_token"], progress)
