"""Board identity and period bucketing (Spec.md D5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.periods import (
    ALL_TIME_BUCKET,
    BoardKey,
    InvalidBucketError,
    Period,
    all_buckets_for,
    bucket_for,
    is_future_bucket,
    validate_bucket,
)


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


class TestBucketFor:
    def test_all_time_is_a_constant(self) -> None:
        assert bucket_for(Period.ALL_TIME, utc(2026, 9, 16, 14, 22)) == ALL_TIME_BUCKET

    def test_daily_is_the_utc_date(self) -> None:
        assert bucket_for(Period.DAILY, utc(2026, 9, 16, 14, 22)) == "2026-09-16"

    def test_weekly_is_the_iso_week(self) -> None:
        assert bucket_for(Period.WEEKLY, utc(2026, 9, 16, 14, 22)) == "2026-W38"

    def test_non_utc_input_is_converted_not_truncated(self) -> None:
        """23:30 on the 16th in UTC+10 is 13:30 on the 16th UTC."""
        moment = datetime(2026, 9, 16, 23, 30, tzinfo=timezone(timedelta(hours=10)))
        assert bucket_for(Period.DAILY, moment) == "2026-09-16"

    def test_conversion_can_move_the_day(self) -> None:
        """02:00 on the 17th in UTC+10 is still the 16th in UTC."""
        moment = datetime(2026, 9, 17, 2, 0, tzinfo=timezone(timedelta(hours=10)))
        assert bucket_for(Period.DAILY, moment) == "2026-09-16"

    def test_naive_datetime_is_rejected(self) -> None:
        """Assuming a timezone silently files scores under the wrong day."""
        with pytest.raises(ValueError, match="timezone-aware"):
            bucket_for(Period.DAILY, datetime(2026, 9, 16, 14, 22))

    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            (utc(2026, 9, 16, 0, 0, 0), "2026-09-16"),
            (utc(2026, 9, 16, 23, 59, 59), "2026-09-16"),
            (utc(2026, 9, 17, 0, 0, 0), "2026-09-17"),
        ],
    )
    def test_daily_boundaries_are_exact(self, moment: datetime, expected: str) -> None:
        assert bucket_for(Period.DAILY, moment) == expected


class TestIsoWeekBoundaries:
    """The ISO year differs from the calendar year at year boundaries.

    Using `.year` instead of the ISO year here would split a single week's
    leaderboard across two buckets that share no scores.
    """

    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            # 2027-01-01 is a Friday, in ISO week 53 *of 2026*.
            (utc(2027, 1, 1), "2026-W53"),
            (utc(2027, 1, 3), "2026-W53"),  # Sunday, still W53 of 2026
            (utc(2027, 1, 4), "2027-W01"),  # Monday, now W01 of 2027
            # 2026-01-01 is a Thursday, in ISO week 1 of 2026.
            (utc(2026, 1, 1), "2026-W01"),
            # 2025-12-29 is a Monday already in ISO week 1 *of 2026*.
            (utc(2025, 12, 29), "2026-W01"),
            (utc(2025, 12, 28), "2025-W52"),
        ],
    )
    def test_iso_year_is_used_not_calendar_year(self, moment: datetime, expected: str) -> None:
        assert bucket_for(Period.WEEKLY, moment) == expected

    def test_a_week_spanning_new_year_is_one_bucket(self) -> None:
        """The decisive property: all seven days share a bucket."""
        buckets = {
            bucket_for(Period.WEEKLY, utc(2026, 12, 28) + timedelta(days=offset))
            for offset in range(7)
        }
        assert buckets == {"2026-W53"}, "a single ISO week must be one bucket"

    def test_consecutive_days_never_skip_a_week_bucket(self) -> None:
        """Walk two years of days; each bucket change must be to a new week."""
        seen: list[str] = []
        for offset in range(730):
            bucket = bucket_for(Period.WEEKLY, utc(2025, 6, 1) + timedelta(days=offset))
            if not seen or seen[-1] != bucket:
                seen.append(bucket)
        assert len(seen) == len(set(seen)), "a week bucket was revisited after changing"


class TestFanOut:
    def test_one_submission_touches_every_period(self) -> None:
        """D5's 3x write amplification, stated as a test."""
        result = all_buckets_for(utc(2026, 9, 16, 14, 22))
        assert result == (
            (Period.ALL_TIME, "ALL"),
            (Period.DAILY, "2026-09-16"),
            (Period.WEEKLY, "2026-W38"),
        )

    def test_fan_out_covers_the_full_enum(self) -> None:
        """Adding a period must not silently skip the write path."""
        assert {period for period, _ in all_buckets_for(utc(2026, 9, 16))} == set(Period)


