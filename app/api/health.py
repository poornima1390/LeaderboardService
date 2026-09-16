"""Readiness endpoint (Spec.md §4).

The status mapping is the substance of this module:

* Postgres unreachable -> ``503 unhealthy``. Nothing can be served or written.
* Redis unreachable    -> ``200 degraded``. Reads fall back to Postgres and
  writes still commit durably via the outbox (D4), so the instance *is*
  serving and must stay in the load balancer pool.

Returning 503 for a degraded-but-working service would pull the entire fleet
out over a cache outage, which is precisely the wrong reaction.
"""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.core.logging import get_logger
from app.repositories import outbox

logger = get_logger(__name__)
router = APIRouter(tags=["ops"])

# A health probe must fail fast: a hung check is indistinguishable from a hung
# service to a load balancer, and a slow probe turns into cascading timeouts.
PROBE_TIMEOUT_S = 2.0


class CheckStatus(StrEnum):
    UP = "up"
    DOWN = "down"
    NOT_CONFIGURED = "not_configured"


class DependencyCheck(BaseModel):
    status: CheckStatus
    latency_ms: float | None = None
    error: str | None = Field(
        default=None,
        description="Failure class only, never a connection string or credential.",
    )


class OutboxLag(BaseModel):
    """How far the rank index is behind Postgres (Spec.md D4, §8).

    The pair worth alerting on: a rising count means the index is falling
    behind, and a rising oldest-age means something is stuck rather than
    merely busy. Reported but deliberately NOT part of the status verdict —
    a backlog means stale ranks, not an inability to serve, and pulling the
    instance out of the pool would only slow the drain.
    """

    pending: int = Field(description="Undelivered index updates.")
    oldest_pending_age_s: float | None = Field(
        default=None, description="Age of the oldest undelivered update, in seconds."
    )


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "unhealthy"]
    version: str
    checks: dict[str, DependencyCheck]
    outbox: OutboxLag | None = None


async def probe_postgres(engine: AsyncEngine | None) -> DependencyCheck:
    """``SELECT 1`` against the pool, bounded by PROBE_TIMEOUT_S."""
    if engine is None:
        return DependencyCheck(status=CheckStatus.NOT_CONFIGURED)
    started = time.perf_counter()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("health.postgres_down", error_type=type(exc).__name__)
        # Class name only. The exception text can contain the host, user and
        # occasionally the password from a DSN.
        return DependencyCheck(status=CheckStatus.DOWN, error=type(exc).__name__)
    return DependencyCheck(
        status=CheckStatus.UP,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


async def probe_redis(client: Redis | None) -> DependencyCheck:
    """``PING`` the rank index. NOT_CONFIGURED is a normal state, not an error."""
    if client is None:
        return DependencyCheck(status=CheckStatus.NOT_CONFIGURED)
    started = time.perf_counter()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            await client.ping()
    except Exception as exc:
        logger.warning("health.redis_down", error_type=type(exc).__name__)
        return DependencyCheck(status=CheckStatus.DOWN, error=type(exc).__name__)
    return DependencyCheck(
        status=CheckStatus.UP,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


def resolve_status(
    postgres: DependencyCheck, redis: DependencyCheck
) -> Literal["ok", "degraded", "unhealthy"]:
    """Collapse the dependency checks into one verdict.

    Pure and separately tested, because this decision — not the probes — is
    what a load balancer acts on.
    """
    if postgres.status is not CheckStatus.UP:
        return "unhealthy"
    if redis.status is CheckStatus.UP:
        return "ok"
    return "degraded"


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Readiness: can this instance serve traffic?",
    responses={
        200: {"description": "Serving, either fully ('ok') or degraded"},
        503: {"description": "Cannot serve — Postgres is unreachable"},
    },
)
async def health(request: Request, response: Response) -> HealthResponse:
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    redis_client: Redis | None = getattr(request.app.state, "redis", None)

    # Probe concurrently: sequential probes would make the endpoint's worst
    # case the sum of both timeouts.
    postgres_check, redis_check = await asyncio.gather(
        probe_postgres(engine),
        probe_redis(redis_client),
    )

    overall = resolve_status(postgres_check, redis_check)
    if overall == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        response.headers["Retry-After"] = "5"

    # Only meaningful when Postgres answered; skipped otherwise so an
    # unhealthy response is not delayed by a second doomed query.
    outbox_lag: OutboxLag | None = None
    if postgres_check.status is CheckStatus.UP and engine is not None:
        outbox_lag = await _probe_outbox(engine)

    return HealthResponse(
        status=overall,
        version=__version__,
        checks={"postgres": postgres_check, "redis": redis_check},
        outbox=outbox_lag,
    )


async def _probe_outbox(engine: AsyncEngine) -> OutboxLag | None:
    """Read the outbox backlog. Never affects the health verdict."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S), engine.connect() as conn:
            pending, oldest_age = await outbox.pending_stats(conn)
    except Exception as exc:
        logger.warning("health.outbox_probe_failed", error_type=type(exc).__name__)
        return None
    return OutboxLag(pending=pending, oldest_pending_age_s=oldest_age)
