"""Leaderboard read contracts (Spec.md §4, §11)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.periods import Period


class LeaderboardEntry(BaseModel):
    rank: int = Field(description="1-based position on this board.", examples=[1])
    user_id: str = Field(examples=["player_99"])
    display_name: str | None = Field(
        default=None, description="Null when the player has not supplied one."
    )
    score: int = Field(examples=[99820])
    achieved_at: datetime = Field(
        description="When this best score was submitted. Server-assigned."
    )


class LeaderboardPage(BaseModel):
    """Top X of a board (Spec.md §4)."""

    game_id: str
    period: Period
    period_bucket: str = Field(examples=["ALL"])
    total_entries: int = Field(description="Ranked users on this board.")
    limit: int
    offset: int
    entries: list[LeaderboardEntry]
    generated_at: datetime
    source: Literal["redis", "postgres"] = Field(
        description=(
            "Which store answered. `postgres` means the rank index was cold or "
            "unreachable and the fallback served the request — exposed so a "
            "degraded read is visible to the caller rather than silent."
        )
    )


class UserContextResponse(BaseModel):
    """A user's rank plus their surroundings (Spec.md §4)."""

    game_id: str
    period: Period
    period_bucket: str
    total_entries: int
    user: LeaderboardEntry
    above: list[LeaderboardEntry] = Field(
        description="Players immediately better, ascending rank. Empty at rank 1."
    )
    below: list[LeaderboardEntry] = Field(
        description="Players immediately worse, descending rank. Empty at last place."
    )
    percentile: float = Field(
        description=(
            "Share of the board this user is at or above, 0-100. Rank 1 is "
            "exactly 100.0. Players care about this far more than a raw rank "
            "of 40,132."
        ),
        examples=[99.72],
    )
    generated_at: datetime
    source: Literal["redis", "postgres"]
