"""邮箱验证:`users` 增加验证状态列

Revision ID: 0007_email_verification
Revises: 0006_recipient_scope
Create Date: 2026-10-12

背景(用户实测反馈):账号只填了邮箱并不代表那个邮箱真的属于本人,而**固定的订阅地址**
要发到邮箱里、日历快照也要发到邮箱里。因此:

- 新注册必须填邮箱,并在注册流程里用 SMTP 发一次 6 位验证码;
- 成员在验证通过前不能使用系统(管理员豁免,便于本机救急);
- 验证通过后再发一封邮件,告知其**固定订阅地址**。

数据步骤:

1. 新增 5 列(全部可空/有默认值,`batch_alter_table` 兼容 SQLite);
2. **已有账号的邮箱视为已验证**:真机上这些邮箱都是本人提供并已在使用(邮件一直在往那里发),
   迁移时必须置位,否则升级完成后所有老成员会立刻被挡在验证页外。
   新注册的账号走注册流程,`email_verified_at` 保持为空。

降级只删列,不需要回填。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_email_verification"
down_revision: str | None = "0006_recipient_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VERIFY_COLUMNS = (
    sa.Column("email_verified_at", sa.DateTime(), nullable=True),
    sa.Column("email_verify_code_hash", sa.String(length=128), nullable=True),
    sa.Column("email_verify_expires_at", sa.DateTime(), nullable=True),
    sa.Column("email_verify_attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("email_verify_sent_at", sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        for column in VERIFY_COLUMNS:
            batch.add_column(column)

    # 升级前就存在的邮箱:邮件一直发得通,视为已验证(否则老成员会被验证页挡住)
    op.get_bind().execute(
        sa.text(
            "UPDATE users SET email_verified_at = created_at "
            "WHERE email IS NOT NULL AND TRIM(email) <> '' AND email_verified_at IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        for column in reversed(VERIFY_COLUMNS):
            batch.drop_column(column.name)