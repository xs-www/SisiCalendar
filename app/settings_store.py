"""应用级非敏感配置的读写。

环境变量给出默认值,数据库中的 `app_settings` 允许在界面上覆盖少量可调项。
SMTP 口令一类敏感信息永远只来自环境,不进入这里的任何键。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AppSetting
from app.timeutil import is_valid_timezone
from app.timezones import resolve_timezone, timezone_help

logger = logging.getLogger(__name__)

# 允许在界面覆盖的键 -> 对应的 Settings 字段
OVERRIDABLE_KEYS = {
    "calendar_name": "calendar_name",
    "default_timezone": "default_timezone",
    "default_event_duration_minutes": "default_event_duration_minutes",
    "mail_max_attempts": "mail_max_attempts",
    "mail_backoff_minutes": "mail_backoff_minutes",
}

INT_KEYS = {"default_event_duration_minutes", "mail_max_attempts"}


class SettingValidationError(ValueError):
    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {value}" for key, value in fields.items()))
        self.fields = fields


def load_overrides(session: Session) -> dict[str, str]:
    rows = session.execute(select(AppSetting)).scalars()
    return {
        row.key: (row.value or "")
        for row in rows
        if row.key in OVERRIDABLE_KEYS and row.value is not None
    }


def validate_overrides(values: dict[str, Any]) -> dict[str, str]:
    """校验并规范化待写入的覆盖项。"""
    errors: dict[str, str] = {}
    cleaned: dict[str, str] = {}

    for key, raw in values.items():
        if key not in OVERRIDABLE_KEYS or raw is None:
            continue
        text = str(raw).strip()
        if key == "calendar_name":
            if not text:
                errors[key] = "日历名称不能为空"
            elif len(text) > 100:
                errors[key] = "日历名称最多 100 个字符"
            else:
                cleaned[key] = text
        elif key == "default_timezone":
            resolved_zone = resolve_timezone(text)
            if resolved_zone is None:
                errors[key] = timezone_help(text)
            else:
                cleaned[key] = resolved_zone
        elif key in INT_KEYS:
            try:
                number = int(text)
            except ValueError:
                errors[key] = "必须是整数"
                continue
            if key == "default_event_duration_minutes" and not (1 <= number <= 24 * 60):
                errors[key] = "默认时长必须在 1 到 1440 分钟之间"
            elif key == "mail_max_attempts" and not (1 <= number <= 20):
                errors[key] = "最大重试次数必须在 1 到 20 之间"
            else:
                cleaned[key] = str(number)
        elif key == "mail_backoff_minutes":
            parts = [chunk.strip() for chunk in text.split(",") if chunk.strip()]
            if not parts:
                errors[key] = "退避间隔不能为空"
                continue
            try:
                minutes = [int(part) for part in parts]
            except ValueError:
                errors[key] = "退避间隔必须是逗号分隔的分钟数"
                continue
            if any(value < 0 or value > 10080 for value in minutes):
                errors[key] = "退避间隔必须在 0 到 10080 分钟之间"
            else:
                cleaned[key] = ",".join(str(value) for value in minutes)

    if errors:
        raise SettingValidationError(errors)
    return cleaned


def save_overrides(session: Session, values: dict[str, Any]) -> dict[str, str]:
    cleaned = validate_overrides(values)
    for key, value in cleaned.items():
        row = session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    session.flush()
    return cleaned


def effective_settings(session: Session, base: Settings | None = None) -> Settings:
    """把数据库覆盖项叠加到环境配置上。"""
    resolved = base or get_settings()
    try:
        overrides = load_overrides(session)
    except Exception:  # noqa: BLE001 - 数据库不可用时退回环境配置
        logger.debug("读取配置覆盖失败,使用环境配置")
        return resolved
    if not overrides:
        return resolved

    updates: dict[str, Any] = {}
    for key, raw in overrides.items():
        field = OVERRIDABLE_KEYS[key]
        if key in INT_KEYS:
            try:
                updates[field] = int(raw)
            except ValueError:
                continue
        else:
            updates[field] = raw
    if not updates:
        return resolved
    if updates.get("default_timezone") and not is_valid_timezone(str(updates["default_timezone"])):
        updates.pop("default_timezone", None)
    return resolved.model_copy(update=updates)
