"""Leaderboard reads: repository selection, fallback, hydration (Spec.md §4).

The interesting logic here is not the queries — those live in the two
RankingRepository implementations — but choosing between them safely.

The hazard is specific. When a Redis key is missing, every command answers
truthfully and uselessly: ZCARD returns 0, ZREVRANGE returns nothing. That is
indistinguishable from a board that genuinely has no scores yet, so a cold or
evicted key would serve an **empty leaderboard with a 200** — the worst
failure this service can have, because nothing about it looks like a failure.

So a zero from the index is never trusted on its own: it is confirmed against
Postgres, which is the source of truth. That extra COUNT runs only when the
index says the board is empty, which is either a genuinely empty board (cheap,
indexed) or a cold key (rare, and worth the cost).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.logging import get_logger
from app.domain.periods import BoardKey
from app.repositories.postgres_ranking import PostgresRanking
from app.repositories.ranking import BoardPage, RankedEntry, RankingRepository, UserContext
from app.repositories.redis_ranking import RankIndexReadError, RedisRanking

logger = get_logger(__name__)

# Boards currently being self-healed, so a hot cold key cannot spawn one
# rebuild task per request. Per-instance and intentionally simple: the guard
# only needs to prevent a local stampede, and a duplicate rebuild across
# instances is harmless because rebuild writes with ZADD GT.
_rebuilding: set[str] = set()


@dataclass(frozen=True, slots=True)
class UserProfile:
    display_name: str | None
    achieved_at: datetime | None


@dataclass(frozen=True, slots=True)
class HydratedEntry:
    """A ranked entry plus the metadata Redis does not hold."""

    rank: int
    user_id: str
    score: int
    display_name: str | None
    achieved_at: datetime | None


@dataclass(frozen=True, slots=True)
class LeaderboardPage:
    board: BoardKey
    entries: tuple[HydratedEntry, ...]
    total: int
    limit: int
    offset: int
    source: str


@dataclass(frozen=True, slots=True)
class UserContextView:
    board: BoardKey
    user: HydratedEntry
    above: tuple[HydratedEntry, ...]
    below: tuple[HydratedEntry, ...]
    total: int
    percentile: float
    source: str


async def _resolve_repository(
    engine: AsyncEngine, redis_client: Redis | None, board: BoardKey
) -> tuple[RankingRepository, bool]:
    """Pick the repository to serve from. Returns (repository, index_was_cold).

    Prefers Redis, falls back to Postgres when the index is unreachable or
    reports a board as empty that Postgres says is not.
    """
    if redis_client is None:
        return PostgresRanking(engine), False

    index = RedisRanking(redis_client)
    try:
        indexed = await index.size(board)
    except RankIndexReadError as exc:
        logger.warning(
            "leaderboard.index_unavailable",
            board=str(board),
            error_type=type(exc).__name__,
            detail="Serving from Postgres",
        )
        return PostgresRanking(engine), False

    if indexed > 0:
        return index, False

    # The index says empty. Confirm against the source of truth before
    # believing it, because "cold key" and "no scores yet" look identical
    # from Redis.
    fallback = PostgresRanking(engine)
    if await fallback.size(board) == 0:
        return index, False  # genuinely empty; either store answers correctly

    logger.warning(
        "leaderboard.index_cold",
        board=str(board),
        detail="Index key is missing or evicted; serving from Postgres and rebuilding",
    )
    return fallback, True


def _schedule_self_heal(engine: AsyncEngine, redis_client: Redis | None, board: BoardKey) -> None:
    """Rebuild one cold board in the background.

    Fire-and-forget on purpose: the response is already being served from
    Postgres, so the reader must not wait. Guarded so that a hot cold key
    triggers one rebuild rather than one per request.
    """
    if redis_client is None:
        return
    key = board.redis_key
    if key in _rebuilding:
        return
    _rebuilding.add(key)

    async def _run() -> None:
        # Imported here to avoid a circular import: the rebuild service reads
        # from the repositories this module also uses.
        from app.services.rebuild import rebuild_index

        try:
            await rebuild_index(engine, redis_client, game_id=board.game_id, period=board.period)
        except Exception:
            logger.exception("leaderboard.self_heal_failed", board=str(board))
        finally:
            _rebuilding.discard(key)

    task = asyncio.create_task(_run(), name=f"self-heal:{key}")
    # Keep a reference so the task is not garbage collected mid-flight, and
    # so an exception is never swallowed silently.
    _self_heal_tasks.add(task)
    task.add_done_callback(_self_heal_tasks.discard)


_self_heal_tasks: set[asyncio.Task[None]] = set()


async def _hydrate(
    engine: AsyncEngine, board: BoardKey, entries: tuple[RankedEntry, ...]
) -> tuple[HydratedEntry, ...]:
    """Attach display_name and achieved_at from Postgres.

    One query for the whole page via `= ANY(...)`, not one per row. Redis
    stores only (member, score) so the index never needs invalidating when a
    player renames themselves — the cost is this single join-by-id.
    """
    if not entries:
        return ()

    user_ids = [entry.user_id for entry in entries]
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT e.user_id, u.display_name, e.achieved_at
                FROM leaderboard_entries e
                JOIN users u ON u.id = e.user_id
                WHERE e.game_id = :game_id
                  AND e.period = :period
                  AND e.period_bucket = :bucket
                  AND e.user_id = ANY(:user_ids)
                """
            ),
            {
                "game_id": board.game_id,
                "period": board.period.value,
                "bucket": board.bucket,
                "user_ids": user_ids,
            },
        )
        profiles = {row[0]: UserProfile(display_name=row[1], achieved_at=row[2]) for row in result}

    # A missing profile is possible when the index is momentarily ahead of a
    # delete. Rendered as nulls rather than dropped, so ranks stay contiguous
    # — a gap in the ranks would look like a pagination bug to a client.
    hydrated: list[HydratedEntry] = []
    for entry in entries:
        profile = profiles.get(entry.user_id)
        hydrated.append(
            HydratedEntry(
                rank=entry.rank,
                user_id=entry.user_id,
                score=entry.score,
                display_name=profile.display_name if profile else None,
                achieved_at=profile.achieved_at if profile else None,
            )
        )
    return tuple(hydrated)


