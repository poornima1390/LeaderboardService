"""The write path: max-semantics, fan-out and the outbox (Spec.md D1, D4, D5).

Against real Postgres, because the guarantees under test are properties of the
SQL statement rather than of the Python around it. The conditional UPSERT is
what makes concurrent submissions converge without locks, and only a real
database can be raced.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.errors import GameInactiveError, GameNotFoundError
from app.domain.periods import Period
from app.repositories import entries, games

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 16, 14, 22, tzinfo=UTC)


async def register(engine: AsyncEngine, game_id: str = "chess") -> None:
    async with engine.begin() as conn:
        await games.create_game(conn, game_id=game_id, name=game_id.title())


async def submit(
    engine: AsyncEngine,
    *,
    user_id: str = "p1",
    score: int,
    game_id: str = "chess",
    when: datetime = NOW,
    display_name: str | None = None,
) -> entries.SubmissionOutcome:
    async with engine.begin() as conn:
        return await entries.submit_score(
            conn,
            game_id=game_id,
            user_id=user_id,
            score=score,
            achieved_at=when,
            display_name=display_name,
        )


async def scores_of(engine: AsyncEngine, user_id: str = "p1") -> dict[str, int]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT period, score FROM leaderboard_entries WHERE user_id = :u"),
            {"u": user_id},
        )
        return {row[0]: row[1] for row in result}


class TestBestScoreSemantics:
    """D1: a standing is max(existing, submitted)."""

    async def test_first_submission_creates_standings(self, db: AsyncEngine) -> None:
        await register(db)
        outcome = await submit(db, score=18500)

        assert len(outcome.standings) == 3
        assert all(standing.improved for standing in outcome.standings)
        assert await scores_of(db) == {
            "all_time": 18500,
            "daily": 18500,
            "weekly": 18500,
        }

    async def test_lower_score_is_ignored(self, db: AsyncEngine) -> None:
        await register(db)
        await submit(db, score=18500)
        outcome = await submit(db, score=9000)

        assert not any(standing.improved for standing in outcome.standings)
        # The reported score is the *current best*, not what was submitted.
        assert all(standing.score == 18500 for standing in outcome.standings)
        assert await scores_of(db) == {
            "all_time": 18500,
            "daily": 18500,
            "weekly": 18500,
        }

    async def test_higher_score_replaces(self, db: AsyncEngine) -> None:
        await register(db)
        await submit(db, score=18500)
        outcome = await submit(db, score=22400)

        assert all(standing.improved for standing in outcome.standings)
        assert await scores_of(db) == {
            "all_time": 22400,
            "daily": 22400,
            "weekly": 22400,
        }

    async def test_equal_score_does_not_improve(self, db: AsyncEngine) -> None:
        await register(db)
        await submit(db, score=18500)
        outcome = await submit(db, score=18500)

        assert not any(standing.improved for standing in outcome.standings)

    async def test_equal_score_preserves_the_original_achieved_at(self, db: AsyncEngine) -> None:
        """Strictly-greater, not >=, so a tie keeps its original timestamp.

        achieved_at is a tiebreak input and a user-visible fact ("you set this
        on Tuesday"). Re-dating it on an equal score would silently move a
        player relative to others tied with them.
        """
        await register(db)
        await submit(db, score=18500, when=NOW)

        later = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
        await submit(db, score=18500, when=later)

        async with db.connect() as conn:
            stored = await conn.scalar(
                text(
                    "SELECT achieved_at FROM leaderboard_entries "
                    "WHERE period = 'all_time' AND user_id = 'p1'"
                )
            )
        assert stored == NOW

    async def test_replaying_a_submission_is_a_no_op(self, db: AsyncEngine) -> None:
        """D1's payoff: retries need no idempotency key."""
        await register(db)
        first = await submit(db, score=18500)
        assert all(standing.improved for standing in first.standings)

        for _ in range(5):
            replay = await submit(db, score=18500)
            assert not any(standing.improved for standing in replay.standings)

        assert await scores_of(db) == {
            "all_time": 18500,
            "daily": 18500,
            "weekly": 18500,
        }


class TestConcurrency:
    """The conditional UPSERT must be safe without application-level locking."""

    async def test_concurrent_submissions_converge_on_the_maximum(self, db: AsyncEngine) -> None:
        """50 racing submissions, shuffled scores, one winner.

        Postgres serialises conflicting ON CONFLICT DO UPDATE on the same row
        and re-evaluates the WHERE against the committed winner, so the
        outcome is max() regardless of arrival order — no lost update, and no
        read-modify-write window in application code.
        """
        await register(db)
        submitted = [17, 4, 99, 23, 56, 88, 1, 42, 73, 12] * 5
        expected_max = max(submitted)

        await asyncio.gather(*(submit(db, score=score) for score in submitted))

        assert await scores_of(db) == {
            "all_time": expected_max,
            "daily": expected_max,
            "weekly": expected_max,
        }

    async def test_exactly_one_submission_claims_each_improvement(self, db: AsyncEngine) -> None:
        """Ascending scores submitted concurrently: improvements must not double-count.

        Each board ends with one row, and the number of outbox rows can never
        exceed the number of submissions — if two writers both believed they
        improved the same standing, the totals would disagree.
        """
        await register(db)
        scores = list(range(1, 31))

        outcomes = await asyncio.gather(*(submit(db, score=s) for s in scores))

        improved_count = sum(len(outcome.improved) for outcome in outcomes)
        async with db.connect() as conn:
            outbox_rows = await conn.scalar(text("SELECT count(*) FROM redis_outbox"))
            entry_rows = await conn.scalar(text("SELECT count(*) FROM leaderboard_entries"))

        assert entry_rows == 3, "one row per board, regardless of write volume"
        assert outbox_rows == improved_count
        assert improved_count <= len(scores) * 3
        assert await scores_of(db) == {
            "all_time": 30,
            "daily": 30,
            "weekly": 30,
        }

    async def test_concurrent_submissions_for_different_users_do_not_interfere(
        self, db: AsyncEngine
    ) -> None:
        await register(db)
        await asyncio.gather(
            *(submit(db, user_id=f"p{i:02d}", score=i * 100) for i in range(1, 21))
        )

        async with db.connect() as conn:
            count = await conn.scalar(
                text("SELECT count(*) FROM leaderboard_entries WHERE period='all_time'")
            )
        assert count == 20


class TestPeriodFanOut:
    """D5: one submission, three boards, each with independent semantics."""

    async def test_fan_out_targets_every_period(self, db: AsyncEngine) -> None:
        await register(db)
        outcome = await submit(db, score=100)

        assert {standing.board.period for standing in outcome.standings} == set(Period)

    async def test_a_new_day_starts_a_fresh_daily_board(self, db: AsyncEngine) -> None:
        """The crux of per-board semantics.

        A high score yesterday must not suppress today's board: today's best
        is a different question from the all-time best.
        """
        await register(db)
        await submit(db, score=50_000, when=datetime(2026, 9, 15, 12, 0, tzinfo=UTC))

        outcome = await submit(db, score=900, when=datetime(2026, 9, 16, 12, 0, tzinfo=UTC))

        by_period = {standing.board.period: standing for standing in outcome.standings}
        assert by_period[Period.ALL_TIME].improved is False
        assert by_period[Period.ALL_TIME].score == 50_000
        assert by_period[Period.DAILY].improved is True, "a new day is a new board"
        assert by_period[Period.DAILY].score == 900
        # 2026-09-15 and 2026-09-16 are both in ISO week 38.
        assert by_period[Period.WEEKLY].improved is False
        assert by_period[Period.WEEKLY].score == 50_000

    async def test_boards_are_scoped_per_game(self, db: AsyncEngine) -> None:
        await register(db, "chess")
        await register(db, "tetris")
        await submit(db, score=100, game_id="chess")
        await submit(db, score=5, game_id="tetris")

        async with db.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT game_id, score FROM leaderboard_entries "
                    "WHERE period='all_time' ORDER BY game_id"
                )
            )
            assert [(row[0], row[1]) for row in result] == [("chess", 100), ("tetris", 5)]


