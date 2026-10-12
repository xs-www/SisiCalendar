"""事件提醒与链接字段

Revision ID: 0004_event_reminders
Revises: 0003_agent_tokens
Create Date: 2026-09-30

`events.reminders` 保存提前分钟数的 JSON 列表(如 `[1440, 15, 0]`),导出为 ICS 的
多个 `VALARM`;`events.url` 保存备注里的链接,导出为 `VEVENT` 的 `URL` 属性。
两列都可空,已有事件保持「无提醒、无链接」,因此升级后导出内容不变、不会重发日历。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_event_reminders"
down_revision: str | None = "0003_agent_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reminders", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("url", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_column("url")
        batch_op.drop_column("reminders")
