"""应用设置 API(非敏感配置)。

SMTP 口令只从环境注入,这里永远只返回“是否已配置”,不回显任何凭据。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AdminDep, CsrfHeaderDep
from app.errors import error_payload
from app.schemas import SettingsUpdate
from app.settings_store import (
    SettingValidationError,
    effective_settings,
    load_overrides,
    save_overrides,
)
from app.timezones import timezone_choices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["settings"])


def _serialize(session: Session) -> dict[str, object]:
    settings = effective_settings(session)
    return {
        "calendar_name": settings.calendar_name,
        "default_timezone": settings.default_timezone,
        "default_event_duration_minutes": settings.default_event_duration_minutes,
        "mail_max_attempts": settings.mail_max_attempts,
        "mail_backoff_minutes": settings.mail_backoff_minutes,
        "subscription_url": settings.subscription_url,
        "feed_cache_seconds": settings.feed_cache_seconds,
        "mail_configured": settings.mail_configured,
        # 常用时区清单:event 接口的 timezone 可以直接填这里的 index
        "timezone_choices": timezone_choices(),
        "overrides": load_overrides(session),
        "smtp": {
            "host": settings.smtp_host or "",
            "port": settings.smtp_port,
            "security": settings.smtp_security,
            "username": settings.smtp_username or "",
            "from_address": settings.smtp_from_address or "",
            "from_name": settings.smtp_from_name,
            # 只暴露“是否已注入”,绝不返回口令本身
            "password_configured": bool(settings.smtp_password),
        },
    }


@router.get("/settings", summary="读取应用设置")
def read_settings(admin: AdminDep, session: Session = Depends(get_db)) -> dict[str, object]:
    return _serialize(session)


@router.patch("/settings", summary="修改应用设置")
def update_settings(
    payload: SettingsUpdate,
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    values = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not values:
        return _serialize(session)
    try:
        save_overrides(session, values)
        session.commit()
    except SettingValidationError as exc:
        session.rollback()
        raise HTTPException(
            422,
            error_payload("validation_error", "设置校验失败", exc.fields),
        ) from exc
    logger.info("设置已更新:%s", ", ".join(sorted(values)))
    return _serialize(session)
