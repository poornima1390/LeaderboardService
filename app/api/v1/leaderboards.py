"""Leaderboard read endpoints (Spec.md §4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from app.core.errors import (
    ErrorEnvelope,
    GameNotFoundError,
    UserNotRankedError,
    ValidationFailedError,
)
from app.domain.periods import (
    BoardKey,
    InvalidBucketError,
    Period,
    bucket_for,
    is_future_bucket,
    validate_bucket,
)
from app.repositories import games
from app.schemas.common import GameId, UserId
from app.schemas.leaderboards import (
    LeaderboardEntry,
    LeaderboardPage,
    UserContextResponse,
)
from app.services import leaderboard as leaderboard_service
from app.services.leaderboard import HydratedEntry

router = APIRouter(tags=["leaderboards"])

# Bounds from Spec.md §5. Out-of-range values are rejected, not clamped:
# silently returning 100 rows for limit=10000 teaches a client its request
# worked.
LIMIT_DEFAULT, LIMIT_MAX = 10, 100
OFFSET_MAX = 10_000
WINDOW_DEFAULT, WINDOW_MAX = 3, 25

PeriodParam = Annotated[Period, Query(description="Time window for this board.")]
BucketParam = Annotated[
    str | None,
    Query(
        description=(
            "Specific window, e.g. `2026-09-16` for daily or `2026-W38` for "
            "weekly. Defaults to the current one. Must match the period's format."
        ),
        examples=["2026-09-16"],
    ),
]


async def _resolve_board(
    request: Request, game_id: str, period: Period, bucket: str | None
) -> BoardKey:
    """Validate the game and the period/bucket pair, returning the board.

    A mismatched pair such as `period=daily&bucket=2026-W38` is rejected rather
    than queried: it addresses a board that can never hold rows, so the honest
    answer is "you asked the wrong question", not an empty leaderboard.
    """
    async with request.app.state.engine.connect() as conn:
        game = await games.get_game(conn, game_id)
    if game is None:
        raise GameNotFoundError(
            f"Game {game_id!r} is not registered. GET /v1/games lists valid ids."
        )

    # Retired games stay readable — only writes are refused (Spec.md §7).
    resolved = bucket if bucket is not None else bucket_for(period, datetime.now(UTC))
    try:
        validate_bucket(period, resolved)
    except InvalidBucketError as exc:
        raise ValidationFailedError(str(exc), field="bucket") from exc

    if is_future_bucket(period, resolved):
        raise ValidationFailedError(f"bucket {resolved!r} is in the future", field="bucket")

    return BoardKey(game_id=game_id, period=period, bucket=resolved)


def _to_entry(entry: HydratedEntry) -> LeaderboardEntry:
    return LeaderboardEntry(
        rank=entry.rank,
        user_id=entry.user_id,
        display_name=entry.display_name,
        score=entry.score,
        # achieved_at is NOT NULL in the schema, so it is always present for a
        # ranked user; epoch is an unreachable fallback that keeps the response
        # model honest rather than Optional for a field that is never null.
        achieved_at=entry.achieved_at or datetime.fromtimestamp(0, UTC),
    )


@router.get(
    "/games/{game_id}/leaderboard",
    response_model=LeaderboardPage,
    summary="Top X of a leaderboard",
    responses={
        404: {"model": ErrorEnvelope, "description": "Game is not registered"},
    },
    description=(
        "Highest scores first, ordered by `score DESC, user_id DESC` compared "
        "byte-wise — a strict total order, so ranks are unique and dense.\n\n"
        "**Offset pagination is deliberate here.** `ZREVRANGE key start stop` "
        "is `O(log N + n)` regardless of offset, so the deep-offset penalty "
        "that normally forces cursor pagination does not exist on the serving "
        "path. The offset cap bounds the degraded Postgres path, where offset "
        "does cost.\n\n"
        "An empty board returns `200` with `total_entries: 0`, not `404`."
    ),
)
async def get_leaderboard(
    request: Request,
    game_id: Annotated[GameId, Path()],
    period: PeriodParam = Period.ALL_TIME,
    bucket: BucketParam = None,
    limit: Annotated[int, Query(ge=1, le=LIMIT_MAX)] = LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0, le=OFFSET_MAX)] = 0,
) -> LeaderboardPage:
    board = await _resolve_board(request, game_id, period, bucket)

    page = await leaderboard_service.get_top(
        engine=request.app.state.engine,
        redis_client=getattr(request.app.state, "redis", None),
        board=board,
        limit=limit,
        offset=offset,
    )

    return LeaderboardPage(
        game_id=board.game_id,
        period=board.period,
        period_bucket=board.bucket,
        total_entries=page.total,
        limit=page.limit,
        offset=page.offset,
        entries=[_to_entry(entry) for entry in page.entries],
        generated_at=datetime.now(UTC),
        source=page.source,
    )


@router.get(
    "/games/{game_id}/users/{user_id}/rank",
    response_model=UserContextResponse,
    summary="A user's rank and their surroundings",
    responses={
        404: {
            "model": ErrorEnvelope,
            "description": "Game is not registered, or the user has no score on this board",
        },
    },
    description=(
        "Returns the user's rank plus up to `window` players either side.\n\n"
        "`window=0` degenerates to 'just my rank', so one endpoint answers "
        "both questions.\n\n"
        "Served by a single Lua script (`ZREVRANK` → `ZREVRANGE` → `ZCARD`), so "
        "the rank, the window and the total come from one consistent snapshot. "
        "Issued as separate commands, a concurrent submission could land "
        "between them and return a window that does not contain the user at "
        "the rank just reported.\n\n"
        "`above` and `below` are short at the boundaries — rank 1 returns an "
        "empty `above`. That is normal output, not an error.\n\n"
        "A valid game with an unranked user returns `USER_NOT_RANKED`, "
        "distinct from `GAME_NOT_FOUND`: the remedies differ entirely."
    ),
)
async def get_user_rank(
    request: Request,
    game_id: Annotated[GameId, Path()],
    user_id: Annotated[UserId, Path()],
    period: PeriodParam = Period.ALL_TIME,
    bucket: BucketParam = None,
    window: Annotated[int, Query(ge=0, le=WINDOW_MAX)] = WINDOW_DEFAULT,
) -> UserContextResponse:
    board = await _resolve_board(request, game_id, period, bucket)

    context = await leaderboard_service.get_user_context(
        engine=request.app.state.engine,
        redis_client=getattr(request.app.state, "redis", None),
        board=board,
        user_id=user_id,
        window=window,
    )
    if context is None:
        raise UserNotRankedError(
            f"User {user_id!r} has no score on leaderboard "
            f"{board.game_id}/{board.period.value}/{board.bucket}"
        )

    return UserContextResponse(
        game_id=board.game_id,
        period=board.period,
        period_bucket=board.bucket,
        total_entries=context.total,
        user=_to_entry(context.user),
        above=[_to_entry(entry) for entry in context.above],
        below=[_to_entry(entry) for entry in context.below],
        percentile=context.percentile,
        generated_at=datetime.now(UTC),
        source=context.source,
    )


__all__ = ["router"]
