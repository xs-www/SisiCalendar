"""SQLite 备份与恢复辅助。

使用 SQLite 在线备份 API 生成一致快照,避免直接拷贝 WAL 模式下正在写入的主文件。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "calendar-"
BACKUP_SUFFIX = ".db"


def backup_path_for(settings: Settings, moment: datetime | None = None) -> Path:
    """生成不冲突的备份文件名(同一秒内连续备份也不会互相覆盖)。"""
    stamp = (moment or utcnow()).strftime("%Y%m%d-%H%M%S")
    candidate = settings.backup_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    if not candidate.exists():
        return candidate
    for index in range(1, 1000):
        candidate = settings.backup_dir / f"{BACKUP_PREFIX}{stamp}-{index}{BACKUP_SUFFIX}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("备份目录中同名备份过多,请先清理")


def create_backup(settings: Settings | None = None, *, dest: Path | None = None) -> Path:
    """生成一份一致的 SQLite 快照。"""
    resolved = settings or get_settings()
    resolved.ensure_directories()
    if not resolved.db_path.exists():
        raise FileNotFoundError(f"数据库不存在:{resolved.db_path}")

    target = dest or backup_path_for(resolved)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    source = sqlite3.connect(str(resolved.db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            with destination:
                source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    logger.info("已生成数据库备份 %s(%s 字节)", target.name, target.stat().st_size)
    return target


def list_backups(settings: Settings | None = None) -> list[Path]:
    resolved = settings or get_settings()
    if not resolved.backup_dir.exists():
        return []
    return sorted(
        (
            path
            for path in resolved.backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def prune_backups(settings: Settings | None = None, *, keep_days: int | None = None) -> int:
    """删除超过保留期的备份。"""
    resolved = settings or get_settings()
    days = resolved.backup_retention_days if keep_days is None else keep_days
    cutoff = utcnow().timestamp() - days * 86400
    removed = 0
    for path in list_backups(resolved):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    if removed:
        logger.info("已清理 %s 份过期备份", removed)
    return removed


def restore_backup(source: Path, settings: Settings | None = None) -> Path:
    """把备份恢复到数据目录。

    恢复前会把当前数据库另存为 `calendar.db.pre-restore`,便于回退;
    调用方必须在容器停止、没有活动连接时执行。
    """
    resolved = settings or get_settings()
    if not source.exists():
        raise FileNotFoundError(f"备份文件不存在:{source}")

    target = resolved.db_path
    preset: Path | None = None
    if target.exists():
        preset = target.with_suffix(".db.pre-restore")
        shutil.copy2(target, preset)
    shutil.copy2(source, target)
    # WAL 与 SHM 必须一并清除,否则会与恢复的主文件不一致
    for suffix in ("-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()
    logger.warning("已从 %s 恢复数据库;恢复前副本:%s", source.name, preset)
    return target
