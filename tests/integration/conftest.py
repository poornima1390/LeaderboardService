"""Integration fixtures: real Postgres and real Redis.

These tests are skipped when the stores are unreachable, so `make test` works
on a bare checkout. CI always has both, so the skips never hide a regression
there.

Mocks are deliberately not used. The central risk in this design is Postgres
and Redis disagreeing about ranking (Spec.md D2/D3), and a mock cannot
disagree with anything.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.main import create_app
from app.models import Base

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]

PROBE_TIMEOUT_S = 3.0


async def _create_schema() -> bool:
    """Drop and recreate the schema. Returns False if Postgres is unreachable."""
    engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"timeout": PROBE_TIMEOUT_S}
    )
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S * 4), engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def schema_ready() -> bool:
    """Build the schema once per session, in its own event loop.

    A plain synchronous fixture calling ``asyncio.run`` rather than a
    session-scoped async fixture: pytest-asyncio runs tests in function-scoped
    loops, and an asyncpg connection is bound to the loop that created it, so a
    session-scoped async engine would hand tests connections belonging to a
    closed loop.

    The schema comes from ``Base.metadata``, which asserts the *models* are
    right. CI separately proves the migration agrees with them by running
    `alembic upgrade head` and then `alembic check`, which fails on any drift.
    """
    return asyncio.run(_create_schema())


@pytest_asyncio.fixture
async def pg_engine(schema_ready: bool) -> AsyncIterator[AsyncEngine]:
    if not schema_ready:
        pytest.skip(f"Postgres unreachable at {DATABASE_URL.rsplit('@', 1)[-1]}")

    # NullPool: no connection outlives the test's event loop.
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def pg(pg_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection whose work is rolled back after each test.

    An outer transaction that is never committed gives perfect isolation
    between tests without recreating the schema, and without TRUNCATE's habit
    of leaving sequences in a surprising state.
    """
    async with pg_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """A flushed Redis database, isolated per test."""
    client: Redis = Redis.from_url(
        REDIS_URL, decode_responses=True, socket_connect_timeout=PROBE_TIMEOUT_S
    )
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip(f"Redis unreachable at {REDIS_URL}")

    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


# --------------------------------------------------------------------------- #
# Fixtures for tests that need *committed* data
#
# The `pg` fixture above rolls back, which gives perfect isolation but makes
# concurrency untestable: two transactions racing for the same row must both
# be able to commit. These fixtures commit for real and clean up afterwards.
# --------------------------------------------------------------------------- #

TRUNCATE_ALL = text(
    "TRUNCATE leaderboard_entries, redis_outbox, users, games RESTART IDENTITY CASCADE"
)


@pytest_asyncio.fixture
async def db(schema_ready: bool) -> AsyncIterator[AsyncEngine]:
    """An engine against an empty database, truncated before and after."""
    if not schema_ready:
        pytest.skip(f"Postgres unreachable at {DATABASE_URL.rsplit('@', 1)[-1]}")

    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(TRUNCATE_ALL)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(TRUNCATE_ALL)
        await engine.dispose()


@pytest_asyncio.fixture
async def api_client(db: AsyncEngine, redis_client: Redis) -> AsyncIterator[httpx.AsyncClient]:
    """The real app, wired to real stores.

    Lifespan is not run, so the background outbox sweeper does not start.
    That is deliberate: a sweeper draining rows concurrently would make
    assertions about the pending backlog racy, and the sweeper has its own
    dedicated tests.
    """
    app = create_app(Settings())
    app.state.engine = db
    app.state.redis = redis_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def degraded_api_client(db: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """The app with NO rank index, exercising the degraded write path."""
    app = create_app(Settings())
    app.state.engine = db
    app.state.redis = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
