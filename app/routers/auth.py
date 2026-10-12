"""登录、登出与邀请码注册。

家庭版起登录主体是 `users` 表里的账号(不再是环境变量里的单一管理员):

- `/login` 校验账号口令,会话里带 `user_id`;
- `/register` 用邀请码自助注册,注册成功即登录;
- 两个入口都有限流,注册额外受邀请码自身的次数与有效期约束。

`ADMIN_USERNAME`/`ADMIN_PASSWORD(_HASH)` 只在首次启动创建首位管理员,
详见 `app/bootstrap.py::ensure_family_setup`。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import email_verify, invites, urls
from app.accounts import (
    AccountError,
    authenticate_user,
    mark_login,
    sync_user_recipient,
    update_profile,
)
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import (
    AnyUserDep,
    CsrfAnyDep,
    CsrfDep,
    optional_user,
    templates,
)
from app.feed_tokens import account_subscription_url
from app.security import (
    LOGIN_CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuditLog,
    create_login_csrf,
    create_session,
    rate_limiter,
    verify_login_csrf,
)
from app.settings_store import SettingValidationError, effective_settings
from app.util import mask_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

LOGIN_RATE_LIMIT = 8
LOGIN_RATE_WINDOW_SECONDS = 300
REGISTER_RATE_LIMIT = 5
REGISTER_RATE_WINDOW_SECONDS = 600


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"


def _safe_next(value: str | None) -> str:
    """只允许跳回站内相对路径,避免开放重定向。"""
    if not value or not value.startswith("/") or value.startswith("//"):
        return urls.url("/admin")
    return value


def _login_context(
    request: Request,
    settings: Settings,
    *,
    error: str | None = None,
    notice: str | None = None,
    next_url: str = "/admin",
) -> dict[str, object]:
    csrf_token = create_login_csrf(settings)
    return {
        "request": request,
        "error": error,
        "notice": notice,
        "next_url": next_url,
        "login_csrf": csrf_token,
        "calendar_name": settings.calendar_name,
        "cookie_name": LOGIN_CSRF_COOKIE_NAME,
        "cookie_value": csrf_token,
    }


def _set_login_cookies(response: Response, settings: Settings, csrf_token: str) -> None:
    response.set_cookie(
        LOGIN_CSRF_COOKIE_NAME,
        csrf_token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=3600,
        path="/",
    )


def _start_session(response: Response, settings: Settings, user_id: str, username: str) -> None:
    token, _ = create_session(user_id=user_id, username=username, settings=settings)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=settings.session_max_age_seconds,
        path="/",
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE_NAME, path="/")


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(
    request: Request,
    next: str = "/admin",
    notice: str | None = None,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_db),
) -> Response:
    if optional_user(request, session) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    context = _login_context(
        request, settings, next_url=_safe_next(next), notice=notice
    )
    response = templates.TemplateResponse(request, "login.html", context)
    _set_login_cookies(response, settings, str(context["login_csrf"]))
    return response


@router.post("/login", include_in_schema=False)
def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    csrf_token: str = Form(default=""),
    next: str = Form(default="/admin"),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_db),
) -> Response:
    client = _client_key(request)
    allowed, retry_after = rate_limiter.check(
        f"login:{client}", limit=LOGIN_RATE_LIMIT, window_seconds=LOGIN_RATE_WINDOW_SECONDS
    )
    target = _safe_next(next)

    if not allowed:
        AuditLog.record("login", "rate_limited", actor=client)
        response = templates.TemplateResponse(
            request,
            "login.html",
            _login_context(
                request,
                settings,
                error=f"尝试过于频繁,请在 {retry_after} 秒后重试。",
                next_url=target,
            ),
            status_code=429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE_NAME)
    if not verify_login_csrf(cookie_token, csrf_token, settings):
        AuditLog.record("login", "csrf_failed", actor=client)
        return templates.TemplateResponse(
            request,
            "login.html",
            _login_context(
                request, settings, error="表单已过期,请重新提交。", next_url=target
            ),
            status_code=400,
        )

    user = authenticate_user(session, username, password)
    if user is None:
        AuditLog.record("login", "denied", actor=client)
        return templates.TemplateResponse(
            request,
            "login.html",
            _login_context(
                request, settings, error="用户名或口令不正确。", next_url=target
            ),
            status_code=401,
        )

    mark_login(session, user)
    session.commit()
    rate_limiter.reset(f"login:{client}")
    AuditLog.record("login", "success", actor=user.username)

    destination = urls.url("/admin/me") if user.must_change_password else target
    if user.must_change_password:
        destination += "?notice=" + "请先修改初始口令"
    elif email_verify.needs_email_verification(user):
        # 还没验证邮箱:先把他送到验证页,别让他在别处碰一鼻子灰
        destination = urls.url("/verify-email")
    response = RedirectResponse(destination, status_code=303)
    _start_session(response, settings, user.id, user.username)
    return response


@router.post("/logout", include_in_schema=False)
def logout(request: Request, current: CsrfDep) -> Response:
    AuditLog.record("logout", "success", actor=current.username)
    response = RedirectResponse(urls.url("/login"), status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


# --------------------------------------------------------------------------
# 邀请码注册
# --------------------------------------------------------------------------
def _register_context(
    request: Request,
    settings: Settings,
    *,
    code: str = "",
    username: str = "",
    display_name: str = "",
    email: str = "",
    error: str | None = None,
) -> dict[str, object]:
    csrf_token = create_login_csrf(settings)
    return {
        "request": request,
        "error": error,
        "code": code,
        "username": username,
        "display_name": display_name,
        "email": email,
        "login_csrf": csrf_token,
        "calendar_name": settings.calendar_name,
        "cookie_value": csrf_token,
    }


@router.get("/register", response_class=HTMLResponse, include_in_schema=False)
def register_page(
    request: Request,
    code: str = "",
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_db),
) -> Response:
    if optional_user(request, session) is not None:
        return RedirectResponse(urls.url("/admin"), status_code=303)
    context = _register_context(request, settings, code=code)
    response = templates.TemplateResponse(request, "register.html", context)
    _set_login_cookies(response, settings, str(context["login_csrf"]))
    return response


@router.post("/register", include_in_schema=False)
def register_submit(
    request: Request,
    code: str = Form(default=""),
    username: str = Form(default=""),
    display_name: str = Form(default=""),
    email: str = Form(default=""),
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
    csrf_token: str = Form(default=""),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_db),
) -> Response:
    client = _client_key(request)
    allowed, retry_after = rate_limiter.check(
        f"register:{client}",
        limit=REGISTER_RATE_LIMIT,
        window_seconds=REGISTER_RATE_WINDOW_SECONDS,
    )
    if not allowed:
        AuditLog.record("register", "rate_limited", actor=client)
        response = templates.TemplateResponse(
            request,
            "register.html",
            _register_context(
                request,
                settings,
                code=code,
                username=username,
                display_name=display_name,
                email=email,
                error=f"尝试过于频繁,请在 {retry_after} 秒后重试。",
            ),
            status_code=429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE_NAME)
    if not verify_login_csrf(cookie_token, csrf_token, settings):
        AuditLog.record("register", "csrf_failed", actor=client)
        return templates.TemplateResponse(
            request,
            "register.html",
            _register_context(
                request,
                settings,
                code=code,
                username=username,
                display_name=display_name,
                email=email,
                error="表单已过期,请重新提交。",
            ),
            status_code=400,
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "register.html",
            _register_context(
                request,
                settings,
                code=code,
                username=username,
                display_name=display_name,
                email=email,
                error="两次输入的口令不一致。",
            ),
            status_code=422,
        )

    if not email.strip():
        return templates.TemplateResponse(
            request,
            "register.html",
            _register_context(
                request,
                settings,
                code=code,
                username=username,
                display_name=display_name,
                email=email,
                error="请填写邮箱:注册后需要用它接收验证码和你的固定订阅地址。",
            ),
            status_code=422,
        )

    try:
        user = invites.register_with_invite(
            session,
            code=code,
            username=username,
            password=password,
            display_name=display_name,
            email=email,
            settings=settings,
        )
    except AccountError as exc:
        session.rollback()
        AuditLog.record("register", "invalid", actor=client)
        return templates.TemplateResponse(
            request,
            "register.html",
            _register_context(
                request,
                settings,
                code=code,
                username=username,
                display_name=display_name,
                email=email,
                error=";".join(exc.fields.values()),
            ),
            status_code=422,
        )
    except ValueError as exc:
        session.rollback()
        AuditLog.record("register", "denied", actor=client)
        return templates.TemplateResponse(
            request,
            "register.html",
            _register_context(
                request,
                settings,
                code=code,
                username=username,
                display_name=display_name,
                email=email,
                error=str(exc),
            ),
            status_code=403,
        )
    except Exception:  # noqa: BLE001 - 未预期错误不要泄露内部细节
        session.rollback()
        logger.exception("注册失败")
        return templates.TemplateResponse(
            request,
            "register.html",
            _register_context(
                request,
                settings,
                code=code,
                username=username,
                display_name=display_name,
                email=email,
                error="注册失败,请稍后重试或联系家庭管理员。",
            ),
            status_code=500,
        )

    # 注册成功:立刻发验证码,账号在验证通过前不能用(管理员豁免,见 app/email_verify.py)
    settings_now = effective_settings(session)
    code_plain = email_verify.issue_code(session, settings_now, user)
    session.commit()
    rate_limiter.reset(f"register:{client}")
    AuditLog.record("register", "success", actor=user.username)
    mailed = email_verify.send_code_mail(settings_now, user, code_plain)
    if mailed:
        AuditLog.record("verify_code_sent", "success", actor=user.username)

    response = RedirectResponse(
        f"{urls.url('/verify-email')}?sent={'1' if mailed else '0'}", status_code=303
    )
    _start_session(response, settings, user.id, user.username)
    return response


# --------------------------------------------------------------------------
# 邮箱验证(注册后的必过关卡,管理员豁免)
# --------------------------------------------------------------------------
def _verify_context(
    request: Request,
    current,
    settings: Settings,
    *,
    notice: str | None = None,
    error: str | None = None,
    sent: str | None = None,
) -> dict[str, object]:
    wait = email_verify.resend_wait_seconds(current.user, settings)
    if sent == "1":
        notice = notice or f"验证码已发到 {mask_email(current.user.email)}。"
    elif sent == "0":
        error = error or "验证码邮件没有发出去(SMTP 未配置或发信失败),请点「重新发送」。"
    return {
        "request": request,
        "current_user": current,
        "csrf_token": current.csrf_token,
        "email": current.user.email or "",
        "resend_wait": wait,
        "code_length": email_verify.CODE_LENGTH,
        "ttl_minutes": max(1, settings.email_code_ttl_seconds // 60),
        "max_attempts": max(1, settings.email_code_max_attempts),
        "notice": notice,
        "error": error,
        "calendar_name": settings.calendar_name,
    }


@router.get("/verify-email", response_class=HTMLResponse, include_in_schema=False)
def verify_email_page(
    request: Request,
    current: AnyUserDep,
    sent: str | None = None,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_db),
) -> Response:
    if not email_verify.needs_email_verification(current.user):
        return RedirectResponse(urls.url("/admin"), status_code=303)
    return templates.TemplateResponse(
        request,
        "verify_email.html",
        _verify_context(request, current, settings, sent=sent),
    )


@router.post("/verify-email", response_class=HTMLResponse, include_in_schema=False)
def verify_email_submit(
    request: Request,
    current: CsrfAnyDep,
    code: str = Form(default=""),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_db),
) -> Response:
    status = email_verify.check_code(session, settings, current.user, code)
    if status != "ok":
        session.commit()
        AuditLog.record("verify_email", status, actor=current.username)
        message = {
            "missing": "还没有可用的验证码,请点「重新发送验证码」。",
            "expired": "验证码已过期,请点「重新发送验证码」。",
            "too_many": "错误次数过多,验证码已作废,请点「重新发送验证码」。",
            "mismatch": "验证码不正确,请再试一次。",
        }.get(status, "验证码校验失败,请重试。")
        return templates.TemplateResponse(
            request,
            "verify_email.html",
            _verify_context(request, current, settings, error=message),
            status_code=422,
        )

    # 验证通过:补上账号收件人,并把**固定订阅地址**发给本人
    sync_user_recipient(session, current.user)
    address = account_subscription_url(session, settings, current.user)
    session.commit()
    AuditLog.record("verify_email", "success", actor=current.username)
    if email_verify.send_subscription_mail(
        settings, current.user, subscription_url=address
    ):
        AuditLog.record("subscription_mail_sent", "success", actor=current.username)
    return RedirectResponse(
        urls.url("/admin/me") + "?notice=" + "邮箱已验证,固定订阅地址已发到你的邮箱。",
        status_code=303,
    )


@router.post("/verify-email/resend", response_class=HTMLResponse, include_in_schema=False)
def verify_email_resend(
    request: Request,
    current: CsrfAnyDep,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_db),
) -> Response:
    if not email_verify.needs_email_verification(current.user):
        return RedirectResponse(urls.url("/admin"), status_code=303)
    wait = email_verify.resend_wait_seconds(current.user, settings)
    if wait > 0:
        return templates.TemplateResponse(
            request,
            "verify_email.html",
            _verify_context(
                request, current, settings, error=f"发送过于频繁,请 {wait} 秒后再试。"
            ),
            status_code=429,
        )
    code_plain = email_verify.issue_code(session, settings, current.user)
    session.commit()
    mailed = email_verify.send_code_mail(settings, current.user, code_plain)
    AuditLog.record("verify_code_sent", "success" if mailed else "failed", actor=current.username)
    return templates.TemplateResponse(
        request,
        "verify_email.html",
        _verify_context(request, current, settings, sent="1" if mailed else "0"),
        status_code=200 if mailed else 503,
    )


@router.post("/verify-email/email", response_class=HTMLResponse, include_in_schema=False)
def verify_email_change(
    request: Request,
    current: CsrfAnyDep,
    email: str = Form(default=""),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_db),
) -> Response:
    """注册时邮箱写错了的补救入口:改地址 → 重新发码。"""
    if not email_verify.needs_email_verification(current.user):
        return RedirectResponse(urls.url("/admin"), status_code=303)
    try:
        update_profile(session, current.user, email=email)
    except AccountError as exc:
        session.rollback()
        return templates.TemplateResponse(
            request,
            "verify_email.html",
            _verify_context(
                request,
                current,
                settings,
                error=";".join(exc.fields.values()),
            ),
            status_code=422,
        )
    except SettingValidationError as exc:
        session.rollback()
        return templates.TemplateResponse(
            request,
            "verify_email.html",
            _verify_context(
                request, current, settings, error=";".join(exc.fields.values())
            ),
            status_code=422,
        )
    email_verify.mark_unverified(session, current.user)
    sync_user_recipient(session, current.user)
    code_plain = email_verify.issue_code(session, settings, current.user)
    session.commit()
    mailed = email_verify.send_code_mail(settings, current.user, code_plain)
    AuditLog.record("verify_email_retarget", "success", actor=current.username)
    return templates.TemplateResponse(
        request,
        "verify_email.html",
        _verify_context(
            request,
            current,
            settings,
            notice=f"验证码已发到 {mask_email(current.user.email)}。" if mailed else None,
            error=None if mailed else "邮件发送失败,请稍后重试。",
        ),
        status_code=200 if mailed else 503,
    )