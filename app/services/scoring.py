"""Score submission orchestration (Spec.md D1, D4, D5).

The sequence, and why it is this order:

1. Validate the game (404 / 409) — before any write, so a bad request costs
   nothing and never leaves a user row behind.
2. In ONE transaction: upsert the user, fan the score out across all three
   period boards with max-semantics, and enqueue an outbox row per improved
   board. Commit.
3. *After* commit, try to apply the updates to the Redis index and mark the
   outbox rows delivered. Best effort.
4. Read ranks back for the response.

Step 3 failing is not an error the client should see. The score is already
durable and the outbox row guarantees the sweeper will apply it, so the
submission succeeded — only the freshness of the index is affected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.logging import get_logger
from app.repositories import entries, games, outbox, rank_index
from app.repositories.rank_index import IndexUpdate, RankIndexUnavailableError

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StandingResult:
    """One board's outcome, enriched with the rank read back after the write."""

    standing: entries.BoardStanding
    rank: int | None


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    user_id: str
    game_id: str
    submitted_score: int
    submitted_at: datetime
    results: tuple[StandingResult, ...]
    index_synced: bool


async def submit_score(
    *,
    engine: AsyncEngine,
    redis_client: Redis | None,
    game_id: str,
    user_id: str,
    score: int,
    display_name: str | None = None,
    now: datetime | None = None,
) -> SubmissionResult:
    """Submit a score and report the resulting standing on every board."""
    submitted_at = (now or datetime.now(UTC)).astimezone(UTC)

    # --- Durable write -------------------------------------------------- #
    async with engine.begin() as conn:
        await games.require_writable_game(conn, game_id)
        outcome = await entries.submit_score(
            conn,
            game_id=game_id,
            user_id=user_id,
            score=score,
            achieved_at=submitted_at,
            display_name=display_name,
        )
    # Committed. From here on, nothing can lose this score.

    logger.info(
        "score.submitted",
        game_id=game_id,
        user_id=user_id,
        score=score,
        improved_boards=[str(standing.board) for standing in outcome.improved],
    )

    # --- Best-effort index update --------------------------------------- #
    index_synced = await _sync_index(engine, redis_client, outcome, user_id=user_id)

    # --- Read ranks back ------------------------------------------------- #
    results = tuple(
        [
            StandingResult(
                standing=standing,
                rank=await _read_rank(redis_client, standing, user_id=user_id),
            )
            for standing in outcome.standings
        ]
    )

    return SubmissionResult(
        user_id=user_id,
        game_id=game_id,
        submitted_score=score,
        submitted_at=submitted_at,
        results=results,
        index_synced=index_synced,
    )


async def _sync_index(
    engine: AsyncEngine,
    redis_client: Redis | None,
    outcome: entries.SubmissionOutcome,
    *,
    user_id: str,
) -> bool:
    """Apply improved standings to the index and settle their outbox rows.

    Returns whether the fast path succeeded. A False return is a normal
    operating state, not a failure to report: the outbox rows stay
    undelivered and the sweeper drains them.
    """
    improved = outcome.improved
    if not improved:
        # Nothing changed, so nothing was enqueued and nothing needs syncing.
        return True
    if redis_client is None:
        return False

    updates = [
        IndexUpdate(
            redis_key=standing.board.redis_key,
            user_id=user_id,
            score=standing.score,
            period=standing.board.period,
        )
        for standing in improved
    ]
    try:
        await rank_index.apply_updates(redis_client, updates)
    except RankIndexUnavailableError:
        logger.warning(
            "score.index_sync_deferred",
            boards=len(improved),
            detail="Score is durable; the outbox sweeper will apply it",
        )
        return False

    # The index is up to date, so these rows no longer need sweeping.
    #
    # Marked in a separate transaction *after* the ZADD, never before: if this
    # update failed we would rather the sweeper redo an idempotent ZADD than
    # mark work delivered that never landed.
    try:
        async with engine.begin() as conn:
            await outbox.mark_delivered(conn, list(outcome.outbox_ids))
    except Exception:
        # Losing this is inconsequential — the sweeper will retry the ZADD,
        # which is a no-op — so it must not fail the submission.
        logger.warning("score.outbox_settle_failed", rows=len(outcome.outbox_ids))

    return True


async def _read_rank(
    redis_client: Redis | None, standing: entries.BoardStanding, *, user_id: str
) -> int | None:
    """Rank for the response, or None when the index cannot answer.

    Deliberately not fatal and deliberately not computed from Postgres here:
    the Postgres ranking fallback arrives in Phase 3 behind the
    RankingRepository, and duplicating it now would mean writing it twice.
    """
    if redis_client is None:
        return None
    try:
        return await rank_index.get_rank(redis_client, standing.board.redis_key, user_id)
    except RankIndexUnavailableError:
        return None
