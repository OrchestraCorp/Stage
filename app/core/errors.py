"""Stable public errors without exception payloads, credentials or input values."""

import logging
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.schemas import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


def error_response(
    request: Request, status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", uuid4().hex)
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, request_id=request_id))
    protocol_headers = {
        name: value
        for name, value in (headers or {}).items()
        if name.lower() in {"allow", "www-authenticate", "retry-after"}
    }
    protocol_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=status,
        content=body.model_dump(),
        headers=protocol_headers,
    )


def unexpected_error_response(request: Request, exc: Exception) -> JSONResponse:
    # Raw exception strings/tracebacks may contain credentials or SQL parameters.
    logger.error(
        "request failed request_id=%s exception_type=%s",
        getattr(request.state, "request_id", "unknown"),
        type(exc).__name__,
    )
    return error_response(request, 500, "INTERNAL_ERROR", "An unexpected error occurred")


def register_exception_handlers(app: FastAPI) -> None:
    async def validation_handler(request: Request, _exc: RequestValidationError) -> JSONResponse:
        return error_response(request, 422, "REQUEST_INVALID", "Request validation failed")

    async def http_handler(request: Request, exc: HTTPException) -> JSONResponse:
        # Arbitrary exception detail may contain private business data.
        return error_response(
            request, exc.status_code, f"HTTP_{exc.status_code}", "Request could not be completed", exc.headers
        )

    async def unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
        return unexpected_error_response(request, exc)

    app.add_exception_handler(RequestValidationError, validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unexpected_handler)


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    500: {"model": ErrorResponse, "description": "Unexpected server error"},
}
