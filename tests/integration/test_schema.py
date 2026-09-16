"""Schema behaviour against real Postgres (Spec.md §3).

Constraints are tested by trying to violate them. A CHECK constraint nobody
has ever seen reject anything is a comment, not a guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.identifiers import FLOAT64_EXACT_INTEGER_LIMIT, MAX_SCORE

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 16, 14, 22, tzinfo=UTC)


async def add_game(pg: AsyncConnection, game_id: str = "chess") -> str:
    await pg.execute(
        text("INSERT INTO games (id, name) VALUES (:id, :name)"),
        {"id": game_id, "name": game_id.title()},
    )
    return game_id


async def add_user(pg: AsyncConnection, user_id: str) -> str:
    await pg.execute(text("INSERT INTO users (id) VALUES (:id)"), {"id": user_id})
    return user_id


async def add_entry(
    pg: AsyncConnection,
    *,
    game_id: str = "chess",
    period: str = "all_time",
    bucket: str = "ALL",
    user_id: str,
    score: int,
    achieved_at: datetime = NOW,
) -> None:
    await pg.execute(
        text(
            "INSERT INTO leaderboard_entries "
            "(game_id, period, period_bucket, user_id, score, achieved_at) "
            "VALUES (:g, :p, :b, :u, :s, :a)"
        ),
        {"g": game_id, "p": period, "b": bucket, "u": user_id, "s": score, "a": achieved_at},
    )


class TestGameConstraints:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "Chess",  # uppercase
            "-chess",  # leading hyphen
            "chess_1",  # underscore not permitted in a slug
            "che ss",
            "chess:blitz",  # ':' would forge a Redis key segment
            "",
            "c" * 65,
        ],
    )
    async def test_rejects_malformed_game_id(self, pg: AsyncConnection, bad_id: str) -> None:
        with pytest.raises((IntegrityError, DBAPIError)):
            await add_game(pg, bad_id)

    @pytest.mark.parametrize("good_id", ["chess", "c", "tetris-99", "0ad", "a" * 64])
    async def test_accepts_well_formed_game_id(self, pg: AsyncConnection, good_id: str) -> None:
        await add_game(pg, good_id)

    async def test_games_default_to_active(self, pg: AsyncConnection) -> None:
        await add_game(pg)
        active = await pg.scalar(text("SELECT is_active FROM games WHERE id='chess'"))
        assert active is True


class TestUserConstraints:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "player:1",  # Redis key separator
            "player 1",
            "player/1",
            "player\n1",
            "",
            "p" * 65,
        ],
    )
    async def test_rejects_malformed_user_id(self, pg: AsyncConnection, bad_id: str) -> None:
        """The charset is load-bearing: these become Redis members and URL segments."""
        with pytest.raises((IntegrityError, DBAPIError)):
            await add_user(pg, bad_id)

    @pytest.mark.parametrize("good_id", ["player_4417", "Player.A", "a-b_c.d", "99", "u" * 64])
    async def test_accepts_well_formed_user_id(self, pg: AsyncConnection, good_id: str) -> None:
        await add_user(pg, good_id)

    async def test_rejects_whitespace_only_display_name(self, pg: AsyncConnection) -> None:
        with pytest.raises((IntegrityError, DBAPIError)):
            await pg.execute(text("INSERT INTO users (id, display_name) VALUES ('p1', '   ')"))

    async def test_allows_absent_display_name(self, pg: AsyncConnection) -> None:
        """A score can be submitted for a user we know nothing else about."""
        await pg.execute(text("INSERT INTO users (id) VALUES ('p1')"))
        assert await pg.scalar(text("SELECT display_name FROM users WHERE id='p1'")) is None


class TestEntryConstraints:
    @pytest.fixture(autouse=True)
    async def _seed(self, pg: AsyncConnection) -> None:
        await add_game(pg)
        await add_user(pg, "p1")

    @pytest.mark.parametrize("bad_score", [-1, MAX_SCORE + 1])
    async def test_rejects_out_of_range_score(self, pg: AsyncConnection, bad_score: int) -> None:
        with pytest.raises((IntegrityError, DBAPIError)):
            await add_entry(pg, user_id="p1", score=bad_score)

    @pytest.mark.parametrize("good_score", [0, 1, MAX_SCORE])
    async def test_accepts_in_range_score(self, pg: AsyncConnection, good_score: int) -> None:
        await add_entry(pg, user_id="p1", score=good_score)

    async def test_max_score_survives_the_float64_round_trip(self, pg: AsyncConnection) -> None:
        """The bound exists so a score is never rounded on its way into Redis.

        A Redis sorted set score is a float64; MAX_SCORE must stay well inside
        its exact-integer range or two distinct scores could compare equal in
        the rank index while differing in Postgres.
        """
        assert MAX_SCORE < FLOAT64_EXACT_INTEGER_LIMIT
        assert int(float(MAX_SCORE)) == MAX_SCORE

        await add_entry(pg, user_id="p1", score=MAX_SCORE)
        stored = await pg.scalar(text("SELECT score FROM leaderboard_entries"))
        assert int(float(stored)) == MAX_SCORE

    @pytest.mark.parametrize(
        ("period", "bucket"),
        [
            ("all_time", "2026-09-16"),  # all_time must use ALL
            ("daily", "ALL"),
            ("daily", "2026-W38"),  # right shape, wrong period
            ("weekly", "2026-09-16"),
            ("yearly", "2026"),  # not a known period at all
        ],
    )
    async def test_rejects_period_bucket_mismatch(
        self, pg: AsyncConnection, period: str, bucket: str
    ) -> None:
        """Storage-level guard: a bucketing bug must not create an unfindable board.

        A row with period='daily' and bucket='2026-W38' would sit in a board no
        query can ever address, and reads would answer "no scores yet".
        """
        with pytest.raises((IntegrityError, DBAPIError)):
            await add_entry(pg, user_id="p1", period=period, bucket=bucket, score=100)

    @pytest.mark.parametrize(
        ("period", "bucket"),
        [("all_time", "ALL"), ("daily", "2026-09-16"), ("weekly", "2026-W38")],
    )
    async def test_accepts_valid_period_bucket_pairs(
        self, pg: AsyncConnection, period: str, bucket: str
    ) -> None:
        await add_entry(pg, user_id="p1", period=period, bucket=bucket, score=100)

    async def test_one_row_per_board_and_user(self, pg: AsyncConnection) -> None:
        """The primary key is what makes a standing a standing."""
        await add_entry(pg, user_id="p1", score=100)
        with pytest.raises(IntegrityError):
            await add_entry(pg, user_id="p1", score=200)

    async def test_same_user_can_rank_on_every_period(self, pg: AsyncConnection) -> None:
        """D5's fan-out: three boards, three rows, one user."""
        await add_entry(pg, user_id="p1", period="all_time", bucket="ALL", score=100)
        await add_entry(pg, user_id="p1", period="daily", bucket="2026-09-16", score=100)
        await add_entry(pg, user_id="p1", period="weekly", bucket="2026-W38", score=100)
        assert await pg.scalar(text("SELECT count(*) FROM leaderboard_entries")) == 3

    async def test_unknown_game_is_rejected(self, pg: AsyncConnection) -> None:
        with pytest.raises(IntegrityError):
            await add_entry(pg, game_id="nope", user_id="p1", score=100)

    async def test_unknown_user_is_rejected(self, pg: AsyncConnection) -> None:
        with pytest.raises(IntegrityError):
            await add_entry(pg, user_id="ghost", score=100)


