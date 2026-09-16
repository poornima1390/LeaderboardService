"""Score submission contract (Spec.md §4, §5, §11)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.periods import Period
from app.schemas.common import DisplayName, GameId, Score, UserId


class ScoreSubmission(BaseModel):
    """Body of ``POST /v1/scores``.

    ``extra="forbid"`` is deliberate and load-bearing. Silently dropping an
    unrecognised field is how a client ships a typo'd ``user_ID`` to production
    and loses scores for a week. It is also what rejects a client-supplied
    ``achieved_at``: timestamps are server-assigned, because a
    client-controlled clock is both a trust hole and a backdated-score exploit
    against the tiebreak.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "user_id": "player_4417",
                    "game_id": "chess",
                    "score": 18500,
                    "display_name": "Ayo",
                }
            ]
        },
    )

    user_id: UserId
    game_id: GameId
    score: Score
    display_name: DisplayName | None = None

    @field_validator("display_name")
    @classmethod
    def _trim_display_name(cls, value: str | None) -> str | None:
        """Trim, and reject a name that was only whitespace.

        Returning None for "   " rather than raising would silently discard
        what the caller sent; raising tells them their input was wrong.
        """
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must not be blank")
        return trimmed


class Standing(BaseModel):
    """The user's resulting position on one board after a submission."""

    period: Period = Field(description="Which time window this board covers.")
    period_bucket: str = Field(
        description="The specific window, e.g. `ALL`, `2026-09-16`, `2026-W38`.",
        examples=["2026-09-16"],
    )
    score: int = Field(
        description="The user's best score on this board *after* the submission.",
        examples=[18500],
    )
    rank: int | None = Field(
        default=None,
        description=(
            "1-based rank on this board, read back after the write. "
            "Null when the rank index is unavailable — the score is still "
            "durably recorded and will be indexed when the index recovers."
        ),
        examples=[12],
    )
    improved: bool = Field(
        description=(
            "Whether this submission raised the user's standing on this board. "
            "False means the existing best was equal or higher; the request was "
            "still successful."
        )
    )


class ScoreSubmissionResponse(BaseModel):
    """Result of a submission, reported per board.

    One submission updates the all-time, daily and weekly boards together
    (Spec.md D5), and best-score semantics apply per board — so a score can be
    a personal best for today without beating an all-time best. Reporting each
    separately is the only honest answer.
    """

    user_id: str
    game_id: str
    submitted_score: int = Field(description="The score as submitted, before comparison.")
    submitted_at: datetime = Field(description="Server-assigned. Client timestamps are rejected.")
    standings: list[Standing]

    @property
    def improved_any(self) -> bool:
        return any(standing.improved for standing in self.standings)
