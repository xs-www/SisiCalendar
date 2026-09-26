"""管理页面与 HTMX 局部片段。

页面使用 Jinja2 + HTMX:表单类交互走 HTMX 局部刷新,日历本身的增删改走 JSON API
(由 `static/js/calendar.js` 调用并附带 CSRF 请求头)。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.deps import AdminDep, CsrfDep, templates
from app.events import get_event, serialize_event
from app.exporter import get_state
from app.mailer import MailError, config_from_settings, send_test_mail, test_connection
from app.mailqueue import process_queue, retry_delivery, send_latest_to_enabled
from app.models import MailDelivery, SyncRecipient, normalize_email
from app.recurrence import SIMPLE_LABELS, split_rrule
from app.security import AuditLog, rate_limiter
from app.settings_store import (
    SettingValidationError,
    effective_settings,
    load_overrides,
    save_overrides,
)
from app.sync_service import generated_at_display, overview, publish
from app.timeutil import iso_utc, parse_date, parse_iso, utc_to_local, utcnow
from app.util import mask_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])

COMMON_TIMEZONES = [
    "Australia/Brisbane",
    "Australia/Sydney",
    "Australia/Melbourne",
    "Australia/Perth",
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Asia/Tokyo",
    "Asia/Singapore",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "UTC",
]

SYNC_STATE_LABELS = {
    "pending": "待发送",
    "sending": "发送中",
    "sent": "已发送",
    "retry_wait": "等待重试",
    "failed": "失败",
}

CHANGE_LABELS = {
    "created": "新增",
    "updated": "修改",
    "deleted": "删除",
    "manual": "手动同步",
}


@lru_cache(maxsize=1)
def all_timezones() -> tuple[str, ...]:
    try:
        return tuple(sorted(available_timezones()))
    except Exception:  # noqa: BLE001 - 缺少 tzdata 时退回常用列表
        return tuple(COMMON_TIMEZONES)


def _recipients(session: Session) -> list[SyncRecipient]:
    return list(
        session.execute(select(SyncRecipient).order_by(SyncRecipient.created_at.asc())).scalars()
    )


def _deliveries(session: Session, limit: int = 30) -> list[MailDelivery]:
    return list(
        session.execute(
            select(MailDelivery).order_by(MailDelivery.created_at.desc()).limit(limit)
        ).scalars()
    )


def _base_context(
    request: Request, session: Session, settings: Settings, *, active: str
) -> dict[str, object]:
    from app.deps import optional_admin

    admin = optional_admin(request)
    return {
        "request": request,
        "active": active,
        "admin": admin,
        "csrf_token": admin.csrf_token if admin else "",
        "settings": settings,
        "calendar_name": settings.calendar_name,
        "default_timezone": settings.default_timezone,
        "subscription_url": settings.subscription_url,
        "mail_configured": settings.mail_configured,
        "now_local": generated_at_display(iso_utc(utcnow()), settings.default_timezone),
    }


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def calendar_page(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    context = _base_context(request, session, settings, active="calendar")
    context.update(
        {
            "sync_state": overview(session, settings=settings),
            "recurrence_labels": SIMPLE_LABELS,
            "status_options": ["confirmed", "tentative", "cancelled"],
        }
    )
    return templates.TemplateResponse(request, "calendar.html", context)


def _prefill_from_query(request: Request, settings: Settings) -> dict[str, object]:
    """支持从日历空白处拖选后预填时间。"""
    start_raw = (request.query_params.get("start") or "").strip()
    end_raw = (request.query_params.get("end") or "").strip()
    wants_all_day = request.query_params.get("all_day") == "1"
    tz_name = settings.default_timezone
    today = utcnow().date()

    if wants_all_day or (start_raw and "T" not in start_raw):
        start_date = parse_date(start_raw[:10]) if start_raw else today
        end_date = parse_date(end_raw[:10]) if end_raw else None
        if start_date is None:
            start_date = today
        if end_date is None or end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        return {
            "all_day": True,
            "start_date": start_date.isoformat(),
            "end_date_exclusive": end_date.isoformat(),
            "start_local": "",
            "end_local": "",
        }

    start_utc = parse_iso(start_raw) if start_raw else None
    end_utc = parse_iso(end_raw) if end_raw else None
    if start_utc is None:
        return {
            "all_day": False,
            "start_date": today.isoformat(),
            "end_date_exclusive": "",
            "start_local": "",
            "end_local": "",
        }
    if end_utc is None or end_utc <= start_utc:
        end_utc = start_utc + timedelta(
            minutes=max(1, settings.default_event_duration_minutes)
        )
    return {
        "all_day": False,
        "start_date": utc_to_local(start_utc, tz_name).date().isoformat(),
        "end_date_exclusive": "",
        "start_local": utc_to_local(start_utc, tz_name).strftime("%Y-%m-%dT%H:%M"),
        "end_local": utc_to_local(end_utc, tz_name).strftime("%Y-%m-%dT%H:%M"),
    }


@router.get("/admin/events/new", response_class=HTMLResponse, include_in_schema=False)
def event_new_page(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    prefill = _prefill_from_query(request, settings)
    context = _base_context(request, session, settings, active="calendar")
    context.update(
        {
            "event": {
                "id": "",
                "title": "",
                "description": "",
                "location": "",
                "all_day": prefill["all_day"],
                "timezone": settings.default_timezone,
                "status": "confirmed",
                "start_local": prefill["start_local"],
                "end_local": prefill["end_local"],
                "start_date": prefill["start_date"],
                "end_date_exclusive": prefill["end_date_exclusive"],
                "rrule": None,
                "updated_at": "",
            },
            "recurrence_simple": "none",
            "recurrence_until": "",
            "mode": "create",
            "timezones": all_timezones(),
            "common_timezones": COMMON_TIMEZONES,
            "recurrence_labels": SIMPLE_LABELS,
        }
    )
    return templates.TemplateResponse(request, "event_form.html", context)


@router.get("/admin/events/{event_id}", response_class=HTMLResponse, include_in_schema=False)
def event_edit_page(
    event_id: str,
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    event = get_event(session, event_id)
    if event is None:
        raise HTTPException(404, "事件不存在或已删除")
    payload = serialize_event(event)
    simple, until = split_rrule(event.rrule)
    context = _base_context(request, session, settings, active="calendar")
    context.update(
        {
            "event": payload,
            "recurrence_simple": simple,
            "recurrence_until": until,
            "mode": "edit",
            "timezones": all_timezones(),
            "common_timezones": COMMON_TIMEZONES,
            "recurrence_labels": SIMPLE_LABELS,
        }
    )
    return templates.TemplateResponse(request, "event_form.html", context)


@router.get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    overrides = load_overrides(session)
    context = _base_context(request, session, settings, active="settings")
    context.update(
        {
            "overrides": overrides,
            "recipients": _recipients(session),
            "timezones": all_timezones(),
            "smtp": {
                "host": settings.smtp_host or "",
                "port": settings.smtp_port,
                "security": settings.smtp_security,
                "username": settings.smtp_username or "",
                "from_address": settings.smtp_from_address or "",
                "from_name": settings.smtp_from_name,
                "password_configured": bool(settings.smtp_password),
            },
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        }
    )
    return templates.TemplateResponse(request, "settings.html", context)


@router.get("/admin/sync", response_class=HTMLResponse, include_in_schema=False)
def sync_page(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    context = _base_context(request, session, settings, active="sync")
    context.update(
        {
            "sync_state": overview(session, settings=settings),
            "deliveries": _deliveries(session),
            "status_labels": SYNC_STATE_LABELS,
            "change_labels": CHANGE_LABELS,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        }
    )
    return templates.TemplateResponse(request, "sync.html", context)


# --------------------------------------------------------------------------
# HTMX 局部片段:同步人员
# --------------------------------------------------------------------------
def _recipient_partial(
    request: Request, session: Session, settings: Settings, *, notice: str | None = None,
    error: str | None = None, status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/recipients.html",
        {
            "request": request,
            "recipients": _recipients(session),
            "csrf_token": _csrf_of(request),
            "notice": notice,
            "error": error,
            "mail_configured": settings.mail_configured,
        },
        status_code=status_code,
    )


def _csrf_of(request: Request) -> str:
    from app.deps import optional_admin

    admin = optional_admin(request)
    return admin.csrf_token if admin else ""


@router.get("/admin/partials/recipients", response_class=HTMLResponse, include_in_schema=False)
def recipients_partial(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    return _recipient_partial(request, session, settings)


@router.post("/admin/partials/recipients", response_class=HTMLResponse, include_in_schema=False)
def add_recipient(
    request: Request,
    admin: CsrfDep,
    session: Session = Depends(get_db),
    email: str = Form(default=""),
    display_name: str = Form(default=""),
    acknowledge: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    normalized = normalize_email(email)
    if not acknowledge:
        return _recipient_partial(
            request,
            session,
            settings,
            error="请先勾选确认:该人员将收到包含全部日程的完整日历。",
            status_code=422,
        )
    if not normalized or "@" not in normalized:
        return _recipient_partial(
            request, session, settings, error="请输入有效的邮箱地址。", status_code=422
        )
    existing = session.execute(
        select(SyncRecipient).where(SyncRecipient.email == normalized)
    ).scalar_one_or_none()
    if existing is not None:
        return _recipient_partial(
            request, session, settings, error="该邮箱已在列表中。", status_code=409
        )

    session.add(
        SyncRecipient(
            email=normalized,
            display_name=display_name.strip() or None,
            enabled=True,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    session.commit()
    AuditLog.record("recipient_add", "success", actor=admin.username, detail=mask_email(normalized))
    return _recipient_partial(
        request, session, settings, notice=f"已添加 {mask_email(normalized)}"
    )


@router.post(
    "/admin/partials/recipients/{recipient_id}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def toggle_recipient(
    recipient_id: str,
    request: Request,
    admin: CsrfDep,
    session: Session = Depends(get_db),
    acknowledge: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    recipient = session.get(SyncRecipient, recipient_id)
    if recipient is None:
        return _recipient_partial(
            request, session, settings, error="同步人员不存在。", status_code=404
        )
    if not recipient.enabled and not acknowledge:
        return _recipient_partial(
            request,
            session,
            settings,
            error="启用前请确认:该人员将收到包含全部日程的完整日历。",
            status_code=422,
        )
    recipient.enabled = not recipient.enabled
    recipient.updated_at = utcnow()
    session.commit()
    state = "已启用" if recipient.enabled else "已停用"
    AuditLog.record(
        "recipient_toggle", state, actor=admin.username, detail=mask_email(recipient.email)
    )
    return _recipient_partial(
        request, session, settings, notice=f"{mask_email(recipient.email)} {state}"
    )


@router.post(
    "/admin/partials/recipients/{recipient_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def remove_recipient(
    recipient_id: str,
    request: Request,
    admin: CsrfDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    recipient = session.get(SyncRecipient, recipient_id)
    if recipient is None:
        return _recipient_partial(
            request, session, settings, error="同步人员不存在。", status_code=404
        )
    masked = mask_email(recipient.email)
    session.delete(recipient)
    session.commit()
    AuditLog.record("recipient_remove", "success", actor=admin.username, detail=masked)
    return _recipient_partial(request, session, settings, notice=f"已移除 {masked}")


# --------------------------------------------------------------------------
# HTMX 局部片段:日历设置与同步操作
# --------------------------------------------------------------------------
@router.post("/admin/actions/settings", response_class=HTMLResponse, include_in_schema=False)
def save_settings_action(
    request: Request,
    admin: CsrfDep,
    session: Session = Depends(get_db),
    calendar_name: str = Form(default=""),
    default_timezone: str = Form(default=""),
    default_event_duration_minutes: str = Form(default="60"),
    mail_max_attempts: str = Form(default="5"),
    mail_backoff_minutes: str = Form(default="1,5,15,60,180"),
) -> Response:
    values = {
        "calendar_name": calendar_name,
        "default_timezone": default_timezone,
        "default_event_duration_minutes": default_event_duration_minutes,
        "mail_max_attempts": mail_max_attempts,
        "mail_backoff_minutes": mail_backoff_minutes,
    }
    try:
        save_overrides(session, values)
        session.commit()
    except SettingValidationError as exc:
        session.rollback()
        refreshed = effective_settings(session)
        return templates.TemplateResponse(
            request,
            "partials/settings_form.html",
            {
                "request": request,
                "csrf_token": admin.csrf_token,
                "settings": refreshed,
                "overrides": load_overrides(session),
                "timezones": all_timezones(),
                "notice": None,
                "error": "保存失败:" + ";".join(exc.fields.values()),
            },
            status_code=422,
        )

    refreshed = effective_settings(session)
    AuditLog.record("settings_save", "success", actor=admin.username)
    return templates.TemplateResponse(
        request,
        "partials/settings_form.html",
        {
            "request": request,
            "csrf_token": admin.csrf_token,
            "settings": refreshed,
            "overrides": load_overrides(session),
            "timezones": all_timezones(),
            "notice": "设置已保存。",
            "error": None,
        },
    )


@router.post("/admin/actions/mail-test", response_class=HTMLResponse, include_in_schema=False)
def mail_test_action(
    request: Request,
    admin: CsrfDep,
    session: Session = Depends(get_db),
    to: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    config = config_from_settings(settings)
    allowed, retry_after = rate_limiter.check("pages:mail-test", limit=10, window_seconds=600)

    notice: str | None = None
    error: str | None = None
    if config is None:
        error = "SMTP 尚未配置(缺少 SMTP_HOST 或 SMTP_FROM_ADDRESS)。"
    elif not allowed:
        error = f"操作过于频繁,请 {retry_after} 秒后再试。"
    else:
        try:
            if to.strip():
                send_test_mail(config, to.strip(), settings.calendar_name)
                notice = f"测试邮件已发送至 {mask_email(to.strip())}"
            else:
                notice = test_connection(config)
            AuditLog.record("mail_test", "success", actor=admin.username)
        except MailError as exc:
            error = str(exc)
            AuditLog.record("mail_test", "failed", actor=admin.username)

    return templates.TemplateResponse(
        request,
        "partials/mail_status.html",
        {
            "request": request,
            "csrf_token": admin.csrf_token,
            "notice": notice,
            "error": error,
            "smtp": {
                "host": settings.smtp_host or "",
                "port": settings.smtp_port,
                "security": settings.smtp_security,
                "from_address": settings.smtp_from_address or "",
                "password_configured": bool(settings.smtp_password),
            },
            "mail_configured": settings.mail_configured,
        },
    )


@router.post("/admin/actions/regenerate", response_class=HTMLResponse, include_in_schema=False)
def regenerate_action(
    request: Request,
    admin: CsrfDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    allowed, retry_after = rate_limiter.check("pages:regenerate", limit=30, window_seconds=600)
    if not allowed:
        return _sync_partial(
            request,
            session,
            settings,
            error=f"操作过于频繁,请 {retry_after} 秒后再试。",
            status_code=429,
        )
    outcome = publish(session, settings=settings, force_new_version=True)
    if outcome.ok:
        notice = f"已重新生成 version={outcome.version}"
        if outcome.mail_created:
            notice += f",并创建 {outcome.mail_created} 个发信任务"
    else:
        notice = None
    return _sync_partial(
        request,
        session,
        settings,
        notice=notice,
        error=outcome.error,
        status_code=200 if outcome.ok else 500,
    )


@router.post("/admin/actions/send-latest", response_class=HTMLResponse, include_in_schema=False)
def send_latest_action(
    request: Request,
    admin: CsrfDep,
    session: Session = Depends(get_db),
    confirm: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    if not confirm:
        return _sync_partial(
            request,
            session,
            settings,
            error="请先二次确认,再向全部启用人员发送完整日历。",
            status_code=422,
        )
    if not settings.mail_configured:
        return _sync_partial(
            request,
            session,
            settings,
            error="SMTP 尚未配置,无法发送。",
            status_code=409,
        )
    allowed, retry_after = rate_limiter.check("pages:send-latest", limit=3, window_seconds=600)
    if not allowed:
        return _sync_partial(
            request,
            session,
            settings,
            error=f"操作过于频繁,请 {retry_after} 秒后再试。",
            status_code=429,
        )

    outcome = publish(session, settings=settings)
    if not outcome.ok:
        return _sync_partial(
            request, session, settings, error=outcome.error, status_code=500
        )
    created = send_latest_to_enabled(session, settings=settings, snapshot_version=outcome.version)
    session.commit()
    summary = process_queue(session, settings=settings, limit=50)
    session.commit()
    AuditLog.record("mail_send_latest", "done", actor=admin.username, detail=f"created={created}")
    notice = (
        f"已创建 {created} 个任务,成功发送 {summary.sent} 封,"
        f"等待重试 {summary.retry_wait} 封,失败 {summary.failed} 封。"
    )
    return _sync_partial(request, session, settings, notice=notice)


@router.post(
    "/admin/actions/retry/{delivery_id}", response_class=HTMLResponse, include_in_schema=False
)
def retry_action(
    delivery_id: str,
    request: Request,
    admin: CsrfDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    delivery = session.get(MailDelivery, delivery_id)
    if delivery is None:
        return _sync_partial(
            request, session, settings, error="发送任务不存在。", status_code=404
        )
    if delivery.status == "sent":
        return _sync_partial(
            request, session, settings, error="该任务已成功发送,无需重试。", status_code=409
        )
    allowed, retry_after = rate_limiter.check("pages:retry", limit=20, window_seconds=600)
    if not allowed:
        return _sync_partial(
            request,
            session,
            settings,
            error=f"操作过于频繁,请 {retry_after} 秒后再试。",
            status_code=429,
        )
    try:
        status = retry_delivery(session, delivery, settings=settings)
        session.commit()
    except MailError as exc:
        session.rollback()
        return _sync_partial(
            request, session, settings, error=f"重试失败:{exc}", status_code=502
        )
    AuditLog.record("mail_retry", status, actor=admin.username, detail=delivery_id)
    return _sync_partial(
        request,
        session,
        settings,
        notice=f"重试结果:{SYNC_STATE_LABELS.get(status, status)}",
    )


def _sync_partial(
    request: Request,
    session: Session,
    settings: Settings,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/sync_status.html",
        {
            "request": request,
            "csrf_token": _csrf_of(request),
            "sync_state": overview(session, settings=settings),
            "deliveries": _deliveries(session),
            "status_labels": SYNC_STATE_LABELS,
            "change_labels": CHANGE_LABELS,
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/admin/partials/sync", response_class=HTMLResponse, include_in_schema=False)
def sync_partial(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    return _sync_partial(request, session, settings)


@router.get("/admin/partials/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_form_partial(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    return templates.TemplateResponse(
        request,
        "partials/settings_form.html",
        {
            "request": request,
            "csrf_token": admin.csrf_token,
            "settings": settings,
            "overrides": load_overrides(session),
            "timezones": all_timezones(),
            "notice": None,
            "error": None,
        },
    )


__all__ = ["router", "all_timezones", "SYNC_STATE_LABELS", "CHANGE_LABELS", "get_state"]
