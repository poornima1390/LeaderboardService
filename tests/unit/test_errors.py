"""The error envelope contract (Spec.md §7).

The guarantee under test: *no* response path emits a non-envelope error,
including ones FastAPI would normally answer itself with `{"detail": ...}`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from app.core.errors import (
    ERROR_STATUS,
    ErrorCode,
    GameInactiveError,
    GameNotFoundError,
    ServiceError,
    UnauthorizedError,
    UserNotRankedError,
    _translate_validation_errors,
)


def assert_is_envelope(body: dict[str, Any]) -> dict[str, Any]:
    """Assert the §7 shape and return the inner error object."""
    assert set(body) == {"error"}, f"top level must be exactly 'error', got {set(body)}"
    error: dict[str, Any] = body["error"]
    assert set(error) >= {"code", "message", "details", "request_id", "documentation_url"}
    assert isinstance(error["details"], list)
    return error


class TestStatusMapping:
    def test_every_code_has_a_status(self) -> None:
        """A code with no status would crash the handler that used it."""
        for code in ErrorCode:
            assert code in ERROR_STATUS, f"{code} is missing from ERROR_STATUS"

    @pytest.mark.parametrize(
        ("exc", "expected_status", "expected_code"),
        [
            (UnauthorizedError("no key"), 401, "UNAUTHORIZED"),
            (GameNotFoundError("nope"), 404, "GAME_NOT_FOUND"),
            (UserNotRankedError("nope"), 404, "USER_NOT_RANKED"),
            (GameInactiveError("retired"), 409, "GAME_INACTIVE"),
        ],
    )
    def test_exception_classes_carry_the_right_status(
        self, exc: ServiceError, expected_status: int, expected_code: str
    ) -> None:
        assert exc.status_code == expected_status
        assert exc.code.value == expected_code

    def test_game_not_found_and_user_not_ranked_share_404_but_differ_in_code(self) -> None:
        """Collapsing these into one 404 sends clients debugging the wrong thing."""
        assert GameNotFoundError("x").status_code == UserNotRankedError("y").status_code == 404
        assert GameNotFoundError("x").code != UserNotRankedError("y").code


class TestValidationTranslation:
    def test_malformed_json_becomes_400_not_422(self) -> None:
        """Unparseable JSON and schema-invalid JSON are different client bugs."""
        code, _, details = _translate_validation_errors(
            [{"type": "json_invalid", "loc": ("body", 0), "msg": "EOF"}]
        )
        assert code is ErrorCode.MALFORMED_JSON
        assert details[0].issue == "json_invalid"

    def test_field_location_drops_the_source_segment(self) -> None:
        code, _, details = _translate_validation_errors(
            [{"type": "greater_than_equal", "loc": ("body", "score"), "input": -50}]
        )
        assert code is ErrorCode.VALIDATION_ERROR
        assert details[0].field == "score"
        assert details[0].value == -50

    def test_nested_field_location_is_dotted(self) -> None:
        _, _, details = _translate_validation_errors(
            [{"type": "missing", "loc": ("body", "player", "id"), "input": None}]
        )
        assert details[0].field == "player.id"

    def test_all_failures_are_reported_together(self) -> None:
        """Clients should be able to fix everything in one round trip."""
        _, _, details = _translate_validation_errors(
            [
                {"type": "too_short", "loc": ("body", "user_id"), "input": ""},
                {"type": "greater_than_equal", "loc": ("body", "score"), "input": -50},
                {"type": "extra_forbidden", "loc": ("body", "achieved_at"), "input": "2020"},
            ]
        )
        assert [d.field for d in details] == ["user_id", "score", "achieved_at"]


class TestEnvelopeOverFramework:
    """Framework-generated errors must not escape in FastAPI's own shape."""

    async def test_unrouted_path_returns_envelope_with_generic_not_found(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/no/such/route")

        assert response.status_code == 404
        error = assert_is_envelope(response.json())
        # NOT_FOUND, not GAME_NOT_FOUND: a typo'd URL is not a bad game slug.
        assert error["code"] == "NOT_FOUND"

    async def test_wrong_method_returns_envelope(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/health")

        assert response.status_code == 405
        assert assert_is_envelope(response.json())["code"] == "METHOD_NOT_ALLOWED"

    async def test_error_response_carries_request_id_matching_header(
        self, client: httpx.AsyncClient
    ) -> None:
        """The id in the body is how a user reports the error; it must match."""
        response = await client.get("/no/such/route")

        error = assert_is_envelope(response.json())
        assert error["request_id"] == response.headers["x-request-id"]

    async def test_documentation_url_points_at_the_code(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/no/such/route")
        assert assert_is_envelope(response.json())["documentation_url"].endswith("#not_found")


class TestServiceErrorHandling:
    """A raised ServiceError must be rendered by the handler, not leak as a 500."""

    @pytest.fixture
    def app_with_failing_route(self, app_instance: FastAPI) -> FastAPI:
        router = APIRouter()

        @router.get("/_test/game-missing")
        async def _missing() -> None:
            raise GameNotFoundError("Game 'nope' is not registered")

        @router.get("/_test/boom")
        async def _boom() -> None:
            raise RuntimeError("password=hunter2 host=db.internal")

        app_instance.include_router(router)
        return app_instance

    @pytest.fixture
    async def failing_client(
        self, app_with_failing_route: FastAPI
    ) -> AsyncIterator[httpx.AsyncClient]:
        transport = httpx.ASGITransport(app=app_with_failing_route, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    async def test_service_error_is_rendered_as_envelope(
        self, failing_client: httpx.AsyncClient
    ) -> None:
        response = await failing_client.get("/_test/game-missing")

        assert response.status_code == 404
        error = assert_is_envelope(response.json())
        assert error["code"] == "GAME_NOT_FOUND"
        assert error["message"] == "Game 'nope' is not registered"

    async def test_unhandled_exception_leaks_nothing_internal(
        self, failing_client: httpx.AsyncClient
    ) -> None:
        """A 500 body must contain the request_id and nothing else useful."""
        response = await failing_client.get("/_test/boom")

        assert response.status_code == 500
        error = assert_is_envelope(response.json())
        assert error["code"] == "INTERNAL_ERROR"
        serialised = json.dumps(response.json())
        for secret in ("hunter2", "db.internal", "RuntimeError", "Traceback"):
            assert secret not in serialised, f"{secret!r} leaked into the 500 response"
        assert error["request_id"], "the request_id is the only thing a user can report"
