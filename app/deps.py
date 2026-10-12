"""FastAPI 依赖:模板环境、登录校验、角色校验与 CSRF 防护。

家庭版起有两级主体:

- **成员**(任何登录账号):管理自己的日历、查看共享日历;
- **管理员**:额外可以管理账号、邀请码、共享日历、SMTP 与远程令牌。

权限判定一律走 `app/accounts.py`;这里只负责「有没有登录」「是不是管理员」「CSRF 对不对」。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import urls
from app.config import Settings, get_settings
from app.db import get_db
from app.email_verify import needs_email_verification
from app.models import ROLE_ADMIN, User
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
#: 模板里的链接前缀:从裸路径进来是 ``,从 `/calendar/...` 进来是 `/calendar`。
#: 用全局函数而不是常量,因为同一个应用同时服务两种入口(见 `app/urls.py`)。
templates.env.globals["bp"] = urls.current_prefix
templates.env.globals["url"] = urls.url
#: 静态资源:带上内容指纹,避免浏览器拿旧 CSS 渲染出错版页面
templates.env.globals["asset"] = urls.asset


class NotAuthenticated(Exception):
    """未登录。页面请求重定向到登录页,API 请求返回 401。"""

    def __init__(self, next_url: str | None = None) -> None:
        super().__init__("未登录")
        self.next_url = next_url


class CsrfFailure(Exception):
    """CSRF 校验失败。"""


class Forbidden(Exception):
    """已登录但权限不足(例如成员访问管理员页面、改别人的私有日历)。"""

    def __init__(self, message: str = "没有权限执行该操作") -> None:
        super().__init__(message)
        self.message = message


class EmailNotVerified(Exception):
    """已登录但邮箱还没验证:页面请求送到验证页,API 请求返回 403。

    验证页自身与「重新发送验证码」用 `require_any_user` / `require_csrf_any`,
    不受这道闸门影响,否则会自己把自己挡在门外。
    """


@dataclass(frozen=True)
class CurrentUser:
    """当前登录账号。`user` 是数据库里的那一行,权限判定都用它。"""

    user: User
    csrf_token: str

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def username(self) -> str:
        return self.user.username

    @property
    def label(self) -> str:
        return self.user.label

    @property
    def is_admin(self) -> bool:
        return self.user.role == ROLE_ADMIN


def is_api_request(request: Request) -> bool:
    return urls.is_api_path(request.url.path)


def current_session(request: Request) -> SessionData | None:
    return read_session(request.cookies.get(SESSION_COOKIE_NAME))


def _load_current(session_data: SessionData | None, session: Session) -> CurrentUser | None:
    if session_data is None:
        return None
    user = session.get(User, session_data.user_id)
    # 账号被删除或停用后,现有会话立刻失效
    if user is None or not user.enabled:
        return None
    return CurrentUser(user=user, csrf_token=session_data.csrf_token)


def optional_user(
    request: Request, session: Session = Depends(get_db)
) -> CurrentUser | None:
    return _load_current(current_session(request), session)


def _gate_verification(current: CurrentUser) -> CurrentUser:
    if needs_email_verification(current.user):
        raise EmailNotVerified()
    return current


def require_user(request: Request, session: Session = Depends(get_db)) -> CurrentUser:
    current = _load_current(current_session(request), session)
    if current is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    return _gate_verification(current)


def require_any_user(request: Request, session: Session = Depends(get_db)) -> CurrentUser:
    """已登录即可,**不**检查邮箱验证:验证页与改邮箱/重发验证码专用。"""
    current = _load_current(current_session(request), session)
    if current is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    return current


def require_admin_user(request: Request, session: Session = Depends(get_db)) -> CurrentUser:
    current = require_user(request, session)
    if not current.is_admin:
        raise Forbidden("该功能仅家庭管理员可用")
    return current


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


def _verify(request: Request, session: Session, candidate: str | None) -> CurrentUser:
    session_data = current_session(request)
    if session_data is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    if not verify_csrf(session_data, candidate):
        raise CsrfFailure()
    current = _load_current(session_data, session)
    if current is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    return _gate_verification(current)


async def require_csrf(request: Request, session: Session = Depends(get_db)) -> CurrentUser:
    """校验会话与 CSRF 令牌(表单或请求头)。所有修改类页面接口都必须依赖它。"""
    return _verify(request, session, await _csrf_candidate(request))


async def require_csrf_any(request: Request, session: Session = Depends(get_db)) -> CurrentUser:
    """校验会话与 CSRF,**不**检查邮箱验证(验证码提交与重发用)。"""
    session_data = current_session(request)
    if session_data is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    if not verify_csrf(session_data, await _csrf_candidate(request)):
        raise CsrfFailure()
    current = _load_current(session_data, session)
    if current is None:
        raise NotAuthenticated(next_url=str(request.url.path))
    return current


async def require_csrf_admin(
    request: Request, session: Session = Depends(get_db)
) -> CurrentUser:
    current = await require_csrf(request, session)
    if not current.is_admin:
        raise Forbidden("该功能仅家庭管理员可用")
    return current


def require_csrf_header(request: Request, session: Session = Depends(get_db)) -> CurrentUser:
    """只接受请求头的同步 CSRF 校验,用于 JSON 接口。"""
    return _verify(request, session, request.headers.get(CSRF_HEADER_NAME))


def require_csrf_header_admin(
    request: Request, session: Session = Depends(get_db)
) -> CurrentUser:
    current = require_csrf_header(request, session)
    if not current.is_admin:
        raise Forbidden("该功能仅家庭管理员可用")
    return current


def settings_dep() -> Settings:
    return get_settings()


UserDep = Annotated[CurrentUser, Depends(require_user)]
AnyUserDep = Annotated[CurrentUser, Depends(require_any_user)]
AdminDep = Annotated[CurrentUser, Depends(require_admin_user)]
CsrfDep = Annotated[CurrentUser, Depends(require_csrf)]
CsrfAnyDep = Annotated[CurrentUser, Depends(require_csrf_any)]
CsrfAdminDep = Annotated[CurrentUser, Depends(require_csrf_admin)]
CsrfHeaderDep = Annotated[CurrentUser, Depends(require_csrf_header)]
CsrfHeaderAdminDep = Annotated[CurrentUser, Depends(require_csrf_header_admin)]
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

    只保留令牌指纹与作用域,方便限流分桶与审计;令牌明文不进入日志或响应。
    `user_id` 决定新建事件的归属账号与权限,`calendar_id` 是令牌固定的落库日历
    (为空表示用该账号的个人日历)。
    """

    fingerprint: str
    user_id: str | None = None
    calendar_id: str | None = None

    @property
    def label(self) -> str:
        return f"agent:{self.fingerprint}"


