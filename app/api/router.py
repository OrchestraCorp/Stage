from fastapi import APIRouter

from app.api.identity import router as identity_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(identity_router)
