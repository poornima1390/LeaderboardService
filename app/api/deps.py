"""Request dependencies: authentication and store handles (Spec.md §6)."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.errors import UnauthorizedError

API_KEY_HEADER = "X-API-Key"
ADMIN_KEY_HEADER = "X-Admin-Key"


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


def get_redis(request: Request) -> Redis | None:
    """The rank index client, or None when Redis is not configured.

    None is a normal value, not an error: Redis is derived state and the
    service is designed to keep working without it (Spec.md D2).
    """
    client: Redis | None = getattr(request.app.state, "redis", None)
    return client


def _check_key(*, presented: str | None, expected: str, header: str) -> None:
    """Compare a presented key against the configured one, in constant time.

    ``secrets.compare_digest`` rather than ``==``: a short-circuiting
    comparison leaks how many leading characters were correct through response
    timing, which turns a brute-force search of the whole keyspace into a
    character-by-character one.

    The failure message never echoes the presented value — it would land in
    logs and in any intermediary that records response bodies.
    """
    if not presented:
        raise UnauthorizedError(f"Missing {header} header")
    if not secrets.compare_digest(presented, expected):
        raise UnauthorizedError(f"Invalid {header}")


async def require_api_key(request: Request) -> None:
    """Guard score submission.

    Authenticates the *caller*, not the subject: any holder of this key can
    submit a score for any user_id. Per-user authentication is explicitly out
    of scope (Spec.md §6, §9); the production answer is per-game credentials
    with user_id taken from a signed token's `sub` claim.
    """
    settings: Settings = request.app.state.settings
    _check_key(
        presented=request.headers.get(API_KEY_HEADER),
        expected=settings.api_key.get_secret_value(),
        header=API_KEY_HEADER,
    )


async def require_admin_key(request: Request) -> None:
    """Guard administrative routes.

    A separate key from the write key, so a leaked game-server credential
    cannot register games or trigger an index rebuild.
    """
    settings: Settings = request.app.state.settings
    _check_key(
        presented=request.headers.get(ADMIN_KEY_HEADER),
        expected=settings.admin_api_key.get_secret_value(),
        header=ADMIN_KEY_HEADER,
    )


RequireApiKey = Annotated[None, Depends(require_api_key)]
RequireAdminKey = Annotated[None, Depends(require_admin_key)]
