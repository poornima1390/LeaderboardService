"""Rebuilding the rank index from Postgres (Spec.md §4, D2).

The index is derived state; this is the path that proves it is genuinely
reconstructible rather than merely described as such. The decisive assertion
is the differential one: after a rebuild, Redis and Postgres must agree
exactly — the same property the Phase 1 tests establish for ordering.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.periods import Period
from app.repositories import entries, games
from app.services import rebuild as rebuild_service
from app.services import scoring

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 16, 14, 22, tzinfo=UTC)
ADMIN = {"X-Admin-Key": "d4a82fe07c6b13594ea2f8db60371cae"}
AUTH = {"X-API-Key": "7f3c91ab5e2d4806bc1f9a73de50c284"}
ALL_TIME_KEY = "lb:chess:all_time:ALL"


async def seed_postgres_only(
    engine: AsyncEngine, standings: list[tuple[str, int]], game_id: str = "chess"
) -> None:
    """Write scores with no index attached — the state a fresh attach finds."""
    async with engine.begin() as conn:
        await games.create_game(conn, game_id=game_id, name=game_id.title())
    for user_id, score in standings:
        await scoring.submit_score(
            engine=engine,
            redis_client=None,  # no index configured
            game_id=game_id,
            user_id=user_id,
            score=score,
            now=NOW,
        )


async def pg_order(engine: AsyncEngine, game_id: str = "chess") -> list[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT user_id FROM leaderboard_entries "
                "WHERE game_id = :g AND period = 'all_time' "
                "ORDER BY score DESC, user_id DESC"
            ),
            {"g": game_id},
        )
        return [row[0] for row in result]


async def redis_order(client: Redis, key: str = ALL_TIME_KEY) -> list[str]:
    return [str(member) for member in await client.zrevrange(key, 0, -1)]


class TestAttachingAnIndexToExistingData:
    async def test_index_is_empty_before_a_rebuild(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """Establishes the problem the rebuild exists to solve.

        Scores written with no index configured produce no outbox rows, so
        nothing else would ever index them.
        """
        await seed_postgres_only(db, [("p1", 100), ("p2", 200)])

        assert await redis_client.zcard(ALL_TIME_KEY) == 0
        async with db.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM redis_outbox")) == 0

    async def test_rebuild_populates_every_board(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        await seed_postgres_only(db, [("p1", 100), ("p2", 200), ("p3", 50)])

        report = await rebuild_service.rebuild_index(db, redis_client)

        assert report.ok
        assert report.boards == 3, "all_time, daily and weekly"
        assert report.entries == 9, "three users on three boards"
        assert await redis_client.zcard(ALL_TIME_KEY) == 3
        assert await redis_client.zcard("lb:chess:daily:2026-09-16") == 3
        assert await redis_client.zcard("lb:chess:weekly:2026-W38") == 3

    async def test_rebuilt_index_agrees_with_postgres_exactly(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """The differential assertion, on identifiers chosen to break naive orders."""
        standings = [
            ("player_99", 500),
            ("player_77", 500),
            ("player_1204", 500),
            ("Player_A", 500),
            ("player_a", 500),
            ("player.c", 500),
            ("player-b", 500),
            ("zzz", 900),
            ("aaa", 100),
        ]
        await seed_postgres_only(db, standings)

        await rebuild_service.rebuild_index(db, redis_client)

        assert await redis_order(redis_client) == await pg_order(db)

    async def test_scores_round_trip_exactly(self, db: AsyncEngine, redis_client: Redis) -> None:
        from app.domain.identifiers import MAX_SCORE

        await seed_postgres_only(db, [("top", MAX_SCORE), ("zero", 0)])

        await rebuild_service.rebuild_index(db, redis_client)

        assert await redis_client.zscore(ALL_TIME_KEY, "top") == MAX_SCORE
        assert await redis_client.zscore(ALL_TIME_KEY, "zero") == 0

    async def test_ttls_are_applied_during_rebuild(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """A rebuild must not leave windowed boards without an expiry, or
        Redis memory would grow forever after every recovery."""
        await seed_postgres_only(db, [("p1", 100)])

        await rebuild_service.rebuild_index(db, redis_client)

        assert await redis_client.ttl(ALL_TIME_KEY) == -1, "all-time never expires"
        assert await redis_client.ttl("lb:chess:daily:2026-09-16") > 0
        assert await redis_client.ttl("lb:chess:weekly:2026-W38") > 0


class TestRecoveryScenarios:
    async def test_rebuild_after_a_flush(self, db: AsyncEngine, redis_client: Redis) -> None:
        """The accidental-FLUSHALL path."""
        await seed_postgres_only(db, [("p1", 100), ("p2", 200)])
        await rebuild_service.rebuild_index(db, redis_client)
        before = await redis_order(redis_client)

        await redis_client.flushdb()
        assert await redis_client.zcard(ALL_TIME_KEY) == 0

        await rebuild_service.rebuild_index(db, redis_client)

        assert await redis_order(redis_client) == before

    async def test_rebuild_is_idempotent(self, db: AsyncEngine, redis_client: Redis) -> None:
        await seed_postgres_only(db, [("p1", 100), ("p2", 200)])

        first = await rebuild_service.rebuild_index(db, redis_client)
        snapshot = await redis_order(redis_client)
        second = await rebuild_service.rebuild_index(db, redis_client)

        assert (first.boards, first.entries) == (second.boards, second.entries)
        assert await redis_order(redis_client) == snapshot

    async def test_rebuild_restores_historical_boards(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """Boards are enumerated from the entries table, not from today's date,
        so yesterday's daily leaderboard comes back too."""
        async with db.begin() as conn:
            await games.create_game(conn, game_id="chess", name="Chess")
        for day, score in ((15, 400), (16, 900)):
            await scoring.submit_score(
                engine=db,
                redis_client=None,
                game_id="chess",
                user_id="p1",
                score=score,
                now=datetime(2026, 9, day, 12, 0, tzinfo=UTC),
            )

        report = await rebuild_service.rebuild_index(db, redis_client)

        assert report.boards == 4, "all_time, weekly, and two daily boards"
        assert await redis_client.zscore("lb:chess:daily:2026-09-15", "p1") == 400
        assert await redis_client.zscore("lb:chess:daily:2026-09-16", "p1") == 900


