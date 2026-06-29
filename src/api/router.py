from fastapi import APIRouter
from src.api.endpoints.chat import router as chat_router
from src.api.endpoints.upload import router as upload_router
from src.api.endpoints.skills import router as skills_router


api_router = APIRouter()
api_router.include_router(chat_router, prefix="/api/v1")
api_router.include_router(upload_router, prefix="/api/v1")
api_router.include_router(skills_router, prefix="/api/v1")
