"""启动引导:数据库迁移、凭据初始化与一致性检查。"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, Settings
from app.db import dispose_engine, init_engine, session_scope
from app.exporter import verify_consistency
from app.mailqueue import recover_stuck_deliveries

logger = logging.getLogger(__name__)

ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


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


def startup_checks(session: Session, settings: Settings) -> None:
    """启动时的数据库与 ICS 一致性检查,以及中断任务恢复。"""
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
    "bootstrap_database",
    "startup_checks",
    "run_migrations",
    "alembic_config",
    "session_scope",
    "shutdown",
    "data_paths",
]
