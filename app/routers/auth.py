"""本机管理员登录与登出。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import Settings, get_settings
from app.deps import CsrfDep, optional_admin, templates
from app.security import (
    LOGIN_CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuditLog,
    authenticate,
    create_login_csrf,
    create_session,
    rate_limiter,
    verify_login_csrf,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

LOGIN_RATE_LIMIT = 8
LOGIN_RATE_WINDOW_SECONDS = 300


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"


def _safe_next(value: str | None) -> str:
    """只允许跳回站内相对路径,避免开放重定向。"""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/admin"
    return value


def _login_context(request: Request, settings: Settings, *, error: str | None = None,
                   next_url: str = "/admin") -> dict[str, object]:
    csrf_token = create_login_csrf(settings)
    return {
        "request": request,
        "error": error,
        "next_url": next_url,
        "login_csrf": csrf_token,
        "calendar_name": settings.calendar_name,
        "cookie_name": LOGIN_CSRF_COOKIE_NAME,
        "cookie_value": csrf_token,
    }


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: str = "/admin", settings: Settings = Depends(get_settings)):
    if optional_admin(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    context = _login_context(request, settings, next_url=_safe_next(next))
    response = templates.TemplateResponse(request, "login.html", context)
    response.set_cookie(
        LOGIN_CSRF_COOKIE_NAME,
        str(context["login_csrf"]),
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=3600,
        path="/",
    )
    return response


@router.post("/login", include_in_schema=False)
def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    csrf_token: str = Form(default=""),
    next: str = Form(default="/admin"),
    settings: Settings = Depends(get_settings),
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

    if not authenticate(username, password, settings):
        AuditLog.record("login", "denied", actor=client)
        return templates.TemplateResponse(
            request,
            "login.html",
            _login_context(
                request, settings, error="用户名或口令不正确。", next_url=target
            ),
            status_code=401,
        )

    token, session_data = create_session(settings)
    rate_limiter.reset(f"login:{client}")
    AuditLog.record("login", "success", actor=session_data.username)

    response = RedirectResponse(target, status_code=303)
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
    return response


@router.post("/logout", include_in_schema=False)
def logout(request: Request, admin: CsrfDep) -> Response:
    AuditLog.record("logout", "success", actor=admin.username)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
