"""Application factory and process lifecycle.

Phase 0 scaffold: configuration, logging, the error envelope, middleware and
readiness. The /v1 domain routes arrive in later phases; the router is mounted
now so the versioned prefix and the OpenAPI grouping are fixed from the start.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.api.v1 import router as v1_router
from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_redis
from app.core.errors import ErrorEnvelope, install_error_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import install_middleware
from app.repositories import rank_index
from app.services.outbox_sweeper import run_sweeper

logger = get_logger(__name__)

API_V1_PREFIX = "/v1"

_DESCRIPTION = """
Real-time global gaming leaderboard.

**Ranking model** — a user's standing on a board is their *best* score
(`max`), which makes score submission idempotent. Boards are keyed by
`game x period`, so one submission updates the all-time, daily and weekly
boards together.

**Storage** — Postgres is the system of record. Redis sorted sets are a
derived, rebuildable index that answers rank queries in `O(log N)`. If Redis
is unavailable the service serves reads from Postgres and reports
`degraded` on `/health`; it does not fail.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the connection pools for the process lifetime.

    Migrations deliberately do NOT run here. Concurrent instances racing to
    migrate on boot is a reliable way to corrupt a deploy, so Alembic runs as a
    separate pre-deploy job (Spec.md §8).
    """
    settings: Settings = app.state.settings

    app.state.engine = create_engine(settings)
    app.state.redis = create_redis(settings)

    logger.info(
        "service.startup",
        version=__version__,
        environment=settings.environment,
        redis_enabled=settings.redis_enabled,
    )

    # The outbox sweeper is what converges the rank index after a Redis
    # outage. It exits on its own when Redis is not configured.
    sweeper = asyncio.create_task(
        run_sweeper(
            app.state.engine,
            app.state.redis,
            interval_s=settings.outbox_sweep_interval_s,
        ),
        name="outbox-sweeper",
    )
    app.state.sweeper = sweeper

    # ZADD GT (Redis >= 6.2) is load-bearing: it is what makes outbox delivery
    # idempotent and order-independent. On an older server every write would
    # silently become an unconditional ZADD, and a stale retry could *lower* a
    # live standing. Checked once, in the background so a slow or unreachable
    # Redis cannot delay startup.
    if app.state.redis is not None:
        app.state.gt_check = asyncio.create_task(
            _warn_if_no_gt(app.state.redis), name="redis-gt-check"
        )

    # Startup does not verify connectivity on purpose: a dependency that is
    # briefly unavailable should leave the instance booting and reporting
    # unhealthy via /health, not crash-looping the container.
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await app.state.engine.dispose()
        logger.info("service.shutdown")


async def _warn_if_no_gt(redis_client: object) -> None:
    """Log loudly if the Redis server predates ZADD GT."""
    from redis.asyncio import Redis

    assert isinstance(redis_client, Redis)
    if not await rank_index.supports_gt(redis_client):
        logger.error(
            "redis.zadd_gt_unsupported",
            detail=(
                "Redis >= 6.2 is required. Without ZADD GT, a retried or "
                "out-of-order outbox delivery can lower a standing."
            ),
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Takes settings as an argument so tests can construct an app against an
    explicit configuration instead of mutating the environment.
    """
    settings = settings or get_settings()

    configure_logging(
        level=settings.log_level,
        json_output=settings.environment != "local",
    )

    app = FastAPI(
        title="Leaderboard Service",
        version=__version__,
        description=_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        # Declared globally so the envelope shows up on every operation in the
        # generated OpenAPI document rather than being documented per-route.
        responses={
            422: {"model": ErrorEnvelope, "description": "Validation failed"},
            500: {"model": ErrorEnvelope, "description": "Internal error"},
        },
    )
    app.state.settings = settings

    # Middleware is registered before handlers so request_id is bound for them.
    install_middleware(app, max_body_bytes=settings.max_request_body_bytes)
    install_error_handlers(app)

    app.include_router(health_router)
    app.include_router(v1_router, prefix=API_V1_PREFIX)

    return app


app = create_app()
