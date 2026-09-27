"""时间工具。

约定:数据库中所有绝对时间都以**不带时区的 UTC** 存储,避免 SQLite 在不同
偏移量字符串之间做字典序比较时出错;展示和 ICS 生成时再按事件时区还原。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_FALLBACK_TZ = "UTC"

# 日期里的中文与斜杠分隔符,统一成 ISO 的短横线写法
_DATE_SEPARATORS = (("年", "-"), ("月", "-"), ("日", " "))
# 2026-11-8 → 2026-11-08;9:5 → 09:05(fromisoformat 要求补零)
_DATE_PAD = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_TIME_PAD = re.compile(r"(?<!\d)(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?(?!\d)")


def _pad_date(match: re.Match[str]) -> str:
    year, month, day = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def _pad_time(match: re.Match[str]) -> str:
    hour, minute, second = match.groups()
    padded = f"{int(hour):02d}:{int(minute):02d}"
    if second is not None:
        padded += f":{int(second):02d}"
    return padded


def normalize_datetime_text(value: str) -> str:
    """把常见的手写时间格式整理成 `datetime.fromisoformat` 能接受的形式。

    支持:斜杠日期(2026/11/03)、中文日期(2026年11月8日)、未补零的月日与时刻
    (2026-11-8 9:5)、空格分隔的日期与时间、结尾的 Z。
    这些写法在 iOS 快捷指令、表单和人工输入里都很常见。
    """
    text = value.strip()
    if not text:
        return ""
    for source, target in _DATE_SEPARATORS:
        text = text.replace(source, target)
    text = re.sub(r"(?<=\d)/(?=\d)", "-", text)
    # “2026年11月8日 14:00” 替换后会出现连续空格,收敛成单个
    text = re.sub(r"\s+", " ", text).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _DATE_PAD.sub(_pad_date, text)
    return _TIME_PAD.sub(_pad_time, text)


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
    text = normalize_datetime_text(value)
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    return as_utc(parsed)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = normalize_datetime_text(value)
    if not text:
        return None
    # 允许“2026-11-03 14:00”这类带时间的输入,只取日期部分
    return date.fromisoformat(text.split(" ")[0].split("T")[0])


def add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)
