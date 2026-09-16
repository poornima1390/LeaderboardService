"""Leaderboard identity: game x period x bucket (Spec.md D5).

A board is identified by three values, and one score submission fans out to
one board per period — all-time, today's daily board and this week's weekly
board. Best-score semantics apply *per board*, so a submission can improve
today's standing without touching the all-time one.

All bucketing is UTC. Local-time period boundaries ("the daily board resets at
midnight in the player's timezone") are a real product requirement that is
explicitly out of scope (Spec.md §9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Final

ALL_TIME_BUCKET: Final = "ALL"

_DAILY_BUCKET_RE: Final = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_WEEKLY_BUCKET_RE: Final = re.compile(r"^(\d{4})-W(\d{2})$")


class Period(StrEnum):
    """The time windows a board can cover.

    Stored in Postgres as ``text`` with a CHECK constraint rather than a native
    ``ENUM`` type. Adding a value to a Postgres enum cannot be done in the same
    transaction as a statement that uses it, and removing one is not supported
    at all; a CHECK constraint is altered with a single ordinary migration.
    """

    ALL_TIME = "all_time"
    DAILY = "daily"
    WEEKLY = "weekly"


class InvalidBucketError(ValueError):
    """A bucket string does not match the format its period requires."""


@dataclass(frozen=True, slots=True)
class BoardKey:
    """A fully-qualified leaderboard identity.

    Frozen so it can be a dict key and cannot be mutated after the Redis key
    and the Postgres primary key have been derived from it — those two must
    always describe the same board.
    """

    game_id: str
    period: Period
    bucket: str

    @property
    def redis_key(self) -> str:
        """The Redis sorted set key for this board.

        Format: ``lb:{game}:{period}:{bucket}``. ``game_id`` and ``bucket`` are
        drawn from restricted charsets that exclude ``:`` (see
        app.domain.identifiers), so the key is unambiguous and cannot be forged
        by a crafted identifier.

        Scaling note: the first move under load is Redis Cluster sharding by
        game, which needs a hash tag — ``lb:{chess}:all_time:ALL``. That is a
        key rename, so it is a migration rather than a config change; the
        braces are deliberately not added now because they would appear in
        every key for a capability nothing yet uses (Spec.md §8).
        """
        return f"lb:{self.game_id}:{self.period.value}:{self.bucket}"

    def __str__(self) -> str:
        return f"{self.game_id}/{self.period.value}/{self.bucket}"


def bucket_for(period: Period, moment: datetime) -> str:
    """Return the bucket ``moment`` falls into for ``period``.

    Args:
        period: The time window.
        moment: An aware datetime. Naive input is rejected rather than assumed
            to be UTC — silently guessing a timezone is how scores land in the
            wrong day's bucket for eight hours.

    Raises:
        ValueError: If ``moment`` is naive.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("moment must be timezone-aware; refusing to assume a timezone")

    if period is Period.ALL_TIME:
        return ALL_TIME_BUCKET

    utc_date = moment.astimezone(UTC).date()
    if period is Period.DAILY:
        return utc_date.isoformat()
    return _iso_week_bucket(utc_date)


def _iso_week_bucket(utc_date: date) -> str:
    """Format a date as ``YYYY-Www`` using the **ISO** year, not the calendar year.

    These differ at year boundaries: 2027-01-01 falls in ISO week 53 of 2026.
    Using ``utc_date.year`` here would label that day ``2027-W53`` — a bucket
    that shares no scores with the ``2026-W53`` bucket its neighbours land in,
    silently splitting one week's leaderboard in two.
    """
    iso_year, iso_week, _ = utc_date.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def all_buckets_for(moment: datetime) -> tuple[tuple[Period, str], ...]:
    """Every (period, bucket) a submission at ``moment`` must update.

    This is D5's write fan-out made explicit: one submission touches three
    boards, which is where the service's fixed 3x write amplification comes
    from.
    """
    return tuple((period, bucket_for(period, moment)) for period in Period)


def validate_bucket(period: Period, bucket: str) -> str:
    """Check that ``bucket`` is well-formed for ``period``, returning it.

    A mismatched pair such as ``period=daily&bucket=2026-W38`` is rejected: it
    would otherwise query a board that can never contain rows and return an
    empty leaderboard, which looks like "no scores yet" rather than "you asked
    the wrong question".

    Raises:
        InvalidBucketError: If the bucket does not match the period's format,
            or encodes a date that does not exist.
    """
    if period is Period.ALL_TIME:
        if bucket != ALL_TIME_BUCKET:
            raise InvalidBucketError(f"all_time takes bucket {ALL_TIME_BUCKET!r}, got {bucket!r}")
        return bucket

    if period is Period.DAILY:
        if not _DAILY_BUCKET_RE.match(bucket):
            raise InvalidBucketError(f"daily bucket must be YYYY-MM-DD, got {bucket!r}")
        try:
            date.fromisoformat(bucket)
        except ValueError as exc:
            # Catches 2026-02-30 and friends, which match the regex but are
            # not real dates.
            raise InvalidBucketError(f"{bucket!r} is not a valid date") from exc
        return bucket

    match = _WEEKLY_BUCKET_RE.match(bucket)
    if not match:
        raise InvalidBucketError(f"weekly bucket must be YYYY-Www, got {bucket!r}")
    iso_year, iso_week = int(match.group(1)), int(match.group(2))
    if not 1 <= iso_week <= 53:
        raise InvalidBucketError(f"ISO week must be 01-53, got {iso_week:02d}")
    try:
        # Week 53 exists only in long ISO years; this rejects e.g. 2026-W53.
        date.fromisocalendar(iso_year, iso_week, 1)
    except ValueError as exc:
        raise InvalidBucketError(f"ISO week {iso_week:02d} does not exist in {iso_year}") from exc
    return bucket


def is_future_bucket(period: Period, bucket: str, *, now: datetime | None = None) -> bool:
    """Whether ``bucket`` lies entirely in the future.

    Querying a future board is always a client mistake, and answering it with
    an empty leaderboard hides that.
    """
    if period is Period.ALL_TIME:
        return False
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    return bucket > bucket_for(period, reference)
