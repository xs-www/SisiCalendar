"""ICS 导出服务:生成、原子替换、版本状态与快照。

流程严格对应开发文档 6.2 节:

1. 调用方在数据库事务中保存事件。
2. 提交成功后把导出状态标记为 `dirty`。
3. 读取全部有效事件生成完整 ICS(并集,内部一致性用)。
4. 写入同目录临时文件,`fsync` 后原子替换。
5. 计算 SHA-256,更新导出版本、生成时间并清除 `dirty`。
6. 为该版本与所有启用人员创建幂等邮件任务。

家庭版(2026-10)起,一次导出会生成**多份文件**:

| 产物 | 内容 | 谁在看 |
|---|---|---|
| `export/calendar.ics` | 全部事件并集 | 内部一致性检查与运维排查,不对外暴露 |
| `export/feeds/user-<id>.ics` | 某个成员可见范围(个人 + 共享) | 该成员的订阅地址 |
| `export/feeds/family.ics` | 家庭共享日历范围 | 外部收件人的家庭订阅地址 |
| `snapshots/calendar-v{N}-<target>.ics` | 同上,版本快照(不可变) | 邮件任务重试 |
| `snapshots/calendar-v{N}-<target>-upcoming.ics` | 该目标里**尚未结束**的日程 | 邮件附件 |

版本号与 `content_hash` 仍然只由**并集**内容决定,所以「改一条日程」只会产生一个新版本;
各目标的文件内容只取决于数据库状态,与导出时刻无关(`AGENTS.md` 不变量 3)。
「尚未结束」的过滤只发生在邮件专用快照上,发布文件与版本快照必须保持完整。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import family_calendar_ids, list_users, target_calendar_ids
from app.config import Settings, get_settings
from app.events import build_range, expand_occurrences
from app.ics import build_calendar
from app.models import CalendarExport, CalendarExportState, Event, MailDelivery
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

_export_lock = threading.RLock()

# 邮件附件只看「尚未结束」,但重复事件要往后展开才能判断它是否还活着。
# 5 年足够长:任何仍在重复的规则在这一窗口内几乎必然存在下一次发生。
MAIL_ATTACHMENT_HORIZON_DAYS = 366 * 5

#: 家庭共享范围的订阅目标关键字
FAMILY_TARGET_KEY = "family"


class ExportError(RuntimeError):
    """导出失败,界面应显示“ICS 待修复”。"""


@dataclass(frozen=True)
class FeedTarget:
    """一次导出要产出的一个订阅目标。"""

    key: str
    user_id: str | None
    name: str
    calendar_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExportResult:
    version: int
    content_hash: str
    generated_at: datetime
    size_bytes: int
    changed: bool
    path: Path
    targets: tuple[str, ...] = ()


def feed_target_key(*, user_id: str | None) -> str:
    return FAMILY_TARGET_KEY if not user_id else f"user-{user_id}"


def feed_path_for(settings: Settings, *, user_id: str | None) -> Path:
    """订阅目标的发布文件(原子替换的正式产物)。"""
    return settings.feed_dir / f"{feed_target_key(user_id=user_id)}.ics"


def target_snapshot_path_for(settings: Settings, version: int, target_key: str) -> Path:
    """某个目标的完整版本快照(不可变,供邮件重试时固定内容)。"""
    return settings.snapshot_dir / f"calendar-v{version:06d}-{target_key}.ics"


def resolve_targets(session: Session) -> list[FeedTarget]:
    """列出本次导出要产出的全部订阅目标:每个成员一个 + 家庭共享一个。"""
    targets: list[FeedTarget] = []
    for user in list_users(session):
        if not user.enabled:
            continue
        targets.append(
            FeedTarget(
                key=feed_target_key(user_id=user.id),
                user_id=user.id,
                name=user.label,
                calendar_ids=tuple(target_calendar_ids(session, user_id=user.id)),
            )
        )
    targets.append(
        FeedTarget(
            key=FAMILY_TARGET_KEY,
            user_id=None,
            name="家庭共享",
            calendar_ids=tuple(family_calendar_ids(session)),
        )
    )
    return targets


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


def active_events(
    session: Session, *, calendar_ids: list[str] | tuple[str, ...] | None = None
) -> list[Event]:
    """未软删除事件,顺序稳定,保证同一数据生成同一哈希。

    `calendar_ids` 给出时只取这些日历里的事件;为空列表直接返回空。
    """
    if calendar_ids is not None and not calendar_ids:
        return []
    stmt = select(Event).where(Event.deleted_at.is_(None))
    if calendar_ids is not None:
        stmt = stmt.where(Event.calendar_id.in_(list(calendar_ids)))
    stmt = stmt.order_by(
        Event.all_day.desc(),
        Event.start_at_utc.asc(),
        Event.start_date.asc(),
        Event.created_at.asc(),
        Event.id.asc(),
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
    """并集版本快照(内部产物)。"""
    return settings.snapshot_dir / f"calendar-v{version:06d}.ics"


def mail_snapshot_path_for(
    settings: Settings, version: int, *, target_key: str | None = None
) -> Path:
    """邮件专用快照(仅尚未结束的日程)。

    不给 `target_key` 时是旧版并集命名(`calendar-v{N}-upcoming.ics`),升级前创建的
    历史版本仍在用;新版本一律按目标落盘。
    """
    if not target_key:
        return settings.snapshot_dir / f"calendar-v{version:06d}-upcoming.ics"
    return settings.snapshot_dir / f"calendar-v{version:06d}-{target_key}-upcoming.ics"


def upcoming_events(
    session: Session,
    *,
    settings: Settings,
    horizon_days: int = MAIL_ATTACHMENT_HORIZON_DAYS,
    calendar_ids: list[str] | tuple[str, ...] | None = None,
) -> list[Event]:
    """**尚未结束**的事件,顺序与 `active_events` 一致。

    判定复用查询侧的展开逻辑,所以三种情况都不会被误删:区间与未来重合的一次性事件、
    正在进行的事件(起点在过去但尚未结束)、以及起点很早但仍在重复的事件。

    时间分界是**此刻**(`utcnow()`):已经结束的一次性事件与已经结束的重复实例都不再保留,
    「今天早上刚开完的会」不会跟着下一次邮件一起发出去。全天事件没有时刻可言,
    仍按应用默认时区的当地日期比较,所以「今天」的全天事件照旧保留。

    `calendar_ids` 限定范围(账号收件人只看得到自己的日历),`None` 表示全部。
    """
    window = build_range(None, None, settings=settings, default_days=horizon_days)
    now = utcnow()
    if now > window.start_utc:
        # `start_date` 不动:全天事件仍按「今天」判断
        window = replace(window, start_utc=now)
    return [
        event
        for event in active_events(session, calendar_ids=calendar_ids)
        if expand_occurrences(event, window)
    ]


def build_target_data(
    events: list[Event],
    target: FeedTarget,
    *,
    settings: Settings,
) -> bytes:
    """某个订阅目标的 ICS 内容。只取决于传入的事件与目标名称。"""
    base = settings.calendar_name or "Sisi Calendar"
    name = f"{base} · {target.name}" if target.name else base
    return build_calendar(
        events,
        calendar_name=name,
        default_timezone=settings.default_timezone,
        settings=settings,
    )


def _build_mail_attachment(
    session: Session,
    settings: Settings,
    target: FeedTarget,
    *,
    fallback: bytes,
) -> bytes:
    """生成邮件附件内容;失败时回退为该目标的完整版本快照,绝不让发信整体失败。"""
    try:
        events = upcoming_events(
            session, settings=settings, calendar_ids=list(target.calendar_ids)
        )
        return build_target_data(events, target, settings=settings)
    except Exception:  # noqa: BLE001 - 附件是次要产物,不能影响发布与订阅
        logger.exception("邮件附件(仅尚未结束的日程)生成失败,本次回退为该目标的完整版本快照")
        return fallback


def _missing_target_files(settings: Settings, targets: list[FeedTarget]) -> bool:
    return any(
        not feed_path_for(settings, user_id=target.user_id).exists() for target in targets
    )


def export_calendar(
    session: Session,
    *,
    settings: Settings | None = None,
    force_new_version: bool = False,
) -> ExportResult:
    """生成并发布最新版 ICS(并集 + 每个订阅目标)。调用方负责提交事务。"""
    resolved = settings or get_settings()
    resolved.ensure_directories()

    with _export_lock:
        state = get_state(session)
        events = active_events(session)
        targets = resolve_targets(session)
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
            and not _missing_target_files(resolved, targets)
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
                targets=tuple(target.key for target in targets),
            )

        version = int(state.version or 0) + 1
        generated_at = utcnow()
        snapshot_path = snapshot_path_for(resolved, version)

        try:
            # 快照先落盘:邮件任务只引用已存在的不可变版本文件
            atomic_write_bytes(snapshot_path, data)
            atomic_write_bytes(published, data)
            for target in targets:
                target_events = active_events(
                    session, calendar_ids=list(target.calendar_ids)
                )
                target_data = build_target_data(target_events, target, settings=resolved)
                atomic_write_bytes(
                    feed_path_for(resolved, user_id=target.user_id), target_data
                )
                atomic_write_bytes(
                    target_snapshot_path_for(resolved, version, target.key), target_data
                )
                atomic_write_bytes(
                    mail_snapshot_path_for(resolved, version, target_key=target.key),
                    _build_mail_attachment(
                        session, resolved, target, fallback=target_data
                    ),
                )
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
            "ICS 已发布 version=%s size=%s events=%s targets=%s",
            version,
            len(data),
            len(events),
            len(targets),
        )
        return ExportResult(
            version=version,
            content_hash=content_hash,
            generated_at=generated_at,
            size_bytes=len(data),
            changed=True,
            path=published,
            targets=tuple(target.key for target in targets),
        )


def sync_if_dirty(
    session: Session, *, settings: Settings | None = None
) -> ExportResult | None:
    """订阅入口与后台任务使用:仅在需要时重新生成。"""
    resolved = settings or get_settings()
    state = get_state(session)
    targets = resolve_targets(session)
    needs_export = (
        state.dirty
        or not resolved.calendar_path.exists()
        or _missing_target_files(resolved, targets)
    )
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
    if _missing_target_files(resolved, resolve_targets(session)):
        try:
            result = export_calendar(session, settings=resolved)
        except ExportError as exc:
            return False, f"订阅文件缺失且重新生成失败:{exc}"
        return True, f"订阅文件缺失,已重新生成 version={result.version}"
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
    """清理所有任务已终结且超过保留期的版本快照(含各订阅目标与邮件附件快照)。

    当前发布版本永不清理。
    """
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
        # 该版本的全部快照:并集、各订阅目标的完整快照与邮件附件快照
        pattern = f"calendar-v{export.version:06d}*.ics"
        for stale in resolved.snapshot_dir.glob(pattern):
            with contextlib.suppress(OSError):
                stale.unlink()
                removed += 1
        session.delete(export)
    if removed:
        session.flush()
        logger.info("已清理 %s 个历史导出版本快照", removed)
    return removed
