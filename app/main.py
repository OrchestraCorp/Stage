from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.router import api_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="OrchestraCorp Stage",
        version="0.1.0",
        description="组织运行与热协作系统底板",
    )
    app.include_router(health_router)
    app.include_router(api_router)

    return app


app = create_app()
