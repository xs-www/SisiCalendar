"""initial schema: 事件、同步人员、发信任务、导出版本与配置

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "calendar_export_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("dirty", sa.Boolean(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "calendar_exports",
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_path", sa.String(length=512), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("version"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("uid", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("start_at_utc", sa.DateTime(), nullable=True),
        sa.Column("end_at_utc", sa.DateTime(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date_exclusive", sa.Date(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False),
        sa.Column("rrule", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('confirmed','tentative','cancelled')", name="ck_events_status"
        ),
        sa.CheckConstraint(
            "(all_day = 0 AND start_at_utc IS NOT NULL AND end_at_utc IS NOT NULL)"
            " OR (all_day = 1 AND start_date IS NOT NULL AND end_date_exclusive IS NOT NULL)",
            name="ck_events_time_shape",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index("ix_events_active_date", ["deleted_at", "start_date"], unique=False)
        batch_op.create_index(
            "ix_events_active_start", ["deleted_at", "start_at_utc"], unique=False
        )
        batch_op.create_index("ix_events_uid", ["uid"], unique=True)

    op.create_table(
        "sync_recipients",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sync_recipients", schema=None) as batch_op:
        batch_op.create_index("ix_sync_recipients_email", ["email"], unique=True)

    op.create_table(
        "mail_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("export_version", sa.Integer(), nullable=False),
        sa.Column("recipient_id", sa.String(length=36), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("trigger_event_id", sa.String(length=36), nullable=True),
        sa.Column("event_title", sa.String(length=200), nullable=True),
        sa.Column("event_time_text", sa.String(length=200), nullable=True),
        sa.Column("change_type", sa.String(length=20), nullable=False),
        sa.Column("attachment_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_path", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','retry_wait','failed')",
            name="ck_mail_deliveries_status",
        ),
        sa.ForeignKeyConstraint(
            ["export_version"], ["calendar_exports.version"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["recipient_id"], ["sync_recipients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "export_version", "recipient_id", name="uq_mail_deliveries_version_recipient"
        ),
    )
    with op.batch_alter_table("mail_deliveries", schema=None) as batch_op:
        batch_op.create_index(
            "ix_mail_deliveries_status_next", ["status", "next_attempt_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("mail_deliveries", schema=None) as batch_op:
        batch_op.drop_index("ix_mail_deliveries_status_next")
    op.drop_table("mail_deliveries")

    with op.batch_alter_table("sync_recipients", schema=None) as batch_op:
        batch_op.drop_index("ix_sync_recipients_email")
    op.drop_table("sync_recipients")

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("ix_events_uid")
        batch_op.drop_index("ix_events_active_start")
        batch_op.drop_index("ix_events_active_date")
    op.drop_table("events")

    op.drop_table("calendar_exports")
    op.drop_table("calendar_export_state")
    op.drop_table("app_settings")
