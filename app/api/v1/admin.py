"""Administrative operations (Spec.md §4)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import RequireAdminKey
from app.core.errors import DependencyUnavailableError, ErrorEnvelope
from app.domain.periods import Period
from app.schemas.common import GameId
from app.services import rebuild as rebuild_service

router = APIRouter(tags=["admin"])


class RebuildRequest(BaseModel):
    """Optional narrowing for a rebuild. Omit both fields to rebuild everything."""

    model_config = ConfigDict(extra="forbid")

    game_id: GameId | None = None
    period: Period | None = None


class RebuildResponse(BaseModel):
    boards: int = Field(description="Boards successfully rebuilt.")
    entries: int = Field(description="Standings written to the index.")
    failed_boards: list[str] = Field(
        default_factory=list,
        description="Boards that could not be rebuilt. Re-running is safe and resumes.",
    )


@router.post(
    "/admin/leaderboards/rebuild",
    response_model=RebuildResponse,
    summary="Rebuild the rank index from Postgres",
    responses={
        401: {"model": ErrorEnvelope, "description": "Missing or invalid X-Admin-Key"},
        503: {"model": ErrorEnvelope, "description": "No rank index is configured"},
    },
    description=(
        "Reconstructs Redis sorted sets from Postgres, which is the source of "
        "truth. Required after attaching an index to a database that already "
        "holds scores, and the recovery path for eviction, an accidental "
        "flush, or a version upgrade.\n\n"
        "Writes with `ZADD GT`, so it is safe to run against a live index: a "
        "submission arriving mid-rebuild keeps its value, and re-running "
        "simply finishes the job.\n\n"
        "Runs synchronously and returns real counts. At a scale where a "
        "rebuild outlasts an HTTP request this should become a background job "
        "with a job store; returning verifiable counts is more useful than a "
        "fire-and-forget `202` until then."
    ),
)
async def rebuild_leaderboards(
    request: Request,
    payload: RebuildRequest | None = None,
    _: RequireAdminKey = None,
) -> RebuildResponse:
    redis_client = getattr(request.app.state, "redis", None)
    if redis_client is None:
        raise DependencyUnavailableError(
            "No rank index is configured; set REDIS_URL to enable rebuilds"
        )

    options = payload or RebuildRequest()
    report = await rebuild_service.rebuild_index(
        request.app.state.engine,
        redis_client,
        game_id=options.game_id,
        period=options.period,
    )
    return RebuildResponse(
        boards=report.boards,
        entries=report.entries,
        failed_boards=report.failed_boards,
    )
