"""Score submission endpoint (Spec.md §4)."""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.deps import RequireApiKey
from app.core.errors import ErrorEnvelope
from app.schemas.scores import ScoreSubmission, ScoreSubmissionResponse, Standing
from app.services import scoring

router = APIRouter(tags=["scores"])


@router.post(
    "/scores",
    response_model=ScoreSubmissionResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit a score",
    responses={
        401: {"model": ErrorEnvelope, "description": "Missing or invalid X-API-Key"},
        404: {"model": ErrorEnvelope, "description": "Game is not registered"},
        409: {"model": ErrorEnvelope, "description": "Game is retired"},
    },
    description=(
        "Records a score and returns the user's resulting standing on every board.\n\n"
        "**Idempotent.** A user's standing is their best score, so replaying an "
        "identical request is a no-op that returns the same body — retry freely, "
        "no idempotency key needed.\n\n"
        "**One submission updates three boards** (all-time, daily, weekly) and "
        "best-score semantics apply per board, so a score can be a personal best "
        "for today without beating an all-time best. Each is reported separately "
        "via `improved`.\n\n"
        "**`achieved_at` is not accepted.** Timestamps are server-assigned; a "
        "client-supplied one would allow backdating a score to win tiebreaks.\n\n"
        "**Succeeds even if the rank index is down.** The score commits durably to "
        "Postgres regardless; only `rank` may come back null, and the index catches "
        "up within seconds."
    ),
)
async def submit_score(
    request: Request,
    payload: ScoreSubmission,
    _: RequireApiKey,
) -> ScoreSubmissionResponse:
    # 200, not 201: this upserts a standing rather than creating an
    # addressable resource, and there is no new URL to point a Location at.
    result = await scoring.submit_score(
        engine=request.app.state.engine,
        redis_client=getattr(request.app.state, "redis", None),
        game_id=payload.game_id,
        user_id=payload.user_id,
        score=payload.score,
        display_name=payload.display_name,
    )

    return ScoreSubmissionResponse(
        user_id=result.user_id,
        game_id=result.game_id,
        submitted_score=result.submitted_score,
        submitted_at=result.submitted_at,
        standings=[
            Standing(
                period=item.standing.board.period,
                period_bucket=item.standing.board.bucket,
                score=item.standing.score,
                rank=item.rank,
                improved=item.standing.improved,
            )
            for item in result.results
        ],
    )
