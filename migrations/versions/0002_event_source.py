"""事件来源与 Agent 幂等键

Revision ID: 0002_event_source
Revises: 0001_initial
Create Date: 2026-09-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_event_source"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        # 已有事件都来自本机管理界面,用 server_default 回填
        batch_op.add_column(
            sa.Column("source", sa.String(length=20), nullable=False, server_default="web")
        )
        batch_op.add_column(sa.Column("client_ref", sa.String(length=200), nullable=True))
        batch_op.create_index("ix_events_source", ["source"], unique=False)
        batch_op.create_index("ix_events_client_ref", ["client_ref"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("ix_events_client_ref")
        batch_op.drop_index("ix_events_source")
        batch_op.drop_column("client_ref")
        batch_op.drop_column("source")
