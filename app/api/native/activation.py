"""Issuer-owned employee activation; no login session or OAuth token is issued."""

import hashlib
import re
import secrets
from datetime import UTC, datetime
from html import escape
from urllib.parse import urlencode

from fastapi import Request
from starlette.responses import HTMLResponse, Response

from app.api.native.common import (
    NativeHTTPError,
    cookie_secret,
    form_contract,
    hidden_input,
    make_oauth_router,
    native_services,
    parse_pairs,
    read_form,
    require_fields,
    require_same_origin,
    set_browser_cookie,
)
from app.native_auth.activation import ActivationProgress
from app.native_auth.errors import NativeCommitUnknown, NativeCredentialError

router = make_oauth_router()


def _cookie_name(issuer: str) -> str:
    return "__Host-stage-activation" if issuer.startswith("https://") else "stage_activation"


def _browser_secret(request: Request, issuer: str, *, required: bool) -> str:
    value = cookie_secret(request, _cookie_name(issuer), required=required)
    if value is None:
        return secrets.token_urlsafe(32)
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        raise NativeHTTPError("access_denied", 400)
    return value


def _csrf_token(browser_secret: str) -> str:
    return hashlib.sha256(b"stage-activation-csrf-v1\x00" + browser_secret.encode("ascii")).hexdigest()


def _require_csrf(browser_secret: str, supplied: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", supplied) is None or not secrets.compare_digest(
        _csrf_token(browser_secret), supplied
    ):
        raise NativeHTTPError("access_denied", 403)


def _page(title: str, content: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title></head><body><main><h1>{escape(title)}</h1>"
        f"{content}</main></body></html>",
        status_code=status,
    )


def _status_link(issuer: str, activation_ref: str) -> str:
    target = issuer + "/oauth/activate/status?" + urlencode({"activation_ref": activation_ref})
    return f'<p><a href="{escape(target, quote=True)}">继续激活或查看结果</a></p>'


def _entry_page(issuer: str, browser_secret: str, *, invalid: bool = False) -> HTMLResponse:
    warning = '<p role="alert">验证未通过，请核对激活信息和密码要求后重试。</p>' if invalid else ""
    target = escape(issuer + "/oauth/activate", quote=True)
    status_target = escape(issuer + "/oauth/activate/status", quote=True)
    return _page(
        "激活企业账号",
        "<p>填写管理员提供的激活编号和激活码，并设置你自己的密码。下一步需要绑定认证器。</p>"
        + warning
        + f'<form method="post" action="{target}">'
        + hidden_input("csrf_token", _csrf_token(browser_secret))
        + '<p><label>激活编号 <input name="activation_ref" autocomplete="off" maxlength="128" required></label></p>'
        '<p><label>激活码 <input type="password" name="code" autocomplete="off" maxlength="256" required></label></p>'
        '<p><label>新密码（12 至 256 个字符） <input type="password" name="password" autocomplete="new-password" '
        'minlength="12" maxlength="256" required></label></p>'
        '<button type="submit">设置密码并绑定认证器</button></form>'
        "<details><summary>已经提交过激活信息</summary>"
        "<p>请使用原浏览器继续激活或查看结果，无需再次填写密码和激活码。</p>"
        f'<form method="get" action="{status_target}">'
        '<p><label>激活编号 <input name="activation_ref" autocomplete="off" maxlength="128" required></label></p>'
        '<button type="submit">继续激活或查看结果</button></form></details>',
        status=400 if invalid else 200,
    )


def _progress_page(
    issuer: str,
    browser_secret: str,
    progress: ActivationProgress,
    *,
    show_recovery: bool = False,
    invalid: bool = False,
) -> HTMLResponse:
    if progress.status == "completed":
        codes = ""
        if show_recovery and progress.recovery_codes:
            items = "".join(f"<li><code>{escape(code)}</code></li>" for code in progress.recovery_codes)
            codes = (
                f'<p>恢复码仅显示这一次，请安全保存。每个恢复码只能使用一次。</p><ul id="recovery-codes">{items}</ul>'
            )
        else:
            codes = "<p>此前显示的恢复码不会再次提供。如果未能保存，请联系管理员。</p>"
        return _page("账号激活完成", codes + "<p>请返回企业应用，使用新密码和认证器登录。</p>")
    if progress.status != "enrolling" or not progress.totp_secret or not progress.totp_uri:
        raise NativeHTTPError()
    warning = '<p role="alert">动态码验证未通过，请核对认证器后重试。</p>' if invalid else ""
    target = escape(issuer + "/oauth/activate/complete", quote=True)
    deadline = escape(progress.expires_at.isoformat(), quote=True)
    return _page(
        "绑定认证器",
        warning
        + "<p>在认证器中添加基于时间的六位动态码账号，保存下方密钥，再输入认证器生成的动态码。</p>"
        + f'<p>密钥：<code id="enrollment-secret">{escape(progress.totp_secret)}</code></p>'
        + f"<details><summary>认证器导入 URI</summary><code>{escape(progress.totp_uri)}</code></details>"
        + f'<p>请在 <time datetime="{deadline}">{deadline}</time> 前完成验证。</p>'
        + f'<form method="post" action="{target}">'
        + hidden_input("activation_ref", progress.activation_ref)
        + hidden_input("csrf_token", _csrf_token(browser_secret))
        + '<p><label>六位动态码 <input name="otp" inputmode="numeric" pattern="[0-9]{6}" '
        'autocomplete="one-time-code" minlength="6" maxlength="6" required></label></p>'
        '<button type="submit">完成激活</button></form>' + _status_link(issuer, progress.activation_ref),
        status=400 if invalid else 200,
    )


