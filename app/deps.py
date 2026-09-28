"""FastAPI 依赖:模板环境、登录校验与 CSRF 防护。"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.security import (
    CSRF_FORM_FIELD,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SessionData,
    read_session,
    verify_csrf,
)
from app.timeutil import utc_to_local

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def local_time_filter(
    value: datetime | None, tz_name: str = "UTC", fmt: str = "%Y-%m-%d %H:%M"
) -> str:
    """模板过滤器 `localtime`:把库里的 naive UTC 时间按指定时区展示。

    缺失值渲染成 `—`,与页面其它空值保持一致;时区名非法时退回 UTC 而不是抛错,
    页面不该因为一条脏配置整页 500。
    """
    if value is None:
        return "—"
    try:
        return utc_to_local(value, tz_name or "UTC").strftime(fmt)
    except Exception:  # noqa: BLE001 - 展示层不对坏数据抛错
        return utc_to_local(value, "UTC").strftime(fmt)


templates.env.filters["localtime"] = local_time_filter


class NotAuthenticated(Exception):
    """未登录。页面请求重定向到登录页,API 请求返回 401。"""

    def __init__(self, next_url: str | None = None) -> None:
        super().__init__("未登录")
        self.next_url = next_url


class CsrfFailure(Exception):
    """CSRF 校验失败。"""


@dataclass(frozen=True)
class AdminUser:
    username: str
    csrf_token: str


def is_api_request(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def current_session(request: Request) -> SessionData | None:
    return read_session(request.cookies.get(SESSION_COOKIE_NAME))


def optional_admin(request: Request) -> AdminUser | None:
    session = current_session(request)
    if session is None:
        return None
    return AdminUser(username=session.username, csrf_token=session.csrf_token)


def require_admin(request: Request) -> AdminUser:
    session = current_session(request)
    if session is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    return AdminUser(username=session.username, csrf_token=session.csrf_token)


async def _csrf_candidate(request: Request) -> str | None:
    header_value = request.headers.get(CSRF_HEADER_NAME)
    if header_value:
        return header_value
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get(CSRF_FORM_FIELD)
        if isinstance(value, str):
            return value
    return None


async def require_csrf(request: Request) -> AdminUser:
    """校验会话与 CSRF 令牌。所有修改类接口都必须依赖它。"""
    session = current_session(request)
    if session is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    candidate = await _csrf_candidate(request)
    if not verify_csrf(session, candidate):
        raise CsrfFailure()
    return AdminUser(username=session.username, csrf_token=session.csrf_token)


def require_csrf_header(request: Request) -> AdminUser:
    """只接受请求头的同步 CSRF 校验,用于 JSON 接口。"""
    session = current_session(request)
    if session is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    if not verify_csrf(session, request.headers.get(CSRF_HEADER_NAME)):
        raise CsrfFailure()
    return AdminUser(username=session.username, csrf_token=session.csrf_token)


def settings_dep() -> Settings:
    return get_settings()


AdminDep = Annotated[AdminUser, Depends(require_admin)]
CsrfDep = Annotated[AdminUser, Depends(require_csrf)]
CsrfHeaderDep = Annotated[AdminUser, Depends(require_csrf_header)]
SettingsDep = Annotated[Settings, Depends(settings_dep)]


def raise_api_error(
    status_code: int, code: str, message: str, fields: dict[str, str] | None = None
) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "fields": fields}},
    )


# --------------------------------------------------------------------------
# 远程 Agent 鉴权
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AgentPrincipal:
    """已通过 Bearer Token 认证的调用方。

    只保留令牌指纹,方便限流分桶与审计;令牌明文不进入日志或响应。
    """

    fingerprint: str

    @property
    def label(self) -> str:
        return f"agent:{self.fingerprint}"


def _bearer_from(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[len("bearer ") :].strip()
    return ""


def require_agent_token(request: Request) -> AgentPrincipal:
    """校验 `Authorization: Bearer <token>`。

    未配置任何令牌时,整个 `/api/agent` 视为不存在,返回 404 而不是 401,
    避免向未授权方暴露「这里有一个需要凭据的接口」。
    """
    tokens = get_settings().agent_tokens
    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "not_found", "message": "Not Found", "fields": None}},
        )

    provided = _bearer_from(request)
    if provided:
        for token in tokens:
            if hmac.compare_digest(provided, token):
                digest = hashlib.sha256(provided.encode()).hexdigest()[:16]
                return AgentPrincipal(fingerprint=digest)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": {
                "code": "unauthorized",
                "message": "缺少或无效的 Agent 令牌",
                "fields": None,
            }
        },
        headers={"WWW-Authenticate": 'Bearer realm="sisi-calendar-agent"'},
    )


AgentDep = Annotated[AgentPrincipal, Depends(require_agent_token)]


UNAUTHORIZED = status.HTTP_401_UNAUTHORIZED
