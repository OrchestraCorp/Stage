import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.identity import IdentityCacheControlMiddleware
from app.api.native_auth import NativeAuthSecurityMiddleware
from app.api.native_auth import router as native_auth_router
from app.api.readiness import router as readiness_router
from app.api.router import api_router
from app.bootstrap.application import ApplicationRuntime
from app.core.config import Settings, settings
from app.core.errors import ERROR_RESPONSES, register_exception_handlers
from app.core.middleware import RequestIdMiddleware, install_safe_access_logging

logger = logging.getLogger(__name__)


def create_app(config: Settings | None = None) -> FastAPI:
    runtime_settings = config or settings
    install_safe_access_logging()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime = ApplicationRuntime(runtime_settings)
        application.state.runtime = runtime
        primary_failed = False
        try:
            await runtime.start()
            yield
        except BaseException:
            primary_failed = True
            raise
        finally:
            closed = False
            try:
                try:
                    await runtime.close()
                    closed = True
                except Exception as error:
                    if not primary_failed:
                        raise
                    logger.error("runtime cleanup failed exception_type=%s", type(error).__name__)
            finally:
                if closed:
                    del application.state.runtime

    app = FastAPI(
        title="OrchestraCorp Stage",
        version="1.0.0",
        description="组织运行与热协作系统底板",
        lifespan=lifespan,
        responses=ERROR_RESPONSES,
        docs_url=None if runtime_settings.env == "prod" else "/docs",
        openapi_url=None if runtime_settings.env == "prod" else "/openapi.json",
        redoc_url=None,
    )
    app.state.settings = runtime_settings
    app.add_middleware(RequestIdMiddleware)
    # Added last to wrap the request/error boundary as well as FastAPI validation.
    app.add_middleware(IdentityCacheControlMiddleware)
    app.add_middleware(NativeAuthSecurityMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(readiness_router)
    app.include_router(api_router)
    app.include_router(native_auth_router)

    return app


app = create_app()
