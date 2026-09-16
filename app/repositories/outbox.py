"""Outbox access: claim pending index work, mark it done, prune (Spec.md D4)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Claim a batch of undelivered rows.
#
# FOR UPDATE SKIP LOCKED is what makes this safe with more than one instance
# running: each sweeper locks the rows it claims, and concurrent sweepers skip
# those rows instead of blocking on them. Without SKIP LOCKED, N instances
# would serialise behind the same batch and do the same work N times.
#
# Ordered by enqueued_at so the oldest backlog drains first, and driven by the
# partial index ix_redis_outbox_pending, which covers only undelivered rows —
# keeping the scan proportional to the backlog rather than to total history.
_CLAIM_PENDING = text(
    """
    SELECT id, redis_key, user_id, score, attempts
    FROM redis_outbox
    WHERE delivered_at IS NULL
    ORDER BY enqueued_at
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
    """
)

_MARK_DELIVERED = text("UPDATE redis_outbox SET delivered_at = now() WHERE id = ANY(:ids)")

_RECORD_FAILURE = text("UPDATE redis_outbox SET attempts = attempts + 1 WHERE id = ANY(:ids)")

# Delivered rows are history, not state. Pruned on a schedule so the table
# does not grow without bound — at three rows per submission it would
# otherwise become the largest table in the database.
_PRUNE_DELIVERED = text(
    """
    DELETE FROM redis_outbox
    WHERE id IN (
        SELECT id FROM redis_outbox
        WHERE delivered_at IS NOT NULL
          AND delivered_at < now() - make_interval(hours => :retain_hours)
        LIMIT :limit
    )
    """
)


@dataclass(frozen=True, slots=True)
class PendingSync:
    id: int
    redis_key: str
    user_id: str
    score: int
    attempts: int


async def claim_pending(conn: AsyncConnection, *, batch_size: int = 100) -> list[PendingSync]:
    """Lock and return a batch of undelivered rows.

    The caller must keep the transaction open until it has recorded the
    outcome: the row locks are what prevent another sweeper from picking up
    the same work.
    """
    result = await conn.execute(_CLAIM_PENDING, {"batch_size": batch_size})
    return [
        PendingSync(id=row[0], redis_key=row[1], user_id=row[2], score=row[3], attempts=row[4])
        for row in result
    ]


async def mark_delivered(conn: AsyncConnection, ids: list[int]) -> None:
    if ids:
        await conn.execute(_MARK_DELIVERED, {"ids": ids})


async def record_failure(conn: AsyncConnection, ids: list[int]) -> None:
    """Increment the attempt counter without marking the rows delivered.

    A rising attempts value on old rows is the clearest single signal that the
    rank index is drifting away from Postgres.
    """
    if ids:
        await conn.execute(_RECORD_FAILURE, {"ids": ids})


async def prune_delivered(
    conn: AsyncConnection, *, retain_hours: int = 24, limit: int = 10_000
) -> int:
    """Delete old delivered rows. Returns how many were removed.

    Bounded by ``limit`` so a large backlog is cleared over several passes
    instead of one long-running DELETE holding locks.
    """
    result = await conn.execute(_PRUNE_DELIVERED, {"retain_hours": retain_hours, "limit": limit})
    return int(result.rowcount or 0)


async def pending_stats(conn: AsyncConnection) -> tuple[int, float | None]:
    """(undelivered count, age in seconds of the oldest undelivered row).

    The metric pair worth alerting on: a rising count means the index is
    falling behind, and a rising oldest-age means something is stuck rather
    than merely busy.
    """
    result = await conn.execute(
        text(
            """
            SELECT count(*),
                   COALESCE(
                       EXTRACT(EPOCH FROM (now() - min(enqueued_at))),
                       0
                   )::float
            FROM redis_outbox
            WHERE delivered_at IS NULL
            """
        )
    )
    row = result.one()
    count = int(row[0])
    return count, (float(row[1]) if count else None)
