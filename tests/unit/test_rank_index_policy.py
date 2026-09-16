"""Index key TTL policy (Spec.md §8)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.periods import Period
from app.repositories.rank_index import BOARD_TTL, MIN_REDIS_VERSION, IndexUpdate


class TestTtlPolicy:
    def test_all_time_never_expires(self) -> None:
        """It is the canonical board; expiring it would force a rebuild for
        the most-read data in the service."""
        assert BOARD_TTL[Period.ALL_TIME] is None
        assert IndexUpdate("k", "u", 1, Period.ALL_TIME).ttl is None

    @pytest.mark.parametrize(
        ("period", "minimum"),
        [(Period.DAILY, timedelta(days=1)), (Period.WEEKLY, timedelta(weeks=1))],
    )
    def test_windowed_boards_outlive_their_window(self, period: Period, minimum: timedelta) -> None:
        """ "Yesterday's leaderboard" is a thing players ask for, so a TTL equal
        to the window would expire a board while it is still interesting."""
        ttl = BOARD_TTL[period]
        assert ttl is not None
        assert ttl > minimum

    def test_every_period_has_an_explicit_policy(self) -> None:
        """A new period defaulting to 'no TTL' would leak Redis memory forever."""
        assert set(BOARD_TTL) == set(Period)

    def test_update_without_a_period_has_no_ttl(self) -> None:
        """Sweeper replays carry no period — they must not reset or add a TTL,
        since the key's lifetime was already set by the original write."""
        assert IndexUpdate("k", "u", 1).ttl is None


def test_gt_requires_redis_6_2() -> None:
    """ZADD GT is what makes outbox delivery idempotent; it landed in 6.2."""
    assert MIN_REDIS_VERSION == (6, 2)
