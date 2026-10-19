"""Request correlation and a safe HTTP failure boundary."""

import re
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import ClientDisconnect, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import unexpected_error_response

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ResponseStreamError(RuntimeError):
    """An already-started response failed; the original exception is not exposed."""


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if REQUEST_ID_PATTERN.fullmatch(incoming) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        response_started = False

        async def correlated_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, correlated_send)
        except ClientDisconnect:
            raise
        except Exception as exc:
            if response_started:
                # Do not emit a second response start or expose an exception via the server logger.
                raise ResponseStreamError(f"response stream failed request_id={request_id}") from None
            response = unexpected_error_response(Request(scope), exc)
            await response(scope, receive, correlated_send)
