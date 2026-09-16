"""Game registry access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.errors import GameInactiveError, GameNotFoundError


@dataclass(frozen=True, slots=True)
class GameRecord:
    id: str
    name: str
    is_active: bool
    created_at: datetime


async def get_game(conn: AsyncConnection, game_id: str) -> GameRecord | None:
    result = await conn.execute(
        text("SELECT id, name, is_active, created_at FROM games WHERE id = :id"),
        {"id": game_id},
    )
    row = result.first()
    if row is None:
        return None
    return GameRecord(id=row[0], name=row[1], is_active=row[2], created_at=row[3])


async def require_writable_game(conn: AsyncConnection, game_id: str) -> GameRecord:
    """Fetch a game that can accept scores, or raise the right error.

    Checked explicitly rather than relying on the foreign key, so an unknown
    slug produces a clean ``404 GAME_NOT_FOUND`` instead of an integrity
    violation surfacing as a 500 — and so a retired game is distinguishable
    from a nonexistent one.
    """
    game = await get_game(conn, game_id)
    if game is None:
        raise GameNotFoundError(
            f"Game {game_id!r} is not registered. GET /v1/games lists valid ids."
        )
    if not game.is_active:
        raise GameInactiveError(f"Game {game_id!r} is retired and accepts no new scores")
    return game


async def list_games(conn: AsyncConnection) -> list[GameRecord]:
    result = await conn.execute(
        text("SELECT id, name, is_active, created_at FROM games ORDER BY id")
    )
    return [
        GameRecord(id=row[0], name=row[1], is_active=row[2], created_at=row[3]) for row in result
    ]


async def create_game(conn: AsyncConnection, *, game_id: str, name: str) -> GameRecord:
    """Register a game idempotently.

    ON CONFLICT DO UPDATE rather than DO NOTHING so re-registering with a new
    display name works, and so the RETURNING clause always yields a row —
    letting the caller treat "created" and "already existed" identically.
    """
    result = await conn.execute(
        text(
            """
            INSERT INTO games (id, name) VALUES (:id, :name)
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
            RETURNING id, name, is_active, created_at
            """
        ),
        {"id": game_id, "name": name},
    )
    row = result.one()
    return GameRecord(id=row[0], name=row[1], is_active=row[2], created_at=row[3])
