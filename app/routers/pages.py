"""管理页面与 HTMX 局部片段。

页面使用 Jinja2 + HTMX:表单类交互走 HTMX 局部刷新,日历本身的增删改走 JSON API
(由 `static/js/calendar.js` 调用并附带 CSRF 请求头)。

家庭版页面分工:

| 页面 | 谁能进 | 作用 |
|---|---|---|
| `/admin` | 所有成员 | 日历(本人可见日历的合并视图,按日历着色) |
| `/admin/events/*` | 所有成员 | 新建/编辑日程(只能选自己有写权限的日历) |
| `/admin/me` | 所有成员 | 个人资料、改口令、我自己的订阅地址 |
| `/admin/members` | 管理员 | 账号、邀请码、共享日历与成员权限 |
| `/admin/settings` | 管理员 | 全局设置、SMTP、收件人、远程接入令牌、家庭订阅地址 |
| `/admin/sync` | 管理员 | ICS 版本与邮件队列 |
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from functools import lru_cache
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import email_verify, feed_tokens, host_port, invites, token_store, urls
from app.accounts import (
    AccountError,
    calendar_access,
    calendar_members_map,
    create_shared_calendar,
    delete_user,
    get_user,
    list_shared_calendars,
    list_users,
    owner_user,
    set_membership,
    set_password,
    set_user_enabled,
    set_user_role,
    sync_account_recipients,
    sync_user_recipient,
    update_profile,
    user_timezone,
)
from app.config import Settings
from app.db import get_db
from app.deps import (
    AdminDep,
    CsrfAdminDep,
    CsrfDep,
    UserDep,
    optional_user,
    templates,
)
from app.events import get_event, serialize_event
from app.exporter import get_state
from app.mailer import MailError, config_from_settings, send_test_mail, test_connection
from app.mailqueue import process_queue, retry_delivery, send_latest_to_enabled
from app.models import MailDelivery, SyncRecipient, normalize_email
from app.recurrence import SIMPLE_LABELS, split_rrule
from app.reminders import custom_reminder_text, preset_options
from app.security import AuditLog, rate_limiter
from app.settings_store import (
    PUBLIC_BASE_URL_KEY,
    SettingValidationError,
    effective_settings,
    load_overrides,
    save_overrides,
)
from app.sync_service import generated_at_display, overview, publish
from app.timeutil import iso_utc, parse_date, parse_iso, utc_to_local, utcnow
from app.timezones import (
    TIMEZONE_CHOICES,
    iana_names,
    known_iana_names,
    timezone_groups,
)
from app.util import mask_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])

COMMON_TIMEZONES = list(iana_names())

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

ROLE_LABELS = {"admin": "管理员", "member": "成员"}
MEMBER_ROLE_LABELS = {"owner": "可管理", "editor": "可编辑", "viewer": "只读"}


@lru_cache(maxsize=1)
def all_timezones() -> tuple[str, ...]:
    try:
        return tuple(sorted(available_timezones()))
    except Exception:  # noqa: BLE001 - 缺少 tzdata 时退回常用列表
        return tuple(COMMON_TIMEZONES)


def _timezone_context(current: str | None = None) -> dict[str, object]:
    """时区下拉框需要的数据:常用清单(带序号)+ 完整 IANA 列表。

    `current` 用于把当前值标记为 `selected`;不在常用清单里的自定义值由模板
    单独补一个选项,避免打开设置页就把用户的时区悄悄改掉。
    """
    return {
        "timezones": all_timezones(),
        "common_timezones": COMMON_TIMEZONES,
        "timezone_choices": TIMEZONE_CHOICES,
        "timezone_groups": timezone_groups(),
        "timezone_known": known_iana_names(),
        "current_timezone": (current or "").strip(),
    }


def _recipients(session: Session) -> list[SyncRecipient]:
    """设置页维护的收件人:**只有外部邮箱**。

    账号收件人由账号资料派生(见 `app/accounts.py::sync_account_recipients`),
    在这里既不能停用也不能删除,所以不进入这张可操作的表格,只在下方只读列出。
    """
    return list(
        session.execute(
            select(SyncRecipient)
            .where(SyncRecipient.user_id.is_(None))
            .order_by(SyncRecipient.created_at.asc())
        ).scalars()
    )


def _account_recipient_entries(session: Session) -> list[dict[str, object]]:
    """只读展示:哪些账号邮箱会**自动**收到日历邮件。

    这是「设置里的同步邮箱」收敛到外部邮箱之后,管理员仍然需要看到的那一半信息:
    账号邮箱不在这里维护,但必须看得见谁在收信、以及没收上的原因。
    """
    entries: list[dict[str, object]] = []
    for user in list_users(session):
        address = (user.email or "").strip()
        verified = user.email_verified_at is not None
        if not user.enabled:
            status = "账号已停用,不收信"
        elif not address:
            status = "未填邮箱"
        elif not verified:
            status = "邮箱待验证"
        else:
            status = "自动同步中"
        entries.append(
            {
                "user": user,
                "email": address,
                "verified": verified,
                "deliverable": bool(user.enabled and address and verified),
                "status": status,
            }
        )
    return entries


def _guard_external_recipient(recipient: SyncRecipient, action: str) -> str | None:
    """账号邮箱只能在账号资料里维护;返回给用户看的提示,外部邮箱返回 `None`。"""
    if recipient.user_id:
        return (
            f"「{mask_email(recipient.email)}」是账号邮箱,自动跟随成员资料,不能在这里{action};"
            "要让这个账号停止收信,请到「成员」页停用该账号。"
        )
    return None


def _deliveries(session: Session, limit: int = 30) -> list[MailDelivery]:
    return list(
        session.execute(
            select(MailDelivery).order_by(MailDelivery.created_at.desc()).limit(limit)
        ).scalars()
    )


def _base_context(
    request: Request, session: Session, settings: Settings, *, active: str
) -> dict[str, object]:
    current = optional_user(request, session)
    tz_name = user_timezone(current.user if current else None, settings)
    return {
        "request": request,
        "active": active,
        "current_user": current,
        "is_admin": bool(current and current.is_admin),
        "csrf_token": current.csrf_token if current else "",
        "settings": settings,
        "calendar_name": settings.calendar_name,
        "default_timezone": tz_name,
        "system_timezone": settings.default_timezone,
        "public_base_url": urls.public_base_url(settings),
        "mail_configured": settings.mail_configured,
        "now_local": generated_at_display(iso_utc(utcnow()), tz_name),
    }


def _csrf_of(request: Request, session: Session) -> str:
    current = optional_user(request, session)
    return current.csrf_token if current else ""


def _calendar_context(session: Session, user) -> dict[str, object]:
    """日历页与表单共用的日历清单(带颜色与可写标记)。"""
    access = calendar_access(session, user)
    return {
        "access": access,
        "calendar_options": [
            {
                "id": calendar.id,
                "name": calendar.name,
                "kind": calendar.kind,
                "color": calendar.color,
                "role": access.role_for(calendar.id),
                "can_write": access.can_write(calendar.id),
            }
            for calendar in access.calendars
        ],
        "default_calendar_id": access.default_write_calendar_id(),
        "can_write_anything": bool(access.writable_calendars),
    }


FEED_PLACEHOLDER_TEXT = (
    "此地址不再提供日历。请使用属于你自己的订阅地址"
    "(形如 /calendar/sisi_feed_xxxx.ics),可在日历应用里查看,或向家庭管理员索取。\n"
)


def public_root(request: Request) -> Response:
    """公开前缀本身(`/calendar`)对订阅端明确回 404 纯文本。

    无令牌地址不再提供日历:订阅端如果把 `/calendar` 存成日历地址,拿到的应该是一句
    看得懂的纯文本,而不是被 303 带到登录页(那会被存成一个 HTML「日历」)。

    浏览器(明确要 `text/html`)则照常送到管理界面:家里人手动输错地址时不该看到一段
    给机器看的话。日历客户端一般发 `*/*` 或 `text/calendar`,走 404 分支。
    """
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(urls.url("/admin"), status_code=303)
    return Response(
        content=FEED_PLACEHOLDER_TEXT,
        status_code=404,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/", include_in_schema=False)
def root(request: Request) -> Response:
    """裸根路径跳管理页;公开前缀本身交给 `public_root()`。"""
    prefix = urls.current_prefix()
    if prefix and request.url.path.rstrip("/") == prefix:
        return public_root(request)
    return RedirectResponse(urls.url("/admin"), status_code=303)


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def calendar_page(
    request: Request,
    current: UserDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    context = _base_context(request, session, settings, active="calendar")
    full = overview(session, settings=settings)
    if current.is_admin:
        sync_state: dict[str, object] = full
    else:
        # 成员只看得到 ICS 生成状态,邮件队列与收件人统计属于管理员视图
        sync_state = {
            "dirty": full["dirty"],
            "version": full["version"],
            "size_bytes": full["size_bytes"],
            "generated_at": full["generated_at"],
            "last_error": full["last_error"],
        }
    context.update(
        {
            "sync_state": sync_state,
            "recurrence_labels": SIMPLE_LABELS,
            "status_options": ["confirmed", "tentative", "cancelled"],
            **_calendar_context(session, current.user),
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
    current: UserDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    prefill = _prefill_from_query(request, settings)
    calendars = _calendar_context(session, current.user)
    requested = (request.query_params.get("calendar") or "").strip()
    selected = requested if calendars["access"].can_write(requested) else calendars[
        "default_calendar_id"
    ]
    context = _base_context(request, session, settings, active="calendar")
    context.update(
        {
            "event": {
                "id": "",
                "title": "",
                "description": "",
                "location": "",
                "url": "",
                "reminders": [],
                "all_day": prefill["all_day"],
                "timezone": user_timezone(current.user, settings),
                "status": "confirmed",
                "start_local": prefill["start_local"],
                "end_local": prefill["end_local"],
                "start_date": prefill["start_date"],
                "end_date_exclusive": prefill["end_date_exclusive"],
                "rrule": None,
                "updated_at": "",
                "calendar_id": selected,
            },
            "recurrence_simple": "none",
            "recurrence_until": "",
            "mode": "create",
            "recurrence_labels": SIMPLE_LABELS,
            "reminder_options": preset_options([]),
            "custom_reminders": "",
            "selected_calendar_id": selected,
            "event_writable": bool(selected),
            **calendars,
            **_timezone_context(user_timezone(current.user, settings)),
        }
    )
    return templates.TemplateResponse(request, "event_form.html", context)


@router.get("/admin/events/{event_id}", response_class=HTMLResponse, include_in_schema=False)
def event_edit_page(
    event_id: str,
    request: Request,
    current: UserDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    access = calendar_access(session, current.user)
    event = get_event(session, event_id, calendar_ids=access.viewable_ids())
    if event is None:
        raise HTTPException(404, "事件不存在、已删除,或不属于你可访问的日历")
    payload = serialize_event(event, access=access)
    simple, until = split_rrule(event.rrule)
    reminder_minutes = list(payload.get("reminders") or [])
    context = _base_context(request, session, settings, active="calendar")
    context.update(
        {
            "event": payload,
            "recurrence_simple": simple,
            "recurrence_until": until,
            "mode": "edit",
            "recurrence_labels": SIMPLE_LABELS,
            "reminder_options": preset_options(reminder_minutes),
            "custom_reminders": custom_reminder_text(reminder_minutes),
            "selected_calendar_id": event.calendar_id,
            "event_writable": access.can_write(event.calendar_id),
            **_calendar_context(session, current.user),
            **_timezone_context(str(payload.get("timezone") or settings.default_timezone)),
        }
    )
    return templates.TemplateResponse(request, "event_form.html", context)


# --------------------------------------------------------------------------
# 我的设置(所有成员)
# --------------------------------------------------------------------------
def _me_context(
    session: Session, settings: Settings, current
) -> dict[str, object]:
    """「我的设置」**整页与局部片段共用**的上下文。

    整页 `me.html` 就是 `{% include "partials/me.html" %}`,所以两边必须喂同一套变量:
    少任何一个,Jinja 只会安静地渲染成空串——页面能打开,但「我的订阅地址」是空的、
    邮箱验证状态也看不见(这不是假设,`test_all_pages_render_with_strict_undefined` 抓的就是它)。
    """
    return {
        "profile": {
            "display_name": current.user.display_name or "",
            "email": current.user.email or "",
            "color": current.user.color or "",
            "timezone": current.user.timezone or "",
        },
        "feed_tokens": feed_tokens.list_feed_tokens(session, user_id=current.user_id),
        "verify_state": {
            "needs_verification": email_verify.needs_email_verification(current.user),
            "verified_at": current.user.email_verified_at,
            "email": current.user.email or "",
        },
        "fixed_feed_url": feed_tokens.account_subscription_url(
            session, settings, current.user
        ),
        "fixed_token_id": getattr(
            feed_tokens.fixed_account_token(session, settings, user_id=current.user_id),
            "id",
            None,
        ),
        # `partials/feeds.html` 的临时地址明文与它用的前缀(只有创建那一次非空)
        "new_token": None,
        "subscription_prefix": urls.public_base_url(settings),
        **_timezone_context(current.user.timezone or ""),
    }


def _me_partial(
    request: Request,
    session: Session,
    settings: Settings,
    current,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    new_token: str | None = None,
) -> Response:
    tz_name = user_timezone(current.user, settings)
    return templates.TemplateResponse(
        request,
        "partials/me.html",
        {
            "request": request,
            "csrf_token": current.csrf_token,
            "current_user": current,
            "default_timezone": tz_name,
            "notice": notice,
            "error": error,
            **_me_context(session, settings, current),
            # `partials/feeds.html` 用 `new_token` / `subscription_prefix` 这对名字,
            # 与 `_feed_partial` 保持一致(名字对不上就只会安静地不显示明文)。
            "new_token": new_token,
        },
        status_code=status_code,
    )


@router.get("/admin/me", response_class=HTMLResponse, include_in_schema=False)
def me_page(
    request: Request,
    current: UserDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    context = _base_context(request, session, settings, active="me")
    context.update(_me_context(session, settings, current))
    context.update(
        {
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        }
    )
    return templates.TemplateResponse(request, "me.html", context)


@router.get("/admin/partials/me", response_class=HTMLResponse, include_in_schema=False)
def me_partial(
    request: Request,
    current: UserDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    return _me_partial(request, session, settings, current)


@router.post("/admin/actions/profile", response_class=HTMLResponse, include_in_schema=False)
def save_profile_action(
    request: Request,
    current: CsrfDep,
    session: Session = Depends(get_db),
    display_name: str = Form(default=""),
    email: str = Form(default=""),
    color: str = Form(default=""),
    timezone: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    previous_email = (current.user.email or "").strip().lower()
    try:
        update_profile(
            session,
            current.user,
            display_name=display_name,
            email=email,
            color=color,
            timezone_name=timezone,
        )
        session.commit()
    except AccountError as exc:
        session.rollback()
        return _me_partial(
            request,
            session,
            effective_settings(session),
            current,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    AuditLog.record("profile_save", "success", actor=current.username)

    # 邮箱变了(或第一次填上)就重新验证:旧验证码作废,发一封新验证码过去。
    # 未验证期间不发日历邮件,也就不会把日程发到一个还没确认归属的地址。
    new_email = (current.user.email or "").strip().lower()
    notice = "个人资料已保存,邮件收件范围已同步。"
    if new_email and new_email != previous_email:
        email_verify.mark_unverified(session, current.user)
        code_plain = email_verify.issue_code(session, settings, current.user)
        sync_user_recipient(session, current.user)
        session.commit()
        if email_verify.send_code_mail(settings, current.user, code_plain):
            AuditLog.record("verify_code_sent", "success", actor=current.username)
            notice = f"邮箱已更新,验证码已发到 {mask_email(current.user.email)}。"
        else:
            notice = "邮箱已更新,但验证码邮件发送失败,请在「邮箱验证」页重新发送。"
    return _me_partial(request, session, settings, current, notice=notice)


@router.post("/admin/actions/password", response_class=HTMLResponse, include_in_schema=False)
def change_password_action(
    request: Request,
    current: CsrfDep,
    session: Session = Depends(get_db),
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    new_password_confirm: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    from app.accounts import verify_user_password

    if not verify_user_password(current.user, current_password):
        AuditLog.record("password_change", "denied", actor=current.username)
        return _me_partial(
            request, session, settings, current, error="当前口令不正确。", status_code=401
        )
    if new_password != new_password_confirm:
        return _me_partial(
            request, session, settings, current, error="两次输入的新口令不一致。", status_code=422
        )
    try:
        set_password(session, current.user, new_password)
        session.commit()
    except AccountError as exc:
        session.rollback()
        return _me_partial(
            request,
            session,
            effective_settings(session),
            current,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    AuditLog.record("password_change", "success", actor=current.username)
    return _me_partial(request, session, settings, current, notice="口令已更新。")


# ---- 我自己的订阅令牌 ----
def _feed_partial(
    request: Request,
    session: Session,
    settings: Settings,
    current,
    *,
    user_id: str | None,
    template: str,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    new_token: str | None = None,
) -> Response:
    rows = feed_tokens.list_feed_tokens(session, user_id=user_id)
    fixed = None
    if user_id:
        fixed = feed_tokens.fixed_account_token(session, settings, user_id=user_id)
    return templates.TemplateResponse(
        request,
        template,
        {
            "request": request,
            "csrf_token": current.csrf_token,
            "current_user": current,
            "feed_tokens": rows,
            "new_token": new_token,
            # 账号的固定地址:由服务端密钥派生,随时可重新展示,不允许停用或撤销
            "fixed_token_id": fixed.id if fixed else None,
            "fixed_feed_url": (
                urls.subscription_url(
                    settings, feed_tokens.account_feed_token(settings, user_id)
                )
                if user_id
                else None
            ),
            "subscription_prefix": urls.public_base_url(settings),
            "default_timezone": settings.default_timezone,
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/admin/partials/feeds", response_class=HTMLResponse, include_in_schema=False)
def my_feeds_partial(
    request: Request,
    current: UserDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    return _feed_partial(
        request,
        session,
        settings,
        current,
        user_id=current.user_id,
        template="partials/feeds.html",
    )


@router.post("/admin/partials/feeds", response_class=HTMLResponse, include_in_schema=False)
def add_my_feed(
    request: Request,
    current: CsrfDep,
    session: Session = Depends(get_db),
    label: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    try:
        plaintext, row = feed_tokens.create_feed_token(
            session, user_id=current.user_id, label=label
        )
    except SettingValidationError as exc:
        session.rollback()
        return _feed_partial(
            request,
            session,
            settings,
            current,
            user_id=current.user_id,
            template="partials/feeds.html",
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    session.commit()
    AuditLog.record("feed_token_add", "success", actor=current.username, detail=row.id)
    return _feed_partial(
        request,
        session,
        settings,
        current,
        user_id=current.user_id,
        template="partials/feeds.html",
        new_token=plaintext,
        notice="订阅地址已创建,明文只显示这一次。",
    )


@router.post(
    "/admin/partials/feeds/{token_id}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def toggle_my_feed(
    token_id: str,
    request: Request,
    current: CsrfDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    row = feed_tokens.get_feed_token(session, token_id)
    if row is None or row.user_id != current.user_id:
        return _feed_partial(
            request,
            session,
            settings,
            current,
            user_id=current.user_id,
            template="partials/feeds.html",
            error="令牌不存在。",
            status_code=404,
        )
    if feed_tokens.is_fixed_account_token(session, settings, row):
        return _feed_partial(
            request,
            session,
            settings,
            current,
            user_id=current.user_id,
            template="partials/feeds.html",
            error="这是账号的固定订阅地址,不能停用。需要临时地址请另建一条。",
            status_code=409,
        )
    feed_tokens.set_enabled(session, row, not row.enabled)
    session.commit()
    state = "已启用" if row.enabled else "已停用"
    AuditLog.record("feed_token_toggle", state, actor=current.username, detail=row.id)
    return _feed_partial(
        request,
        session,
        settings,
        current,
        user_id=current.user_id,
        template="partials/feeds.html",
        notice=f"{row.label} {state}",
    )


@router.post(
    "/admin/partials/feeds/{token_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def revoke_my_feed(
    token_id: str,
    request: Request,
    current: CsrfDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    row = feed_tokens.get_feed_token(session, token_id)
    if row is None or row.user_id != current.user_id:
        return _feed_partial(
            request,
            session,
            settings,
            current,
            user_id=current.user_id,
            template="partials/feeds.html",
            error="令牌不存在。",
            status_code=404,
        )
    label = row.label
    if feed_tokens.is_fixed_account_token(session, settings, row):
        return _feed_partial(
            request,
            session,
            settings,
            current,
            user_id=current.user_id,
            template="partials/feeds.html",
            error="这是账号的固定订阅地址,不能撤销(它随时可以重新展示给本人)。",
            status_code=409,
        )
    feed_tokens.delete(session, row)
    session.commit()
    AuditLog.record("feed_token_revoke", "success", actor=current.username, detail=token_id)
    return _feed_partial(
        request,
        session,
        settings,
        current,
        user_id=current.user_id,
        template="partials/feeds.html",
        notice=f"已撤销:{label}",
    )


# --------------------------------------------------------------------------
# 成员与邀请码(管理员)
# --------------------------------------------------------------------------
def _shared_calendar_entries(session: Session) -> list[dict[str, object]]:
    """共享日历 + 各自成员角色的列表,成员页与令牌表单共用。"""
    return [
        {"calendar": calendar, "roles": calendar_members_map(session, calendar)}
        for calendar in list_shared_calendars(session)
    ]


def _members_context(session: Session, settings: Settings, *, refresh: bool = True) -> dict:
    return {
        "members": list_users(session),
        "invites": invites.list_invites(session),
        "shared_calendars": _shared_calendar_entries(session),
        "member_role_labels": MEMBER_ROLE_LABELS,
        "role_labels": ROLE_LABELS,
        "default_timezone": settings.default_timezone,
        # 邀请链接的固定前半段:明文只在创建那一次出现,列表里只能提示这个形状
        "invite_base_url": f"{urls.public_base_url(settings)}/register?code=",
        "public_base_url": urls.public_base_url(settings),
        # 一次性明文(邀请链接 / 重置口令)默认不存在——整页与片段必须喂同一套变量,
        # 否则 Jinja 会把缺失的名字安静地渲染成空串(整页 /admin/members 就是经 include 渲染的)
        "new_invite": None,
        "new_invite_url": None,
        "new_password": None,
        "reset_username": None,
    }


def _members_partial(
    request: Request,
    session: Session,
    settings: Settings,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    new_invite: str | None = None,
    new_invite_url: str | None = None,
    new_password: str | None = None,
    reset_username: str | None = None,
) -> Response:
    context = {
        "request": request,
        "csrf_token": _csrf_of(request, session),
        # 片段模板里要用它标出「这是你自己」;整页版本由 `_base_context` 提供同一个键
        "current_user": optional_user(request, session),
        "notice": notice,
        "error": error,
        **_members_context(session, settings),
        # 一次性明文必须在展开模板默认值**之后**写,否则会被默认的 None 覆盖掉
        "new_invite": new_invite,
        "new_invite_url": new_invite_url,
        "new_password": new_password,
        "reset_username": reset_username,
    }
    return templates.TemplateResponse(
        request, "partials/members.html", context, status_code=status_code
    )


@router.get("/admin/members", response_class=HTMLResponse, include_in_schema=False)
def members_page(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    context = _base_context(request, session, settings, active="members")
    context.update(
        {
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            **_members_context(session, settings),
        }
    )
    return templates.TemplateResponse(request, "members.html", context)


@router.get("/admin/partials/members", response_class=HTMLResponse, include_in_schema=False)
def members_partial(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    return _members_partial(request, session, settings)


@router.get("/admin/partials/invites", response_class=HTMLResponse, include_in_schema=False)
def invites_partial(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    return _members_partial(request, session, settings)


@router.post("/admin/partials/invites", response_class=HTMLResponse, include_in_schema=False)
def create_invite_action(
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    label: str = Form(default=""),
    max_uses: str = Form(default="1"),
    valid_days: str = Form(default="0"),
) -> Response:
    settings = effective_settings(session)
    expires_at = None
    try:
        days = int(valid_days or 0)
    except ValueError:
        session.rollback()
        return _members_partial(
            request, session, settings, error="有效天数必须是整数。", status_code=422
        )
    if days > 0:
        expires_at = utcnow() + timedelta(days=days)
    try:
        plaintext, row = invites.create_invite(
            session,
            label=label,
            max_uses=max_uses,
            expires_at=expires_at,
            created_by_user_id=admin.user_id,
        )
    except SettingValidationError as exc:
        session.rollback()
        return _members_partial(
            request,
            session,
            settings,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    session.commit()
    AuditLog.record("invite_create", "success", actor=admin.username, detail=row.id)
    return _members_partial(
        request,
        session,
        settings,
        new_invite=plaintext,
        new_invite_url=urls.invite_url(settings, plaintext),
        notice=f"邀请码「{row.label}」已创建,复制下面的邀请链接发给家人即可。",
    )


@router.post(
    "/admin/partials/invites/{invite_id}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def toggle_invite_action(
    invite_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    row = invites.get_invite(session, invite_id)
    if row is None:
        return _members_partial(
            request, session, settings, error="邀请码不存在。", status_code=404
        )
    invites.set_enabled(session, row, not row.enabled)
    session.commit()
    state = "已启用" if row.enabled else "已停用"
    AuditLog.record("invite_toggle", state, actor=admin.username, detail=row.id)
    return _members_partial(request, session, settings, notice=f"{row.label} {state}")


@router.post(
    "/admin/partials/invites/{invite_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def delete_invite_action(
    invite_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    row = invites.get_invite(session, invite_id)
    if row is None:
        return _members_partial(
            request, session, settings, error="邀请码不存在。", status_code=404
        )
    label = row.label
    invites.delete(session, row)
    session.commit()
    AuditLog.record("invite_delete", "success", actor=admin.username, detail=invite_id)
    return _members_partial(request, session, settings, notice=f"已删除邀请码:{label}")


@router.post(
    "/admin/partials/members/{user_id}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def toggle_member_action(
    user_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    user = get_user(session, user_id)
    if user is None:
        return _members_partial(
            request, session, settings, error="账号不存在。", status_code=404
        )
    if user.id == admin.user_id:
        return _members_partial(
            request, session, settings, error="不能停用当前登录的管理员账号。", status_code=409
        )
    set_user_enabled(session, user, not user.enabled)
    session.commit()
    state = "已启用" if user.enabled else "已停用"
    AuditLog.record("member_toggle", state, actor=admin.username, detail=user.username)
    return _members_partial(request, session, settings, notice=f"{user.username} {state}")


@router.post(
    "/admin/partials/members/{user_id}/reset",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def reset_member_password_action(
    user_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    user = get_user(session, user_id)
    if user is None:
        return _members_partial(
            request, session, settings, error="账号不存在。", status_code=404
        )
    temporary = secrets.token_urlsafe(9)
    set_password(session, user, temporary, must_change=True)
    session.commit()
    AuditLog.record("member_reset_password", "success", actor=admin.username, detail=user.username)
    return _members_partial(
        request,
        session,
        settings,
        new_password=temporary,
        reset_username=user.username,
        notice=f"已重置 {user.username} 的口令,明文只显示这一次。",
    )


@router.post(
    "/admin/partials/members/{user_id}/role",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def set_member_role_action(
    user_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    role: str = Form(default="member"),
) -> Response:
    settings = effective_settings(session)
    user = get_user(session, user_id)
    if user is None:
        return _members_partial(
            request, session, settings, error="账号不存在。", status_code=404
        )
    if user.id == admin.user_id and role != "admin":
        return _members_partial(
            request, session, settings, error="不能取消当前登录管理员的权限。", status_code=409
        )
    try:
        set_user_role(session, user, role)
    except AccountError as exc:
        session.rollback()
        return _members_partial(
            request,
            session,
            settings,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    session.commit()
    AuditLog.record("member_role", role, actor=admin.username, detail=user.username)
    return _members_partial(
        request, session, settings, notice=f"{user.username} 现在是{ROLE_LABELS.get(role, role)}"
    )


@router.post(
    "/admin/partials/members/{user_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def delete_member_action(
    user_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    user = get_user(session, user_id)
    if user is None:
        return _members_partial(
            request, session, settings, error="账号不存在。", status_code=404
        )
    if user.id == admin.user_id:
        return _members_partial(
            request, session, settings, error="不能删除当前登录的账号。", status_code=409
        )
    if user.role == "admin" and len([u for u in list_users(session) if u.is_admin]) <= 1:
        return _members_partial(
            request, session, settings, error="至少需要保留一个管理员。", status_code=409
        )
    username = user.username
    delete_user(session, user)
    session.commit()
    AuditLog.record("member_delete", "success", actor=admin.username, detail=username)
    return _members_partial(request, session, settings, notice=f"已删除账号:{username}")


@router.post(
    "/admin/partials/members/{user_id}/calendar-role",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def set_calendar_role_action(
    user_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    calendar_id: str = Form(default=""),
    role: str = Form(default="viewer"),
) -> Response:
    settings = effective_settings(session)
    user = get_user(session, user_id)
    if user is None:
        return _members_partial(
            request, session, settings, error="账号不存在。", status_code=404
        )
    calendar = next(
        (item for item in list_shared_calendars(session) if item.id == calendar_id), None
    )
    if calendar is None:
        return _members_partial(
            request, session, settings, error="共享日历不存在。", status_code=404
        )
    try:
        set_membership(session, calendar, user, role)
    except AccountError as exc:
        session.rollback()
        return _members_partial(
            request,
            session,
            settings,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    session.commit()
    AuditLog.record(
        "calendar_role", role, actor=admin.username, detail=f"{user.username}@{calendar.name}"
    )
    return _members_partial(
        request,
        session,
        settings,
        notice=f"{user.username} 在「{calendar.name}」的权限:{MEMBER_ROLE_LABELS.get(role, role)}",
    )


@router.post(
    "/admin/partials/shared-calendars", response_class=HTMLResponse, include_in_schema=False
)
def create_shared_calendar_action(
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    name: str = Form(default=""),
    color: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    try:
        calendar = create_shared_calendar(
            session, name=name, color=color or None, creator=admin.user
        )
    except AccountError as exc:
        session.rollback()
        return _members_partial(
            request,
            session,
            settings,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    session.commit()
    AuditLog.record("shared_calendar_create", "success", actor=admin.username, detail=calendar.id)
    return _members_partial(
        request,
        session,
        settings,
        notice=f"已创建共享日历「{calendar.name}」,新成员默认只读,可在下表调整权限。",
    )


# --------------------------------------------------------------------------
# 全局设置与同步(管理员)
# --------------------------------------------------------------------------
@router.get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    overrides = load_overrides(session)
    # 账号收件人由账号资料派生:整页渲染前对齐一次(被历史操作删掉/停用的行会自动补回)
    if sync_account_recipients(session):
        session.commit()
    context = _base_context(request, session, settings, active="settings")
    context.update(
        {
            "overrides": overrides,
            # `partials/settings_form.html` 整页 include:变量名必须与 HTMX 局部渲染一致
            **_settings_form_context(request, settings, overrides),
            "recipients": _recipients(session),
            "account_recipients": _account_recipient_entries(session),
            "tokens": token_store.list_tokens(session),
            "family_feed_tokens": feed_tokens.list_feed_tokens(session, user_id=None),
            "users": list_users(session),
            "shared_calendars": _shared_calendar_entries(session),
            "default_owner": owner_user(session),
            # `partials/family_feeds.html` 里的循环变量名叫 `feed_tokens`(与整页同键)
            "feed_tokens": feed_tokens.list_feed_tokens(session, user_id=None),
            **_timezone_context(overrides.get("default_timezone", settings.default_timezone)),
            # 一次性明文(家庭订阅令牌 / 远程接入令牌)默认不存在:
            # 整页 settings.html 是经 include 渲染这些片段的,变量名必须齐全
            "new_token": None,
            "subscription_prefix": urls.public_base_url(settings),
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
# HTMX 局部片段:邮件收件人
# --------------------------------------------------------------------------
def _recipient_partial(
    request: Request,
    session: Session,
    settings: Settings,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    # 账号收件人由账号资料派生:渲染前对齐一次,页面与邮件队列看到的是同一份事实
    if sync_account_recipients(session):
        session.commit()
    return templates.TemplateResponse(
        request,
        "partials/recipients.html",
        {
            "request": request,
            "recipients": _recipients(session),
            "account_recipients": _account_recipient_entries(session),
            "users": list_users(session),
            "csrf_token": _csrf_of(request, session),
            "default_timezone": settings.default_timezone,
            "notice": notice,
            "error": error,
            "mail_configured": settings.mail_configured,
        },
        status_code=status_code,
    )


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
    admin: CsrfAdminDep,
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
            error="请先勾选确认:外部邮箱会收到家庭共享日历(不含任何人的私人日程)。",
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
            user_id=None,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    session.commit()
    AuditLog.record(
        "recipient_add", "success", actor=admin.username, detail=mask_email(normalized)
    )
    return _recipient_partial(
        request,
        session,
        settings,
        notice=f"已添加外部收件人 {mask_email(normalized)}(家庭共享范围)",
    )


@router.post(
    "/admin/partials/recipients/{recipient_id}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def toggle_recipient(
    recipient_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    acknowledge: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    recipient = session.get(SyncRecipient, recipient_id)
    if recipient is None:
        return _recipient_partial(
            request, session, settings, error="收件人不存在。", status_code=404
        )
    blocked = _guard_external_recipient(recipient, "停用或启用")
    if blocked:
        return _recipient_partial(request, session, settings, error=blocked, status_code=409)
    if not recipient.enabled and not acknowledge:
        return _recipient_partial(
            request,
            session,
            settings,
            error="启用前请确认:该邮箱将收到对应范围的日历快照。",
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
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    recipient = session.get(SyncRecipient, recipient_id)
    if recipient is None:
        return _recipient_partial(
            request, session, settings, error="收件人不存在。", status_code=404
        )
    blocked = _guard_external_recipient(recipient, "移除")
    if blocked:
        return _recipient_partial(request, session, settings, error=blocked, status_code=409)
    masked = mask_email(recipient.email)
    session.delete(recipient)
    session.commit()
    AuditLog.record("recipient_remove", "success", actor=admin.username, detail=masked)
    return _recipient_partial(request, session, settings, notice=f"已移除 {masked}")


# --------------------------------------------------------------------------
# HTMX 局部片段:远程接入令牌(管理员)
# --------------------------------------------------------------------------
def _token_partial(
    request: Request,
    session: Session,
    settings: Settings,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    new_token: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/tokens.html",
        {
            "request": request,
            "tokens": token_store.list_tokens(session),
            "users": list_users(session),
            "shared_calendars": _shared_calendar_entries(session),
            "default_owner": owner_user(session),
            "csrf_token": _csrf_of(request, session),
            "default_timezone": settings.default_timezone,
            "new_token": new_token,
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/admin/partials/tokens", response_class=HTMLResponse, include_in_schema=False)
def tokens_partial(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    return _token_partial(request, session, settings)


@router.post("/admin/partials/tokens", response_class=HTMLResponse, include_in_schema=False)
def add_token(
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    label: str = Form(default=""),
    user_id: str = Form(default=""),
    calendar_id: str = Form(default=""),
    acknowledge: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    if not acknowledge:
        return _token_partial(
            request,
            session,
            settings,
            error="请先勾选确认:该令牌持有者可远程新建日程并触发邮件同步。",
            status_code=422,
        )
    owner = owner_user(session)
    target_user_id = user_id.strip() or (owner.id if owner else None)
    if target_user_id is None:
        return _token_partial(
            request, session, settings, error="没有可归属的账号。", status_code=409
        )
    target_user = get_user(session, target_user_id)
    if target_user is None:
        return _token_partial(
            request, session, settings, error="归属账号不存在。", status_code=404
        )
    if calendar_id.strip():
        access = calendar_access(session, target_user)
        if not access.can_write(calendar_id.strip()):
            return _token_partial(
                request,
                session,
                settings,
                error="该账号对所选日历没有写权限,请换一个日历或提升权限。",
                status_code=422,
            )
    try:
        plaintext, row = token_store.create_token(
            session,
            label,
            user_id=target_user_id,
            calendar_id=calendar_id.strip() or None,
        )
    except SettingValidationError as exc:
        session.rollback()
        return _token_partial(
            request,
            session,
            settings,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    session.commit()
    AuditLog.record("agent_token_add", "success", actor=admin.username, detail=row.id)
    return _token_partial(
        request,
        session,
        settings,
        new_token=plaintext,
        notice=f"令牌已创建(指纹 {row.token_hash[:16]},归属 {target_user.username})",
    )


@router.post(
    "/admin/partials/tokens/{token_id}", response_class=HTMLResponse, include_in_schema=False
)
def rename_token_route(
    token_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    label: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    token = token_store.get_token(session, token_id)
    if token is None:
        return _token_partial(
            request, session, settings, error="令牌不存在。", status_code=404
        )
    try:
        token_store.rename(session, token, label)
    except SettingValidationError as exc:
        session.rollback()
        return _token_partial(
            request,
            session,
            settings,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    session.commit()
    AuditLog.record("agent_token_rename", "success", actor=admin.username, detail=token.id)
    return _token_partial(request, session, settings, notice=f"已改名:{token.label}")


@router.post(
    "/admin/partials/tokens/{token_id}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def toggle_token(
    token_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    token = token_store.get_token(session, token_id)
    if token is None:
        return _token_partial(
            request, session, settings, error="令牌不存在。", status_code=404
        )
    token_store.set_enabled(session, token, not token.enabled)
    session.commit()
    state = "已启用" if token.enabled else "已停用"
    AuditLog.record("agent_token_toggle", state, actor=admin.username, detail=token.id)
    return _token_partial(request, session, settings, notice=f"{token.label} {state}")


@router.post(
    "/admin/partials/tokens/{token_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def revoke_token(
    token_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    token = token_store.get_token(session, token_id)
    if token is None:
        return _token_partial(
            request, session, settings, error="令牌不存在。", status_code=404
        )
    label = token.label
    token_store.delete(session, token)
    session.commit()
    AuditLog.record("agent_token_revoke", "success", actor=admin.username, detail=token_id)
    return _token_partial(request, session, settings, notice=f"已撤销:{label}")


# --------------------------------------------------------------------------
# HTMX 局部片段:家庭订阅地址(管理员)
# --------------------------------------------------------------------------
def _family_feed_partial(
    request: Request,
    session: Session,
    settings: Settings,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    new_token: str | None = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/family_feeds.html",
        {
            "request": request,
            "feed_tokens": feed_tokens.list_feed_tokens(session, user_id=None),
            "new_token": new_token,
            "csrf_token": _csrf_of(request, session),
            "subscription_prefix": urls.public_base_url(settings),
            "default_timezone": settings.default_timezone,
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/admin/partials/family-feeds", response_class=HTMLResponse, include_in_schema=False)
def family_feeds_partial(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    return _family_feed_partial(request, session, settings)


@router.post("/admin/partials/family-feeds", response_class=HTMLResponse, include_in_schema=False)
def add_family_feed(
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    label: str = Form(default="家庭共享日历"),
) -> Response:
    settings = effective_settings(session)
    try:
        plaintext, row = feed_tokens.create_feed_token(session, user_id=None, label=label)
    except SettingValidationError as exc:
        session.rollback()
        return _family_feed_partial(
            request,
            session,
            settings,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )
    session.commit()
    AuditLog.record("family_feed_add", "success", actor=admin.username, detail=row.id)
    return _family_feed_partial(
        request,
        session,
        settings,
        new_token=plaintext,
        notice="家庭订阅地址已创建,明文只显示这一次。",
    )


@router.post(
    "/admin/partials/family-feeds/{token_id}/toggle",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def toggle_family_feed(
    token_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    row = feed_tokens.get_feed_token(session, token_id)
    if row is None or row.user_id is not None:
        return _family_feed_partial(
            request, session, settings, error="令牌不存在。", status_code=404
        )
    feed_tokens.set_enabled(session, row, not row.enabled)
    session.commit()
    state = "已启用" if row.enabled else "已停用"
    AuditLog.record("family_feed_toggle", state, actor=admin.username, detail=row.id)
    return _family_feed_partial(request, session, settings, notice=f"{row.label} {state}")


@router.post(
    "/admin/partials/family-feeds/{token_id}/delete",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def revoke_family_feed(
    token_id: str,
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    settings = effective_settings(session)
    row = feed_tokens.get_feed_token(session, token_id)
    if row is None or row.user_id is not None:
        return _family_feed_partial(
            request, session, settings, error="令牌不存在。", status_code=404
        )
    label = row.label
    feed_tokens.delete(session, row)
    session.commit()
    AuditLog.record("family_feed_revoke", "success", actor=admin.username, detail=token_id)
    return _family_feed_partial(request, session, settings, notice=f"已撤销:{label}")


# --------------------------------------------------------------------------
# HTMX 局部片段:日历设置与同步操作(管理员)
# --------------------------------------------------------------------------
def _tailnet_origin_hint(request: Request, settings: Settings) -> str:
    """从本次请求推导「对外地址」候选;只有 tailnet 域名(`*.ts.net`)才自动填入。

    经 Tailscale Serve 访问时,请求里带着节点域名(`Host`)与转发头
    (`X-Forwarded-Proto`)。这两个头**只是建议来源,不是安全判据**:
    值最终要管理员确认保存后才写库,而且只允许写到 `*.ts.net` 上。
    """
    host = (request.headers.get("host") or "").strip().lower()
    if not host or host.startswith("["):
        return ""
    name = host.rsplit(":", 1)[0]
    if not name.endswith(".ts.net"):
        return ""
    scheme = (
        (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    )
    if scheme not in {"http", "https"}:
        scheme = "https"
    return f"{scheme}://{name}"


def _serve_command(settings: Settings) -> str:
    """给宿主机复制用的 `tailscale serve` 命令。

    端口取自 `HOST_PORT`(**不能照抄文档里的 443**:`compose.nas.yaml` 发布的是 18443),
    路径取自挂载前缀;`--set-path` 与目标 URL 的路径必须逐字相同。
    """
    base_path = urls.public_base_path(settings)
    target = f"http://127.0.0.1:{settings.host_port}{base_path}"
    if base_path:
        return f"tailscale serve --bg --yes --https=443 --set-path={base_path} {target}"
    return f"tailscale serve --bg --yes --https=443 {target}"


def _settings_form_context(
    request: Request, settings: Settings, overrides: dict[str, str]
) -> dict[str, object]:
    """`partials/settings_form.html` 的上下文。

    `settings.html` 整页 include 这个片段、HTMX 保存后又只换这一个片段,两条渲染路径
    必须给出**同一套变量**(模板开了 `StrictUndefined`,少一个就是 500)。
    """
    env_settings = getattr(request.app.state, "settings", settings)
    override_origin = overrides.get(PUBLIC_BASE_URL_KEY, "")
    suggested_origin = "" if override_origin else _tailnet_origin_hint(request, settings)
    return {
        "settings": settings,
        "overrides": overrides,
        # 对外地址卡片:当前生效值 / 环境变量值 / 库里的 origin 覆盖 / 自动检测到的候选
        "public_base_url_effective": urls.public_base_url(settings),
        "public_base_url_env": urls.public_base_url(env_settings),
        "public_base_url_override": override_origin,
        "public_base_url_input": override_origin
        or suggested_origin
        or urls.public_base_url(env_settings),
        "public_base_url_suggested": suggested_origin,
        "public_base_path": urls.public_base_path(settings),
        "host_port": settings.host_port,
        "serve_command": _serve_command(settings),
    }


def _settings_form_response(
    request: Request, session: Session, settings: Settings, admin, *,
    notice: str | None, error: str | None, status_code: int = 200,
) -> Response:
    overrides = load_overrides(session)
    return templates.TemplateResponse(
        request,
        "partials/settings_form.html",
        {
            "request": request,
            "csrf_token": admin.csrf_token,
            **_settings_form_context(request, settings, overrides),
            **_timezone_context(settings.default_timezone),
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


@router.post("/admin/actions/settings", response_class=HTMLResponse, include_in_schema=False)
def save_settings_action(
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    calendar_name: str = Form(default=""),
    default_timezone: str = Form(default=""),
    default_event_duration_minutes: str = Form(default="60"),
    mail_max_attempts: str = Form(default="5"),
    mail_backoff_minutes: str = Form(default="1,5,15,60,180"),
    public_base_url: str = Form(default=""),
) -> Response:
    values = {
        "calendar_name": calendar_name,
        "default_timezone": default_timezone,
        "default_event_duration_minutes": default_event_duration_minutes,
        "mail_max_attempts": mail_max_attempts,
        "mail_backoff_minutes": mail_backoff_minutes,
        # 对外地址:只允许改 origin;空值 = 撤销覆盖、回到环境变量
        "public_base_url": public_base_url,
    }
    try:
        save_overrides(session, values, effective_settings(session))
        session.commit()
    except SettingValidationError as exc:
        session.rollback()
        return _settings_form_response(
            request,
            session,
            effective_settings(session),
            admin,
            notice=None,
            error="保存失败:" + ";".join(exc.fields.values()),
            status_code=422,
        )

    AuditLog.record("settings_save", "success", actor=admin.username)
    return _settings_form_response(
        request, session, effective_settings(session), admin, notice="设置已保存。", error=None
    )


@router.post(
    "/admin/actions/host-port-check", response_class=HTMLResponse, include_in_schema=False
)
def host_port_check_action(
    request: Request,
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
) -> Response:
    """探测宿主端口映射是否真的通了(管理员点一下才跑,不在页面加载时自动跑)。

    只连 `host.docker.internal:<HOST_PORT>/healthz`,主机名写死、端口只认数字,
    见 `app/host_port.py`。
    """
    settings = effective_settings(session)
    reachable, detail = host_port.probe_host_port(settings)
    AuditLog.record(
        "host_port_check", "success" if reachable else "failed", actor=admin.username
    )
    return templates.TemplateResponse(
        request,
        "partials/host_port_check.html",
        {
            "request": request,
            "reachable": reachable,
            "detail": detail,
            "host_port": settings.host_port,
        },
    )


@router.post("/admin/actions/mail-test", response_class=HTMLResponse, include_in_schema=False)
def mail_test_action(
    request: Request,
    admin: CsrfAdminDep,
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
    admin: CsrfAdminDep,
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
    outcome = publish(
        session,
        settings=settings,
        force_new_version=True,
        actor_user_id=admin.user_id,
        actor_label=admin.label,
    )
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
    admin: CsrfAdminDep,
    session: Session = Depends(get_db),
    confirm: str = Form(default=""),
) -> Response:
    settings = effective_settings(session)
    if not confirm:
        return _sync_partial(
            request,
            session,
            settings,
            error="请先二次确认,再向全部启用收件人发送日历快照(每人只收到自己范围里尚未结束的日程)。",
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

    outcome = publish(
        session, settings=settings, actor_user_id=admin.user_id, actor_label=admin.label
    )
    if not outcome.ok:
        return _sync_partial(
            request, session, settings, error=outcome.error, status_code=500
        )
    created = send_latest_to_enabled(
        session,
        settings=settings,
        snapshot_version=outcome.version,
        actor_user_id=admin.user_id,
        actor_label=admin.label,
    )
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
    admin: CsrfAdminDep,
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
            "csrf_token": _csrf_of(request, session),
            "sync_state": overview(session, settings=settings),
            "deliveries": _deliveries(session),
            "status_labels": SYNC_STATE_LABELS,
            "change_labels": CHANGE_LABELS,
            # 邮件队列里的时间按日历默认时区展示,避免只看到 UTC 还要自己换算
            "default_timezone": settings.default_timezone,
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
    return _settings_form_response(
        request, session, settings, admin, notice=None, error=None
    )


__all__ = [
    "router",
    "all_timezones",
    "timezone_groups",
    "SYNC_STATE_LABELS",
    "CHANGE_LABELS",
    "MEMBER_ROLE_LABELS",
    "ROLE_LABELS",
    "get_state",
]