async def get_top(
    *,
    engine: AsyncEngine,
    redis_client: Redis | None,
    board: BoardKey,
    limit: int,
    offset: int,
) -> LeaderboardPage:
    """A page of the leaderboard, highest score first."""
    repository, cold = await _resolve_repository(engine, redis_client, board)
    if cold:
        _schedule_self_heal(engine, redis_client, board)

    page: BoardPage = await repository.top(board, limit=limit, offset=offset)
    return LeaderboardPage(
        board=board,
        entries=await _hydrate(engine, board, page.entries),
        total=page.total,
        limit=limit,
        offset=offset,
        source=repository.source,
    )


async def get_user_context(
    *,
    engine: AsyncEngine,
    redis_client: Redis | None,
    board: BoardKey,
    user_id: str,
    window: int,
) -> UserContextView | None:
    """A user's rank and surroundings, or None if they are not ranked."""
    repository, cold = await _resolve_repository(engine, redis_client, board)
    if cold:
        _schedule_self_heal(engine, redis_client, board)

    context: UserContext | None = await repository.around(board, user_id, window=window)
    if context is None:
        return None

    hydrated = await _hydrate(engine, board, (context.user, *context.above, *context.below))
    by_rank = {entry.rank: entry for entry in hydrated}

    return UserContextView(
        board=board,
        user=by_rank[context.user.rank],
        above=tuple(by_rank[entry.rank] for entry in context.above),
        below=tuple(by_rank[entry.rank] for entry in context.below),
        total=context.total,
        percentile=context.percentile,
        source=repository.source,
    )
