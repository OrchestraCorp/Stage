from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.router import api_router
from app.core.config import Settings, settings
from app.core.db import create_engine, create_session_factory
from app.core.errors import ERROR_RESPONSES, register_exception_handlers
from app.core.middleware import RequestIdMiddleware


def create_app(config: Settings | None = None) -> FastAPI:
    runtime_settings = config or settings

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(runtime_settings.database_url)
        try:
            application.state.engine = engine
            application.state.session_factory = create_session_factory(engine)
            yield
        finally:
            try:
                await engine.dispose()
            finally:
                if hasattr(application.state, "session_factory"):
                    del application.state.session_factory
                if hasattr(application.state, "engine"):
                    del application.state.engine

    app = FastAPI(
        title="OrchestraCorp Stage",
        version="0.1.0",
        description="组织运行与热协作系统底板",
        lifespan=lifespan,
        responses=ERROR_RESPONSES,
        docs_url=None if runtime_settings.env == "prod" else "/docs",
        openapi_url=None if runtime_settings.env == "prod" else "/openapi.json",
        redoc_url=None,
    )
    app.state.settings = runtime_settings
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router)

    return app


app = create_app()
