"""Game registry contract."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.identifiers import GAME_NAME_MAX_LENGTH
from app.domain.periods import Period
from app.schemas.common import GameId


class GameCreate(BaseModel):
    """Body of ``POST /v1/admin/games``."""

    model_config = ConfigDict(extra="forbid")

    id: GameId
    name: str = Field(min_length=1, max_length=GAME_NAME_MAX_LENGTH, examples=["Chess"])

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class GameOut(BaseModel):
    id: str
    name: str
    is_active: bool
    created_at: datetime
    periods: list[Period] = Field(
        default_factory=lambda: list(Period),
        description="Time windows available for this game.",
    )


class GameList(BaseModel):
    games: list[GameOut]
