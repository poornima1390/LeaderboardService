"""The write path: conditional UPSERT fan-out plus outbox, in one transaction.

This module is where Spec.md D1 (best score wins) and D4 (transactional
outbox) actually live. The important property is that a submission is *one*
round trip of SQL per concern and holds no application-side read-modify-write
window, so concurrent submissions for the same user converge on the maximum
without locks held across the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.periods import BoardKey, Period, all_buckets_for

# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #

# Auto-create the user. A score can be submitted for a player we know nothing
# else about, so there is no registration step to orchestrate.
#
# COALESCE on update rather than overwrite: a submission without a
# display_name must not erase one that was set earlier, while a submission
# carrying one is allowed to backfill or update it.
_UPSERT_USER = text(
    """
    INSERT INTO users (id, display_name)
    VALUES (:user_id, :display_name)
    ON CONFLICT (id) DO UPDATE
        SET display_name = COALESCE(EXCLUDED.display_name, users.display_name)
    """
)

# The heart of the write path.
#
# One statement writes every affected board (D5's three-way fan-out), and the
# WHERE on DO UPDATE enforces max-semantics inside the statement:
#
#   * the row is inserted if the user has no standing on that board;
#   * it is updated only if the new score is strictly greater;
#   * otherwise nothing happens and the row is NOT returned.
#
# That makes RETURNING an exact report of which boards improved, with no
# second query and no read-modify-write race. Under concurrency Postgres
# serialises conflicting ON CONFLICT DO UPDATE on the same row and re-evaluates
# the WHERE against the winner's committed value, so N racing submissions
# converge on max(all scores) regardless of arrival order.
#
# Strictly greater, not >=: an equal score leaves achieved_at alone, so the
# earlier achievement keeps its place rather than being silently re-dated.
_UPSERT_ENTRIES = text(
    """
    INSERT INTO leaderboard_entries
        (game_id, period, period_bucket, user_id, score, achieved_at, updated_at)
    SELECT :game_id, board.period, board.bucket, :user_id, :score, :achieved_at, now()
    FROM (VALUES {values}) AS board(period, bucket)
    ON CONFLICT (game_id, period, period_bucket, user_id) DO UPDATE
        SET score       = EXCLUDED.score,
            achieved_at = EXCLUDED.achieved_at,
            updated_at  = now()
        WHERE EXCLUDED.score > leaderboard_entries.score
    RETURNING period, period_bucket, score
    """
)

# Read back the full set of standings, including boards the submission did not
# improve -- RETURNING above cannot report those, and the response has to state
# the user's actual current score on every board.
_SELECT_STANDINGS = text(
    """
    SELECT period, period_bucket, score
    FROM leaderboard_entries
    WHERE game_id = :game_id
      AND user_id = :user_id
      AND (period, period_bucket) IN ({pairs})
    """
)

# Written in the same transaction as the UPSERT, so a committed score can never
# be lost to a Redis failure. Only improved boards are enqueued: syncing a
# board whose standing did not change is pure waste.
# RETURNING id so the caller can mark the row delivered when the fast-path
# ZADD succeeds. Without that, every successful inline sync would still leave
# work for the sweeper: harmless (ZADD GT is idempotent) but it would make the
# pending-backlog metric useless, and that metric is the alarm for the index
# drifting away from Postgres.
_INSERT_OUTBOX = text(
    """
    INSERT INTO redis_outbox (redis_key, user_id, score)
    VALUES (:redis_key, :user_id, :score)
    RETURNING id
    """
)


@dataclass(frozen=True, slots=True)
class BoardStanding:
    """A user's standing on one board after a write."""

    board: BoardKey
    score: int
    improved: bool


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    standings: tuple[BoardStanding, ...]
    # Outbox row ids for the improved boards, positionally aligned with
    # `improved`. Empty when nothing changed.
    outbox_ids: tuple[int, ...] = ()

    @property
    def improved(self) -> tuple[BoardStanding, ...]:
        """The boards that changed — exactly what needs syncing to the index."""
        return tuple(standing for standing in self.standings if standing.improved)


async def submit_score(
    conn: AsyncConnection,
    *,
    game_id: str,
    user_id: str,
    score: int,
    achieved_at: datetime,
    display_name: str | None = None,
) -> SubmissionOutcome:
    """Record a score across every period board and enqueue index updates.

    The caller owns the transaction. Everything here — the user upsert, the
    entry fan-out and the outbox rows — must commit together or not at all,
    which is what makes the outbox a durable record of intent rather than a
    hopeful side effect.

    Args:
        conn: An open connection inside a transaction.
        game_id: Must already exist and be active; validated by the caller so
            the failure is a clean 404/409 rather than a foreign key violation.
        user_id: Created on demand if unknown.
        score: Already range-validated by the request schema.
        achieved_at: Server-assigned submission time; also the tiebreak input.
        display_name: Optional; backfills but never erases.

    Returns:
        The user's standing on every affected board, flagged with whether this
        submission improved it.
    """
    await conn.execute(_UPSERT_USER, {"user_id": user_id, "display_name": display_name})

    boards = all_buckets_for(achieved_at)

    # Bucket values are interpolated as literals rather than bound, because a
    # VALUES list has a variable number of rows and cannot be expressed with a
    # fixed parameter set. They are safe: every value comes from
    # app.domain.periods, which produces them from a UTC datetime -- none is
    # caller-controlled. game_id, user_id and score remain bound parameters.
    values_sql = ", ".join(f"('{period.value}', '{bucket}')" for period, bucket in boards)
    params = {
        "game_id": game_id,
        "user_id": user_id,
        "score": score,
        "achieved_at": achieved_at,
    }

    improved_result = await conn.execute(
        text(str(_UPSERT_ENTRIES).format(values=values_sql)), params
    )
    improved_boards = {(row[0], row[1]) for row in improved_result}

    standings_result = await conn.execute(
        text(str(_SELECT_STANDINGS).format(pairs=values_sql)),
        {"game_id": game_id, "user_id": user_id},
    )
    current_scores = {(row[0], row[1]): row[2] for row in standings_result}

    standings: list[BoardStanding] = []
    for period, bucket in boards:
        key = (period.value, bucket)
        standings.append(
            BoardStanding(
                board=BoardKey(game_id=game_id, period=period, bucket=bucket),
                # Present for every board: the UPSERT guarantees a row exists
                # on each one by the time we read back.
                score=current_scores[key],
                improved=key in improved_boards,
            )
        )

    improved = tuple(standing for standing in standings if standing.improved)

    outbox_ids: list[int] = []
    for standing in improved:
        result = await conn.execute(
            _INSERT_OUTBOX,
            {
                "redis_key": standing.board.redis_key,
                "user_id": user_id,
                "score": standing.score,
            },
        )
        outbox_ids.append(int(result.scalar_one()))

    return SubmissionOutcome(standings=tuple(standings), outbox_ids=tuple(outbox_ids))


async def read_standing(
    conn: AsyncConnection, *, game_id: str, user_id: str, period: Period, bucket: str
) -> int | None:
    """A single board's score for a user, or None if they are not ranked on it."""
    result = await conn.execute(
        text(
            "SELECT score FROM leaderboard_entries "
            "WHERE game_id = :game_id AND period = :period "
            "AND period_bucket = :bucket AND user_id = :user_id"
        ),
        {
            "game_id": game_id,
            "period": period.value,
            "bucket": bucket,
            "user_id": user_id,
        },
    )
    row = result.first()
    return int(row[0]) if row else None
