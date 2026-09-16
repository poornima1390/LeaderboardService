"""Background drain of undelivered index work (Spec.md D4).

The sweeper is what turns "Redis was down when the score was written" into a
few seconds of staleness rather than a permanently wrong leaderboard. It is
allowed to be simple and careless about duplicates, because ``ZADD GT`` is
idempotent and order-independent — retrying delivered work costs a no-op.

Correctness rests on one detail: rows are claimed with ``FOR UPDATE SKIP
LOCKED`` inside the same transaction that settles them, so multiple instances
sweep in parallel without duplicating or blocking each other.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.logging import get_logger
from app.repositories import outbox, rank_index
from app.repositories.rank_index import IndexUpdate, RankIndexUnavailableError

logger = get_logger(__name__)

BATCH_SIZE = 200
PRUNE_EVERY_N_PASSES = 120
DELIVERED_RETENTION_HOURS = 24

# Back-off bounds for when Redis is unreachable. Without a ceiling a long
# outage would push the interval out to hours and leave the index stale long
# after recovery; without any back-off the sweeper would hammer a dead server
# and fill the log.
MAX_BACKOFF_MULTIPLIER = 12


@dataclass
class SweepStats:
    """Cumulative counters, surfaced for logging and tests."""

    passes: int = 0
    delivered: int = 0
    failed: int = 0
    pruned: int = 0


async def sweep_once(
    engine: AsyncEngine, redis_client: Redis, *, batch_size: int = BATCH_SIZE
) -> tuple[int, int]:
    """Drain one batch. Returns (delivered, failed).

    The claim, the Redis write and the settle all happen inside one
    transaction. Holding it across the Redis call is deliberate: it keeps the
    row locks until the outcome is known, so a crash mid-batch rolls back to
    undelivered and the work is simply retried.
    """
    delivered: list[int] = []
    failed: list[int] = []

    async with engine.begin() as conn:
        pending = await outbox.claim_pending(conn, batch_size=batch_size)
        if not pending:
            return 0, 0

        updates = [
            IndexUpdate(redis_key=row.redis_key, user_id=row.user_id, score=row.score)
            for row in pending
        ]
        try:
            await rank_index.apply_updates(redis_client, updates)
            delivered = [row.id for row in pending]
        except RankIndexUnavailableError:
            failed = [row.id for row in pending]

        await outbox.mark_delivered(conn, delivered)
        await outbox.record_failure(conn, failed)

    return len(delivered), len(failed)


async def run_sweeper(
    engine: AsyncEngine,
    redis_client: Redis | None,
    *,
    interval_s: float,
    stats: SweepStats | None = None,
) -> None:
    """Sweep forever on an interval. Intended to run as a background task.

    Exits immediately when Redis is not configured — there is no index to
    sync, and the service is knowingly serving in degraded mode.

    Never raises: an exception escaping here would kill the task silently and
    the index would stop converging with no signal beyond a slowly rising
    backlog.
    """
    if redis_client is None:
        logger.info(
            "outbox_sweeper.disabled",
            reason="Redis is not configured; no rank index to synchronise",
        )
        return

    stats = stats or SweepStats()
    backoff = 1
    logger.info("outbox_sweeper.started", interval_s=interval_s)

    try:
        while True:
            await asyncio.sleep(interval_s * backoff)
            try:
                delivered, failed = await sweep_once(engine, redis_client)
                stats.passes += 1
                stats.delivered += delivered
                stats.failed += failed

                if failed:
                    backoff = min(backoff * 2, MAX_BACKOFF_MULTIPLIER)
                    logger.warning(
                        "outbox_sweeper.deferred",
                        failed=failed,
                        backoff_multiplier=backoff,
                    )
                else:
                    if backoff != 1 and delivered:
                        logger.info("outbox_sweeper.recovered", delivered=delivered)
                    backoff = 1
                    if delivered:
                        logger.info("outbox_sweeper.delivered", rows=delivered)

                if stats.passes % PRUNE_EVERY_N_PASSES == 0:
                    async with engine.begin() as conn:
                        removed = await outbox.prune_delivered(
                            conn, retain_hours=DELIVERED_RETENTION_HOURS
                        )
                    stats.pruned += removed
                    if removed:
                        logger.info("outbox_sweeper.pruned", rows=removed)

            except asyncio.CancelledError:
                raise
            except Exception:
                # Postgres unreachable, or anything else unexpected. Log and
                # keep the loop alive; the backlog is durable and waiting.
                backoff = min(backoff * 2, MAX_BACKOFF_MULTIPLIER)
                logger.exception("outbox_sweeper.pass_failed", backoff_multiplier=backoff)
    except asyncio.CancelledError:
        logger.info(
            "outbox_sweeper.stopped",
            passes=stats.passes,
            delivered=stats.delivered,
        )
        raise
