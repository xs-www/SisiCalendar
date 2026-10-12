"""家庭多人共享日历:账号、日历、成员、邀请码与订阅令牌

Revision ID: 0005_family_accounts
Revises: 0004_event_reminders
Create Date: 2026-10-07

本次迁移只做**结构变更**,不动任何既有数据:

- 新增 `users` / `calendars` / `calendar_members` / `invite_codes` / `feed_tokens`;
- `events` 增加 `calendar_id`(归属日历)与 `created_by_user_id`(创建人);
- `sync_recipients` 增加 `user_id`(对应账号;为空即外部邮箱,只收家庭共享日历);
- `agent_tokens` 增加 `user_id`(令牌归属)与 `calendar_id`(默认落库日历)。

历史数据的回填(首位管理员、个人日历、家庭共享日历、把既有事件挂到首位管理员的
个人日历、给历史 Agent 令牌补归属)由启动引导 `app/bootstrap.py::ensure_family_setup`
在每次启动时幂等完成,因为它需要读取环境里的管理员凭据。

所有新列都可空,所以升级本身不会让既有数据失效;在引导执行之前,事件暂时没有归属
日历,订阅入口按「尚未完成升级」处理。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_family_accounts"
down_revision: str | None = "0004_event_reminders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------- 账号 ----------
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("color", sa.String(length=16), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("role IN ('admin','member')", name="ck_users_role"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=False)
    op.create_index("ix_users_enabled", "users", ["enabled"], unique=False)

    # ---------- 日历 ----------
    op.create_table(
        "calendars",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        sa.Column("color", sa.String(length=16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('personal','shared')", name="ck_calendars_kind"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_calendars_kind", "calendars", ["kind"], unique=False)
    op.create_index("ix_calendars_owner_user_id", "calendars", ["owner_user_id"], unique=False)

    op.create_table(
        "calendar_members",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("calendar_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner','editor','viewer')", name="ck_calendar_members_role"
        ),
        sa.ForeignKeyConstraint(["calendar_id"], ["calendars.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("calendar_id", "user_id", name="uq_calendar_members_pair"),
    )
    op.create_index(
        "ix_calendar_members_calendar_id", "calendar_members", ["calendar_id"], unique=False
    )
    op.create_index("ix_calendar_members_user_id", "calendar_members", ["user_id"], unique=False)

    # ---------- 邀请码 ----------
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invite_codes_code_hash", "invite_codes", ["code_hash"], unique=True)

    # ---------- 订阅令牌 ----------
    op.create_table(
        "feed_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feed_tokens_user_id", "feed_tokens", ["user_id"], unique=False)
    op.create_index("ix_feed_tokens_token_hash", "feed_tokens", ["token_hash"], unique=True)

    # ---------- 既有表新增列 ----------
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("calendar_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("created_by_user_id", sa.String(length=36), nullable=True))
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index("ix_events_calendar_id", ["calendar_id"], unique=False)
        batch_op.create_index(
            "ix_events_calendar_start",
            ["calendar_id", "deleted_at", "start_at_utc"],
            unique=False,
        )

    with op.batch_alter_table("sync_recipients", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
    with op.batch_alter_table("sync_recipients", schema=None) as batch_op:
        batch_op.create_index("ix_sync_recipients_user_id", ["user_id"], unique=False)

    with op.batch_alter_table("agent_tokens", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("calendar_id", sa.String(length=36), nullable=True))
    with op.batch_alter_table("agent_tokens", schema=None) as batch_op:
        batch_op.create_index("ix_agent_tokens_user_id", ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("agent_tokens", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_tokens_user_id")
    with op.batch_alter_table("agent_tokens", schema=None) as batch_op:
        batch_op.drop_column("calendar_id")
        batch_op.drop_column("user_id")

    with op.batch_alter_table("sync_recipients", schema=None) as batch_op:
        batch_op.drop_index("ix_sync_recipients_user_id")
    with op.batch_alter_table("sync_recipients", schema=None) as batch_op:
        batch_op.drop_column("user_id")

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("ix_events_calendar_start")
        batch_op.drop_index("ix_events_calendar_id")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_column("created_by_user_id")
        batch_op.drop_column("calendar_id")

    op.drop_index("ix_feed_tokens_token_hash", table_name="feed_tokens")
    op.drop_index("ix_feed_tokens_user_id", table_name="feed_tokens")
    op.drop_table("feed_tokens")

    op.drop_index("ix_invite_codes_code_hash", table_name="invite_codes")
    op.drop_table("invite_codes")

    op.drop_index("ix_calendar_members_user_id", table_name="calendar_members")
    op.drop_index("ix_calendar_members_calendar_id", table_name="calendar_members")
    op.drop_table("calendar_members")

    op.drop_index("ix_calendars_owner_user_id", table_name="calendars")
    op.drop_index("ix_calendars_kind", table_name="calendars")
    op.drop_table("calendars")

    op.drop_index("ix_users_enabled", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")