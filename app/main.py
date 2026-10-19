from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import settings


def create_app() -> FastAPI:
    app = FastAPI(
        title="OrchestraCorp Stage",
        version="0.1.0",
        description="组织运行与热协作系统底板",
    )
    app.include_router(api_router)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name, "env": settings.env}

    return app


app = create_app()
