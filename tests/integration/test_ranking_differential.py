"""RedisRanking and PostgresRanking must be indistinguishable (Spec.md D2/D10).

This is the highest-value test in the suite. The design answers rank queries
from Redis while holding truth in Postgres, so any disagreement between the
two is a wrong leaderboard — and the disagreements are subtle: they surface
only on ties, and only for identifiers differing by case or punctuation.

Having two independent implementations of one protocol is what makes this
assertable at all. A mock cannot disagree with anything.
"""

from __future__ import annotations

import random
import string
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.periods import BoardKey, Period
from app.repositories import games
from app.repositories.postgres_ranking import PostgresRanking
from app.repositories.ranking import RankingRepository
from app.repositories.redis_ranking import RedisRanking
from app.services import scoring

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 16, 14, 22, tzinfo=UTC)
BOARD = BoardKey(game_id="chess", period=Period.ALL_TIME, bucket="ALL")

# Chosen to break naive orderings: byte order is not numeric order, a
# case-insensitive collation folds Player_A with player_a, and glibc/ICU
# collations reorder or ignore punctuation entirely.
ADVERSARIAL_IDS = [
    "player_99",
    "player_77",
    "player_2",
    "player_1204",
    "player_10",
    "Player_A",
    "player_a",
    "player.c",
    "player-b",
    "player_d",
    "PLAYER",
    "player",
    "0",
    "z",
]


async def seed(engine: AsyncEngine, redis_client: Redis, standings: list[tuple[str, int]]) -> None:
    """Write the same standings through the real write path, to both stores."""
    async with engine.begin() as conn:
        await games.create_game(conn, game_id="chess", name="Chess")
    for user_id, score in standings:
        await scoring.submit_score(
            engine=engine,
            redis_client=redis_client,
            game_id="chess",
            user_id=user_id,
            score=score,
            now=NOW,
        )


def both(engine: AsyncEngine, redis_client: Redis) -> tuple[RankingRepository, ...]:
    return RedisRanking(redis_client), PostgresRanking(engine)


class TestSizeAgreement:
    async def test_size_matches(self, db: AsyncEngine, redis_client: Redis) -> None:
        await seed(db, redis_client, [(uid, 100) for uid in ADVERSARIAL_IDS])
        index, fallback = both(db, redis_client)

        assert await index.size(BOARD) == await fallback.size(BOARD)

    async def test_empty_board_size_matches(self, db: AsyncEngine, redis_client: Redis) -> None:
        index, fallback = both(db, redis_client)
        assert await index.size(BOARD) == await fallback.size(BOARD) == 0


