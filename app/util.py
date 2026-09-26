"""通用小工具。"""

from __future__ import annotations


def mask_email(value: str | None) -> str:
    """日志与界面中脱敏显示邮箱。"""
    if not value or "@" not in value:
        return "***"
    local, _, domain = value.partition("@")
    if not local:
        return f"***@{domain}"
    head = local[0]
    return f"{head}{'*' * max(2, min(len(local) - 1, 6))}@{domain}"


def truncate(value: str | None, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def clean_optional_text(value: str | None, *, limit: int) -> str | None:
    """去掉首尾空白;空字符串归一为 None;超长时截断由调用方决定是否报错。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]
