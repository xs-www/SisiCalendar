"""回归:升级后第二次启动曾经必崩(同类真实缺陷)。

线上真机升级时发现:把**多个**历史外部收件人都写成 `user_id = 首位管理员` 之后,
同一个账号名下出现多行「账号收件人」,第二次启动时取「唯一一行」的逻辑抛
`MultipleResultsFound`,容器进入重启循环。

这里固定住两条约束:

1. 首次升级把历史收件人的**收信范围**(`scope_user_id`)绑到首位管理员,`user_id` 保持为空;
2. 无论库里是「多行历史收件人」还是「被旧引导写坏的多行绑定」,反复启动都不得抛异常。

迁移 `0006_recipient_scope` 里的重复行修复逻辑也在这里直接测一遍(测试库是
`create_all` 建出来的,不会执行迁移,所以单独调用它的修复函数)。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.accounts import owner_user, recipient_scope_user_id, target_calendar_ids
from app.bootstrap import ensure_family_setup
from app.models import SyncRecipient, User
from app.timeutil import utcnow
from sqlalchemy import select

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0006_recipient_scope.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location("migration_0006", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_recipient(email: str) -> SyncRecipient:
    return SyncRecipient(
        email=email, display_name=None, enabled=True, created_at=utcnow(), updated_at=utcnow()
    )


def test_two_legacy_recipients_survive_repeated_bootstrap(session, settings) -> None:
    """两个历史外部收件人:都保持首位管理员的范围,而且反复启动不崩。"""
    session.add_all(
        [_legacy_recipient("old-a@example.test"), _legacy_recipient("old-b@example.test")]
    )
    session.commit()

    for _ in range(3):  # 第二次启动曾经就是崩溃点
        ensure_family_setup(session, settings)
        session.commit()

    owner = owner_user(session)
    recipients = list(session.execute(select(SyncRecipient)).scalars())
    assert len(recipients) == 2
    for recipient in recipients:
        assert recipient.user_id is None, "历史收件人不是账号收件人,不得占用 user_id"
        assert recipient.scope_user_id == owner.id
        # 范围解析出的是首位管理员的可见日历集合
        assert target_calendar_ids(session, user_id=recipient_scope_user_id(recipient)) == (
            target_calendar_ids(session, user_id=owner.id)
        )


def test_bootstrap_recovers_from_duplicate_account_recipients(session, settings, family) -> None:
    """被旧引导写坏的库(同一个账号名下多行)也必须能起来。"""
    session.add_all(
        [
            SyncRecipient(
                email="admin@example.test",
                enabled=True,
                user_id=family.owner_id,
                created_at=utcnow(),
                updated_at=utcnow(),
            ),
            SyncRecipient(
                email="old@example.test",
                enabled=True,
                user_id=family.owner_id,
                created_at=utcnow(),
                updated_at=utcnow(),
            ),
        ]
    )
    session.commit()

    ensure_family_setup(session, settings)
    session.commit()
    ensure_family_setup(session, settings)
    session.commit()

    rows = {row.email: row for row in session.execute(select(SyncRecipient)).scalars()}
    assert len(rows) == 2


def test_migration_repair_splits_duplicate_bindings(session, settings, family) -> None:
    """迁移里的修复:`user_id` 重复时保留邮箱与账号一致的账号行,其余降级为范围绑定。"""
    owner = session.get(User, family.owner_id)
    assert owner is not None
    owner.email = "admin@example.test"
    session.add_all(
        [
            SyncRecipient(
                email="admin@example.test",
                enabled=True,
                user_id=owner.id,
                scope_user_id=owner.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            ),
            SyncRecipient(
                email="old@example.test",
                enabled=True,
                user_id=owner.id,
                scope_user_id=owner.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            ),
        ]
    )
    session.commit()

    module = _migration_module()
    module._repair_duplicate_bindings(session.connection())
    session.commit()

    rows = {row.email: row for row in session.execute(select(SyncRecipient)).scalars()}
    assert rows["admin@example.test"].user_id == owner.id, "邮箱与账号一致的行才是账号收件人"
    assert rows["old@example.test"].user_id is None
    assert rows["old@example.test"].scope_user_id == owner.id, "历史邮箱保持原有收信范围"


def test_migration_repair_demotes_everything_when_account_has_no_email(
    session, settings, family
) -> None:
    """账号本身没有邮箱时,它名下不该有账号收件人:全部降级为范围绑定。"""
    owner = owner_user(session)
    session.add(
        SyncRecipient(
            email="old@example.test",
            enabled=True,
            user_id=owner.id,
            scope_user_id=owner.id,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    session.commit()
    # 造出「多行」才能触发修复路径
    session.add(
        SyncRecipient(
            email="old2@example.test",
            enabled=True,
            user_id=owner.id,
            scope_user_id=owner.id,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    session.commit()

    module = _migration_module()
    module._repair_duplicate_bindings(session.connection())
    session.commit()

    for row in session.execute(select(SyncRecipient)).scalars():
        assert row.user_id is None
        assert row.scope_user_id == owner.id