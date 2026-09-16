"""Game registry endpoints.

``POST /v1/admin/games`` is an addition to the original spec, which described
the registry but gave no way to populate it — without registration every
submission would 404 and the service would be unusable. It sits behind the
admin key rather than the write key, so a leaked game-server credential cannot
create games.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.deps import RequireAdminKey
from app.core.errors import ErrorEnvelope, GameNotFoundError
from app.repositories import games
from app.schemas.games import GameCreate, GameList, GameOut

router = APIRouter(tags=["games"])


def _to_out(record: games.GameRecord) -> GameOut:
    return GameOut(
        id=record.id,
        name=record.name,
        is_active=record.is_active,
        created_at=record.created_at,
    )


@router.get(
    "/games",
    response_model=GameList,
    summary="List registered games",
    description=(
        "Public. Lets a client discover valid game slugs and the periods "
        "available for each, rather than hardcoding them."
    ),
)
async def list_games(request: Request) -> GameList:
    async with request.app.state.engine.connect() as conn:
        records = await games.list_games(conn)
    return GameList(games=[_to_out(record) for record in records])


@router.get(
    "/games/{game_id}",
    response_model=GameOut,
    summary="Fetch one game",
    responses={404: {"model": ErrorEnvelope, "description": "Game is not registered"}},
)
async def get_game(request: Request, game_id: str) -> GameOut:
    async with request.app.state.engine.connect() as conn:
        record = await games.get_game(conn, game_id)
    if record is None:
        raise GameNotFoundError(f"Game {game_id!r} is not registered")
    return _to_out(record)


@router.post(
    "/admin/games",
    response_model=GameOut,
    status_code=status.HTTP_200_OK,
    summary="Register a game",
    responses={401: {"model": ErrorEnvelope, "description": "Missing or invalid X-Admin-Key"}},
    description=(
        "Idempotent: re-registering an existing slug updates its display name "
        "and returns the existing record, so this is safe to run from a "
        "provisioning script. 200 rather than 201 for that reason — the call "
        "does not reliably create anything."
    ),
)
async def create_game(
    request: Request,
    payload: GameCreate,
    _: RequireAdminKey,
) -> GameOut:
    async with request.app.state.engine.begin() as conn:
        record = await games.create_game(conn, game_id=payload.id, name=payload.name)
    return _to_out(record)