class TestConcurrencySafety:
    async def test_a_submission_during_a_rebuild_is_not_lost(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """Why rebuild writes with ZADD GT instead of a shadow key + RENAME.

        A RENAME would atomically replace the live key and silently discard
        every write that arrived while the shadow key was being built. GT
        merges instead: the newer, higher score survives.
        """
        await seed_postgres_only(db, [(f"p{i:03d}", i) for i in range(1, 51)])

        # A live submission lands while the rebuild is in flight.
        async def submit_live() -> None:
            await asyncio.sleep(0)
            await scoring.submit_score(
                engine=db,
                redis_client=redis_client,
                game_id="chess",
                user_id="late_arrival",
                score=999_999,
                now=NOW,
            )

        await asyncio.gather(
            rebuild_service.rebuild_index(db, redis_client, batch_size=5),
            submit_live(),
        )

        assert await redis_client.zscore(ALL_TIME_KEY, "late_arrival") == 999_999
        assert await redis_order(redis_client) == await pg_order(db)

    async def test_rebuild_never_lowers_a_fresher_score(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """GT protects against a rebuild replaying a stale Postgres read."""
        await seed_postgres_only(db, [("p1", 100)])
        # The index already holds something newer than what the rebuild reads.
        await redis_client.zadd(ALL_TIME_KEY, {"p1": 5000})

        await rebuild_service.rebuild_index(db, redis_client)

        assert await redis_client.zscore(ALL_TIME_KEY, "p1") == 5000


class TestScoping:
    async def test_rebuild_can_target_one_game(self, db: AsyncEngine, redis_client: Redis) -> None:
        await seed_postgres_only(db, [("p1", 100)], game_id="chess")
        await seed_postgres_only(db, [("p1", 200)], game_id="tetris")

        report = await rebuild_service.rebuild_index(db, redis_client, game_id="chess")

        assert report.boards == 3
        assert await redis_client.zcard(ALL_TIME_KEY) == 1
        assert await redis_client.zcard("lb:tetris:all_time:ALL") == 0

    async def test_rebuild_can_target_one_period(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        await seed_postgres_only(db, [("p1", 100)])

        report = await rebuild_service.rebuild_index(db, redis_client, period=Period.ALL_TIME)

        assert report.boards == 1
        assert await redis_client.zcard(ALL_TIME_KEY) == 1
        assert await redis_client.zcard("lb:chess:daily:2026-09-16") == 0

    async def test_rebuilding_an_empty_database_is_a_no_op(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        report = await rebuild_service.rebuild_index(db, redis_client)
        assert (report.boards, report.entries, report.ok) == (0, 0, True)


class TestBatching:
    async def test_large_board_rebuilds_across_batches(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """Bounded memory: a rebuild must not hold a whole leaderboard at once."""
        async with db.begin() as conn:
            await games.create_game(conn, game_id="chess", name="Chess")
            for index in range(250):
                await entries.submit_score(
                    conn,
                    game_id="chess",
                    user_id=f"p{index:04d}",
                    score=index,
                    achieved_at=NOW,
                    enqueue_index_sync=False,
                )

        report = await rebuild_service.rebuild_index(db, redis_client, batch_size=25)

        assert report.entries == 750, "250 users x 3 boards"
        assert await redis_client.zcard(ALL_TIME_KEY) == 250
        assert await redis_order(redis_client) == await pg_order(db)


class TestAdminEndpoint:
    async def test_rebuild_requires_the_admin_key(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.post("/v1/admin/leaderboards/rebuild", json={})
        assert response.status_code == 401

    async def test_the_write_key_cannot_trigger_a_rebuild(
        self, api_client: httpx.AsyncClient
    ) -> None:
        """A rebuild is expensive; a leaked game-server key must not start one."""
        response = await api_client.post(
            "/v1/admin/leaderboards/rebuild",
            headers={"X-Admin-Key": AUTH["X-API-Key"]},
            json={},
        )
        assert response.status_code == 401

    async def test_rebuild_over_http_reports_counts(
        self, api_client: httpx.AsyncClient, db: AsyncEngine, redis_client: Redis
    ) -> None:
        await seed_postgres_only(db, [("p1", 100), ("p2", 200)])

        response = await api_client.post("/v1/admin/leaderboards/rebuild", headers=ADMIN, json={})

        assert response.status_code == 200
        payload = response.json()
        assert payload == {"boards": 3, "entries": 6, "failed_boards": []}
        assert await redis_client.zcard(ALL_TIME_KEY) == 2

    async def test_rebuild_accepts_no_body(
        self, api_client: httpx.AsyncClient, db: AsyncEngine
    ) -> None:
        await seed_postgres_only(db, [("p1", 100)])
        response = await api_client.post("/v1/admin/leaderboards/rebuild", headers=ADMIN)
        assert response.status_code == 200

    async def test_rebuild_can_be_scoped_over_http(
        self, api_client: httpx.AsyncClient, db: AsyncEngine
    ) -> None:
        await seed_postgres_only(db, [("p1", 100)])

        response = await api_client.post(
            "/v1/admin/leaderboards/rebuild",
            headers=ADMIN,
            json={"period": "all_time"},
        )

        assert response.status_code == 200
        assert response.json()["boards"] == 1

    async def test_unknown_field_is_rejected(self, api_client: httpx.AsyncClient) -> None:
        response = await api_client.post(
            "/v1/admin/leaderboards/rebuild", headers=ADMIN, json={"games": "chess"}
        )
        assert response.status_code == 422

    async def test_rebuild_without_an_index_returns_503(
        self, degraded_api_client: httpx.AsyncClient
    ) -> None:
        """Honest failure: there is nothing to rebuild into."""
        response = await degraded_api_client.post(
            "/v1/admin/leaderboards/rebuild", headers=ADMIN, json={}
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert response.headers["retry-after"]
