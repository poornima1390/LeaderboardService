"""Redis-outage recovery via the outbox (Spec.md D4).

These are the paths that only exist because the design keeps a derived index,
and they are the ones most likely to be written and never exercised. The
claim being tested is specific: a Redis outage costs *staleness*, never a lost
score, and the index reconverges without operator intervention.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.repositories import entries, games, outbox
from app.services import scoring
from app.services.outbox_sweeper import SweepStats, run_sweeper, sweep_once

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 16, 14, 22, tzinfo=UTC)


class BrokenRedis:
    """A client that fails every command, like a Redis that has gone away."""

    def __init__(self) -> None:
        self.attempts = 0

    def pipeline(self, transaction: bool = True) -> BrokenRedis:
        return self

    def zadd(self, *args: object, **kwargs: object) -> None:
        return None

    def expire(self, *args: object, **kwargs: object) -> None:
        return None

    async def execute(self) -> None:
        self.attempts += 1
        raise RedisConnectionError("connection refused")

    async def zrevrank(self, *args: object) -> int:
        raise RedisConnectionError("connection refused")


async def register(engine: AsyncEngine, game_id: str = "chess") -> None:
    async with engine.begin() as conn:
        await games.create_game(conn, game_id=game_id, name=game_id.title())


async def pending_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            await conn.scalar(text("SELECT count(*) FROM redis_outbox WHERE delivered_at IS NULL"))
            or 0
        )


async def delivered_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            await conn.scalar(
                text("SELECT count(*) FROM redis_outbox WHERE delivered_at IS NOT NULL")
            )
            or 0
        )


class TestWriteSucceedsWhileRedisIsDown:
    async def test_score_is_durable_and_response_still_succeeds(self, db: AsyncEngine) -> None:
        """The central promise: a cache outage must not fail a write."""
        await register(db)
        broken = BrokenRedis()

        result = await scoring.submit_score(
            engine=db,
            redis_client=broken,  # type: ignore[arg-type]
            game_id="chess",
            user_id="p1",
            score=18500,
            now=NOW,
        )

        assert result.index_synced is False
        assert len(result.results) == 3
        assert all(item.standing.score == 18500 for item in result.results)
        # Rank is unknowable without the index, and that is reported honestly
        # rather than guessed.
        assert all(item.rank is None for item in result.results)

        async with db.connect() as conn:
            stored = await conn.scalar(
                text("SELECT score FROM leaderboard_entries WHERE period='all_time'")
            )
        assert stored == 18500, "the score must be durable regardless of Redis"

    async def test_work_is_left_pending_for_the_sweeper(self, db: AsyncEngine) -> None:
        await register(db)
        await scoring.submit_score(
            engine=db,
            redis_client=BrokenRedis(),  # type: ignore[arg-type]
            game_id="chess",
            user_id="p1",
            score=18500,
            now=NOW,
        )

        assert await pending_count(db) == 3
        assert await delivered_count(db) == 0

    async def test_no_redis_configured_behaves_the_same(self, db: AsyncEngine) -> None:
        """REDIS_URL unset is the same degraded path, reached at startup."""
        await register(db)
        result = await scoring.submit_score(
            engine=db, redis_client=None, game_id="chess", user_id="p1", score=100, now=NOW
        )

        assert result.index_synced is False
        assert await pending_count(db) == 3


class TestRecovery:
    async def test_sweeper_drains_the_backlog_after_redis_returns(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """The whole design in one test: outage, backlog, recovery, convergence."""
        await register(db)
        await scoring.submit_score(
            engine=db,
            redis_client=BrokenRedis(),  # type: ignore[arg-type]
            game_id="chess",
            user_id="p1",
            score=18500,
            now=NOW,
        )
        assert await pending_count(db) == 3
        assert await redis_client.zcard("lb:chess:all_time:ALL") == 0

        delivered, failed = await sweep_once(db, redis_client)

        assert (delivered, failed) == (3, 0)
        assert await pending_count(db) == 0
        # The index now agrees with Postgres.
        assert await redis_client.zscore("lb:chess:all_time:ALL", "p1") == 18500
        assert await redis_client.zscore("lb:chess:daily:2026-09-16", "p1") == 18500
        assert await redis_client.zscore("lb:chess:weekly:2026-W38", "p1") == 18500

    async def test_index_converges_to_the_maximum_not_the_last_write(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """Deliveries are applied with ZADD GT, so order cannot matter.

        Scores are enqueued ascending then the rows are *reordered* so the
        stale ones deliver last. A plain ZADD would leave the index showing
        900; GT leaves it showing the true best.
        """
        await register(db)
        for score in (900, 22400, 5000):
            await scoring.submit_score(
                engine=db,
                redis_client=BrokenRedis(),  # type: ignore[arg-type]
                game_id="chess",
                user_id="p1",
                score=score,
                now=NOW,
            )

        # Force worst-case delivery order: highest score first, stale last.
        async with db.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE redis_outbox SET enqueued_at = now() - "
                    "make_interval(secs => score / 100.0)"
                )
            )

        await sweep_once(db, redis_client)

        async with db.connect() as conn:
            truth = await conn.scalar(
                text("SELECT score FROM leaderboard_entries WHERE period='all_time'")
            )
        assert await redis_client.zscore("lb:chess:all_time:ALL", "p1") == truth == 22400

    async def test_sweeping_an_empty_backlog_is_free(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        assert await sweep_once(db, redis_client) == (0, 0)

    async def test_redelivering_already_applied_work_is_harmless(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """At-least-once delivery is only safe because ZADD GT is idempotent."""
        await register(db)
        await scoring.submit_score(
            engine=db,
            redis_client=BrokenRedis(),  # type: ignore[arg-type]
            game_id="chess",
            user_id="p1",
            score=18500,
            now=NOW,
        )
        await sweep_once(db, redis_client)

        # Simulate a crash between ZADD and the settle: rows look pending again.
        async with db.begin() as conn:
            await conn.execute(text("UPDATE redis_outbox SET delivered_at = NULL"))

        delivered, failed = await sweep_once(db, redis_client)

        assert (delivered, failed) == (3, 0)
        assert await redis_client.zscore("lb:chess:all_time:ALL", "p1") == 18500


class TestFailureAccounting:
    async def test_failed_delivery_increments_attempts_and_stays_pending(
        self, db: AsyncEngine
    ) -> None:
        """A rising attempts count is the signal that the index is drifting."""
        await register(db)
        async with db.begin() as conn:
            await entries.submit_score(
                conn,
                game_id="chess",
                user_id="p1",
                score=100,
                achieved_at=NOW,
            )

        delivered, failed = await sweep_once(db, BrokenRedis())  # type: ignore[arg-type]

        assert (delivered, failed) == (0, 3)
        assert await pending_count(db) == 3
        async with db.connect() as conn:
            attempts = await conn.scalar(text("SELECT min(attempts) FROM redis_outbox"))
        assert attempts == 1

    async def test_repeated_failures_accumulate(self, db: AsyncEngine) -> None:
        await register(db)
        async with db.begin() as conn:
            await entries.submit_score(
                conn, game_id="chess", user_id="p1", score=100, achieved_at=NOW
            )

        for _ in range(3):
            await sweep_once(db, BrokenRedis())  # type: ignore[arg-type]

        async with db.connect() as conn:
            assert await conn.scalar(text("SELECT min(attempts) FROM redis_outbox")) == 3
        assert await pending_count(db) == 3


class TestConcurrentSweepers:
    async def test_two_sweepers_split_the_work_without_duplicating_it(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """FOR UPDATE SKIP LOCKED, which matters at instance_count > 1.

        Each sweeper locks the rows it claims and the other skips them rather
        than blocking. Together they must deliver exactly the backlog — never
        more, which would mean the same row was claimed twice.
        """
        await register(db)
        for index in range(20):
            await scoring.submit_score(
                engine=db,
                redis_client=BrokenRedis(),  # type: ignore[arg-type]
                game_id="chess",
                user_id=f"p{index:02d}",
                score=index + 1,
                now=NOW,
            )
        backlog = await pending_count(db)
        assert backlog == 60  # 20 users x 3 boards

        results = await asyncio.gather(
            sweep_once(db, redis_client, batch_size=25),
            sweep_once(db, redis_client, batch_size=25),
            sweep_once(db, redis_client, batch_size=25),
        )

        total_delivered = sum(delivered for delivered, _ in results)
        assert total_delivered == backlog, "work was duplicated or dropped"
        assert await pending_count(db) == 0
        assert await redis_client.zcard("lb:chess:all_time:ALL") == 20


class TestPruning:
    async def test_old_delivered_rows_are_removed(self, db: AsyncEngine) -> None:
        """At three rows per submission the outbox would otherwise be the
        largest table in the database."""
        await register(db)
        async with db.begin() as conn:
            await entries.submit_score(
                conn, game_id="chess", user_id="p1", score=100, achieved_at=NOW
            )
            await conn.execute(
                text("UPDATE redis_outbox SET delivered_at = now() - interval '48 hours'")
            )

        async with db.begin() as conn:
            removed = await outbox.prune_delivered(conn, retain_hours=24)

        assert removed == 3
        async with db.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM redis_outbox")) == 0

    async def test_recent_and_pending_rows_are_kept(self, db: AsyncEngine) -> None:
        """Pruning must never touch undelivered work."""
        await register(db)
        async with db.begin() as conn:
            await entries.submit_score(
                conn, game_id="chess", user_id="p1", score=100, achieved_at=NOW
            )

        async with db.begin() as conn:
            removed = await outbox.prune_delivered(conn, retain_hours=24)

        assert removed == 0
        assert await pending_count(db) == 3


class TestStats:
    async def test_pending_stats_report_backlog_and_age(self, db: AsyncEngine) -> None:
        await register(db)
        async with db.begin() as conn:
            await entries.submit_score(
                conn, game_id="chess", user_id="p1", score=100, achieved_at=NOW
            )
            await conn.execute(
                text("UPDATE redis_outbox SET enqueued_at = now() - interval '90 seconds'")
            )

        async with db.connect() as conn:
            count, oldest_age = await outbox.pending_stats(conn)

        assert count == 3
        assert oldest_age is not None and oldest_age >= 89

    async def test_empty_backlog_reports_no_age(self, db: AsyncEngine) -> None:
        async with db.connect() as conn:
            assert await outbox.pending_stats(conn) == (0, None)


class TestSweeperTask:
    async def test_sweeper_exits_immediately_without_redis(self, db: AsyncEngine) -> None:
        """No index to sync means no reason for the task to exist."""
        await asyncio.wait_for(run_sweeper(db, None, interval_s=0.01), timeout=2)

    async def test_running_sweeper_drains_a_backlog_on_its_own(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """End to end through the background task, not just sweep_once."""
        await register(db)
        await scoring.submit_score(
            engine=db,
            redis_client=BrokenRedis(),  # type: ignore[arg-type]
            game_id="chess",
            user_id="p1",
            score=18500,
            now=NOW,
        )

        stats = SweepStats()
        task = asyncio.create_task(run_sweeper(db, redis_client, interval_s=0.02, stats=stats))
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while await pending_count(db) > 0:
                assert asyncio.get_running_loop().time() < deadline, "sweeper never drained"
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert stats.delivered == 3
        assert await redis_client.zscore("lb:chess:all_time:ALL", "p1") == 18500

    async def test_sweeper_survives_a_failing_pass(
        self, db: AsyncEngine, redis_client: Redis
    ) -> None:
        """An exception escaping the loop would silently stop convergence.

        The task must keep running through a Redis outage and pick the work up
        when it clears — with nothing more than a delay.
        """
        await register(db)
        async with db.begin() as conn:
            await entries.submit_score(
                conn, game_id="chess", user_id="p1", score=100, achieved_at=NOW
            )

        broken = BrokenRedis()
        stats = SweepStats()
        task = asyncio.create_task(
            run_sweeper(db, broken, interval_s=0.02, stats=stats)  # type: ignore[arg-type]
        )
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while stats.failed == 0:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.05)
            assert not task.done(), "the sweeper died instead of backing off"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert await pending_count(db) == 3, "work is retained, not discarded"
