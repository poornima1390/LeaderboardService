"""Request-scoped middleware: correlation IDs, access logging, body size limit."""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response

from app.core.errors import ErrorCode, error_response
from app.core.logging import get_logger

logger = get_logger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"

# Crockford base32, as used by ULID: no I, L, O or U, so the ids survive being
# read aloud or transcribed from a screenshot in a support ticket.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MAX_INBOUND_REQUEST_ID_LEN = 64

Handler = Callable[[Request], Awaitable[Response]]


def new_request_id() -> str:
    """Generate a 26-character ULID.

    Lexicographically sortable by creation time to millisecond precision
    (ids minted within the same millisecond order randomly between themselves),
    which makes grepping a log aggregator for "everything after this id" work
    without a timestamp filter. A UUID4 would be equally unique but unordered.
    """
    timestamp_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    value = (timestamp_ms << 80) | randomness
    return "".join(_CROCKFORD[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


def _sanitise_inbound_request_id(raw: str | None) -> str | None:
    """Accept a caller-supplied correlation id, but only if it is safe.

    Propagating a client's id lets a game server correlate its logs with ours.
    It is still untrusted input that ends up in our log stream, so anything
    with the wrong shape is discarded rather than sanitised in place — a
    partially-cleaned id is worse than a fresh one, because it looks trustworthy.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > _MAX_INBOUND_REQUEST_ID_LEN:
        return None
    if not all(ch.isalnum() or ch in "-_" for ch in candidate):
        return None
    return candidate


def install_middleware(app: FastAPI, *, max_body_bytes: int) -> None:
    """Register middleware.

    Order is load-bearing. Starlette inserts each added middleware at the
    *front* of the stack, so the **last** one registered is the outermost.
    ``request_context`` is therefore registered last, which puts it outside
    everything else and guarantees that:

    * every response — including one short-circuited by the body-size guard —
      carries an ``X-Request-ID`` and appears in the access log, and
    * ``request.state.request_id`` is already set for any inner middleware.

    Registering them the other way round silently produces 413s with a null
    request_id and no log line, which is exactly the response you most want to
    be able to trace.
    """

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next: Handler) -> Response:
        """Reject oversized bodies with 413 (Spec.md §5).

        Checks Content-Length only. A chunked request without the header is not
        rejected here; the real defence for that is the reverse proxy's own body
        limit, which is where a streaming limit belongs. Cheap, correct for
        every well-behaved client, and honest about what it does not cover.
        """
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return error_response(
                    code=ErrorCode.MALFORMED_JSON,
                    message="Invalid Content-Length header",
                    request_id=getattr(request.state, "request_id", None),
                )
            if declared > max_body_bytes:
                return error_response(
                    code=ErrorCode.PAYLOAD_TOO_LARGE,
                    message=f"Request body exceeds the {max_body_bytes} byte limit",
                    request_id=getattr(request.state, "request_id", None),
                )
        return await call_next(request)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Handler) -> Response:
        """Outermost: assign a request id and emit exactly one access log line."""
        request_id = (
            _sanitise_inbound_request_id(request.headers.get(REQUEST_ID_HEADER)) or new_request_id()
        )
        request.state.request_id = request_id

        # clear_contextvars, not bind alone: the context is per-task and could
        # otherwise inherit bindings from a previous request on the same worker.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handler builds the response; this only guarantees
            # the access line is emitted even for an unhandled failure.
            logger.exception(
                "request.failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request.completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response
