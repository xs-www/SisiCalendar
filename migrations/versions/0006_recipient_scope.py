"""收件人范围与身份分离:`sync_recipients.scope_user_id`

Revision ID: 0006_recipient_scope
Revises: 0005_family_accounts
Create Date: 2026-10-11

背景(线上发现的缺陷):`0005` 之后的启动引导把**每一个**历史外部收件人都写成
`sync_recipients.user_id = 首位管理员`,于是同一个账号名下出现多行收件人。
`user_id` 在代码里被当成「这行属于哪个账号」的身份标记(账号收件人唯一),
第二次启动时 `sync_user_recipient()` 用 `scalar_one_or_none()` 取行,
直接抛 `MultipleResultsFound`,应用启动失败、容器进入重启循环。

本次迁移把两件事拆开:

- `user_id`:**身份**。非空 = 这行是该账号的账号收件人(由 `users.email` 派生,一行);
- `scope_user_id`:**范围**。发给该邮箱的附件按哪个账号的可见日历裁剪;为空 = 家庭共享日历。

数据步骤:

1. 账号收件人(原本 `user_id` 非空)范围就是它所属的账号:`scope_user_id = user_id`。
2. 修复缺陷:同一个 `user_id` 下若有多行,只保留邮箱与该账号 `users.email` 一致的那一行
   作为账号收件人;其余行保留原有收信范围(`scope_user_id = 该账号`)但把 `user_id` 置空,
   于是它们变回「沿用某账号范围的外部邮箱」,行为与升级前一致(仍旧收到全部日程)。
   账号本身没有邮箱时,该账号名下不应有账号收件人,全部行同样降级为范围绑定。

降级时把范围折回 `user_id`(仅在该行没有 `user_id` 时),再删列。折回后同一个账号名下
可能又出现多行——那是 `0005` 的原始缺陷状态,仅用于回退,不保证再升级时的表现。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_recipient_scope"
down_revision: str | None = "0005_family_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _repair_duplicate_bindings(connection: sa.Connection) -> None:
    """把挤在同一个账号名下的多行收件人拆成「一行账号收件人 + 若干范围绑定」。"""
    duplicate_users = connection.execute(
        sa.text(
            "SELECT user_id FROM sync_recipients "
            "WHERE user_id IS NOT NULL GROUP BY user_id HAVING COUNT(*) > 1"
        )
    ).scalars()
    for user_id in list(duplicate_users):
        account_email = connection.execute(
            sa.text("SELECT email FROM users WHERE id = :uid"), {"uid": user_id}
        ).scalar()
        rows = connection.execute(
            sa.text(
                "SELECT id, email FROM sync_recipients WHERE user_id = :uid ORDER BY created_at, id"
            ),
            {"uid": user_id},
        ).all()
        keeper = None
        if _normalize(account_email):
            keeper = next(
                (row.id for row in rows if _normalize(row.email) == _normalize(account_email)),
                None,
            )
        connection.execute(
            sa.text(
                "UPDATE sync_recipients SET scope_user_id = :uid, user_id = NULL "
                "WHERE user_id = :uid AND (:keeper IS NULL OR id <> :keeper)"
            ),
            {"uid": user_id, "keeper": keeper},
        )


def upgrade() -> None:
    with op.batch_alter_table("sync_recipients") as batch:
        batch.add_column(sa.Column("scope_user_id", sa.String(length=36), nullable=True))
        batch.create_index("ix_sync_recipients_scope_user_id", ["scope_user_id"])

    connection = op.get_bind()
    # 账号收件人的范围 = 它所属的账号
    connection.execute(
        sa.text(
            "UPDATE sync_recipients SET scope_user_id = user_id WHERE user_id IS NOT NULL"
        )
    )
    _repair_duplicate_bindings(connection)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE sync_recipients SET user_id = scope_user_id "
            "WHERE user_id IS NULL AND scope_user_id IS NOT NULL"
        )
    )
    with op.batch_alter_table("sync_recipients") as batch:
        batch.drop_index("ix_sync_recipients_scope_user_id")
        batch.drop_column("scope_user_id")