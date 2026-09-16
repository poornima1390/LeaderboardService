"""Backing store lifecycle: the Postgres engine and the Redis client.

No ORM session factory is exposed. The models exist to define the schema and
drive migrations, but every query in app/repositories is hand-written Core SQL
on a connection — the ranking queries have to align exactly with a specific
index and ordering (Spec.md D3), and an ORM layer would obscure the one thing
about them that must not drift.

Postgres is the system of record; Redis is a derived, rebuildable rank index
(Spec.md D2). That asymmetry is enforced here: the engine is required for the
service to function, while the Redis client is optional and its absence is a
degraded — not fatal — state.
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def asyncpg_connect_args(settings: Settings) -> dict[str, Any]:
    """Driver arguments that cannot be expressed in the URL.

    Shared by the application engine and by Alembic (migrations/env.py), so
    the two negotiate TLS identically. They previously did not, and the
    difference hid a real failure: migrations connected successfully while the
    service could not, because only one of them set an ``ssl`` argument.

    * ``ssl`` — the asyncpg *dialect* rejects libpq's ``sslmode`` query
      parameter, so config strips it from the URL and it is re-applied here.
      The value is passed through as the libpq mode string rather than as a
      boolean: asyncpg understands 'disable', 'allow', 'prefer', 'require',
      'verify-ca' and 'verify-full' with libpq's exact semantics.

      This distinction is not cosmetic. ``ssl=True`` means *encrypt and fully
      verify the certificate* (equivalent to verify-full), whereas
      ``sslmode=require`` — what DigitalOcean's connection string actually
      asks for — means *encrypt without verifying*. Mapping require to True
      silently raises the requirement and fails with
      SSLCertVerificationError, because the provider's CA is not in the
      container's trust store.

      Upgrading to verify-full is the right end state, but it needs the
      provider CA bundle shipped in the image and pinned; until then we honour
      exactly what the connection string requests.
    * ``statement_cache_size`` — must be 0 behind a transaction-mode pooler,
      where a prepared statement can be handed a different backend connection
      than the one that prepared it (Spec.md §8).
    """
    connect_args: dict[str, Any] = {
        "statement_cache_size": settings.db_statement_cache_size,
        "timeout": 10,
    }
    if settings.db_ssl_mode is not None:
        connect_args["ssl"] = settings.db_ssl_mode
    return connect_args


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async Postgres engine."""
    return create_async_engine(
        settings.database_url,
        connect_args=asyncpg_connect_args(settings),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Managed Postgres and pgbouncer both drop idle connections; recycling
        # below their timeout avoids handing the app a dead socket.
        pool_recycle=1800,
        pool_pre_ping=True,
        echo=False,
    )


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
