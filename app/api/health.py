from fastapi import APIRouter

from app.core.config import settings
from app.core.schemas import HealthResponse

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=HealthResponse,
    operation_id="health_liveness",
    summary="Liveness probe",
    description=(
        "Returns application process metadata. This endpoint does not probe the database or other dependencies."
    ),
    responses={200: {"description": "Liveness response"}},
)
async def health() -> HealthResponse:
    """Report that the Stage process is alive without performing dependency checks."""

    return HealthResponse(status="ok", app=settings.app_name, env=settings.env)
