"""时间工具。

约定:数据库中所有绝对时间都以**不带时区的 UTC** 存储,避免 SQLite 在不同
偏移量字符串之间做字典序比较时出错;展示和 ICS 生成时再按事件时区还原。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_FALLBACK_TZ = "UTC"


def utcnow() -> datetime:
    """当前 UTC 时间(naive)。"""
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc(value: datetime | None) -> datetime | None:
    """把带时区的时间转换成 naive UTC;naive 输入按 UTC 解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(UTC).replace(tzinfo=None, microsecond=0)


def as_aware_utc(value: datetime | None) -> datetime | None:
    """把 naive UTC 还原成带 UTC 时区的时间对象。"""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def get_zone(name: str | None) -> ZoneInfo:
    """按 IANA 名称取时区,失败时回退到 UTC。"""
    if not name:
        return ZoneInfo(DEFAULT_FALLBACK_TZ)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_FALLBACK_TZ)


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def local_to_utc(local_dt: datetime, tz_name: str) -> datetime:
    """把某个时区的本地时间转换为 naive UTC。"""
    zone = get_zone(tz_name)
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=zone)
    else:
        local_dt = local_dt.astimezone(zone)
    return local_dt.astimezone(UTC).replace(tzinfo=None, microsecond=0)


def utc_to_local(utc_dt: datetime, tz_name: str) -> datetime:
    """把 naive UTC 转换为某个时区的本地时间。"""
    zone = get_zone(tz_name)
    return utc_dt.replace(tzinfo=UTC).astimezone(zone)


def iso_z(value: datetime | None) -> str | None:
    """输出带 Z 后缀的 UTC ISO 字符串,供 ICS 与 API 使用。"""
    if value is None:
        return None
    aware = as_aware_utc(value)
    assert aware is not None
    return aware.strftime("%Y%m%dT%H%M%SZ")


def iso_utc(value: datetime | None) -> str | None:
    """输出 RFC 3339 风格的 UTC 字符串,供 JSON 响应使用。"""
    if value is None:
        return None
    aware = as_aware_utc(value)
    assert aware is not None
    return aware.isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    """解析 API 传入的 ISO 8601 时间,返回 naive UTC。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return as_utc(parsed)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value.strip())


def add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)
