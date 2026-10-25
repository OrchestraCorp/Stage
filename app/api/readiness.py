"""Safe dependency readiness; process liveness is exposed separately."""

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.bootstrap.application import ApplicationRuntime

router = APIRouter(tags=["system"])


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    components: dict[str, Literal["ready", "unavailable", "disabled"]]


@router.get(
    "/ready",
    response_model=ReadyResponse,
    operation_id="runtime_readiness",
    summary="Required component readiness",
    responses={503: {"model": ReadyResponse, "description": "Required components are unavailable"}},
)
async def readiness(request: Request, response: Response) -> ReadyResponse:
    """Check supervised dependency readiness without exposing credentials or internal diagnostics."""
    runtime: ApplicationRuntime | None = getattr(request.app.state, "runtime", None)
    ready = runtime is not None and runtime.ready
    response.status_code = 200 if ready else 503
    return ReadyResponse.model_validate(
        {
            "status": "ready" if ready else "not_ready",
            "components": runtime.public_status() if runtime else {"database": "unavailable", "redis": "disabled"},
        }
    )
