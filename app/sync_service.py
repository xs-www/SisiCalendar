"""同步编排:把“保存 → 生成 ICS → 固化版本 → 创建发信任务”串成一条链路。

路由、后台任务与命令行都通过这里触发,保证触发顺序与错误处理完全一致。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.exporter import ExportError, export_calendar, get_state, mark_dirty
from app.mailqueue import ensure_delivery_tasks
from app.models import CHANGE_MANUAL, Event
from app.timeutil import iso_utc, utc_to_local

logger = logging.getLogger(__name__)


@dataclass
class SyncOutcome:
    """一次同步的结果,直接用于界面提示。"""

    ok: bool
    changed: bool = False
    version: int | None = None
    generated_at: str | None = None
    mail_created: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "version": self.version,
            "generated_at": self.generated_at,
            "mail_created": self.mail_created,
            "error": self.error,
        }


def mark_export_dirty(session: Session) -> None:
    """在保存事件的同一个事务里把导出标记为待更新。"""
    mark_dirty(session)


def event_time_text(event: Event) -> str:
    """给邮件正文与界面用的一行时间描述。"""
    tz_name = event.timezone or "UTC"
    if event.all_day:
        start = event.start_date
        end = event.end_date_exclusive
        if start is None:
            return "全天"
        if end is None or end == date.fromordinal(start.toordinal() + 1):
            return f"{start.isoformat()}(全天)"
        return f"{start.isoformat()} 至 {date.fromordinal(end.toordinal() - 1).isoformat()}(全天)"
    if event.start_at_utc is None or event.end_at_utc is None:
        return "时间未设置"
    start_local = utc_to_local(event.start_at_utc, tz_name)
    end_local = utc_to_local(event.end_at_utc, tz_name)
    if start_local.date() == end_local.date():
        return (
            f"{start_local.strftime('%Y-%m-%d %H:%M')}–{end_local.strftime('%H:%M')}"
            f"({tz_name})"
        )
    return (
        f"{start_local.strftime('%Y-%m-%d %H:%M')} 至 "
        f"{end_local.strftime('%Y-%m-%d %H:%M')}({tz_name})"
    )


def _persist_failure(session: Session, message: str) -> None:
    """回滚会丢掉导出过程中写入的错误摘要,这里单独补记,保证界面可见。"""
    try:
        state = get_state(session)
        state.dirty = True
        state.last_error = message[:1000]
        session.commit()
    except Exception:  # noqa: BLE001 - 记录失败不能掩盖原始错误
        session.rollback()
        logger.exception("无法写入导出失败状态")


def publish(
    session: Session,    *,
    settings: Settings | None = None,
    change_type: str = CHANGE_MANUAL,
    event: Event | None = None,
    event_title: str | None = None,
    time_text: str | None = None,
    force_new_version: bool = False,
) -> SyncOutcome:
    """生成新版 ICS 并为启用人员创建发信任务。

    调用前调用方必须已经提交过一次事务(事件已落库、`dirty` 已置位),
    这样即便导出失败,用户的编辑也不会丢失。
    """
    resolved = settings or get_settings()
    title = event_title if event_title is not None else (event.title if event else None)
    time_label = time_text if time_text is not None else (event_time_text(event) if event else None)

    try:
        result = export_calendar(
            session, settings=resolved, force_new_version=force_new_version
        )
        created = 0
        if result.changed:
            created = ensure_delivery_tasks(
                session,
                export_version=result.version,
                change_type=change_type,
                trigger_event_id=event.id if event else None,
                event_title=title,
                event_time_text=time_label,
                settings=resolved,
            )
        session.commit()
    except ExportError as exc:
        session.rollback()
        _persist_failure(session, str(exc))
        logger.error("同步失败,已保留数据库修改并标记待修复:%s", exc)
        return SyncOutcome(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 不让邮件或文件问题影响日程保存
        session.rollback()
        _persist_failure(session, f"未预期错误:{type(exc).__name__}")
        logger.exception("同步过程中出现未预期错误")
        return SyncOutcome(ok=False, error=f"未预期错误:{type(exc).__name__}")

    return SyncOutcome(
        ok=True,
        changed=result.changed,
        version=result.version,
        generated_at=iso_utc(result.generated_at),
        mail_created=created,
    )


def regenerate(
    session: Session, *, settings: Settings | None = None, force_new_version: bool = False
) -> SyncOutcome:
    """手动重新生成 ICS,不创建新的邮件任务以外的副作用。"""
    return publish(
        session,
        settings=settings,
        change_type=CHANGE_MANUAL,
        force_new_version=force_new_version,
    )


def repair_if_needed(session: Session, *, settings: Settings | None = None) -> SyncOutcome:
    """后台任务:仅在 dirty 时重新生成。"""
    resolved = settings or get_settings()
    state = get_state(session)
    if not state.dirty and resolved.calendar_path.exists():
        return SyncOutcome(
            ok=True,
            changed=False,
            version=state.version,
            generated_at=iso_utc(state.generated_at),
        )
    return publish(session, settings=resolved, change_type=CHANGE_MANUAL)


def state_snapshot(session: Session, *, settings: Settings | None = None) -> dict[str, object]:
    resolved = settings or get_settings()
    state = get_state(session)
    path = resolved.calendar_path
    size = path.stat().st_size if path.exists() else 0
    return {
        "version": int(state.version or 0),
        "content_hash": state.content_hash,
        "generated_at": iso_utc(state.generated_at),
        "dirty": bool(state.dirty),
        "last_error": state.last_error,
        "size_bytes": size,
        "file_exists": path.exists(),
        "subscription_url": resolved.subscription_url,
        "mail_configured": resolved.mail_configured,
    }


def overview(session: Session, *, settings: Settings | None = None) -> dict[str, object]:
    """同步总览:导出状态 + 收件人 + 队列计数,供页面与 API 共用。"""
    from sqlalchemy import select

    from app.mailqueue import queue_counts
    from app.models import MailDelivery, SyncRecipient
    from app.util import mask_email

    resolved = settings or get_settings()
    payload = state_snapshot(session, settings=resolved)
    payload.update(queue_counts(session))

    recipients = list(session.execute(select(SyncRecipient)).scalars())
    last_success = session.execute(
        select(MailDelivery.sent_at)
        .where(MailDelivery.sent_at.is_not(None))
        .order_by(MailDelivery.sent_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    recent = list(
        session.execute(
            select(MailDelivery).order_by(MailDelivery.created_at.desc()).limit(20)
        ).scalars()
    )

    counts = queue_counts(session)
    payload.update(
        {
            "recipients_total": len(recipients),
            "recipients_enabled": sum(1 for item in recipients if item.enabled),
            "deliveries_pending": counts.get("pending", 0),
            "deliveries_sent": counts.get("sent", 0),
            "deliveries_retry_wait": counts.get("retry_wait", 0),
            "deliveries_failed": counts.get("failed", 0),
            "last_delivery_success_at": iso_utc(last_success),
            "recent_deliveries": [
                {
                    "id": row.id,
                    "export_version": row.export_version,
                    "recipient_email": mask_email(row.recipient_email),
                    "change_type": row.change_type,
                    "status": row.status,
                    "attempt_count": row.attempt_count,
                    "next_attempt_at": iso_utc(row.next_attempt_at),
                    "error_message": row.error_message,
                    "created_at": iso_utc(row.created_at),
                    "sent_at": iso_utc(row.sent_at),
                }
                for row in recent
            ],
        }
    )
    payload["generated_at_display"] = generated_at_display(
        payload.get("generated_at"), resolved.default_timezone
    )
    return payload


def generated_at_display(value: str | None, tz_name: str) -> str:
    """把 UTC ISO 字符串转成给用户看的本地时间。"""
    if not value:
        return "尚未生成"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return utc_to_local(parsed.replace(tzinfo=None), tz_name).strftime("%Y-%m-%d %H:%M:%S")
