"""Backing store lifecycle: the Postgres engine and the Redis client.

Postgres is the system of record; Redis is a derived, rebuildable rank index
(Spec.md D2). That asymmetry is enforced here: the engine is required for the
service to function, while the Redis client is optional and its absence is a
degraded — not fatal — state.
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[Any]


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async Postgres engine.

    ``connect_args`` carries the two things the asyncpg dialect needs that
    cannot live in the URL:

    * ``ssl`` — the dialect rejects libpq's ``sslmode`` query parameter, so
      config strips it from the URL and it is re-applied here.
    * ``statement_cache_size`` — must be 0 behind a transaction-mode pooler,
      where a prepared statement can be handed a different backend connection
      than the one that prepared it (Spec.md §8).
    """
    connect_args: dict[str, Any] = {
        "statement_cache_size": settings.db_statement_cache_size,
        "timeout": 10,
    }
    if settings.db_ssl_mode is not None:
        # asyncpg takes ssl=True for "encrypt, and verify if we can", which is
        # what sslmode=require/verify-* all reduce to at this layer.
        connect_args["ssl"] = True

    return create_async_engine(
        settings.database_url,
        connect_args=connect_args,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Managed Postgres and pgbouncer both drop idle connections; recycling
        # below their timeout avoids handing the app a dead socket.
        pool_recycle=1800,
        pool_pre_ping=True,
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def create_redis(settings: Settings) -> Redis | None:
    """Build the Redis client, or None when Redis is not configured.

    Returning None rather than raising is the whole point: the service is
    designed to serve reads from Postgres when the rank index is unavailable,
    so "no Redis" is a startup-time expression of that same degraded path.
    """
    if settings.redis_url is None:
        logger.warning(
            "redis.not_configured",
            detail="Serving in degraded mode: rank queries will use the Postgres fallback",
        )
        return None

    client: Redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=2.0,
        socket_connect_timeout=2.0,
        health_check_interval=30,
    )
    return client
