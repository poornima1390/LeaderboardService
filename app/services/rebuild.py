"""Rebuild the rank index from Postgres (Spec.md §4 admin route, D2).

The index is derived state, and derived state needs a reconstruct path that is
tested rather than theoretical. This is that path, and it is needed for:

* **attaching a rank index to a database that already holds scores** — those
  scores never produced outbox rows, so nothing else would ever index them;
* Redis eviction, or a windowed board's TTL expiring while still interesting;
* an accidental FLUSHALL, or a Valkey/Redis version upgrade that starts empty.

Rebuild writes with ``ZADD GT`` straight into the live keys rather than
building a shadow key and renaming over it. That choice is deliberate:

* it is **concurrency-safe** — a submission landing mid-rebuild keeps its
  value, because GT never lowers a score, whereas a RENAME would silently
  discard every write that arrived during the rebuild;
* it is **resumable** — a rebuild that dies halfway leaves a partially
  corrected index rather than a half-built shadow key, and re-running it
  simply finishes the job.

The cost is that it cannot *remove* a member present in Redis but absent from
Postgres. That can only arise after a deletion, and the service exposes no
delete endpoint yet (Spec.md §9). When it does, full replacement will need a
shadow key plus a replay of anything enqueued during the swap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.logging import get_logger
from app.domain.periods import BoardKey, Period
from app.repositories import entries, rank_index
from app.repositories.rank_index import IndexUpdate

logger = get_logger(__name__)

BATCH_SIZE = 1000


@dataclass
class RebuildReport:
    """What a rebuild actually did, per board and in total."""

    boards: int = 0
    entries: int = 0
    failed_boards: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed_boards


async def rebuild_index(
    engine: AsyncEngine,
    redis_client: Redis,
    *,
    game_id: str | None = None,
    period: Period | None = None,
    batch_size: int = BATCH_SIZE,
) -> RebuildReport:
    """Reconstruct index keys from Postgres.

    Args:
        engine: Postgres, the source of truth.
        redis_client: The index to populate.
        game_id: Restrict to one game; None rebuilds every game.
        period: Restrict to one period; None rebuilds all three.
        batch_size: Rows per Postgres fetch and per Redis pipeline.

    Returns:
        A report of boards and rows written, plus any boards that failed.
        Individual board failures do not abort the run — a single unreachable
        moment should not force restarting a large rebuild from scratch.
    """
    report = RebuildReport()

    async with engine.connect() as conn:
        boards = await entries.list_boards(conn, game_id=game_id, period=period)

    logger.info(
        "rebuild.started",
        boards=len(boards),
        game_id=game_id,
        period=period.value if period else None,
    )

    for board_game_id, board_period, bucket in boards:
        board = BoardKey(game_id=board_game_id, period=board_period, bucket=bucket)
        written = 0
        try:
            # A fresh connection per board keeps each streaming cursor's
            # transaction short, rather than holding one open for the whole run.
            async with engine.connect() as conn:
                async for batch in entries.stream_board(
                    conn,
                    game_id=board_game_id,
                    period=board_period,
                    bucket=bucket,
                    batch_size=batch_size,
                ):
                    await rank_index.apply_updates(
                        redis_client,
                        [
                            IndexUpdate(
                                redis_key=board.redis_key,
                                user_id=row.user_id,
                                score=row.score,
                                period=board_period,
                            )
                            for row in batch
                        ],
                    )
                    written += len(batch)
        except Exception as exc:
            logger.warning(
                "rebuild.board_failed",
                board=str(board),
                error_type=type(exc).__name__,
            )
            report.failed_boards.append(str(board))
            continue

        report.boards += 1
        report.entries += written
        logger.info("rebuild.board_done", board=str(board), entries=written)

    logger.info(
        "rebuild.finished",
        boards=report.boards,
        entries=report.entries,
        failed=len(report.failed_boards),
    )
    return report
