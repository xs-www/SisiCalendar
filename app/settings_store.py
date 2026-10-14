"""应用级非敏感配置的读写。

环境变量给出默认值,数据库中的 `app_settings` 允许在界面上覆盖少量可调项。
SMTP 口令一类敏感信息永远只来自环境,不进入这里的任何键。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import urls
from app.config import Settings, get_settings
from app.models import AppSetting
from app.timeutil import is_valid_timezone
from app.timezones import resolve_timezone, timezone_help

logger = logging.getLogger(__name__)

#: 允许在界面覆盖的键 -> 对应的 Settings 字段
OVERRIDABLE_KEYS = {
    "calendar_name": "calendar_name",
    "default_timezone": "default_timezone",
    "default_event_duration_minutes": "default_event_duration_minutes",
    "mail_max_attempts": "mail_max_attempts",
    "mail_backoff_minutes": "mail_backoff_minutes",
    "public_base_url": "public_base_url",
}

#: 对外基地址:库里**只存 origin**(`https://节点[:端口]`),完整地址由它 +
#: 环境变量给出的挂载前缀拼出来。理由:路径前缀是启动时挂载路由用的
#: (`app/main.py`),运行时改它会让链接与路由错位,所以界面只能改域名/端口。
PUBLIC_BASE_URL_KEY = "public_base_url"

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


def normalize_public_base_origin(raw: object, settings: Settings) -> str:
    """把「对外地址」输入规范成 origin;不合法时抛 `SettingValidationError`。

    只接受 `<scheme>://<host>[:port]`,允许用户直接粘贴完整基地址(路径部分必须与
    当前挂载前缀一致,否则报错并说明「路径前缀只能改环境变量」)。返回 `https://主机[:端口]`,
    不带路径、不带尾斜杠。
    """
    text = str(raw or "").strip()
    if not text:
        raise SettingValidationError({PUBLIC_BASE_URL_KEY: "对外地址不能为空"})
    candidate = text if "://" in text else f"https://{text}"
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        raise SettingValidationError(
            {PUBLIC_BASE_URL_KEY: "端口不是一个数字(形如 `https://主机:18443`)"}
        ) from None

    if parts.scheme not in {"http", "https"}:
        raise SettingValidationError(
            {PUBLIC_BASE_URL_KEY: "只支持 http 或 https 地址"}
        )
    host = parts.hostname
    if not host:
        raise SettingValidationError(
            {
                PUBLIC_BASE_URL_KEY: (
                    "请填写主机名,例如 `sisi-laptop.tail921448.ts.net`"
                    "(Tailscale 里 `tailscale status` 能看到)"
                )
            }
        )
    if parts.username or parts.password:
        raise SettingValidationError(
            {PUBLIC_BASE_URL_KEY: "地址里不要带用户名或口令"}
        )
    if parts.query or parts.fragment:
        raise SettingValidationError(
            {PUBLIC_BASE_URL_KEY: "地址里不要带 `?` 或 `#` 后面的内容"}
        )

    base_path = urls.public_base_path(settings)
    supplied_path = urls.normalize_prefix(parts.path)
    # 只填域名(或只写到 `/`)时接受:路径前缀由我们按环境变量补上;
    # 但只要写了路径,就必须与挂载前缀逐字一致 —— 这里绝不能悄悄改挂载点。
    if supplied_path and supplied_path != base_path:
        raise SettingValidationError(
            {
                PUBLIC_BASE_URL_KEY: (
                    f"路径部分只能是 `{base_path or '(无)'}`,现在填的是 "
                    f"`{parts.path}`。路径前缀决定了公开面的挂载点,"
                    "只能改环境变量 `PUBLIC_BASE_URL` 并重建容器,界面里改不了。"
                )
            }
        )

    normalized_host = f"[{host}]" if ":" in host else host.lower()
    origin = f"{parts.scheme}://{normalized_host}"
    if port:
        origin += f":{port}"
    return origin


def compose_public_base_url(origin: str, settings: Settings) -> str | None:
    """`origin + 当前挂载前缀` → 完整基地址;origin 不合法/为空时返回 `None`。

    返回 `None` 表示「这次覆盖值不可用」,调用方应退回环境配置。
    """
    text = str(origin or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username or parts.password:
            return None
    except ValueError:  # pragma: no cover - 端口异常
        return None
    return f"{parts.scheme}://{parts.netloc}{urls.public_base_path(settings)}"


def validate_overrides(
    values: dict[str, Any], settings: Settings | None = None
) -> dict[str, str]:
    """校验并规范化待写入的覆盖项。"""
    errors: dict[str, str] = {}
    cleaned: dict[str, str] = {}

    for key, raw in values.items():
        if key not in OVERRIDABLE_KEYS or raw is None:
            continue
        text = str(raw).strip()
        if key == PUBLIC_BASE_URL_KEY:
            if not text:
                # 空值 = 撤销覆盖,回到环境变量(写库由 save_overrides 处理)
                cleaned[key] = ""
                continue
            try:
                cleaned[key] = normalize_public_base_origin(text, settings or get_settings())
            except SettingValidationError as exc:
                errors[key] = exc.fields[key]
            continue
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


def save_overrides(
    session: Session, values: dict[str, Any], settings: Settings | None = None
) -> dict[str, str]:
    cleaned = validate_overrides(values, settings)
    for key, value in cleaned.items():
        row = session.get(AppSetting, key)
        if not value:
            # 空值 = 撤销这一项覆盖,回到环境变量
            if row is not None:
                session.delete(row)
            continue
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
        if key == PUBLIC_BASE_URL_KEY:
            # 库里只有 origin;完整地址 = origin + 环境变量给的挂载前缀
            composed = compose_public_base_url(raw, resolved)
            if composed:
                updates[field] = composed
            continue
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
