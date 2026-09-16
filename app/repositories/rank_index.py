"""The Redis rank index: derived, rebuildable, never the source of truth.

Writes here are deliberately *idempotent and order-independent*, which is what
lets the outbox (Spec.md D4) deliver at-least-once with no consistency
protocol. The mechanism is ``ZADD ... GT``, verified against Redis 8:

    ZADD k GT 900 p   -> 900     raises
    ZADD k GT 300 p   -> 900     never lowers
    ZADD k GT 900 p   -> 900     replay is a no-op
    GT on a new member inserts it

So a retried sync, or two syncs delivered out of order, cannot regress a
standing. Phase 3 adds the read side (windowed queries via Lua, and the
Postgres fallback).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger
from app.domain.periods import Period

logger = get_logger(__name__)

# Bounded memory for time-windowed boards. Safe *because* the index is derived:
# an expired key rebuilds from Postgres on the next read (Spec.md D2).
#
# Both are generously longer than the window they cover, so "yesterday's
# leaderboard" and "last week's" stay instantly available, which is what
# players actually ask for.
BOARD_TTL: dict[Period, timedelta | None] = {
    Period.ALL_TIME: None,  # never expires; it is the canonical board
    Period.DAILY: timedelta(days=8),
    Period.WEEKLY: timedelta(weeks=5),
}

# Minimum Redis version. GT was added in 6.2; without it this module would
# silently fall back to unconditional ZADD, which *can* lower a score and would
# make out-of-order delivery corrupting.
MIN_REDIS_VERSION = (6, 2)


class RankIndexUnavailableError(RuntimeError):
    """The index could not be reached or written.

    Never fatal to a submission: the score is already durable in Postgres and
    the outbox row guarantees the index catches up.
    """


@dataclass(frozen=True, slots=True)
class IndexUpdate:
    """One standing to apply to the index."""

    redis_key: str
    user_id: str
    score: int
    period: Period | None = None

    @property
    def ttl(self) -> timedelta | None:
        if self.period is None:
            return None
        return BOARD_TTL.get(self.period)


async def apply_updates(client: Redis, updates: list[IndexUpdate]) -> int:
    """Apply standings to the index. Returns the number of keys written.

    All updates go in a single pipeline: one network round trip regardless of
    the fan-out, which matters because every submission produces three of them.

    Raises:
        RankIndexUnavailableError: On any Redis failure. The caller is expected
            to continue — the outbox will retry.
    """
    if not updates:
        return 0

    try:
        pipe = client.pipeline(transaction=False)
        for update in updates:
            # gt=True is the whole design. Without it a stale replay could
            # lower a live standing.
            pipe.zadd(update.redis_key, {update.user_id: update.score}, gt=True)
            ttl = update.ttl
            if ttl is not None:
                # Refreshed on every write, so an active board's TTL always
                # measures from its last activity rather than its creation.
                pipe.expire(update.redis_key, ttl)
        await pipe.execute()
    except (RedisError, OSError, TimeoutError) as exc:
        logger.warning(
            "rank_index.write_failed",
            error_type=type(exc).__name__,
            keys=len(updates),
        )
        raise RankIndexUnavailableError(str(exc)) from exc

    return len(updates)


async def get_rank(client: Redis, redis_key: str, user_id: str) -> int | None:
    """1-based rank on a board, or None if the user is not on it.

    Redis' ZREVRANK is 0-based; the API is 1-based, and this is the single
    place that conversion happens. Verified equal to Postgres'
    ``row_number() OVER (ORDER BY score DESC, user_id DESC)``.
    """
    try:
        rank = await client.zrevrank(redis_key, user_id)
    except (RedisError, OSError, TimeoutError) as exc:
        raise RankIndexUnavailableError(str(exc)) from exc
    # redis-py types this as a union because the same call can be pipelined.
    # Against a live client it is an int or None.
    if not isinstance(rank, int):
        return None
    return rank + 1


async def board_size(client: Redis, redis_key: str) -> int:
    """Number of ranked users on a board. O(1) via ZCARD."""
    try:
        return int(await client.zcard(redis_key))
    except (RedisError, OSError, TimeoutError) as exc:
        raise RankIndexUnavailableError(str(exc)) from exc


async def supports_gt(client: Redis) -> bool:
    """Whether the server implements ``ZADD GT`` (Redis >= 6.2).

    Checked at startup rather than assumed. On an older server every write
    would silently become an unconditional ZADD, and out-of-order outbox
    delivery would then corrupt standings instead of converging — a failure
    that would look like random score regressions under load.
    """
    try:
        info = await client.info("server")
        # Valkey reports `redis_version` as a compatibility alias (Valkey 8
        # answers "7.2.4"), so that field is checked first and works today.
        # `valkey_version` is the fallback in case a future release drops it,
        # since Valkey has supported GT since its fork point.
        raw = str(info.get("redis_version") or info.get("valkey_version") or "0.0")
        parts = tuple(int(part) for part in raw.split(".")[:2])
    except (RedisError, OSError, TimeoutError, ValueError):
        return False
    return parts >= MIN_REDIS_VERSION