def _unknown_page(issuer: str, activation_ref: str, browser_secret: str, cookie_seconds: int) -> HTMLResponse:
    response = _page(
        "正在确认激活结果",
        '<p role="alert">暂时无法确认刚才的提交结果。请在当前浏览器查看结果，避免重新提交秘密信息。</p>'
        + _status_link(issuer, activation_ref),
        status=503,
    )
    # The first form may have used nearly all of its cookie lifetime before the
    # mutation committed. Preserve the same recovery proof; database deadlines
    # still bound the ceremony and no login session is established.
    set_browser_cookie(response, _cookie_name(issuer), browser_secret, issuer, cookie_seconds)
    return response


def _unavailable_page(issuer: str) -> HTMLResponse:
    return _page(
        "无法继续激活",
        '<p role="alert">激活信息不可用。请确认使用原浏览器；如仍无法继续，请联系管理员重新提供激活信息。</p>'
        + f'<p><a href="{escape(issuer + "/oauth/activate", quote=True)}">返回激活入口</a></p>',
        status=400,
    )


@router.get("/oauth/activate", operation_id="native_activation_entry", response_class=HTMLResponse)
async def activation_entry(request: Request) -> Response:
    if request.scope.get("query_string", b""):
        raise NativeHTTPError()
    async with native_services(request) as (facade, _services):
        issuer = facade.config.issuer
        browser_secret = _browser_secret(request, issuer, required=False)
        response = _entry_page(issuer, browser_secret)
        set_browser_cookie(response, _cookie_name(issuer), browser_secret, issuer, facade.config.login_request_seconds)
        return response


@router.post(
    "/oauth/activate",
    operation_id="native_activation_begin",
    response_class=HTMLResponse,
    openapi_extra=form_contract(["activation_ref", "code", "password", "csrf_token"]),
)
async def activation_begin(request: Request) -> Response:
    values = await read_form(request)
    require_fields(values, {"activation_ref", "code", "password", "csrf_token"})
    async with native_services(request) as (facade, services):
        issuer = facade.config.issuer
        require_same_origin(request, issuer)
        browser_secret = _browser_secret(request, issuer, required=True)
        _require_csrf(browser_secret, values["csrf_token"])
        try:
            progress = await services.activation.begin(
                activation_ref=values["activation_ref"],
                code=values["code"],
                password=values["password"],
                browser_secret=browser_secret,
                csrf_token=values["csrf_token"],
                peer_key=request.client.host if request.client is not None else "unavailable",
            )
        except NativeCredentialError as exc:
            if exc.code not in {"invalid_credentials", "invalid_password", "activation_unavailable"}:
                raise
            return _entry_page(issuer, browser_secret, invalid=True)
        except NativeCommitUnknown:
            return _unknown_page(issuer, values["activation_ref"], browser_secret, facade.config.login_request_seconds)
        response = _progress_page(issuer, browser_secret, progress)
        cookie_seconds = max(1, int((progress.expires_at - datetime.now(UTC)).total_seconds()))
        set_browser_cookie(response, _cookie_name(issuer), browser_secret, issuer, cookie_seconds)
        return response


@router.get(
    "/oauth/activate/status",
    operation_id="native_activation_status",
    response_class=HTMLResponse,
    openapi_extra={
        "parameters": [{"name": "activation_ref", "in": "query", "required": True, "schema": {"type": "string"}}]
    },
)
async def activation_status(request: Request) -> Response:
    values = parse_pairs(request.scope.get("query_string", b""))
    require_fields(values, {"activation_ref"})
    async with native_services(request) as (facade, services):
        issuer = facade.config.issuer
        require_same_origin(request, issuer)
        browser_secret = _browser_secret(request, issuer, required=True)
        # A public activation reference must not provide unlimited access to
        # the organization's exclusive activation coordination, even on denial.
        await services.credentials.throttle_activation(
            request.client.host if request.client is not None else "unavailable"
        )
        try:
            progress = await services.activation.progress(
                activation_ref=values["activation_ref"], browser_secret=browser_secret
            )
        except NativeCredentialError as exc:
            if exc.code != "activation_unavailable":
                raise
            return _unavailable_page(issuer)
        return _progress_page(issuer, browser_secret, progress)


@router.post(
    "/oauth/activate/complete",
    operation_id="native_activation_complete",
    response_class=HTMLResponse,
    openapi_extra=form_contract(["activation_ref", "csrf_token", "otp"]),
)
async def activation_complete(request: Request) -> Response:
    values = await read_form(request)
    require_fields(values, {"activation_ref", "csrf_token", "otp"})
    async with native_services(request) as (facade, services):
        issuer = facade.config.issuer
        require_same_origin(request, issuer)
        browser_secret = _browser_secret(request, issuer, required=True)
        _require_csrf(browser_secret, values["csrf_token"])
        try:
            progress = await services.activation.complete(
                activation_ref=values["activation_ref"],
                browser_secret=browser_secret,
                csrf_token=values["csrf_token"],
                code=values["otp"],
                peer_key=request.client.host if request.client is not None else "unavailable",
            )
        except NativeCredentialError as exc:
            if exc.code == "activation_unavailable":
                return _unavailable_page(issuer)
            if exc.code != "invalid_credentials":
                raise
            try:
                progress = await services.activation.progress(
                    activation_ref=values["activation_ref"], browser_secret=browser_secret
                )
            except NativeCredentialError as progress_error:
                if progress_error.code != "activation_unavailable":
                    raise
                return _unavailable_page(issuer)
            return _progress_page(issuer, browser_secret, progress, invalid=True)
        except NativeCommitUnknown:
            return _unknown_page(issuer, values["activation_ref"], browser_secret, facade.config.login_request_seconds)
        return _progress_page(issuer, browser_secret, progress, show_recovery=True)
