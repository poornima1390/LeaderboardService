"""Database schema (Spec.md §3).

Four tables:

* ``games``  -- a registry, so an unknown slug is an error rather than a
  silently-created empty board.
* ``users``  -- caller-supplied identities, auto-created on first submission.
* ``leaderboard_entries`` -- one row per (board, user): that user's current
  best score on that board. The table the whole service exists to serve.
* ``redis_outbox`` -- durable intent to sync a standing into the rank index.

The single most important detail here is ``COLLATE "C"`` on every column whose
value becomes a Redis sorted set member. See :class:`LeaderboardEntry`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    desc,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.identifiers import (
    DISPLAY_NAME_MAX_LENGTH,
    GAME_ID_MAX_LENGTH,
    GAME_ID_PATTERN,
    GAME_NAME_MAX_LENGTH,
    MAX_SCORE,
    MIN_SCORE,
    USER_ID_MAX_LENGTH,
    USER_ID_PATTERN,
)
from app.domain.periods import Period
from app.models.base import Base, created_at_column

# Byte-wise comparison, matching Redis' `memcmp` on sorted set members.
#
# Declared on the *column* rather than per query, so every ORDER BY, index and
# comparison is byte-wise by construction and no future query can drift away
# from Redis. Verified empirically: inside a database whose own collation is
# en_US.utf8, a column carrying this collation orders identically to
# ZREVRANGE, while one using the database default does not (Spec.md D3).
BYTEWISE = "C"

# Postgres regex operator strings for the CHECK constraints. The patterns come
# from app.domain.identifiers so the database and the API cannot disagree about
# what a valid identifier is; the Python anchors translate directly.
_PG_GAME_ID_REGEX = GAME_ID_PATTERN.replace("\\-", "-")
_PG_USER_ID_REGEX = USER_ID_PATTERN.replace("\\-", "-")


class Game(Base):
    """A registered game. Boards exist only for games listed here."""

    __tablename__ = "games"

    # A readable slug as the primary key, not a surrogate UUID: it makes URLs
    # self-describing (/v1/games/chess/leaderboard) and removes a lookup from
    # the hot write path. The cost is that renaming a game is a migration —
    # acceptable, since the slug is an external contract.
    id: Mapped[str] = mapped_column(
        String(GAME_ID_MAX_LENGTH, collation=BYTEWISE),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(String(GAME_NAME_MAX_LENGTH), nullable=False)

    # Retired games keep their boards readable but accept no new scores (409).
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"id ~ '{_PG_GAME_ID_REGEX}'", name="id_format"),
        CheckConstraint("length(name) BETWEEN 1 AND 128", name="name_length"),
    )


class User(Base):
    """A player identity, supplied by the calling game server."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(USER_ID_MAX_LENGTH, collation=BYTEWISE),
        primary_key=True,
    )
    # Optional: a score can be submitted for a user we know nothing else about.
    # Stored here rather than in Redis so the rank index holds only
    # (member, score) and never needs invalidating when a name changes.
    display_name: Mapped[str | None] = mapped_column(String(DISPLAY_NAME_MAX_LENGTH), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(f"id ~ '{_PG_USER_ID_REGEX}'", name="id_format"),
        CheckConstraint(
            "display_name IS NULL OR length(btrim(display_name)) BETWEEN 1 AND 64",
            name="display_name_length",
        ),
    )


class LeaderboardEntry(Base):
    """A user's current best score on one board.

    Not an event log: best-score semantics (Spec.md D1) mean a submission
    either raises this row's score or does nothing, so one row per
    (board, user) is the complete state.
    """

    __tablename__ = "leaderboard_entries"

    game_id: Mapped[str] = mapped_column(
        String(GAME_ID_MAX_LENGTH, collation=BYTEWISE),
        ForeignKey("games.id", ondelete="CASCADE"),
        primary_key=True,
    )
    period: Mapped[str] = mapped_column(String(16), primary_key=True)
    period_bucket: Mapped[str] = mapped_column(String(16), primary_key=True)

    # COLLATE "C" is what makes this table's ordering match Redis exactly.
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_MAX_LENGTH, collation=BYTEWISE),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    score: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Server-assigned. A client-controlled timestamp would be both a trust hole
    # and a backdated-score exploit.
    achieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The ranking index. Column order and direction mirror D3's canonical
        # order exactly -- score DESC, user_id DESC -- so top-N, rank and
        # window queries are index-only scans and agree with ZREVRANGE.
        #
        # achieved_at is INCLUDEd rather than indexed: it is returned by every
        # read but never ordered or filtered on, so carrying it in the leaf
        # pages avoids a heap fetch per row without widening the B-tree.
        Index(
            "ix_leaderboard_entries_board_rank",
            "game_id",
            "period",
            "period_bucket",
            desc("score"),
            desc("user_id"),
            postgresql_include=["achieved_at"],
        ),
        # Serves "which boards is this user on", used by the fan-out response
        # and by user deletion.
        Index("ix_leaderboard_entries_user", "user_id", "game_id"),
        CheckConstraint(
            f"score BETWEEN {MIN_SCORE} AND {MAX_SCORE}",
            name="score_range",
        ),
        CheckConstraint(
            "period IN (" + ", ".join(f"'{period.value}'" for period in Period) + ")",
            name="period_valid",
        ),
        # Enforces the period/bucket pairing at the storage layer, so a bug in
        # the bucketing code cannot quietly create a board that no query can
        # ever find (e.g. period='daily' with bucket='2026-W38').
        CheckConstraint(
            "(period = 'all_time' AND period_bucket = 'ALL') "
            "OR (period = 'daily' AND period_bucket ~ '^\\d{4}-\\d{2}-\\d{2}$') "
            "OR (period = 'weekly' AND period_bucket ~ '^\\d{4}-W\\d{2}$')",
            name="bucket_matches_period",
        ),
    )


class RedisOutbox(Base):
    """Durable intent to apply a standing to the rank index (Spec.md D4).

    A row is written in the *same transaction* as the entry UPSERT, so a
    committed score can never be lost by a Redis failure. Delivery is
    at-least-once, which is safe because the Redis write is ``ZADD GT`` --
    idempotent and order-independent, so a retry or an out-of-order delivery
    cannot regress a score.
    """

    __tablename__ = "redis_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # The full Redis key, not its components: the sweeper should replay exactly
    # what the writer intended, without re-deriving a key and risking a
    # different answer after a code change.
    redis_key: Mapped[str] = mapped_column(String(160), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(USER_ID_MAX_LENGTH, collation=BYTEWISE), nullable=False
    )
    score: Mapped[int] = mapped_column(BigInteger, nullable=False)

    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Drives backoff, and a rising value is the alert that Redis is unhealthy.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        # Partial index: the sweeper's scan stays proportional to the
        # *undelivered* backlog (normally a handful of rows) rather than to
        # total write volume, which grows forever.
        Index(
            "ix_redis_outbox_pending",
            "enqueued_at",
            postgresql_where=text("delivered_at IS NULL"),
        ),
        CheckConstraint(f"score BETWEEN {MIN_SCORE} AND {MAX_SCORE}", name="score_range"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )
