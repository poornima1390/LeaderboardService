"""Health endpoint status mapping (Spec.md §4).

The mapping is load-bearing operationally: a load balancer acts on it. Redis
down must stay in the pool; Postgres down must leave it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest

from app.api.health import (
    CheckStatus,
    DependencyCheck,
    probe_postgres,
    probe_redis,
    resolve_status,
)


class TestResolveStatus:
    """The pure decision function, exhaustively."""

    def test_both_up_is_ok(self) -> None:
        assert resolve_status(_up(), _up()) == "ok"

    def test_redis_down_is_degraded_not_unhealthy(self) -> None:
        """The whole point: a cache outage must not empty the LB pool."""
        assert resolve_status(_up(), _down()) == "degraded"

    def test_redis_not_configured_is_degraded(self) -> None:
        assert resolve_status(_up(), _absent()) == "degraded"

    @pytest.mark.parametrize(
        "redis_state",
        [CheckStatus.UP, CheckStatus.DOWN, CheckStatus.NOT_CONFIGURED],
    )
    def test_postgres_down_is_unhealthy_regardless_of_redis(self, redis_state: CheckStatus) -> None:
        """Postgres is the system of record; without it nothing can be served."""
        assert resolve_status(_down(), DependencyCheck(status=redis_state)) == "unhealthy"

    def test_postgres_not_configured_is_unhealthy(self) -> None:
        """No DATABASE_URL means nothing can be served — never 'degraded'."""
        assert resolve_status(_absent(), _up()) == "unhealthy"


class TestProbes:
    async def test_postgres_probe_with_no_engine_is_not_configured(self) -> None:
        assert (await probe_postgres(None)).status is CheckStatus.NOT_CONFIGURED

    async def test_redis_probe_with_no_client_is_not_configured(self) -> None:
        assert (await probe_redis(None)).status is CheckStatus.NOT_CONFIGURED

    async def test_redis_probe_reports_down_without_leaking_details(self) -> None:
        """The error field must carry a class name, never a connection string."""

        class ExplodingRedis:
            async def ping(self) -> bool:
                raise ConnectionError("redis://user:hunter2@cache.internal:6379 refused")

        check = await probe_redis(ExplodingRedis())  # type: ignore[arg-type]
        assert check.status is CheckStatus.DOWN
        assert check.error == "ConnectionError"
        assert "hunter2" not in str(check.model_dump())

    async def test_probe_times_out_rather_than_hanging(self) -> None:
        """A hung probe is indistinguishable from a hung service to an LB."""

        class HangingRedis:
            async def ping(self) -> bool:
                await asyncio.sleep(30)
                return True

        check = await asyncio.wait_for(
            probe_redis(HangingRedis()),  # type: ignore[arg-type]
            timeout=5,
        )
        assert check.status is CheckStatus.DOWN


class TestHealthEndpoint:
    async def test_returns_503_when_postgres_unreachable(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.api.health.probe_postgres", _stub(_down()))
        monkeypatch.setattr("app.api.health.probe_redis", _stub(_up()))

        response = await client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert response.headers["retry-after"] == "5"

    async def test_returns_200_degraded_when_only_redis_is_down(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.api.health.probe_postgres", _stub(_up()))
        monkeypatch.setattr("app.api.health.probe_redis", _stub(_down()))

        response = await client.get("/health")

        assert response.status_code == 200, "a cache outage must not fail readiness"
        assert response.json()["status"] == "degraded"

    async def test_returns_200_ok_when_all_dependencies_up(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.api.health.probe_postgres", _stub(_up()))
        monkeypatch.setattr("app.api.health.probe_redis", _stub(_up()))

        response = await client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"]["postgres"]["status"] == "up"
        assert body["checks"]["redis"]["status"] == "up"
        assert body["version"]

    async def test_response_carries_request_id_header(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.api.health.probe_postgres", _stub(_up()))
        monkeypatch.setattr("app.api.health.probe_redis", _stub(_up()))

        response = await client.get("/health")

        assert len(response.headers["x-request-id"]) == 26


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _up() -> DependencyCheck:
    return DependencyCheck(status=CheckStatus.UP, latency_ms=1.0)


def _down() -> DependencyCheck:
    return DependencyCheck(status=CheckStatus.DOWN, error="ConnectionError")


def _absent() -> DependencyCheck:
    return DependencyCheck(status=CheckStatus.NOT_CONFIGURED)


def _stub(result: DependencyCheck) -> Callable[[object], Awaitable[DependencyCheck]]:
    async def _probe(_: object) -> DependencyCheck:
        return result

    return _probe
