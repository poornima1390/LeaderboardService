"""API key authentication (Spec.md §6)."""

from __future__ import annotations

import inspect

import pytest

from app.api import deps
from app.core.errors import ErrorCode, UnauthorizedError

KEY = "7f3c91ab5e2d4806bc1f9a73de50c284"


class TestKeyComparison:
    def test_accepts_the_configured_key(self) -> None:
        deps._check_key(presented=KEY, expected=KEY, header="X-API-Key")

    def test_missing_key_is_unauthorized(self) -> None:
        with pytest.raises(UnauthorizedError) as exc:
            deps._check_key(presented=None, expected=KEY, header="X-API-Key")
        assert exc.value.code is ErrorCode.UNAUTHORIZED
        assert "Missing" in exc.value.message

    def test_empty_key_is_unauthorized(self) -> None:
        with pytest.raises(UnauthorizedError):
            deps._check_key(presented="", expected=KEY, header="X-API-Key")

    @pytest.mark.parametrize(
        "presented",
        [
            "x" * 32,  # same length, wrong value
            KEY[:-1],  # one char short
            KEY + "a",  # one char long
            KEY.upper(),  # case matters
            KEY[:-1] + "0",  # differs only in the last char
            "7f3c91ab",  # correct prefix only
        ],
    )
    def test_wrong_keys_are_rejected(self, presented: str) -> None:
        with pytest.raises(UnauthorizedError):
            deps._check_key(presented=presented, expected=KEY, header="X-API-Key")

    def test_failure_message_never_echoes_the_presented_value(self) -> None:
        """The message reaches logs and any intermediary recording bodies."""
        secret = "super-secret-attempt-value"
        with pytest.raises(UnauthorizedError) as exc:
            deps._check_key(presented=secret, expected=KEY, header="X-API-Key")
        assert secret not in exc.value.message
        assert KEY not in exc.value.message

    def test_uses_a_constant_time_comparison(self) -> None:
        """`==` short-circuits, leaking the length of the correct prefix through
        response timing and turning a keyspace search into a per-character one.

        Asserted by inspection rather than by timing: a timing test on a shared
        CI runner is noise, and would either be flaky or prove nothing.
        """
        source = inspect.getsource(deps._check_key)
        assert "compare_digest" in source
        assert "presented == expected" not in source


class TestHeaderNames:
    def test_write_and_admin_headers_are_distinct(self) -> None:
        """One leaked game-server key must not grant administrative access."""
        assert deps.API_KEY_HEADER != deps.ADMIN_KEY_HEADER
        assert deps.API_KEY_HEADER == "X-API-Key"
        assert deps.ADMIN_KEY_HEADER == "X-Admin-Key"

    def test_header_name_appears_in_the_error(self) -> None:
        """So a caller can tell *which* credential was wrong."""
        with pytest.raises(UnauthorizedError, match="X-Admin-Key"):
            deps._check_key(presented=None, expected=KEY, header="X-Admin-Key")
