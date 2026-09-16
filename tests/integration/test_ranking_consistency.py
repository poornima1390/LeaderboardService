"""Postgres and Redis must agree on ranking, exactly (Spec.md D2/D3).

This is the highest-value test in the suite. The design serves rank queries
from Redis while holding truth in Postgres, so any disagreement between them
is a wrong leaderboard — and the disagreements are subtle: they only appear on
ties, and only for identifiers that differ by case or punctuation.

Both stores are real. A mock cannot disagree with anything, which is precisely
the property under test.
"""

from __future__ import annotations

import random
import string
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 16, 14, 22, tzinfo=UTC)
BOARD = ("chess", "all_time", "ALL")
REDIS_KEY = "lb:chess:all_time:ALL"

# Identifiers chosen to break naive orderings:
#   player_99 / player_77 / player_2 / player_1204 / player_10
#       -> byte order is not numeric order
#   Player_A vs player_a
#       -> a case-insensitive collation folds these together
#   player.c vs player-b vs player_d
#       -> '.'(0x2E) '-'(0x2D) '_'(0x5F); glibc/ICU collations reorder or
#          ignore punctuation entirely
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


async def seed(pg: AsyncConnection, redis_client: Redis, standings: list[tuple[str, int]]) -> None:
    """Write the same standings to both stores."""
    game_id, period, bucket = BOARD
    await pg.execute(
        text("INSERT INTO games (id, name) VALUES (:g, 'Chess') ON CONFLICT DO NOTHING"),
        {"g": game_id},
    )
    for user_id, score in standings:
        await pg.execute(
            text("INSERT INTO users (id) VALUES (:u) ON CONFLICT DO NOTHING"),
            {"u": user_id},
        )
        await pg.execute(
            text(
                "INSERT INTO leaderboard_entries "
                "(game_id, period, period_bucket, user_id, score, achieved_at) "
                "VALUES (:g, :p, :b, :u, :s, :a)"
            ),
            {"g": game_id, "p": period, "b": bucket, "u": user_id, "s": score, "a": NOW},
        )
    if standings:
        await redis_client.zadd(REDIS_KEY, dict(standings))


async def pg_order(pg: AsyncConnection) -> list[str]:
    """The board in canonical order (Spec.md D3), straight from Postgres."""
    game_id, period, bucket = BOARD
    result = await pg.execute(
        text(
            "SELECT user_id FROM leaderboard_entries "
            "WHERE game_id = :g AND period = :p AND period_bucket = :b "
            "ORDER BY score DESC, user_id DESC"
        ),
        {"g": game_id, "p": period, "b": bucket},
    )
    return [row[0] for row in result]


# redis-py's async command signatures are typed as broad unions (a command
# can be pipelined, which changes the return type). These shims pin the types
# we actually get with decode_responses=True, so the assertions below read as
# comparisons rather than as a pile of isinstance checks. Phase 3 introduces
# the equivalent in application code.
async def zrevrange(client: Redis, key: str, start: int, stop: int) -> list[str]:
    return [str(member) for member in await client.zrevrange(key, start, stop)]


async def zrevrank(client: Redis, key: str, member: str) -> int:
    rank = await client.zrevrank(key, member)
    assert isinstance(rank, int), f"{member!r} is not ranked on {key}"
    return rank


async def redis_order(redis_client: Redis) -> list[str]:
    return await zrevrange(redis_client, REDIS_KEY, 0, -1)