class TestBoardKey:
    def test_redis_key_format(self) -> None:
        key = BoardKey("chess", Period.DAILY, "2026-09-16")
        assert key.redis_key == "lb:chess:daily:2026-09-16"

    def test_all_time_redis_key(self) -> None:
        assert BoardKey("chess", Period.ALL_TIME, "ALL").redis_key == "lb:chess:all_time:ALL"

    def test_distinct_boards_never_collide(self) -> None:
        keys = {
            BoardKey("chess", period, bucket).redis_key
            for period, bucket in all_buckets_for(utc(2026, 9, 16))
        } | {
            BoardKey("tetris", period, bucket).redis_key
            for period, bucket in all_buckets_for(utc(2026, 9, 16))
        }
        assert len(keys) == 6

    def test_is_hashable_and_frozen(self) -> None:
        key = BoardKey("chess", Period.ALL_TIME, "ALL")
        assert len({key, BoardKey("chess", Period.ALL_TIME, "ALL")}) == 1
        with pytest.raises(AttributeError):
            key.game_id = "tetris"  # type: ignore[misc]

    @given(
        game=st.from_regex(r"\A[a-z0-9][a-z0-9-]{0,20}\Z"),
        bucket=st.sampled_from(["ALL", "2026-09-16", "2026-W38"]),
    )
    def test_redis_key_has_exactly_four_segments(self, game: str, bucket: str) -> None:
        """No identifier can inject an extra ':' and forge another board's key."""
        key = BoardKey(game, Period.DAILY, bucket).redis_key
        assert len(key.split(":")) == 4


class TestValidateBucket:
    @pytest.mark.parametrize(
        ("period", "bucket"),
        [
            (Period.ALL_TIME, "ALL"),
            (Period.DAILY, "2026-09-16"),
            (Period.DAILY, "2024-02-29"),  # a real leap day
            (Period.WEEKLY, "2026-W38"),
            (Period.WEEKLY, "2026-W53"),  # 2026 is a long ISO year
            (Period.WEEKLY, "2026-W01"),
        ],
    )
    def test_accepts_well_formed(self, period: Period, bucket: str) -> None:
        assert validate_bucket(period, bucket) == bucket

    @pytest.mark.parametrize(
        ("period", "bucket"),
        [
            (Period.ALL_TIME, "2026-09-16"),
            (Period.DAILY, "ALL"),
            (Period.DAILY, "2026-W38"),  # right shape, wrong period
            (Period.DAILY, "2026-02-30"),  # matches the regex, not a real date
            (Period.DAILY, "2026-13-01"),
            (Period.DAILY, "26-09-16"),
            (Period.WEEKLY, "2026-09-16"),
            (Period.WEEKLY, "2026-W00"),
            (Period.WEEKLY, "2026-W54"),
            (Period.WEEKLY, "2025-W53"),  # 2025 has only 52 ISO weeks
            (Period.WEEKLY, "2026-W5"),
            (Period.DAILY, ""),
        ],
    )
    def test_rejects_malformed(self, period: Period, bucket: str) -> None:
        with pytest.raises(InvalidBucketError):
            validate_bucket(period, bucket)

    def test_mismatched_period_and_bucket_is_an_error_not_an_empty_board(self) -> None:
        """period=daily&bucket=2026-W38 must not answer 'no scores yet'."""
        with pytest.raises(InvalidBucketError, match="YYYY-MM-DD"):
            validate_bucket(Period.DAILY, "2026-W38")

    @given(st.datetimes(timezones=st.just(UTC)))
    def test_every_generated_bucket_validates(self, moment: datetime) -> None:
        """Round-trip property: what we produce, we accept."""
        for period in Period:
            bucket = bucket_for(period, moment)
            assert validate_bucket(period, bucket) == bucket


class TestFutureBucket:
    def test_all_time_is_never_future(self) -> None:
        assert is_future_bucket(Period.ALL_TIME, "ALL") is False

    def test_today_is_not_future(self) -> None:
        now = utc(2026, 9, 16, 12, 0)
        assert is_future_bucket(Period.DAILY, "2026-09-16", now=now) is False

    def test_tomorrow_is_future(self) -> None:
        now = utc(2026, 9, 16, 12, 0)
        assert is_future_bucket(Period.DAILY, "2026-09-17", now=now) is True

    def test_yesterday_is_not_future(self) -> None:
        now = utc(2026, 9, 16, 12, 0)
        assert is_future_bucket(Period.DAILY, "2026-09-15", now=now) is False

    def test_weekly_comparison_survives_the_iso_year_boundary(self) -> None:
        """On 2027-01-01 the current week is 2026-W53, and 2027-W01 is future."""
        now = utc(2027, 1, 1, 12, 0)
        assert is_future_bucket(Period.WEEKLY, "2026-W53", now=now) is False
        assert is_future_bucket(Period.WEEKLY, "2027-W01", now=now) is True
        assert is_future_bucket(Period.WEEKLY, "2026-W52", now=now) is False
