"""config change log

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02 03:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import bot.storage.types

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("path", sa.String(length=200), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("timestamp", bot.storage.types.UtcDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("config_changes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_config_changes_path"), ["path"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_config_changes_timestamp"), ["timestamp"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("config_changes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_config_changes_timestamp"))
        batch_op.drop_index(batch_op.f("ix_config_changes_path"))
    op.drop_table("config_changes")