class TestOrderingAgreement:
    async def test_adversarial_identifiers_order_identically(
        self, pg: AsyncConnection, redis_client: Redis
    ) -> None:
        """All tied on score, so ordering is decided entirely by the tiebreak."""
        await seed(pg, redis_client, [(uid, 98110) for uid in ADVERSARIAL_IDS])

        assert await pg_order(pg) == await redis_order(redis_client)

    async def test_mixed_scores_and_ties_order_identically(
        self, pg: AsyncConnection, redis_client: Redis
    ) -> None:
        standings = [
            (uid, score)
            for score in (99999, 98110, 500, 0)
            for uid in ADVERSARIAL_IDS[: 4 if score else 2]
        ]
        # De-duplicate user ids across score groups; a user has one standing.
        seen: set[str] = set()
        unique: list[tuple[str, int]] = []
        for uid, score in standings:
            if uid not in seen:
                seen.add(uid)
                unique.append((uid, score))

        await seed(pg, redis_client, unique)

        assert await pg_order(pg) == await redis_order(redis_client)

    async def test_ordering_agrees_over_many_random_boards(
        self, pg: AsyncConnection, redis_client: Redis
    ) -> None:
        """Differential test over randomly generated boards.

        Seeded for reproducibility. A plain loop rather than Hypothesis because
        Hypothesis re-runs the test body against one function-scoped fixture,
        which would share a single database transaction across every example.

        Scores are drawn from a deliberately small range so ties are common —
        ties are the only place these two stores can disagree.
        """
        # Seeded, so a divergence is reproducible from the failure message.
        rng = random.Random(20260916)  # noqa: S311 - not cryptographic
        alphabet = string.ascii_letters + string.digits + "_.-"
        game_id, period, bucket = BOARD

        for round_number in range(40):
            user_ids = {
                "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12)))
                for _ in range(rng.randint(2, 25))
            }
            standings = [(uid, rng.randint(0, 5)) for uid in user_ids]

            await seed(pg, redis_client, standings)
            pg_result, redis_result = await pg_order(pg), await redis_order(redis_client)
            assert pg_result == redis_result, (
                f"divergence in round {round_number} with seed 20260916: "
                f"postgres={pg_result} redis={redis_result}"
            )

            # Reset both stores for the next round.
            await pg.execute(text("DELETE FROM leaderboard_entries"))
            await pg.execute(text("DELETE FROM users"))
            await redis_client.delete(REDIS_KEY)
            _ = game_id, period, bucket


class TestRankAgreement:
    async def test_zrevrank_matches_postgres_row_number(
        self, pg: AsyncConnection, redis_client: Redis
    ) -> None:
        """ZREVRANK is 0-based; row_number() is 1-based. Off-by-one lives here."""
        await seed(pg, redis_client, [(uid, 98110) for uid in ADVERSARIAL_IDS])
        game_id, period, bucket = BOARD

        result = await pg.execute(
            text(
                "SELECT user_id, row_number() OVER "
                "(ORDER BY score DESC, user_id DESC) - 1 AS zero_based_rank "
                "FROM leaderboard_entries "
                "WHERE game_id = :g AND period = :p AND period_bucket = :b"
            ),
            {"g": game_id, "p": period, "b": bucket},
        )
        for user_id, expected_rank in result:
            actual = await zrevrank(redis_client, REDIS_KEY, user_id)
            assert actual == expected_rank, f"rank mismatch for {user_id}"

    async def test_count_of_better_rows_matches_zrevrank(
        self, pg: AsyncConnection, redis_client: Redis
    ) -> None:
        """The other way to compute a rank in SQL must give the same answer.

        `COUNT(*)` of rows that sort ahead is the formulation Spec.md D2 names
        as the unbounded one. It has to agree with the window function, or the
        Postgres fallback and the Redis path would return different ranks for
        the same user.
        """
        standings = [(uid, 1000 - index * 10) for index, uid in enumerate(ADVERSARIAL_IDS)]
        standings += [("tied_a", 500), ("tied_b", 500)]
        await seed(pg, redis_client, standings)
        game_id, period, bucket = BOARD

        for user_id, score in standings:
            better = await pg.scalar(
                text(
                    "SELECT count(*) FROM leaderboard_entries "
                    "WHERE game_id = :g AND period = :p AND period_bucket = :b "
                    "AND (score, user_id) > (:s, :u)"
                ),
                {"g": game_id, "p": period, "b": bucket, "s": score, "u": user_id},
            )
            zrank = await zrevrank(redis_client, REDIS_KEY, user_id)
            assert isinstance(better, int)
            assert better == zrank, f"rank formulations disagree for {user_id}"

    async def test_zcard_matches_row_count(self, pg: AsyncConnection, redis_client: Redis) -> None:
        await seed(pg, redis_client, [(uid, 100) for uid in ADVERSARIAL_IDS])

        assert await redis_client.zcard(REDIS_KEY) == await pg.scalar(
            text("SELECT count(*) FROM leaderboard_entries")
        )