class TestCascades:
    async def test_deleting_a_user_removes_their_standings(self, pg: AsyncConnection) -> None:
        """Supports erasure without leaving orphaned rows in every board."""
        await add_game(pg)
        await add_user(pg, "p1")
        await add_entry(pg, user_id="p1", score=100)
        await add_entry(pg, user_id="p1", period="daily", bucket="2026-09-16", score=100)

        await pg.execute(text("DELETE FROM users WHERE id='p1'"))

        assert await pg.scalar(text("SELECT count(*) FROM leaderboard_entries")) == 0

    async def test_deleting_a_game_removes_its_boards(self, pg: AsyncConnection) -> None:
        await add_game(pg)
        await add_user(pg, "p1")
        await add_entry(pg, user_id="p1", score=100)

        await pg.execute(text("DELETE FROM games WHERE id='chess'"))

        assert await pg.scalar(text("SELECT count(*) FROM leaderboard_entries")) == 0


class TestOutboxConstraints:
    async def test_pending_rows_are_indexed_partially(self, pg: AsyncConnection) -> None:
        """The partial index is what keeps the sweeper's scan bounded."""
        definition = await pg.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_redis_outbox_pending'")
        )
        assert "delivered_at IS NULL" in definition

    async def test_attempts_defaults_to_zero_and_cannot_go_negative(
        self, pg: AsyncConnection
    ) -> None:
        await pg.execute(
            text(
                "INSERT INTO redis_outbox (redis_key, user_id, score) "
                "VALUES ('lb:chess:all_time:ALL', 'p1', 100)"
            )
        )
        assert await pg.scalar(text("SELECT attempts FROM redis_outbox")) == 0
        with pytest.raises((IntegrityError, DBAPIError)):
            await pg.execute(text("UPDATE redis_outbox SET attempts = -1"))

    async def test_outbox_does_not_require_a_known_user(self, pg: AsyncConnection) -> None:
        """No FK on purpose: the outbox is a replay log, not a relation.

        A foreign key here would let a cascading user deletion silently discard
        pending index work, and would make the sweeper's writes depend on rows
        it does not own.
        """
        await pg.execute(
            text(
                "INSERT INTO redis_outbox (redis_key, user_id, score) "
                "VALUES ('lb:chess:all_time:ALL', 'never_registered', 100)"
            )
        )
        assert await pg.scalar(text("SELECT count(*) FROM redis_outbox")) == 1
