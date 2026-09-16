"""The ranking contract, and the types both implementations must agree on.

Spec.md D2 ships *two* implementations of this protocol:

* :class:`~app.repositories.redis_ranking.RedisRanking` — the serving path.
  ``O(log N)`` for every operation.
* :class:`~app.repositories.postgres_ranking.PostgresRanking` — the degraded
  path when the index is cold or unreachable, the oracle for rebuilds, and the
  reference implementation the differential tests compare against.

Keeping both is not redundancy. The rebuild path needs the Postgres queries
anyway, and having two independent implementations of one contract turns
cross-store consistency from something we hope for into something CI asserts.

Deliberately absent from these types: ``display_name`` and ``achieved_at``.
Redis holds only ``(member, score)``, so those are hydrated from Postgres by
the service layer. That keeps the index with exactly one job — it never needs
invalidating when a player renames themselves — and it means the differential
tests compare exactly the fields both stores are responsible for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.periods import BoardKey


@dataclass(frozen=True, slots=True)
class RankedEntry:
    """One standing, with its position on the board.

    ``rank`` is **1-based**. Redis' ZREVRANK is 0-based, and the conversion
    happens once, inside RedisRanking — an off-by-one here would be invisible
    until a user noticed they were "rank 0".
    """

    rank: int
    user_id: str
    score: int


@dataclass(frozen=True, slots=True)
class BoardPage:
    """A slice of a leaderboard."""

    entries: tuple[RankedEntry, ...]
    total: int


@dataclass(frozen=True, slots=True)
class UserContext:
    """A user's rank and their immediate surroundings (Spec.md §4).

    ``above`` and ``below`` are short at the boundaries — the top-ranked user
    has an empty ``above`` — and that is normal output, not an error.
    """

    user: RankedEntry
    above: tuple[RankedEntry, ...]
    below: tuple[RankedEntry, ...]
    total: int

    @property
    def percentile(self) -> float:
        """Share of the board at or below this user, as a percentage.

        ``(1 - (rank - 1) / total) * 100``, so rank 1 is exactly 100.0 and a
        single-entry board reads 100.0. The more obvious
        ``(total - rank) / total`` gets both of those boundaries wrong.
        """
        if self.total <= 0:
            return 0.0
        return round((1 - (self.user.rank - 1) / self.total) * 100, 2)


class RankingRepository(Protocol):
    """Read-side ranking operations, in canonical order (Spec.md D3).

    Every implementation must order by ``score DESC, user_id DESC`` compared
    byte-wise. That is not an implementation detail: it is the contract, and
    the reason two stores can answer the same question interchangeably.
    """

    @property
    def source(self) -> str:
        """Identifier returned to clients, for operational transparency."""
        ...

    async def size(self, board: BoardKey) -> int:
        """Number of ranked users on a board."""
        ...

    async def top(self, board: BoardKey, *, limit: int, offset: int) -> BoardPage:
        """A page of the leaderboard, highest first."""
        ...

    async def around(self, board: BoardKey, user_id: str, *, window: int) -> UserContext | None:
        """A user's rank plus up to ``window`` neighbours either side.

        Returns None when the user has no standing on this board — which the
        API surfaces as ``USER_NOT_RANKED``, distinct from an unknown game.
        """
        ...
