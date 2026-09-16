"""Ranking served from Redis sorted sets — the fast path (Spec.md D2).

Every operation is logarithmic or constant:

    top(limit, offset)   ZREVRANGE   O(log N + limit)
    around(user, window) ZREVRANK    O(log N) + O(window)
    size()               ZCARD       O(1)

Both compound reads run as Lua scripts rather than as a sequence of commands.
That is not a micro-optimisation: a script executes atomically, so the rank,
the window and the total come from one consistent snapshot. Issued separately,
a concurrent ZADD could land between the ZREVRANK and the ZREVRANGE and return
a window that does not actually contain the user at the rank just reported.
"""

from __future__ import annotations

from typing import Any, Final

from redis.asyncio import Redis
from redis.commands.core import AsyncScript
from redis.exceptions import RedisError

from app.domain.periods import BoardKey
from app.repositories.ranking import BoardPage, RankedEntry, UserContext

SOURCE: Final = "redis"


class RankIndexReadError(RuntimeError):
    """The index could not answer. The caller falls back to Postgres."""


# Returns: { zero_based_rank, window_start, total, [member, score, ...] }
#
# `if not rank` covers ZREVRANK's Lua return of `false` for an absent member
# (it is false, not nil). Returning false propagates to None in redis-py.
_AROUND_LUA: Final = """
local rank = redis.call('ZREVRANK', KEYS[1], ARGV[1])
if not rank then return false end
local window = tonumber(ARGV[2])
local start = rank - window
if start < 0 then start = 0 end
local rows = redis.call('ZREVRANGE', KEYS[1], start, rank + window, 'WITHSCORES')
return { rank, start, redis.call('ZCARD', KEYS[1]), rows }
"""

# Returns: { total, [member, score, ...] }
#
# Scripted for the same reason: `total` must describe the same snapshot the
# rows came from, or a client paginating a busy board sees rows and totals
# that cannot both be true.
_TOP_LUA: Final = """
local stop = tonumber(ARGV[1]) + tonumber(ARGV[2]) - 1
local rows = redis.call('ZREVRANGE', KEYS[1], ARGV[1], stop, 'WITHSCORES')
return { redis.call('ZCARD', KEYS[1]), rows }
"""


def _parse_pairs(raw: Any) -> list[tuple[str, int]]:
    """Turn a flat WITHSCORES reply into (member, score) pairs.

    Scores arrive as bulk strings, so they keep full precision on the way out
    of Lua — Lua numbers returned directly would be coerced to integers by
    Redis, which happens to be harmless here but is not a property to rely on.
    """
    flat = list(raw or [])
    return [(str(flat[index]), int(float(flat[index + 1]))) for index in range(0, len(flat) - 1, 2)]


class RedisRanking:
    """RankingRepository backed by Redis sorted sets."""

    def __init__(self, client: Redis) -> None:
        self._client = client
        # register_script uses EVALSHA and transparently falls back to EVAL if
        # the script is not cached — so a Redis restart or a cross-instance
        # deploy costs one EVAL, not an error.
        self._around: AsyncScript = client.register_script(_AROUND_LUA)
        self._top: AsyncScript = client.register_script(_TOP_LUA)

    @property
    def source(self) -> str:
        return SOURCE

    async def size(self, board: BoardKey) -> int:
        try:
            return int(await self._client.zcard(board.redis_key))
        except (RedisError, OSError, TimeoutError) as exc:
            raise RankIndexReadError(str(exc)) from exc

    async def top(self, board: BoardKey, *, limit: int, offset: int) -> BoardPage:
        try:
            reply = await self._top(keys=[board.redis_key], args=[offset, limit])
        except (RedisError, OSError, TimeoutError) as exc:
            raise RankIndexReadError(str(exc)) from exc

        total = int(reply[0])
        pairs = _parse_pairs(reply[1])
        # Ranks are dense and derivable because the ordering is a strict total
        # order (Spec.md D3): no two entries can share a position.
        entries = tuple(
            RankedEntry(rank=offset + index + 1, user_id=user_id, score=score)
            for index, (user_id, score) in enumerate(pairs)
        )
        return BoardPage(entries=entries, total=total)

    async def around(self, board: BoardKey, user_id: str, *, window: int) -> UserContext | None:
        try:
            reply = await self._around(keys=[board.redis_key], args=[user_id, window])
        except (RedisError, OSError, TimeoutError) as exc:
            raise RankIndexReadError(str(exc)) from exc

        if not reply:
            return None

        zero_based_rank, window_start, total = (
            int(reply[0]),
            int(reply[1]),
            int(reply[2]),
        )
        pairs = _parse_pairs(reply[3])

        entries = [
            RankedEntry(rank=window_start + index + 1, user_id=member, score=score)
            for index, (member, score) in enumerate(pairs)
        ]
        target_rank = zero_based_rank + 1

        user = next(entry for entry in entries if entry.rank == target_rank)
        return UserContext(
            user=user,
            above=tuple(entry for entry in entries if entry.rank < target_rank),
            below=tuple(entry for entry in entries if entry.rank > target_rank),
            total=total,
        )
