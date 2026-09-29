"""远程 Agent 接入令牌(平台内管理)

Revision ID: 0003_agent_tokens
Revises: 0002_event_source
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_agent_tokens"
down_revision: str | None = "0002_event_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 只存 sha256(token) 哈希;明文不在库中,创建时返回一次即丢弃。
    op.create_table(
        "agent_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_agent_tokens_hash"),
    )
    op.create_index(
        "ix_agent_tokens_enabled", "agent_tokens", ["enabled"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_agent_tokens_enabled", table_name="agent_tokens")
    op.drop_table("agent_tokens")