class TestWindowAgreement:
    async def test_surroundings_match_between_stores(
        self, pg: AsyncConnection, redis_client: Redis
    ) -> None:
        """The +/-k window around a user, computed both ways (Spec.md §4)."""
        standings = [(f"p{index:03d}", 1000 - index) for index in range(50)]
        await seed(pg, redis_client, standings)
        game_id, period, bucket = BOARD
        window = 3

        for target in ("p000", "p001", "p025", "p048", "p049"):
            zrank = await zrevrank(redis_client, REDIS_KEY, target)
            start = max(0, zrank - window)
            redis_window = await zrevrange(redis_client, REDIS_KEY, start, zrank + window)

            pg_result = await pg.execute(
                text(
                    "SELECT user_id FROM leaderboard_entries "
                    "WHERE game_id = :g AND period = :p AND period_bucket = :b "
                    "ORDER BY score DESC, user_id DESC OFFSET :off LIMIT :lim"
                ),
                {
                    "g": game_id,
                    "p": period,
                    "b": bucket,
                    "off": start,
                    "lim": (zrank + window) - start + 1,
                },
            )
            assert [row[0] for row in pg_result] == redis_window, f"window differs at {target}"

    async def test_top_ranked_user_has_no_one_above(
        self, pg: AsyncConnection, redis_client: Redis
    ) -> None:
        """Boundary case: rank 0 in Redis, and an empty 'above' list."""
        await seed(pg, redis_client, [("leader", 999), ("second", 500)])

        assert await zrevrank(redis_client, REDIS_KEY, "leader") == 0
        assert await zrevrange(redis_client, REDIS_KEY, 0, -1) == ["leader", "second"]


class TestFloatPrecision:
    async def test_large_scores_survive_the_redis_float64_score(
        self, pg: AsyncConnection, redis_client: Redis
    ) -> None:
        """A Redis sorted set score is a float64; MAX_SCORE must not round.

        If it did, two distinct scores could compare equal in the rank index
        while differing in Postgres — a divergence no ordering rule can fix.
        """
        from app.domain.identifiers import MAX_SCORE

        near_max = [
            ("a_top", MAX_SCORE),
            ("b_one_less", MAX_SCORE - 1),
            ("c_two_less", MAX_SCORE - 2),
        ]
        await seed(pg, redis_client, near_max)

        assert await pg_order(pg) == await redis_order(redis_client)
        # Scores must come back exactly, not as 1.0000000000000002e12.
        for user_id, expected in near_max:
            stored = await redis_client.zscore(REDIS_KEY, user_id)
            assert stored is not None
            assert int(stored) == expected


class TestIndexUsage:
    async def test_top_n_query_uses_the_rank_index(
        self, pg: AsyncConnection, redis_client: Redis
    ) -> None:
        """The Postgres fallback path must not degrade to a sort.

        Without the index this still returns correct rows, so only EXPLAIN can
        catch the regression — and the fallback is exactly the path that runs
        when the system is already under stress.
        """
        await seed(pg, redis_client, [(f"p{index:04d}", index) for index in range(300)])
        game_id, period, bucket = BOARD

        # Planner would otherwise prefer a seq scan on a tiny table.
        await pg.execute(text("SET LOCAL enable_seqscan = off"))
        # EXPLAIN returns one row per plan line; scalar() would see only the
        # first ("Limit ...") and miss the scan node underneath it.
        # Interpolated rather than bound because EXPLAIN on a prepared
        # statement plans against generic parameters and can choose a
        # different path than the real query. Every value here is a
        # module-level test constant.
        result = await pg.execute(
            text(
                "EXPLAIN (FORMAT TEXT) SELECT user_id, score, achieved_at "  # noqa: S608
                "FROM leaderboard_entries "
                f"WHERE game_id = '{game_id}' AND period = '{period}' "
                f"AND period_bucket = '{bucket}' "
                "ORDER BY score DESC, user_id DESC LIMIT 10"
            )
        )
        plan = "\n".join(row[0] for row in result)
        assert "ix_leaderboard_entries_board_rank" in plan, plan
        assert "Sort" not in plan, f"ordering was not satisfied by the index:\n{plan}"
