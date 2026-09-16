"""The single error envelope and its handlers (Spec.md §7).

Every failure leaving this service — application error, framework validation
error, or unhandled exception — is shaped identically:

    {"error": {"code", "message", "details": [...], "request_id",
               "documentation_url"}}

FastAPI's own handlers produce ``{"detail": ...}`` instead, so they are
overridden in :func:`install_error_handlers`. Clients branch on the stable
``code``, never on ``message``, which we reserve the right to reword.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)

_DOCS_BASE = "https://github.com/poornima1390/LeaderboardService/blob/main/docs/errors.md"


class ErrorCode(StrEnum):
    """Stable, machine-readable error identifiers.

    Each maps to exactly one HTTP status via :data:`ERROR_STATUS`.
    """

    MALFORMED_JSON = "MALFORMED_JSON"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    GAME_NOT_FOUND = "GAME_NOT_FOUND"
    USER_NOT_RANKED = "USER_NOT_RANKED"
    GAME_INACTIVE = "GAME_INACTIVE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.MALFORMED_JSON: status.HTTP_400_BAD_REQUEST,
    ErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.METHOD_NOT_ALLOWED: status.HTTP_405_METHOD_NOT_ALLOWED,
    ErrorCode.VALIDATION_ERROR: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.GAME_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.USER_NOT_RANKED: status.HTTP_404_NOT_FOUND,
    ErrorCode.GAME_INACTIVE: status.HTTP_409_CONFLICT,
    ErrorCode.PAYLOAD_TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
    ErrorCode.DEPENDENCY_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


class ErrorDetail(BaseModel):
    """One specific thing that was wrong, so a client can fix all of them at once."""

    field: str | None = Field(default=None, examples=["score"])
    issue: str = Field(examples=["greater_than_equal"])
    value: Any = Field(default=None, examples=[-50])


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
    request_id: str | None = None
    documentation_url: str | None = None


class ErrorEnvelope(BaseModel):
    """The only error shape this service emits."""

    error: ErrorBody


class ServiceError(Exception):
    """Base class for errors that map to a deliberate HTTP response.

    Carrying the :class:`ErrorCode` on the exception means the handler needs no
    knowledge of individual failure sites, and the status code cannot drift
    away from the code.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        details: list[ErrorDetail] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or []
        self.headers = headers or {}

    @property
    def status_code(self) -> int:
        return ERROR_STATUS[self.code]


class UnauthorizedError(ServiceError):
    code = ErrorCode.UNAUTHORIZED


class GameNotFoundError(ServiceError):
    code = ErrorCode.GAME_NOT_FOUND


class GameInactiveError(ServiceError):
    code = ErrorCode.GAME_INACTIVE


class UserNotRankedError(ServiceError):
    code = ErrorCode.USER_NOT_RANKED


class PayloadTooLargeError(ServiceError):
    code = ErrorCode.PAYLOAD_TOO_LARGE


class DependencyUnavailableError(ServiceError):
    """A backing store we cannot serve without is unreachable."""

    code = ErrorCode.DEPENDENCY_UNAVAILABLE

    def __init__(self, message: str, *, retry_after_s: int = 5, **kwargs: Any) -> None:
        headers = {"Retry-After": str(retry_after_s), **(kwargs.pop("headers", None) or {})}
        super().__init__(message, headers=headers, **kwargs)


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def error_response(
    *,
    code: ErrorCode,
    message: str,
    request_id: str | None = None,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build an envelope response. The one place error JSON is constructed."""
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or [],
            request_id=request_id,
            documentation_url=f"{_DOCS_BASE}#{code.value.lower()}",
        )
    )
    return JSONResponse(
        status_code=ERROR_STATUS[code],
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def _translate_validation_errors(
    raw_errors: list[Any],
) -> tuple[ErrorCode, str, list[ErrorDetail]]:
    """Turn Pydantic's error list into our details list.

    Distinguishes unparseable JSON (400) from well-formed JSON that failed the
    schema (422). They are a meaningfully different fix for the caller: one
    means "your serialiser is broken", the other "your field is wrong".
    """
    if any(err.get("type") == "json_invalid" for err in raw_errors):
        return (
            ErrorCode.MALFORMED_JSON,
            "Request body is not valid JSON",
            [ErrorDetail(field=None, issue="json_invalid", value=None)],
        )

    details: list[ErrorDetail] = []
    for err in raw_errors:
        # loc is ("body", "score") / ("query", "limit") / ("path", "game_id");
        # drop the source segment and join the rest for nested fields.
        loc = [str(part) for part in err.get("loc", ())]
        field = ".".join(loc[1:]) if len(loc) > 1 else (loc[0] if loc else None)
        details.append(
            ErrorDetail(field=field, issue=str(err.get("type", "invalid")), value=err.get("input"))
        )
    return ErrorCode.VALIDATION_ERROR, "Request validation failed", details


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers so no code path can emit a non-envelope error."""

    @app.exception_handler(ServiceError)
    async def _handle_service_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ServiceError)
        logger.info(
            "request.rejected",
            error_code=exc.code.value,
            status_code=exc.status_code,
            reason=exc.message,
        )
        return error_response(
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
            details=exc.details,
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        code, message, details = _translate_validation_errors(list(exc.errors()))
        logger.info(
            "request.invalid",
            error_code=code.value,
            fields=[d.field for d in details if d.field],
        )
        return error_response(
            code=code,
            message=message,
            request_id=_request_id(request),
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
        """Catch framework-raised HTTP errors (404 on an unrouted path, 405, ...).

        Without this, an unknown URL would return Starlette's ``{"detail":
        "Not Found"}`` and break the envelope contract for the most common
        error a client hits while integrating.
        """
        assert isinstance(exc, StarletteHTTPException)
        code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = str(exc.detail) if exc.detail else code.value.replace("_", " ").title()
        return error_response(
            code=code,
            message=message,
            request_id=_request_id(request),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Last resort. Logs everything, returns nothing internal.

        The response body carries only the request_id — no stack trace, no SQL,
        no dependency names. Anything more is a free reconnaissance gift.
        """
        logger.exception(
            "request.unhandled_exception",
            error_type=type(exc).__name__,
            path=request.url.path,
        )
        return error_response(
            code=ErrorCode.INTERNAL_ERROR,
            message="An internal error occurred. Quote the request_id when reporting this.",
            request_id=_request_id(request),
        )


# Framework-raised statuses mapped back onto our own vocabulary.
#
# 404 maps to the generic NOT_FOUND, deliberately *not* GAME_NOT_FOUND: an
# unrouted path is a different problem from an unregistered game slug, and
# telling a client with a typo'd URL that its game does not exist sends it
# debugging the wrong thing. Domain 404s raise GameNotFoundError /
# UserNotRankedError explicitly and never reach this table.
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    status.HTTP_400_BAD_REQUEST: ErrorCode.MALFORMED_JSON,
    status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    status.HTTP_409_CONFLICT: ErrorCode.GAME_INACTIVE,
    status.HTTP_413_CONTENT_TOO_LARGE: ErrorCode.PAYLOAD_TOO_LARGE,
    status.HTTP_422_UNPROCESSABLE_CONTENT: ErrorCode.VALIDATION_ERROR,
    status.HTTP_503_SERVICE_UNAVAILABLE: ErrorCode.DEPENDENCY_UNAVAILABLE,
}
