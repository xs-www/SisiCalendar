"""FastAPI 应用装配。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, urls
from app.config import Settings, get_settings
from app.db import session_scope
from app.deps import STATIC_DIR, CsrfFailure, EmailNotVerified, Forbidden, NotAuthenticated
from app.errors import error_payload, install_error_handlers
from app.routers import (
    api_agent,
    api_events,
    api_recipients,
    api_settings,
    api_sync,
    auth,
    feed,
    health,
    pages,
)
from app.security import ensure_admin_credential
from app.util import mask_email

logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

ADMIN_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    # FullCalendar 的图标字体(上/下月箭头)是内嵌的 base64 data URI。
    # 不显式声明的 font-src 会回退到 default-src 'self',把字体拦掉,
    # 按钮就会退化成方块(私有区码位没有字形)。
    "font-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _log_credentials(settings: Settings) -> None:
    generated = ensure_admin_credential(settings)
    if generated:
        logger.warning("=" * 64)
        logger.warning("首次启动已生成家庭管理员口令(只显示这一次)")
        logger.warning("用户名:%s", settings.admin_username)
        logger.warning("口令:%s", generated)
        logger.warning("请立即保存到密码管理器,或改用 ADMIN_PASSWORD_HASH 注入。")
        logger.warning("登录后请在「我的设置」里修改口令,并用邀请码添加家庭成员。")
        logger.warning("=" * 64)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.bootstrap import bootstrap_database, shutdown, startup_checks
    from app.scheduler import get_scheduler

    settings: Settings = app.state.settings
    _log_credentials(settings)
    bootstrap_database(settings)

    with session_scope() as session:
        startup_checks(session, settings)

    scheduler = None
    if settings.scheduler_enabled:
        scheduler = get_scheduler(settings)
        scheduler.start()
    else:
        logger.info("后台任务已按配置禁用")

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=True)
        shutdown()
        logger.info("应用已停止,数据目录:%s", settings.data_dir)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(resolved)
    resolved.ensure_directories()

    app = FastAPI(
        title="SisiCalendar",
        description="本地日程与订阅日历服务",
        version=__version__,
        lifespan=lifespan,
        # 管理界面使用严格 CSP,Swagger UI 需要外部 CDN,因此只暴露 OpenAPI 文档本身
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    app.state.settings = resolved

    install_error_handlers(app)
    _install_exception_pages(app)

    base_path = urls.public_base_path(resolved)
    static_files = StaticFiles(directory=str(STATIC_DIR))
    app.mount("/static", static_files, name="static")

    # 管理面(本机)与公开面(家庭成员)是同一套路由的两种入口:
    # 裸路径给本机 `http://localhost:443/admin`,带前缀的给 `PUBLIC_BASE_URL`。
    routers = (
        health.router,
        feed.router,
        auth.router,
        pages.router,
        api_events.router,
        api_agent.router,
        api_recipients.router,
        api_settings.router,
        api_sync.router,
    )
    for router in routers:
        app.include_router(router)
    if base_path:
        app.mount(f"{base_path}/static", static_files, name="static-public")
        # 公开前缀本身不带尾斜杠时也要直接回 404,别让 Starlette 先来一次 307
        app.add_api_route(
            base_path,
            pages.public_root,
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
        for router in routers:
            app.include_router(router, prefix=base_path, include_in_schema=False)
        logger.info("公开面挂载前缀:%s(基地址 %s)", base_path, urls.public_base_url(resolved))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        # 前缀必须在进入路由之前绑定:模板与重定向都靠它决定链接形状
        urls.bind_prefix(request.url.path, base_path)
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if urls.is_admin_path(request.url.path):
            response.headers.setdefault("Content-Security-Policy", ADMIN_CSP)
        return response

    return app


def _install_exception_pages(app: FastAPI) -> None:
    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(
        request: Request, exc: NotAuthenticated
    ) -> JSONResponse | RedirectResponse:
        if urls.is_api_path(request.url.path):
            return JSONResponse(
                error_payload("unauthorized", "需要登录后才能访问"),
                status_code=401,
            )
        target = exc.next_url or urls.url("/admin")
        login = urls.url("/login")
        return RedirectResponse(f"{login}?next={quote(target, safe='/')}", status_code=303)

    @app.exception_handler(EmailNotVerified)
    async def _email_not_verified(
        request: Request, exc: EmailNotVerified
    ) -> JSONResponse | RedirectResponse:
        """邮箱没验证:页面送到验证页,接口返回 403(账号已登录,只是还不能用)。"""
        if urls.is_api_path(request.url.path):
            return JSONResponse(
                error_payload(
                    "email_not_verified",
                    "邮箱还没有验证,请先到「邮箱验证」页面输入收到的验证码",
                ),
                status_code=403,
            )
        return RedirectResponse(urls.url("/verify-email"), status_code=303)

    @app.exception_handler(CsrfFailure)
    async def _csrf_failure(request: Request, exc: CsrfFailure) -> JSONResponse | HTMLResponse:
        if urls.is_api_path(request.url.path):
            return JSONResponse(
                error_payload("csrf_failed", "CSRF 令牌缺失或无效,请刷新页面后重试"),
                status_code=403,
            )
        return HTMLResponse(
            "<h1>403</h1><p>CSRF 令牌缺失或无效,请返回并刷新页面后重试。</p>",
            status_code=403,
        )

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden) -> JSONResponse | HTMLResponse:
        if urls.is_api_path(request.url.path):
            return JSONResponse(
                error_payload("forbidden", exc.message),
                status_code=403,
            )
        return HTMLResponse(
            f"<h1>403</h1><p>{exc.message}。请返回日历页,或换一个有权限的账号。</p>",
            status_code=403,
        )


app = create_app()


def _masked_settings(settings: Settings) -> dict[str, object]:
    """给日志用的配置摘要,绝不包含口令。"""
    return {
        "data_dir": str(settings.data_dir),
        "default_timezone": settings.default_timezone,
        "smtp_host": settings.smtp_host or "",
        "smtp_from": mask_email(settings.smtp_from_address),
        "mail_configured": settings.mail_configured,
    }


def static_dir() -> Path:
    return STATIC_DIR
