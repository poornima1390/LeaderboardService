"""Shared fixtures.

Environment variables are set here, before any ``app`` module is imported,
because the service is designed to refuse to start without them
(Spec.md §8) — including at import time.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest

TEST_ENV = {
    "ENVIRONMENT": "ci",
    "LOG_LEVEL": "WARNING",
    # Same values CI uses, so a local run and a CI run exercise the same URLs.
    "DATABASE_URL": (
        "postgresql+asyncpg://leaderboard:leaderboard@localhost:5432/leaderboard_test"
    ),
    "REDIS_URL": "redis://localhost:6379/0",
    # Deliberately not prefixed with "test"/"changeme"/etc: the config layer
    # rejects placeholder-looking secrets, and these have to clear that bar.
    "API_KEY": "7f3c91ab5e2d4806bc1f9a73de50c284",
    "ADMIN_API_KEY": "d4a82fe07c6b13594ea2f8db60371cae",
}

for _key, _value in TEST_ENV.items():
    os.environ.setdefault(_key, _value)

# Imported after the environment is populated, not before.
import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def settings() -> Iterator[Settings]:
    """A fresh Settings instance, bypassing the process-wide lru_cache."""
    yield Settings()


@pytest.fixture
def app_instance() -> FastAPI:
    """An app built from the test configuration.

    Lifespan is not run, so ``app.state.engine`` and ``app.state.redis`` are
    absent. That is deliberate: it keeps unit tests free of live dependencies
    and makes the "cannot reach Postgres" path the default, which is the one
    most likely to be wrong.
    """
    return create_app(Settings())


@pytest.fixture
async def client(app_instance: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app_instance)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
