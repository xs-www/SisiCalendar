"""持久化邮件发送队列。

对应开发文档 6.5 节:

- 幂等键为 `export_version + recipient_id`,数据库层唯一约束兜底。
- 每位收件人单独发送,一位失败不影响其他收件人。
- 重试使用固定退避序列,默认 1、5、15、60、180 分钟。
- 附件始终取自该任务记录的不可变快照,重试不会冒充新版。
- 发信失败不回滚本地日程或 ICS。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import urls
from app.accounts import recipient_scope_user_id
from app.config import Settings, get_settings
from app.exporter import (
    feed_target_key,
    mail_snapshot_path_for,
    target_snapshot_path_for,
)
from app.mailer import MailError, SmtpConfig, build_message, config_from_settings, send_message
from app.models import (
    CHANGE_CREATED,
    CHANGE_DELETED,
    CHANGE_MANUAL,
    CHANGE_UPDATED,
    CalendarExport,
    CalendarExportState,
    MailDelivery,
    SyncRecipient,
)
from app.timeutil import iso_utc, utcnow
from app.util import mask_email

logger = logging.getLogger(__name__)

SUBJECT_TEMPLATES = {
    CHANGE_CREATED: "日程已更新:{title}",
    CHANGE_UPDATED: "日程已更新:{title}",
    CHANGE_DELETED: "日程已移除:{title}",
    CHANGE_MANUAL: "日历快照:{calendar}",
}


@dataclass
class QueueSummary:
    attempted: int = 0
    sent: int = 0
    failed: int = 0
    retry_wait: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "sent": self.sent,
            "failed": self.failed,
            "retry_wait": self.retry_wait,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def enabled_recipients(session: Session) -> list[SyncRecipient]:
    stmt = (
        select(SyncRecipient)
        .where(SyncRecipient.enabled.is_(True))
        .order_by(SyncRecipient.created_at.asc())
    )
    return list(session.execute(stmt).scalars())


def attachment_source(
    export: CalendarExport, settings: Settings, *, target_key: str
) -> tuple[Path, str]:
    """发信任务要用的不可变附件:优先该订阅目标「仅尚未结束的日程」的邮件快照。

    回退顺序(都不存在时才继续往下):该目标的邮件快照 → 该目标的完整快照 →
    版本并集快照。这样升级前创建的旧版本、被清理过快照的版本都仍然发得出去,
    宁可多发历史日程,也不能让任务因为找不到文件而失败。
    返回的哈希始终是**实际被附上的那个文件**的 SHA-256,发送前会再校验一次。
    """
    for candidate in (
        mail_snapshot_path_for(settings, export.version, target_key=target_key),
        target_snapshot_path_for(settings, export.version, target_key),
    ):
        try:
            return candidate, hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            continue
    return Path(export.snapshot_path), export.content_hash


def ensure_delivery_tasks(
    session: Session,
    *,
    export_version: int,
    change_type: str = CHANGE_MANUAL,
    trigger_event_id: str | None = None,
    event_title: str | None = None,
    event_time_text: str | None = None,
    settings: Settings | None = None,
) -> int:
    """为指定导出版本创建缺失的发信任务,返回新建数量。

    幂等:同一 (export_version, recipient_id) 已存在任务时跳过,因此重复保存
    不会向同一个人重复发送同一版本。

    附件按**收件人范围**取:账号收件人拿到「他可见日历里尚未结束的日程」,
    外部邮箱拿到「家庭共享日历里尚未结束的日程」。
    """
    resolved = settings or get_settings()
    if not resolved.mail_configured:
        logger.debug("SMTP 未配置,跳过创建发信任务")
        return 0

    export = session.get(CalendarExport, export_version)
    if export is None:
        logger.warning("导出版本不存在,无法创建发信任务 version=%s", export_version)
        return 0

    recipients = enabled_recipients(session)
    if not recipients:
        return 0

    existing = {
        recipient_id
        for (recipient_id,) in session.execute(
            select(MailDelivery.recipient_id).where(
                MailDelivery.export_version == export_version
            )
        )
    }

    created = 0
    sources: dict[str, tuple[Path, str]] = {}
    for recipient in recipients:
        if recipient.id in existing:
            continue
        key = feed_target_key(user_id=recipient_scope_user_id(recipient))
        if key not in sources:
            sources[key] = attachment_source(export, resolved, target_key=key)
        attachment_path, attachment_hash = sources[key]
        session.add(
            MailDelivery(
                export_version=export_version,
                recipient_id=recipient.id,
                recipient_email=recipient.email,
                trigger_event_id=trigger_event_id,
                event_title=(event_title or None),
                event_time_text=(event_time_text or None),
                change_type=change_type,
                attachment_hash=attachment_hash,
                snapshot_path=str(attachment_path),
                status="pending",
                attempt_count=0,
                next_attempt_at=utcnow(),
                created_at=utcnow(),
            )
        )
        created += 1
    if created:
        session.flush()
        logger.info(
            "已创建 %s 个发信任务 version=%s change=%s", created, export_version, change_type
        )
    return created


def _subject_for(
    delivery: MailDelivery, *, calendar_name: str, event_title: str | None
) -> str:
    template = SUBJECT_TEMPLATES.get(delivery.change_type, SUBJECT_TEMPLATES[CHANGE_MANUAL])
    title = (event_title or "").strip()
    if "{title}" in template:
        if not title:
            template = SUBJECT_TEMPLATES[CHANGE_MANUAL]
        else:
            return template.format(title=title[:120])
    return template.format(calendar=calendar_name)


def _body_for(
    delivery: MailDelivery,
    *,
    calendar_name: str,
    scope_label: str,
    subscription_hint: str,
    generated_at: datetime | None,
    event_title: str | None,
    event_time_text: str | None,
) -> str:
    action = {
        CHANGE_CREATED: "新增日程",
        CHANGE_UPDATED: "修改日程",
        CHANGE_DELETED: "删除日程",
        CHANGE_MANUAL: "手动同步",
    }.get(delivery.change_type, "日历更新")

    lines = [
        f"{calendar_name} 的日历已更新。",
        "",
        f"本次操作:{action}",
    ]
    if event_title:
        lines.append(f"事件标题:{event_title}")
    if event_time_text:
        lines.append(f"事件时间:{event_time_text}")
    if generated_at is not None:
        lines.append(f"日历生成时间(UTC):{iso_utc(generated_at)}")
    lines.extend(
        [
            "",
            f"附件 calendar.ics 是**{scope_label}**里尚未结束的日程快照。"
            "完整历史(含过去事件)始终可以从长期订阅地址获得。",
            "",
        ]
    )
    if subscription_hint:
        lines.append(f"你的固定订阅地址(长期有效,需要 Tailscale):{subscription_hint}")
    else:
        lines.append(
            "长期订阅地址:每位家庭成员都有自己的固定地址(每人一条、只在本人界面显示),"
            "主人可以在日历管理界面「我的订阅地址」里查看。"
        )
    lines.extend(
        [
            "",
            "提示:邮件附件只是一次性快照,不会随日程变化自动更新;",
            "需要持续自动更新请使用上面的订阅地址。",
        ]
    )
    return "\n".join(lines)


def _next_attempt(attempt_count: int, backoff_minutes: list[int], now: datetime) -> datetime:
    index = min(max(attempt_count - 1, 0), len(backoff_minutes) - 1)
    return now + timedelta(minutes=backoff_minutes[index])


def _scope_hints(
    recipient: SyncRecipient | None, *, settings: Settings, session: Session
) -> tuple[str, str]:
    """邮件正文里的「附件范围」与「订阅地址」。

    附件范围由 `recipient_scope_user_id()` 决定:账号收件人是**他自己可见的日历**,
    历史外部邮箱沿用绑定时那个账号的范围,其余外部邮箱只有家庭共享日历。

    订阅地址只发给**账号收件人**(`user_id` 非空),而且一定是他自己那条**固定地址**:
    由服务端密钥与账号 ID 派生,所以正文里可以随时重算出来,不必依赖「只显示一次」的明文。
    外部邮箱是替别人收信,正文里不给任何地址。
    """
    from app.accounts import get_user, recipient_scope_user_id
    from app.feed_tokens import account_feed_token

    scope_user_id = recipient_scope_user_id(recipient) if recipient is not None else None
    if scope_user_id:
        label = "你可见的日历(个人 + 家庭共享)"
        if recipient is not None and recipient.user_id:
            hint = urls.subscription_url(
                settings, account_feed_token(settings, recipient.user_id)
            )
            return label, hint
        # 历史外部邮箱收到的是**被绑定账号**的范围,正文不能说成「你可见的日历」
        scope_user = get_user(session, scope_user_id)
        label = (
            f"{scope_user.label if scope_user is not None else '某成员'}"
            "可见的日历(个人 + 家庭共享)"
        )
        return label, ""
    return "家庭共享日历", ""


def _load_attachment(delivery: MailDelivery) -> bytes:
    path = Path(delivery.snapshot_path)
    if not path.exists():
        raise MailError("导出版本快照缺失,无法发送")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != delivery.attachment_hash:
        raise MailError("快照内容哈希与任务记录不一致,已拒绝发送")
    return data


def attempt_delivery(
    session: Session,
    delivery: MailDelivery,
    *,
    config: SmtpConfig,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    """尝试发送一个任务,返回终态字符串。"""
    moment = now or utcnow()
    if delivery.status == "sent":
        return "sent"

    delivery.status = "sending"
    delivery.attempt_count = int(delivery.attempt_count or 0) + 1
    session.flush()

    event_title = delivery.event_title
    event_time_text = delivery.event_time_text
    try:
        attachment = _load_attachment(delivery)
        export = session.get(CalendarExport, delivery.export_version)
        state = session.get(CalendarExportState, CalendarExportState.SINGLETON_ID)
        recipient = session.get(SyncRecipient, delivery.recipient_id)
        scope_label, subscription_hint = _scope_hints(
            recipient, settings=settings, session=session
        )
        body = _body_for(
            delivery,
            calendar_name=settings.calendar_name,
            scope_label=scope_label,
            subscription_hint=subscription_hint,
            generated_at=export.created_at if export else (state.generated_at if state else None),
            event_title=event_title,
            event_time_text=event_time_text,
        )
        message = build_message(
            config=config,
            to_address=delivery.recipient_email,
            to_name=None,
            subject=_subject_for(
                delivery, calendar_name=settings.calendar_name, event_title=event_title
            ),
            body_text=body,
            attachment=attachment,
        )
        send_message(config, message)
    except MailError as exc:
        return _handle_failure(delivery, settings, str(exc), moment)
    except Exception as exc:  # noqa: BLE001 - 任何异常都不能影响其他收件人
        logger.exception("发送任务出现未预期异常 delivery=%s", delivery.id)
        return _handle_failure(delivery, settings, f"未预期错误:{type(exc).__name__}", moment)

    delivery.status = "sent"
    delivery.sent_at = moment
    delivery.error_message = None
    delivery.next_attempt_at = None
    recipient = session.get(SyncRecipient, delivery.recipient_id)
    if recipient is not None:
        recipient.last_sent_at = moment
    session.flush()
    logger.info(
        "邮件已发送 recipient=%s version=%s",
        mask_email(delivery.recipient_email),
        delivery.export_version,
    )
    return "sent"


def _handle_failure(
    delivery: MailDelivery, settings: Settings, message: str, moment: datetime
) -> str:
    delivery.error_message = message[:1000]
    if int(delivery.attempt_count or 0) >= max(1, settings.mail_max_attempts):
        delivery.status = "failed"
        delivery.next_attempt_at = None
        logger.warning(
            "邮件发送最终失败 recipient=%s attempts=%s",
            mask_email(delivery.recipient_email),
            delivery.attempt_count,
        )
        return "failed"
    delivery.status = "retry_wait"
    delivery.next_attempt_at = _next_attempt(
        int(delivery.attempt_count or 0), settings.backoff_minutes, moment
    )
    logger.warning(
        "邮件发送失败将重试 recipient=%s attempts=%s next=%s",
        mask_email(delivery.recipient_email),
        delivery.attempt_count,
        delivery.next_attempt_at,
    )
    return "retry_wait"


def process_queue(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> QueueSummary:
    """处理到期的发送任务。调用方负责提交事务。"""
    resolved = settings or get_settings()
    summary = QueueSummary()
    config = config_from_settings(resolved)
    if config is None:
        return summary

    moment = now or utcnow()
    stmt = (
        select(MailDelivery)
        .where(MailDelivery.status.in_(("pending", "retry_wait")))
        .where(
            (MailDelivery.next_attempt_at.is_(None))
            | (MailDelivery.next_attempt_at <= moment)
        )
        .order_by(MailDelivery.created_at.asc())
        .limit(limit)
    )
    deliveries = list(session.execute(stmt).scalars())
    for delivery in deliveries:
        summary.attempted += 1
        outcome = attempt_delivery(
            session, delivery, config=config, settings=resolved, now=moment
        )
        session.flush()
        if outcome == "sent":
            summary.sent += 1
        elif outcome == "failed":
            summary.failed += 1
            if delivery.error_message:
                summary.errors.append(delivery.error_message)
        elif outcome == "retry_wait":
            summary.retry_wait += 1
        else:
            summary.skipped += 1
    return summary


def retry_delivery(
    session: Session, delivery: MailDelivery, *, settings: Settings | None = None
) -> str:
    """手动重试一次失败任务,立即尝试发送。"""
    resolved = settings or get_settings()
    config = config_from_settings(resolved)
    if config is None:
        raise MailError("SMTP 未配置,无法重试")
    if delivery.status == "sent":
        return "sent"
    delivery.status = "pending"
    delivery.next_attempt_at = utcnow()
    delivery.error_message = None
    return attempt_delivery(session, delivery, config=config, settings=resolved)


def recover_stuck_deliveries(
    session: Session, *, settings: Settings | None = None, older_than_minutes: int = 15
) -> int:
    """把崩溃时卡在 sending 的任务放回队列。"""
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    stmt = select(MailDelivery).where(
        MailDelivery.status == "sending",
        (MailDelivery.next_attempt_at.is_(None)) | (MailDelivery.next_attempt_at <= cutoff),
    )
    stuck = list(session.execute(stmt).scalars())
    for delivery in stuck:
        delivery.status = "retry_wait"
        delivery.next_attempt_at = utcnow()
        delivery.error_message = "发送中断,已自动重新入队"
    if stuck:
        session.flush()
        logger.warning("已恢复 %s 个中断的发送任务", len(stuck))
    return len(stuck)


def queue_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(MailDelivery.status, func.count()).group_by(MailDelivery.status)
    ).all()
    counts = dict.fromkeys(("pending", "sending", "sent", "retry_wait", "failed"), 0)
    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


def send_latest_to_enabled(
    session: Session,
    *,
    settings: Settings | None = None,
    snapshot_version: int | None = None,
) -> int:
    """为当前(或指定)导出版本补齐任务,返回新建任务数。"""
    if snapshot_version is None:
        state = session.get(CalendarExportState, CalendarExportState.SINGLETON_ID)
        snapshot_version = state.version if state else 0
    return ensure_delivery_tasks(
        session,
        export_version=snapshot_version or 0,
        change_type=CHANGE_MANUAL,
        settings=settings,
    )
