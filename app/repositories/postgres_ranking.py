"""Ranking served from Postgres — the degraded path and the reference.

Three jobs (Spec.md D2):

1. **Fallback** when the rank index is cold or unreachable, so a Redis outage
   degrades latency rather than correctness. This is the reason a cold key can
   never produce a silently empty leaderboard.
2. **Oracle** for the differential tests, which assert that this and
   :class:`RedisRanking` return identical answers for identical data.
3. Source of truth for index rebuilds.

Its cost is the honest reason Redis exists. Every query here is bounded by the
*rank* being asked about rather than by the size of the answer:

    top(limit, offset)   index-only scan, but OFFSET reads and discards
                         `offset` rows
    around(user, window) rank is COUNT(*) of every better-scoring row --
                         unbounded, and the operation the whole design is for
    size()               COUNT(*) over the board partition

Acceptable for a fallback, and precisely why it is not the serving path.

A note on the f-strings below: the only interpolated values are the
module-level ``_BOARD_FILTER`` and ``_ORDER_BY`` constants, and every
caller-supplied value is a bound parameter. ``_ORDER_BY`` is shared rather than
inlined so the canonical ordering cannot drift between these four queries,
which is why ruff's S608 is suppressed for this file in pyproject.toml.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.periods import BoardKey
from app.repositories.ranking import BoardPage, RankedEntry, UserContext

SOURCE: Final = "postgres"

# The canonical order (Spec.md D3), written out once.
#
# user_id DESC, not ASC: ZREVRANGE orders equal scores by member *descending*,
# and the columns are declared COLLATE "C" so the comparison is byte-wise like
# Redis' memcmp. Both halves of that are verified against real engines.
_ORDER_BY: Final = "score DESC, user_id DESC"

_BOARD_FILTER: Final = "game_id = :game_id AND period = :period AND period_bucket = :bucket"


class PostgresRanking:
    """RankingRepository backed by the leaderboard_entries table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @property
    def source(self) -> str:
        return SOURCE

    def _params(self, board: BoardKey) -> dict[str, str]:
        return {
            "game_id": board.game_id,
            "period": board.period.value,
            "bucket": board.bucket,
        }

    async def size(self, board: BoardKey) -> int:
        async with self._engine.connect() as conn:
            return await self._size(conn, board)

    async def _size(self, conn: AsyncConnection, board: BoardKey) -> int:
        result = await conn.scalar(
            text(f"SELECT count(*) FROM leaderboard_entries WHERE {_BOARD_FILTER}"),
            self._params(board),
        )
        return int(result or 0)

    async def top(self, board: BoardKey, *, limit: int, offset: int) -> BoardPage:
        # One connection for both statements, so `total` and the rows describe
        # the same snapshot rather than two moments a few milliseconds apart.
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT user_id, score FROM leaderboard_entries "
                    f"WHERE {_BOARD_FILTER} "
                    f"ORDER BY {_ORDER_BY} "
                    "OFFSET :offset LIMIT :limit"
                ),
                {**self._params(board), "offset": offset, "limit": limit},
            )
            rows = list(result)
            total = await self._size(conn, board)

        entries = tuple(
            RankedEntry(rank=offset + index + 1, user_id=row[0], score=int(row[1]))
            for index, row in enumerate(rows)
        )
        return BoardPage(entries=entries, total=total)

    async def around(self, board: BoardKey, user_id: str, *, window: int) -> UserContext | None:
        async with self._engine.connect() as conn:
            score = await conn.scalar(
                text(
                    "SELECT score FROM leaderboard_entries "
                    f"WHERE {_BOARD_FILTER} AND user_id = :user_id"
                ),
                {**self._params(board), "user_id": user_id},
            )
            if score is None:
                return None

            # Rank via COUNT of rows that sort ahead. The row-wise comparison
            # `(score, user_id) > (:score, :user_id)` expresses "sorts before
            # me under score DESC, user_id DESC" exactly, and is verified equal
            # to row_number() over the same ordering.
            #
            # This COUNT is the unbounded operation Spec.md D2 names as the
            # reason the serving path is Redis.
            better = await conn.scalar(
                text(
                    "SELECT count(*) FROM leaderboard_entries "
                    f"WHERE {_BOARD_FILTER} AND (score, user_id) > (:score, :user_id)"
                ),
                {**self._params(board), "score": score, "user_id": user_id},
            )
            rank = int(better or 0) + 1

            # The window is computed from its clamped bounds, not as a
            # fixed 2*window+1 rows.
            #
            # This distinction is not cosmetic, and the differential test
            # against RedisRanking is what surfaced it. Redis clamps `start`
            # to 0 but leaves `stop` at rank + window, so a window clamped at
            # the top of the board returns *fewer* rows. Taking a fixed
            # 2*window+1 rows here instead silently extended the window
            # downwards to compensate: the rank-1 user with window=2 got four
            # entries below them from Postgres and two from Redis.
            #
            # The contract is "up to `window` entries either side", so the
            # clamped behaviour is the correct one.
            zero_based_rank = rank - 1
            window_start = max(0, zero_based_rank - window)
            window_end = zero_based_rank + window
            result = await conn.execute(
                text(
                    "SELECT user_id, score FROM leaderboard_entries "
                    f"WHERE {_BOARD_FILTER} "
                    f"ORDER BY {_ORDER_BY} "
                    "OFFSET :offset LIMIT :limit"
                ),
                {
                    **self._params(board),
                    "offset": window_start,
                    "limit": window_end - window_start + 1,
                },
            )
            rows = list(result)
            total = await self._size(conn, board)

        entries = [
            RankedEntry(rank=window_start + index + 1, user_id=row[0], score=int(row[1]))
            for index, row in enumerate(rows)
        ]
        user = next(entry for entry in entries if entry.rank == rank)
        return UserContext(
            user=user,
            above=tuple(entry for entry in entries if entry.rank < rank),
            below=tuple(entry for entry in entries if entry.rank > rank),
            total=total,
        )
