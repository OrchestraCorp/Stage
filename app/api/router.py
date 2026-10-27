from fastapi import APIRouter

from app.api.administration import router as administration_router
from app.api.assignments import router as assignments_router
from app.api.identity import router as identity_router
from app.api.native_auth import account_router
from app.api.onboarding import router as onboarding_router
from app.api.work import router as work_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(identity_router)
api_router.include_router(assignments_router)
api_router.include_router(work_router)
api_router.include_router(account_router)
api_router.include_router(administration_router)
api_router.include_router(onboarding_router)