def _bearer_from(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[len("bearer ") :].strip()
    return ""


def require_agent_token(
    request: Request, session: Session = Depends(get_db)
) -> AgentPrincipal:
    """校验 `Authorization: Bearer <token>`。

    令牌有两个来源,取并集:

    - `.env` 里的 `AGENT_API_TOKEN`(逗号分隔多个)——兜底/恢复通道,归属首位管理员;
    - 数据库里 `agent_tokens` 表里启用且哈希匹配的令牌(平台内管理,带账号与日历作用域)。

    两个来源都没有任何令牌时,整个 `/api/agent` 视为不存在,返回 404 而不是 401,
    避免向未授权方暴露「这里有一个需要凭据的接口」。
    """
    from sqlalchemy import select

    from app.accounts import owner_user
    from app.models import AgentToken

    settings = get_settings()
    env_tokens = settings.agent_tokens
    db_tokens = list(
        session.execute(
            select(AgentToken).where(AgentToken.enabled.is_(True))
        ).scalars()
    )
    if not env_tokens and not db_tokens:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "not_found", "message": "Not Found", "fields": None}},
        )

    provided = _bearer_from(request)
    if provided:
        digest = hashlib.sha256(provided.encode()).hexdigest()
        # env 里的令牌以明文存放,直接常量时间比对;数据库中只存哈希
        for token in env_tokens:
            if hmac.compare_digest(provided, token):
                owner = owner_user(session)
                return AgentPrincipal(
                    fingerprint=digest[:16],
                    user_id=owner.id if owner else None,
                    calendar_id=None,
                )
        for row in db_tokens:
            if hmac.compare_digest(digest, row.token_hash):
                # 历史令牌(升级前创建)没有归属账号:兜底绑到首位管理员,
                # 与启动引导的处理保持一致,不至于让旧令牌直接失效。
                user_id = row.user_id
                if user_id is None:
                    owner = owner_user(session)
                    user_id = owner.id if owner else None
                return AgentPrincipal(
                    fingerprint=digest[:16],
                    user_id=user_id,
                    calendar_id=row.calendar_id,
                )

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