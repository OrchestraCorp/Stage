from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Stable response contract for the process liveness probe."""

    model_config = ConfigDict(title="HealthResponse")

    status: Literal["ok"] = Field(description="The process is alive.")
    app: str = Field(description="Configured Stage application name.")
    env: str = Field(description="Configured runtime environment name.")
