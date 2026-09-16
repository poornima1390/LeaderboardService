"""Initial schema: games, users, leaderboard_entries, redis_outbox

Revision ID: 0001_initial
Revises:
Create date: 2026-09-16

Two things in this migration carry the design and are easy to lose in a later
hand-edit:

1. ``COLLATE "C"`` on every column whose value becomes a Redis sorted set
   member (``games.id``, ``users.id``, and the ``game_id`` / ``user_id``
   columns elsewhere). Redis compares members byte-wise; Postgres text
   ordering follows the database collation, which is generally not byte-wise.
   Without this the rank index and the table disagree about ties (Spec.md D3).

2. The direction of ``ix_leaderboard_entries_board_rank``:
   ``score DESC, user_id DESC``. ``ZREVRANGE`` and ``ZREVRANK`` order equal
   scores by member *descending*, so ``ASC`` here would look more natural and
   be wrong.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Byte-wise collation, matching Redis' memcmp on sorted set members.
BYTEWISE = "C"


def upgrade() -> None:
    op.create_table(
        "games",
        sa.Column("id", sa.String(length=64, collation=BYTEWISE), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_games")),
        sa.CheckConstraint("id ~ '^[a-z0-9][a-z0-9-]{0,63}$'", name=op.f("ck_games_id_format")),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 128", name=op.f("ck_games_name_length")),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64, collation=BYTEWISE), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.CheckConstraint("id ~ '^[A-Za-z0-9_.-]{1,64}$'", name=op.f("ck_users_id_format")),
        sa.CheckConstraint(
            "display_name IS NULL OR length(btrim(display_name)) BETWEEN 1 AND 64",
            name=op.f("ck_users_display_name_length"),
        ),
    )

    op.create_table(
        "leaderboard_entries",
        sa.Column("game_id", sa.String(length=64, collation=BYTEWISE), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("period_bucket", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.String(length=64, collation=BYTEWISE), nullable=False),
        sa.Column("score", sa.BigInteger(), nullable=False),
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "game_id",
            "period",
            "period_bucket",
            "user_id",
            name=op.f("pk_leaderboard_entries"),
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name=op.f("fk_leaderboard_entries_game_id_games"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_leaderboard_entries_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "score BETWEEN 0 AND 1000000000000",
            name=op.f("ck_leaderboard_entries_score_range"),
        ),
        sa.CheckConstraint(
            "period IN ('all_time', 'daily', 'weekly')",
            name=op.f("ck_leaderboard_entries_period_valid"),
        ),
        sa.CheckConstraint(
            "(period = 'all_time' AND period_bucket = 'ALL') "
            "OR (period = 'daily' AND period_bucket ~ '^\\d{4}-\\d{2}-\\d{2}$') "
            "OR (period = 'weekly' AND period_bucket ~ '^\\d{4}-W\\d{2}$')",
            name=op.f("ck_leaderboard_entries_bucket_matches_period"),
        ),
    )

    # The ranking index. Direction mirrors Spec.md D3's canonical order, which
    # is what ZREVRANGE / ZREVRANK produce; achieved_at is INCLUDEd so reads
    # are index-only without widening the B-tree.
    op.create_index(
        "ix_leaderboard_entries_board_rank",
        "leaderboard_entries",
        [
            "game_id",
            "period",
            "period_bucket",
            sa.literal_column("score DESC"),
            sa.literal_column("user_id DESC"),
        ],
        unique=False,
        postgresql_include=["achieved_at"],
    )
    op.create_index(
        "ix_leaderboard_entries_user",
        "leaderboard_entries",
        ["user_id", "game_id"],
        unique=False,
    )

    op.create_table(
        "redis_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("redis_key", sa.String(length=160), nullable=False),
        sa.Column("user_id", sa.String(length=64, collation=BYTEWISE), nullable=False),
        sa.Column("score", sa.BigInteger(), nullable=False),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_redis_outbox")),
        sa.CheckConstraint(
            "score BETWEEN 0 AND 1000000000000", name=op.f("ck_redis_outbox_score_range")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_redis_outbox_attempts_non_negative")),
    )

    # Partial: keeps the sweeper's scan proportional to the undelivered
    # backlog rather than to total write volume.
    op.create_index(
        "ix_redis_outbox_pending",
        "redis_outbox",
        ["enqueued_at"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    # Reverse creation order so foreign keys never block a drop. CI asserts
    # this path works (`downgrade base` then `upgrade head`), because a
    # migration that cannot be rolled back is a deploy that cannot be aborted.
    op.drop_index("ix_redis_outbox_pending", table_name="redis_outbox")
    op.drop_table("redis_outbox")
    op.drop_index("ix_leaderboard_entries_user", table_name="leaderboard_entries")
    op.drop_index("ix_leaderboard_entries_board_rank", table_name="leaderboard_entries")
    op.drop_table("leaderboard_entries")
    op.drop_table("users")
    op.drop_table("games")
