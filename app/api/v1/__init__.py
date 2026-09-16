"""Version 1 of the public API."""

from fastapi import APIRouter

from app.api.v1.games import router as games_router
from app.api.v1.scores import router as scores_router

router = APIRouter()
router.include_router(scores_router)
router.include_router(games_router)

__all__ = ["router"]
