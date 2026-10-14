"""发信任务记录操作发起人:`mail_deliveries` 增加两列

Revision ID: 0008_delivery_actor
Revises: 0007_email_verification
Create Date: 2026-10-12

背景(用户反馈):同步邮件只说「本次操作:新增日程」,收信的家人看不出**是谁**改的。
邮件是纯文本、收件端不做换算,所以发起人必须在建任务那一刻固化进任务记录,
重试时不能再现算(否则重试发的是「当前登录者」而不是当时那个人)。

两列的分工:

- `actor_user_id`:发起人的账号 id,发信时按它现查 `users.display_name or username`
  ——界面上改了显示名,没发出去的邮件也跟着用新名字;
- `actor_label`:发起人显示名的**快照**,账号被删除(或改名后地址已失效)时兜底。

只加列、不回填:升级前已有的历史任务本来就没有发起人信息,正文与队列都显示「未记录」,
比编造一个名字安全。**不要**用 `trigger_event_id` 去猜:事件的 `created_by_user_id`
是「谁建的」而不是「谁改的」,猜错会把别人的改动记到创建人头上。

`batch_alter_table` 只加列、**不携带外键**(与 0005/0006/0007 的加列一致):
真机升级上来的库没有这条外键,删账号时由 `app/accounts.py::delete_user` 显式置空,
否则会留下指向已删除账号的悬空 `actor_user_id`(AGENTS.md 不变量 20 的同一条理由)。

降级只删这两列,不需要回填(历史任务丢掉的只是「谁操作的」这层说明)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_delivery_actor"
down_revision: str | None = "0007_email_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR_COLUMNS = (
    sa.Column("actor_user_id", sa.String(length=36), nullable=True),
    sa.Column("actor_label", sa.String(length=100), nullable=True),
)


def upgrade() -> None:
    with op.batch_alter_table("mail_deliveries") as batch:
        for column in ACTOR_COLUMNS:
            batch.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table("mail_deliveries") as batch:
        for column in reversed(ACTOR_COLUMNS):
            batch.drop_column(column.name)