class TestOutboxEnqueue:
    """D4: intent to index is written in the same transaction as the score."""

    async def test_improved_boards_are_enqueued(self, db: AsyncEngine) -> None:
        await register(db)
        outcome = await submit(db, score=100)

        async with db.connect() as conn:
            result = await conn.execute(
                text("SELECT redis_key, user_id, score, delivered_at FROM redis_outbox")
            )
            rows = list(result)

        assert len(rows) == 3 == len(outcome.outbox_ids)
        assert {row[0] for row in rows} == {
            "lb:chess:all_time:ALL",
            "lb:chess:daily:2026-09-16",
            "lb:chess:weekly:2026-W38",
        }
        assert all(row[1] == "p1" and row[2] == 100 for row in rows)
        assert all(row[3] is None for row in rows), "enqueued, not yet delivered"

    async def test_unimproved_boards_are_not_enqueued(self, db: AsyncEngine) -> None:
        """Syncing a standing that did not change is pure waste."""
        await register(db)
        await submit(db, score=100)
        async with db.begin() as conn:
            await conn.execute(text("DELETE FROM redis_outbox"))

        outcome = await submit(db, score=50)

        async with db.connect() as conn:
            pending = await conn.scalar(text("SELECT count(*) FROM redis_outbox"))
        assert pending == 0
        assert outcome.outbox_ids == ()

    async def test_partial_improvement_enqueues_only_changed_boards(self, db: AsyncEngine) -> None:
        await register(db)
        await submit(db, score=50_000, when=datetime(2026, 9, 15, 12, 0, tzinfo=UTC))
        async with db.begin() as conn:
            await conn.execute(text("DELETE FROM redis_outbox"))

        await submit(db, score=900, when=datetime(2026, 9, 16, 12, 0, tzinfo=UTC))

        async with db.connect() as conn:
            result = await conn.execute(text("SELECT redis_key FROM redis_outbox"))
            keys = {row[0] for row in result}
        assert keys == {"lb:chess:daily:2026-09-16"}, "only the daily board changed"

    async def test_outbox_row_carries_the_stored_score_not_the_submitted_one(
        self, db: AsyncEngine
    ) -> None:
        """The index must converge on what Postgres holds, not on a request value."""
        await register(db)
        await submit(db, score=500)
        async with db.begin() as conn:
            await conn.execute(text("DELETE FROM redis_outbox"))
        await submit(db, score=900)

        async with db.connect() as conn:
            result = await conn.execute(text("SELECT DISTINCT score FROM redis_outbox"))
            assert [row[0] for row in result] == [900]


