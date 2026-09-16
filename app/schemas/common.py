"""Field types shared across request and response models.

Constraints are built from ``app.domain.identifiers`` so the API and the
database CHECK constraints cannot disagree about what a valid identifier is.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.domain.identifiers import (
    DISPLAY_NAME_MAX_LENGTH,
    GAME_ID_MAX_LENGTH,
    GAME_ID_PATTERN,
    MAX_SCORE,
    MIN_SCORE,
    USER_ID_MAX_LENGTH,
    USER_ID_PATTERN,
)

GameId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=GAME_ID_MAX_LENGTH,
        pattern=GAME_ID_PATTERN,
        description="Lowercase game slug, e.g. `chess`.",
        examples=["chess"],
    ),
]

UserId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=USER_ID_MAX_LENGTH,
        pattern=USER_ID_PATTERN,
        description=(
            "Caller-supplied player identifier. Letters, digits, `_`, `.` and `-` only: "
            "the value becomes a Redis sorted set member and a URL path segment, so "
            "`:`, `/` and whitespace are rejected."
        ),
        examples=["player_4417"],
    ),
]

Score = Annotated[
    int,
    Field(
        ge=MIN_SCORE,
        le=MAX_SCORE,
        # strict=True rejects every lax coercion Pydantic would otherwise
        # perform, and each one it blocks is a real client bug:
        #
        #   "100"  -> 100   a stringly-typed client, silently accepted
        #   True   -> 1     a boolean score recorded as 1
        #   100.0  -> 100   a float where the contract says integer
        #
        # The boolean case is the compelling one. `True` is an `int` in Python,
        # so lax validation turns a type error in the caller into a legitimate
        # score of 1 that no amount of downstream validation can detect.
        strict=True,
        description=(
            f"Integer score, {MIN_SCORE}..{MAX_SCORE}. Must be a JSON integer: "
            "strings, booleans and floats are rejected rather than coerced. The "
            "upper bound sits far below 2**53, so the value is represented exactly "
            "by the float64 a Redis sorted set score uses."
        ),
        examples=[18500],
    ),
]

DisplayName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=DISPLAY_NAME_MAX_LENGTH,
        description="Optional human-readable name. Whitespace is trimmed.",
        examples=["Ayo"],
    ),
]
