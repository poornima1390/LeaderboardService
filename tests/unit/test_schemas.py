"""Request-model validation (Spec.md §5).

Exhaustive and fast: these run without any dependency, so the edge cases are
cheap to cover here rather than over HTTP.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.identifiers import MAX_SCORE
from app.schemas.games import GameCreate
from app.schemas.scores import ScoreSubmission


def submission(**overrides: object) -> ScoreSubmission:
    payload: dict[str, object] = {"user_id": "player_4417", "game_id": "chess", "score": 100}
    payload.update(overrides)
    return ScoreSubmission(**payload)


class TestUserId:
    @pytest.mark.parametrize("user_id", ["p", "player_4417", "Player.A", "a-b_c.d", "99", "u" * 64])
    def test_accepts_valid(self, user_id: str) -> None:
        assert submission(user_id=user_id).user_id == user_id

    @pytest.mark.parametrize(
        "user_id",
        [
            "",
            "u" * 65,
            "player:1",  # Redis key separator — would forge a board key
            "player/1",  # breaks URL routing
            "player 1",
            "player\n1",
            "player\x001",
            "player#1",
            "pläyer",  # non-ASCII: bytes differ from what callers expect
        ],
    )
    def test_rejects_invalid(self, user_id: str) -> None:
        with pytest.raises(ValidationError):
            submission(user_id=user_id)


class TestGameId:
    @pytest.mark.parametrize("game_id", ["c", "chess", "tetris-99", "0ad", "a" * 64])
    def test_accepts_valid(self, game_id: str) -> None:
        assert submission(game_id=game_id).game_id == game_id

    @pytest.mark.parametrize(
        "game_id", ["", "Chess", "-chess", "chess_1", "chess:blitz", "a" * 65, "ches s"]
    )
    def test_rejects_invalid(self, game_id: str) -> None:
        with pytest.raises(ValidationError):
            submission(game_id=game_id)


class TestScore:
    @pytest.mark.parametrize("score", [0, 1, 999, MAX_SCORE])
    def test_accepts_in_range(self, score: int) -> None:
        assert submission(score=score).score == score

    @pytest.mark.parametrize("score", [-1, -1000, MAX_SCORE + 1, 10**15])
    def test_rejects_out_of_range(self, score: int) -> None:
        with pytest.raises(ValidationError):
            submission(score=score)

    @pytest.mark.parametrize(
        "score",
        [
            1.5,
            100.0,  # a float where the contract says integer
            "100",  # a stringly-typed client
            "abc",
            None,
            [1],
            float("nan"),
        ],
    )
    def test_rejects_non_integer(self, score: object) -> None:
        """strict=True, so none of these are coerced into a valid score."""
        with pytest.raises(ValidationError):
            submission(score=score)

    def test_rejects_boolean(self) -> None:
        """The compelling case for strict validation.

        `True` is an `int` in Python, so lax coercion would turn a caller's
        type error into a legitimate score of 1 — undetectable downstream.
        """
        with pytest.raises(ValidationError):
            submission(score=True)
        with pytest.raises(ValidationError):
            submission(score=False)


class TestDisplayName:
    def test_is_optional(self) -> None:
        assert submission().display_name is None

    def test_is_trimmed(self) -> None:
        assert submission(display_name="  Ayo  ").display_name == "Ayo"

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n "])
    def test_rejects_blank(self, blank: str) -> None:
        """Returning None for whitespace would silently discard the input."""
        with pytest.raises(ValidationError):
            submission(display_name=blank)

    def test_rejects_overlong(self) -> None:
        with pytest.raises(ValidationError):
            submission(display_name="n" * 65)

    def test_allows_unicode(self) -> None:
        """Unlike user_id, a display name is never a key or a path segment."""
        assert submission(display_name="Ayọ 🎮").display_name == "Ayọ 🎮"


class TestStrictBody:
    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
            submission(nickname="Ayo")

    def test_misspelled_field_is_rejected_not_ignored(self) -> None:
        """The failure this prevents: a typo'd field silently dropped."""
        with pytest.raises(ValidationError):
            ScoreSubmission(user_ID="p1", game_id="chess", score=1)  # type: ignore[call-arg]

    def test_client_timestamp_is_rejected(self) -> None:
        """Server clock only — a client one enables backdating for tiebreaks."""
        with pytest.raises(ValidationError):
            submission(achieved_at="2020-01-01T00:00:00Z")

    def test_rank_cannot_be_asserted_by_the_client(self) -> None:
        with pytest.raises(ValidationError):
            submission(rank=1)


class TestGameCreate:
    def test_accepts_valid(self) -> None:
        game = GameCreate(id="chess", name="  Chess  ")
        assert (game.id, game.name) == ("chess", "Chess")

    @pytest.mark.parametrize("name", ["", "   "])
    def test_rejects_blank_name(self, name: str) -> None:
        with pytest.raises(ValidationError):
            GameCreate(id="chess", name=name)

    def test_rejects_overlong_name(self) -> None:
        with pytest.raises(ValidationError):
            GameCreate(id="chess", name="n" * 129)

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            GameCreate(id="chess", name="Chess", is_active=False)  # type: ignore[call-arg]
