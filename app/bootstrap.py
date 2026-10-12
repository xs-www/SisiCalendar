"""启动引导:数据库迁移、账号与日历初始化、一致性检查。"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, Settings
from app.db import dispose_engine, init_engine, session_scope
from app.exporter import mark_dirty, verify_consistency
from app.mailqueue import recover_stuck_deliveries

logger = logging.getLogger(__name__)

ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

#: 升级时自动建的共享日历名称(管理员之后可以改名或再建新的)
DEFAULT_FAMILY_CALENDAR_NAME = "家庭共享日历"


def alembic_config(settings: Settings) -> AlembicConfig:
    config = AlembicConfig(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    return config


def run_migrations(settings: Settings) -> None:
    """把数据库升级到最新版本。单实例应用在启动时执行。"""
    if not ALEMBIC_INI.exists() or not MIGRATIONS_DIR.exists():
        raise RuntimeError("缺少 Alembic 配置或迁移目录,无法初始化数据库")
    command.upgrade(alembic_config(settings), "head")
    logger.info("数据库迁移已完成")


def bootstrap_database(settings: Settings) -> None:
    settings.ensure_directories()
    run_migrations(settings)
    init_engine(settings)


def ensure_family_setup(session: Session, settings: Settings) -> None:
    """幂等地把数据库补齐成家庭版:首位管理员、个人日历、家庭共享日历、历史回填。

    这段逻辑刻意放在启动引导而不是 Alembic 里,因为它需要读环境变量里的管理员凭据
    (首次启动时生成或注入的口令哈希)。每次启动都会执行,但除了首次升级外不会改动
    任何业务数据:

    - 没有账号时,用 `ADMIN_USERNAME` + `ADMIN_PASSWORD(_HASH)` 建首位管理员;
    - 没有共享日历时,建一个「家庭共享日历」,管理员是 owner、成员是 viewer;
    - 给缺个人日历/缺共享日历成员关系的账号补上;
    - 把 `calendar_id` 为空的历史事件(升级前创建的)挂到首位管理员的个人日历;
    - **首次升级**时,历史外部收件人保持原有「收到全部日程」的行为,把收信范围
      (`scope_user_id`)绑到首位管理员(`user_id` 保持为空——那是「账号收件人」的身份标记,
      每个账号只能有一行);之后新增的外部收件人只收家庭共享日历;
    - 历史 Agent 令牌(Cookie 时代的 .env 令牌没有归属)补上归属账号。
    """
    from app.accounts import (
        create_shared_calendar,
        join_shared_calendars,
        list_shared_calendars,
        list_users,
        owner_user,
        personal_calendar,
        sync_user_recipient,
    )
    from app.feed_tokens import ensure_account_feed_token
    from app.models import (
        DEFAULT_USER_COLORS,
        ROLE_ADMIN,
        AgentToken,
        Event,
        SyncRecipient,
        User,
        normalize_username,
    )
    from app.security import ensure_admin_credential, load_admin_hash
    from app.timeutil import utcnow

    changed = False
    users = list_users(session)
    first_upgrade = not users

    if first_upgrade:
        generated = ensure_admin_credential(settings)
        stored_hash = load_admin_hash(settings)
        if not stored_hash:
            raise RuntimeError("缺少管理员凭据,无法初始化首位账号")
        now = utcnow()
        owner = User(
            username=normalize_username(settings.admin_username) or "admin",
            display_name="家庭管理员",
            email=None,
            password_hash=stored_hash,
            role=ROLE_ADMIN,
            color=DEFAULT_USER_COLORS[0],
            timezone=None,
            enabled=True,
            must_change_password=bool(generated),
            created_at=now,
            updated_at=now,
        )
        session.add(owner)
        session.flush()
        personal_calendar(session, owner)
        logger.warning("已创建首位管理员账号:%s", owner.username)
        users = [owner]
        changed = True

    owner = owner_user(session)
    if owner is None:  # pragma: no cover - 上面刚保证过
        raise RuntimeError("数据库里没有任何账号")

    if not list_shared_calendars(session):
        create_shared_calendar(
            session,
            name=DEFAULT_FAMILY_CALENDAR_NAME,
            color=DEFAULT_USER_COLORS[1],
            creator=owner,
        )
        logger.info("已创建共享日历:%s", DEFAULT_FAMILY_CALENDAR_NAME)
        changed = True

    for user in list_users(session):
        personal_calendar(session, user)
        join_shared_calendars(session, user)
        sync_user_recipient(session, user)
        # 每个账号都必须有一条**固定**订阅地址(由服务端密钥派生,永不改变)。
        # 建号时已经生成过;这里兜底覆盖「升级上来的老账号」与「建号时没拿到 settings」。
        ensure_account_feed_token(session, settings, user)

    # 历史事件:升级前它们没有归属日历,全部挂到首位管理员的个人日历
    legacy_events = list(
        session.execute(select(Event).where(Event.calendar_id.is_(None))).scalars()
    )
    if legacy_events:
        target = personal_calendar(session, owner)
        for event in legacy_events:
            event.calendar_id = target.id
            if event.created_by_user_id is None:
                event.created_by_user_id = owner.id
        logger.info("已把 %s 条历史日程归入 %s", len(legacy_events), target.name)
        changed = True

    # 首次升级:历史外部收件人原本收到「全部日程」,把范围绑到首位管理员以保持行为不变。
    # 注意只改 scope_user_id:user_id 是「账号收件人」的身份标记,绑多个邮箱会让同一个
    # 账号名下出现多行,第二次启动时取「唯一一行」的逻辑就会炸(真机升级踩过这个坑)。
    if first_upgrade:
        legacy_recipients = list(
            session.execute(
                select(SyncRecipient).where(
                    SyncRecipient.user_id.is_(None),
                    SyncRecipient.scope_user_id.is_(None),
                )
            ).scalars()
        )
        for recipient in legacy_recipients:
            recipient.scope_user_id = owner.id
            recipient.updated_at = utcnow()
        if legacy_recipients:
            logger.info(
                "已把 %s 个历史收件人的收信范围绑到首位管理员(保持原有的收信范围)",
                len(legacy_recipients),
            )
            changed = True
        for token in session.execute(
            select(AgentToken).where(AgentToken.user_id.is_(None))
        ).scalars():
            token.user_id = owner.id
            token.updated_at = utcnow()
            changed = True

    if changed:
        mark_dirty(session)
        session.commit()


def startup_checks(session: Session, settings: Settings) -> None:
    """启动时的账号/日历初始化、数据库与 ICS 一致性检查,以及中断任务恢复。"""
    ensure_family_setup(session, settings)

    ok, detail = verify_consistency(session, settings=settings)
    if ok:
        logger.info("一致性检查通过:%s", detail)
    else:
        logger.error("一致性检查未通过:%s", detail)

    recovered = recover_stuck_deliveries(session, settings=settings)
    if recovered:
        logger.warning("已恢复 %s 个中断的发信任务", recovered)


def shutdown() -> None:
    dispose_engine()


def data_paths(settings: Settings) -> list[Path]:
    return [settings.data_dir, settings.export_dir, settings.snapshot_dir, settings.backup_dir]


__all__ = [
    "DEFAULT_FAMILY_CALENDAR_NAME",
    "bootstrap_database",
    "ensure_family_setup",
    "startup_checks",
    "run_migrations",
    "alembic_config",
    "session_scope",
    "shutdown",
    "data_paths",
]