class TestTopAgreement:
    async def test_all_tied_scores_order_identically(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """Ordering decided entirely by the tiebreak — the only place they
        can disagree."""
        await seed(db, redis_client, [(uid, 98110) for uid in ADVERSARIAL_IDS])
        index, fallback = both(db, redis_client)

        assert await index.top(BOARD, limit=100, offset=0) == await fallback.top(
            BOARD, limit=100, offset=0
        )

    async def test_mixed_scores_order_identically(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        standings = [
            (uid, score)
            for uid, score in zip(
                ADVERSARIAL_IDS,
                [900, 900, 900, 500, 500, 100, 100, 100, 7, 7, 1, 1, 0, 0],
                strict=True,
            )
        ]
        await seed(db, redis_client, standings)
        index, fallback = both(db, redis_client)

        assert await index.top(BOARD, limit=100, offset=0) == await fallback.top(
            BOARD, limit=100, offset=0
        )

    @pytest.mark.parametrize(
        ("limit", "offset"),
        [(1, 0), (3, 0), (5, 2), (10, 4), (1, 13), (100, 0), (5, 20)],
    )
    async def test_every_page_matches(
        self, db: AsyncEngine, redis_client: Redis, limit: int, offset: int
    ) -> None:
        """Including an offset past the end, which must give an empty page
        rather than an error from either store."""
        await seed(db, redis_client, [(uid, 500) for uid in ADVERSARIAL_IDS])
        index, fallback = both(db, redis_client)

        assert await index.top(BOARD, limit=limit, offset=offset) == await fallback.top(
            BOARD, limit=limit, offset=offset
        )

    async def test_ranks_are_dense_and_one_based(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """A strict total order means no two entries share a position."""
        await seed(db, redis_client, [(uid, 500) for uid in ADVERSARIAL_IDS])

        for repository in both(db, redis_client):
            page = await repository.top(BOARD, limit=100, offset=0)
            assert [entry.rank for entry in page.entries] == list(
                range(1, len(ADVERSARIAL_IDS) + 1)
            ), repository.source

    async def test_offset_pages_continue_the_ranking(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """rank == offset + index + 1 must hold across a page boundary."""
        await seed(db, redis_client, [(uid, 500) for uid in ADVERSARIAL_IDS])

        for repository in both(db, redis_client):
            first = await repository.top(BOARD, limit=5, offset=0)
            second = await repository.top(BOARD, limit=5, offset=5)
            assert [entry.rank for entry in second.entries] == [6, 7, 8, 9, 10]
            assert not {entry.user_id for entry in first.entries} & {
                entry.user_id for entry in second.entries
            }, repository.source


class TestAroundAgreement:
    async def test_every_user_context_matches(self, db: AsyncEngine, redis_client: Redis) -> None:
        """Walk every user on a tied board and compare the whole context."""
        await seed(db, redis_client, [(uid, 98110) for uid in ADVERSARIAL_IDS])
        index, fallback = both(db, redis_client)

        for user_id in ADVERSARIAL_IDS:
            assert await index.around(BOARD, user_id, window=3) == await fallback.around(
                BOARD, user_id, window=3
            ), f"context differs for {user_id}"

    @pytest.mark.parametrize("window", [0, 1, 3, 25])
    async def test_every_window_size_matches(
        self, db: AsyncEngine, redis_client: Redis, window: int
    ) -> None:
        await seed(db, redis_client, [(f"p{i:03d}", 1000 - i) for i in range(40)])
        index, fallback = both(db, redis_client)

        for user_id in ("p000", "p001", "p020", "p038", "p039"):
            assert await index.around(BOARD, user_id, window=window) == await fallback.around(
                BOARD, user_id, window=window
            ), f"differs for {user_id} at window={window}"

    async def test_boundaries_match(self, db: AsyncEngine, redis_client: Redis) -> None:
        """Rank 1 has nothing above; last place has nothing below."""
        await seed(db, redis_client, [(f"p{i:02d}", 100 - i) for i in range(10)])
        index, fallback = both(db, redis_client)

        top = await index.around(BOARD, "p00", window=3)
        bottom = await index.around(BOARD, "p09", window=3)
        assert top is not None and bottom is not None
        assert top.above == ()
        assert bottom.below == ()
        assert top == await fallback.around(BOARD, "p00", window=3)
        assert bottom == await fallback.around(BOARD, "p09", window=3)

    async def test_window_larger_than_board_matches(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        await seed(db, redis_client, [("solo", 100)])
        index, fallback = both(db, redis_client)

        context = await index.around(BOARD, "solo", window=25)
        assert context is not None
        assert (context.user.rank, context.above, context.below) == (1, (), ())
        assert context == await fallback.around(BOARD, "solo", window=25)

    async def test_unranked_user_returns_none_from_both(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        await seed(db, redis_client, [("p1", 100)])
        index, fallback = both(db, redis_client)

        assert await index.around(BOARD, "ghost", window=3) is None
        assert await fallback.around(BOARD, "ghost", window=3) is None

    async def test_percentile_matches(self, db: AsyncEngine, redis_client: Redis) -> None:
        await seed(db, redis_client, [(f"p{i:03d}", 1000 - i) for i in range(50)])
        index, fallback = both(db, redis_client)

        for user_id in ("p000", "p024", "p049"):
            from_index = await index.around(BOARD, user_id, window=1)
            from_fallback = await fallback.around(BOARD, user_id, window=1)
            assert from_index is not None and from_fallback is not None
            assert from_index.percentile == from_fallback.percentile

    async def test_rank_one_is_exactly_one_hundred_percent(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        await seed(db, redis_client, [(f"p{i:03d}", 1000 - i) for i in range(1000)])
        context = await RedisRanking(redis_client).around(BOARD, "p000", window=0)
        assert context is not None
        assert context.percentile == 100.0


class TestRandomisedDifferential:
    async def test_random_boards_agree_across_every_operation(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """Differential test over randomly generated boards.

        Seeded, so a divergence is reproducible from the failure message.
        Scores come from a deliberately narrow range so ties are common —
        ties are the only place the two stores can disagree.

        A plain loop rather than Hypothesis: Hypothesis re-runs the test body
        against one function-scoped fixture, which would share a single
        database transaction and one Redis database across every example.
        """
        rng = random.Random(20260917)  # noqa: S311 - not cryptographic
        alphabet = string.ascii_letters + string.digits + "_.-"
        index, fallback = both(db, redis_client)

        async with db.begin() as conn:
            await games.create_game(conn, game_id="chess", name="Chess")

        for round_number in range(15):
            user_ids = {
                "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 10)))
                for _ in range(rng.randint(2, 18))
            }
            standings = [(uid, rng.randint(0, 4)) for uid in user_ids]

            for user_id, score in standings:
                await scoring.submit_score(
                    engine=db,
                    redis_client=redis_client,
                    game_id="chess",
                    user_id=user_id,
                    score=score,
                    now=NOW,
                )

            context = f"round {round_number}, seed 20260917"

            assert await index.size(BOARD) == await fallback.size(BOARD), context
            for limit, offset in ((5, 0), (3, 2), (100, 0)):
                assert await index.top(BOARD, limit=limit, offset=offset) == await fallback.top(
                    BOARD, limit=limit, offset=offset
                ), f"top(limit={limit}, offset={offset}) differs in {context}"
            for user_id, _ in standings:
                assert await index.around(BOARD, user_id, window=2) == await fallback.around(
                    BOARD, user_id, window=2
                ), f"around({user_id!r}) differs in {context}"

            # Reset both stores for the next round.
            from sqlalchemy import text

            async with db.begin() as conn:
                await conn.execute(text("DELETE FROM leaderboard_entries"))
                await conn.execute(text("DELETE FROM redis_outbox"))
                await conn.execute(text("DELETE FROM users"))
            await redis_client.delete(BOARD.redis_key)
            await redis_client.delete("lb:chess:daily:2026-09-16")
            await redis_client.delete("lb:chess:weekly:2026-W38")
