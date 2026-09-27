"""FastAPI 应用装配。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Settings, get_settings
from app.db import session_scope
from app.deps import STATIC_DIR, CsrfFailure, NotAuthenticated
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
        logger.warning("首次启动已生成管理员口令(只显示这一次)")
        logger.warning("用户名:%s", settings.admin_username)
        logger.warning("口令:%s", generated)
        logger.warning("请立即保存到密码管理器,或改用 ADMIN_PASSWORD_HASH 注入。")
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

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(feed.router)
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(api_events.router)
    app.include_router(api_agent.router)
    app.include_router(api_recipients.router)
    app.include_router(api_settings.router)
    app.include_router(api_sync.router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if request.url.path.startswith(("/admin", "/login")):
            response.headers.setdefault("Content-Security-Policy", ADMIN_CSP)
        return response

    return app


def _install_exception_pages(app: FastAPI) -> None:
    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(
        request: Request, exc: NotAuthenticated
    ) -> JSONResponse | RedirectResponse:
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                error_payload("unauthorized", "需要登录后才能访问"),
                status_code=401,
            )
        target = exc.next_url or "/admin"
        return RedirectResponse(f"/login?next={target}", status_code=303)

    @app.exception_handler(CsrfFailure)
    async def _csrf_failure(request: Request, exc: CsrfFailure) -> JSONResponse | HTMLResponse:
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                error_payload("csrf_failed", "CSRF 令牌缺失或无效,请刷新页面后重试"),
                status_code=403,
            )
        return HTMLResponse(
            "<h1>403</h1><p>CSRF 令牌缺失或无效,请返回并刷新页面后重试。</p>",
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
