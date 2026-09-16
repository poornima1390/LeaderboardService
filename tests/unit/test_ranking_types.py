"""Ranking value types, in particular the percentile boundaries."""

from __future__ import annotations

import pytest

from app.repositories.ranking import RankedEntry, UserContext


def context(rank: int, total: int) -> UserContext:
    return UserContext(
        user=RankedEntry(rank=rank, user_id="u", score=0),
        above=(),
        below=(),
        total=total,
    )


class TestPercentile:
    def test_rank_one_is_exactly_one_hundred(self) -> None:
        """The obvious `(total - rank) / total` gives 99.9995 here, which
        rounds to 100.0 only by luck and reads as a bug in a response."""
        assert context(rank=1, total=184_203).percentile == 100.0

    def test_single_entry_board_is_one_hundred(self) -> None:
        """`(total - rank) / total` gives 0.0 for the only player on a board —
        the second boundary the naive formula gets wrong."""
        assert context(rank=1, total=1).percentile == 100.0

    def test_last_place_is_above_zero_not_zero(self) -> None:
        """Last place is still ahead of nobody, but is not 'worse than 100%'."""
        assert context(rank=7, total=7).percentile == pytest.approx(14.29, abs=0.01)

    def test_mid_board(self) -> None:
        # rank 5 of 7 -> (1 - 4/7) * 100
        assert context(rank=5, total=7).percentile == 42.86

    def test_large_board_midpoint(self) -> None:
        assert context(rank=500, total=1000).percentile == pytest.approx(50.1, abs=0.1)

    def test_rounded_to_two_decimals(self) -> None:
        value = context(rank=12, total=3891).percentile
        assert value == 99.72
        assert len(str(value).split(".")[1]) <= 2

    def test_empty_board_does_not_divide_by_zero(self) -> None:
        """Unreachable in practice — a ranked user implies a non-empty board —
        but a ZeroDivisionError in a response path is not an acceptable
        failure mode for an impossible case."""
        assert context(rank=1, total=0).percentile == 0.0

    @pytest.mark.parametrize("total", [1, 2, 10, 1000])
    def test_percentile_decreases_monotonically_with_rank(self, total: int) -> None:
        values = [context(rank=rank, total=total).percentile for rank in range(1, total + 1)]
        assert values == sorted(values, reverse=True)
        assert values[0] == 100.0
        assert all(0 < value <= 100 for value in values)


class TestRankedEntry:
    def test_is_frozen(self) -> None:
        entry = RankedEntry(rank=1, user_id="u", score=5)
        with pytest.raises(AttributeError):
            entry.rank = 2  # type: ignore[misc]

    def test_equality_is_structural(self) -> None:
        """The differential tests compare these directly, so value equality
        has to mean what it appears to mean."""
        assert RankedEntry(1, "u", 5) == RankedEntry(1, "u", 5)
        assert RankedEntry(1, "u", 5) != RankedEntry(2, "u", 5)
