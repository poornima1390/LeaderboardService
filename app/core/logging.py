"""Structured JSON logging (Spec.md §8).

One JSON object per line, with ``request_id`` bound to every line emitted
during a request so a user reporting an error hands us the exact key needed to
find it. ``structlog.contextvars`` carries that binding across ``await`` points
without threading it through every call signature.

Everything is routed through the stdlib ``logging`` module rather than printed
directly. That costs one layer of indirection and buys a single pipeline for
our own logs and third-party ones (uvicorn, SQLAlchemy, asyncpg) — without it,
a production log stream is half JSON and half whatever uvicorn felt like.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "sqlalchemy.engine", "alembic")


def _shared_processors() -> list[Any]:
    """Processors applied to both structlog and stdlib-originated records."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure structlog on top of the stdlib logging module.

    Args:
        level: Minimum level to emit.
        json_output: JSON lines when True. False gives coloured, human-readable
            output — what you want locally, and useless to a log aggregator.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    shared = _shared_processors()

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared,
            # Hands the event dict to the stdlib handler's formatter below
            # instead of rendering here, so both log sources share a renderer.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # Applied only to records that did NOT come from structlog, so
            # third-party logs gain the same timestamp/level/context fields.
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True

    # Uvicorn's access log duplicates our request middleware line with less
    # detail and no request_id. Ours is strictly better, so silence theirs.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, typed for mypy --strict."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
