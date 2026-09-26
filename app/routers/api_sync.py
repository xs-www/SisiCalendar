"""同步状态、手动重新生成、群发与失败重试 API。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AdminDep, CsrfHeaderDep
from app.errors import error_payload
from app.mailer import MailError, config_from_settings, send_test_mail, test_connection
from app.mailqueue import (
    process_queue,
    retry_delivery,
    send_latest_to_enabled,
)
from app.models import MailDelivery
from app.schemas import SendLatestRequest
from app.security import AuditLog, rate_limiter
from app.settings_store import effective_settings
from app.sync_service import overview, publish, state_snapshot
from app.timeutil import iso_utc
from app.util import mask_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["sync"])

SEND_LATEST_LIMIT = 3
SEND_LATEST_WINDOW = 600
RETRY_LIMIT = 20
RETRY_WINDOW = 600


def _recent_deliveries(session: Session, limit: int = 20) -> list[dict[str, object]]:
    rows = session.execute(
        select(MailDelivery)
        .order_by(MailDelivery.created_at.desc())
        .limit(limit)
    ).scalars()
    return [
        {
            "id": row.id,
            "export_version": row.export_version,
            "recipient_id": row.recipient_id,
            "recipient_email": mask_email(row.recipient_email),
            "change_type": row.change_type,
            "status": row.status,
            "attempt_count": row.attempt_count,
            "next_attempt_at": iso_utc(row.next_attempt_at),
            "error_message": row.error_message,
            "created_at": iso_utc(row.created_at),
            "sent_at": iso_utc(row.sent_at),
            "attachment_hash": row.attachment_hash,
        }
        for row in rows
    ]


@router.get("/sync/status", summary="查询 ICS 与邮件同步状态")
def sync_status(admin: AdminDep, session: Session = Depends(get_db)) -> dict[str, object]:
    settings = effective_settings(session)
    return overview(session, settings=settings)


@router.post("/sync/calendar", summary="手动重新生成 ICS")
def regenerate_calendar(
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
    force: bool = Query(default=True, description="即使内容未变化也生成新版本"),
) -> dict[str, object]:
    settings = effective_settings(session)
    allowed, retry_after = rate_limiter.check(
        "sync:calendar", limit=30, window_seconds=600
    )
    if not allowed:
        raise HTTPException(
            429,
            error_payload("rate_limited", f"操作过于频繁,请 {retry_after} 秒后再试"),
        )
    outcome = publish(session, settings=settings, force_new_version=force)
    return {
        "ok": outcome.ok,
        "sync": outcome.as_dict(),
        "status": state_snapshot(session, settings=settings),
    }


@router.post("/sync/mail/send-latest", summary="确认后向当前启用人员发送最新版")
def send_latest(
    payload: SendLatestRequest,
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    if not payload.confirm:
        raise HTTPException(
            422,
            error_payload(
                "confirmation_required",
                "向全部启用人员发送完整日历前必须二次确认",
                {"confirm": "必须显式确认"},
            ),
        )

    settings = effective_settings(session)
    if not settings.mail_configured:
        raise HTTPException(
            409,
            error_payload("mail_not_configured", "SMTP 尚未配置,无法发送邮件"),
        )

    allowed, retry_after = rate_limiter.check(
        "sync:send-latest", limit=SEND_LATEST_LIMIT, window_seconds=SEND_LATEST_WINDOW
    )
    if not allowed:
        raise HTTPException(
            429,
            error_payload("rate_limited", f"操作过于频繁,请 {retry_after} 秒后再试"),
        )

    outcome = publish(session, settings=settings)
    if not outcome.ok:
        return {"ok": False, "sync": outcome.as_dict(), "error": outcome.error}

    created = send_latest_to_enabled(session, settings=settings, snapshot_version=outcome.version)
    session.commit()
    summary = process_queue(session, settings=settings, limit=50)
    session.commit()
    AuditLog.record("mail_send_latest", "done", actor=admin.username, detail=f"created={created}")
    return {
        "ok": True,
        "created": created,
        "summary": summary.as_dict(),
        "sync": outcome.as_dict(),
    }


@router.get("/mail-deliveries", summary="查询最近发信任务")
def list_deliveries(
    admin: AdminDep,
    session: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    return {"items": _recent_deliveries(session, limit=limit)}


@router.post("/mail-deliveries/{delivery_id}/retry", summary="重试一次失败的发送任务")
def retry_one(
    delivery_id: str,
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    settings = effective_settings(session)
    allowed, retry_after = rate_limiter.check(
        "sync:retry", limit=RETRY_LIMIT, window_seconds=RETRY_WINDOW
    )
    if not allowed:
        raise HTTPException(
            429,
            error_payload("rate_limited", f"操作过于频繁,请 {retry_after} 秒后再试"),
        )

    delivery = session.get(MailDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(404, error_payload("not_found", "发送任务不存在"))
    if delivery.status == "sent":
        raise HTTPException(409, error_payload("conflict", "该任务已成功发送,无需重试"))
    if delivery.status == "sending":
        raise HTTPException(409, error_payload("conflict", "该任务正在发送中"))

    try:
        status = retry_delivery(session, delivery, settings=settings)
        session.commit()
    except MailError as exc:
        session.rollback()
        raise HTTPException(
            502, error_payload("mail_failed", f"重试失败:{exc}")
        ) from exc

    AuditLog.record("mail_retry", status, actor=admin.username, detail=delivery_id)
    return {"ok": status in ("sent", "retry_wait"), "status": status, "id": delivery_id}


@router.post("/settings/mail/test", summary="测试邮件连接或发送测试邮件")
def mail_test(
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
    to: str | None = Query(default=None, description="填写则发送测试邮件"),
) -> dict[str, object]:
    settings = effective_settings(session)
    config = config_from_settings(settings)
    if config is None:
        raise HTTPException(
            409, error_payload("mail_not_configured", "SMTP 尚未配置(缺少主机或发件地址)")
        )

    allowed, retry_after = rate_limiter.check(
        "settings:mail-test", limit=10, window_seconds=600
    )
    if not allowed:
        raise HTTPException(
            429,
            error_payload("rate_limited", f"操作过于频繁,请 {retry_after} 秒后再试"),
        )

    try:
        if to:
            send_test_mail(config, to.strip(), settings.calendar_name)
            AuditLog.record("mail_test", "sent", actor=admin.username, detail=mask_email(to))
            return {"ok": True, "message": f"测试邮件已发送至 {mask_email(to)}"}
        detail = test_connection(config)
        AuditLog.record("mail_test", "connected", actor=admin.username)
        return {"ok": True, "message": detail}
    except MailError as exc:
        AuditLog.record("mail_test", "failed", actor=admin.username)
        raise HTTPException(502, error_payload("mail_failed", str(exc))) from exc

