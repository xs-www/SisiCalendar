"""ICS 导出服务:生成、原子替换、版本状态与快照。

流程严格对应开发文档 6.2 节:

1. 调用方在数据库事务中保存事件。
2. 提交成功后把导出状态标记为 `dirty`。
3. 读取全部有效事件生成完整 ICS。
4. 写入同目录临时文件,`fsync` 后原子替换 `export/calendar.ics`。
5. 计算 SHA-256,更新导出版本、生成时间并清除 `dirty`。
6. 为该版本与所有启用人员创建幂等邮件任务。

任何一步失败都不会破坏数据库,也不会让订阅端读到半写入文件。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.ics import build_calendar
from app.models import CalendarExport, CalendarExportState, Event, MailDelivery
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

_export_lock = threading.RLock()


class ExportError(RuntimeError):
    """导出失败,界面应显示“ICS 待修复”。"""


@dataclass(frozen=True)
class ExportResult:
    version: int
    content_hash: str
    generated_at: datetime
    size_bytes: int
    changed: bool
    path: Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时文件 + 原子替换,保证读者只会看到完整的旧文件或新文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise


def get_state(session: Session) -> CalendarExportState:
    state = session.get(CalendarExportState, CalendarExportState.SINGLETON_ID)
    if state is None:
        state = CalendarExportState(id=CalendarExportState.SINGLETON_ID, version=0, dirty=True)
        session.add(state)
        session.flush()
    return state


def active_events(session: Session) -> list[Event]:
    """全部未软删除事件,顺序稳定,保证同一数据生成同一哈希。"""
    stmt = (
        select(Event)
        .where(Event.deleted_at.is_(None))
        .order_by(
            Event.all_day.desc(),
            Event.start_at_utc.asc(),
            Event.start_date.asc(),
            Event.created_at.asc(),
            Event.id.asc(),
        )
    )
    return list(session.execute(stmt).scalars())


def mark_dirty(session: Session, error: str | None = None) -> CalendarExportState:
    state = get_state(session)
    state.dirty = True
    if error:
        state.last_error = error
    session.flush()
    return state


def snapshot_path_for(settings: Settings, version: int) -> Path:
    return settings.snapshot_dir / f"calendar-v{version:06d}.ics"


def export_calendar(
    session: Session,
    *,
    settings: Settings | None = None,
    force_new_version: bool = False,
) -> ExportResult:
    """生成并发布最新版 ICS。调用方负责提交事务。"""
    resolved = settings or get_settings()
    resolved.ensure_directories()

    with _export_lock:
        state = get_state(session)
        events = active_events(session)
        try:
            data = build_calendar(
                events,
                calendar_name=resolved.calendar_name,
                default_timezone=resolved.default_timezone,
                settings=resolved,
            )
        except Exception as exc:  # noqa: BLE001 - 生成失败必须回写状态
            state.dirty = True
            state.last_error = f"ICS 生成失败:{exc}"
            session.flush()
            logger.exception("ICS 生成失败")
            raise ExportError(str(exc)) from exc

        content_hash = hashlib.sha256(data).hexdigest()
        published = resolved.calendar_path

        if (
            not force_new_version
            and state.content_hash == content_hash
            and published.exists()
            and state.version > 0
        ):
            state.dirty = False
            state.last_error = None
            session.flush()
            return ExportResult(
                version=state.version,
                content_hash=content_hash,
                generated_at=state.generated_at or utcnow(),
                size_bytes=len(data),
                changed=False,
                path=published,
            )

        version = int(state.version or 0) + 1
        generated_at = utcnow()
        snapshot_path = snapshot_path_for(resolved, version)

        try:
            # 快照先落盘:邮件任务只引用已存在的不可变版本文件
            atomic_write_bytes(snapshot_path, data)
            atomic_write_bytes(published, data)
        except Exception as exc:  # noqa: BLE001
            state.dirty = True
            state.last_error = f"ICS 写入失败:{exc}"
            session.flush()
            logger.exception("ICS 写入失败")
            raise ExportError(str(exc)) from exc

        session.add(
            CalendarExport(
                version=version,
                content_hash=content_hash,
                snapshot_path=str(snapshot_path),
                size_bytes=len(data),
                created_at=generated_at,
            )
        )
        state.version = version
        state.content_hash = content_hash
        state.generated_at = generated_at
        state.dirty = False
        state.last_error = None
        session.flush()

        logger.info(
            "ICS 已发布 version=%s size=%s events=%s", version, len(data), len(events)
        )
        return ExportResult(
            version=version,
            content_hash=content_hash,
            generated_at=generated_at,
            size_bytes=len(data),
            changed=True,
            path=published,
        )


def sync_if_dirty(
    session: Session, *, settings: Settings | None = None
) -> ExportResult | None:
    """`/calendar` 与后台任务使用:仅在需要时重新生成。"""
    resolved = settings or get_settings()
    state = get_state(session)
    needs_export = state.dirty or not resolved.calendar_path.exists()
    if not needs_export:
        return None
    return export_calendar(session, settings=resolved)


def verify_consistency(
    session: Session, *, settings: Settings | None = None
) -> tuple[bool, str]:
    """启动时执行一次数据库与 ICS 一致性检查。"""
    resolved = settings or get_settings()
    state = get_state(session)
    path = resolved.calendar_path
    if not path.exists():
        try:
            export_calendar(session, settings=resolved)
        except ExportError as exc:
            return False, f"发布文件缺失且重新生成失败:{exc}"
        return True, "发布文件缺失,已重新生成"
    if state.dirty:
        try:
            result = export_calendar(session, settings=resolved)
        except ExportError as exc:
            return False, f"标记为 dirty 且重新生成失败:{exc}"
        return True, f"检测到 dirty,已重新生成 version={result.version}"
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if state.content_hash and actual != state.content_hash:
        try:
            result = export_calendar(session, settings=resolved, force_new_version=True)
        except ExportError as exc:
            return False, f"内容哈希不一致且重新生成失败:{exc}"
        return True, f"内容哈希不一致,已重新生成 version={result.version}"
    return True, "数据库与 ICS 一致"


def latest_snapshot(
    session: Session, version: int, *, settings: Settings | None = None
) -> Path | None:
    resolved = settings or get_settings()
    export = session.get(CalendarExport, version)
    if export is None:
        return None
    path = Path(export.snapshot_path)
    if path.exists():
        return path
    fallback = snapshot_path_for(resolved, version)
    return fallback if fallback.exists() else None


def cleanup_snapshots(session: Session, *, settings: Settings | None = None) -> int:
    """清理所有任务已终结且超过保留期的版本快照。当前发布版本永不清理。"""
    resolved = settings or get_settings()
    state = get_state(session)
    cutoff = utcnow().timestamp() - resolved.export_snapshot_retention_days * 86400
    removed = 0
    exports = list(session.execute(select(CalendarExport)).scalars())
    for export in exports:
        if export.version == state.version:
            continue
        if export.created_at is not None and export.created_at.timestamp() > cutoff:
            continue
        pending = session.execute(
            select(MailDelivery.id).where(
                MailDelivery.export_version == export.version,
                MailDelivery.status.not_in(("sent", "failed")),
            )
        ).first()
        if pending is not None:
            continue
        path = Path(export.snapshot_path)
        with contextlib.suppress(OSError):
            if path.exists():
                path.unlink()
                removed += 1
        session.delete(export)
    if removed:
        session.flush()
        logger.info("已清理 %s 个历史导出版本快照", removed)
    return removed