class TestUserLifecycle:
    async def test_unknown_user_is_created_on_first_submission(self, db: AsyncEngine) -> None:
        await register(db)
        await submit(db, user_id="brand_new", score=10)

        async with db.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM users WHERE id='brand_new'")) == 1

    async def test_display_name_is_backfilled(self, db: AsyncEngine) -> None:
        await register(db)
        await submit(db, score=10)
        await submit(db, score=20, display_name="Ayo")

        async with db.connect() as conn:
            assert await conn.scalar(text("SELECT display_name FROM users")) == "Ayo"

    async def test_submission_without_a_name_does_not_erase_one(self, db: AsyncEngine) -> None:
        """COALESCE, not overwrite: most submissions carry no name."""
        await register(db)
        await submit(db, score=10, display_name="Ayo")
        await submit(db, score=20, display_name=None)

        async with db.connect() as conn:
            assert await conn.scalar(text("SELECT display_name FROM users")) == "Ayo"


class TestGameValidation:
    async def test_unregistered_game_raises_not_found(self, db: AsyncEngine) -> None:
        async with db.connect() as conn:
            with pytest.raises(GameNotFoundError):
                await games.require_writable_game(conn, "nope")

    async def test_retired_game_raises_inactive(self, db: AsyncEngine) -> None:
        """A distinct error from 'not found': existing boards stay readable."""
        await register(db)
        async with db.begin() as conn:
            await conn.execute(text("UPDATE games SET is_active = false"))
        async with db.connect() as conn:
            with pytest.raises(GameInactiveError):
                await games.require_writable_game(conn, "chess")

    async def test_registration_is_idempotent(self, db: AsyncEngine) -> None:
        await register(db)
        async with db.begin() as conn:
            record = await games.create_game(conn, game_id="chess", name="Chess Deluxe")

        assert record.name == "Chess Deluxe"
        async with db.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM games")) == 1
