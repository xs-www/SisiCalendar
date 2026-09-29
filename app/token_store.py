"""远程 Agent 接入令牌的生成与持久化管理。

安全约定(参照 AGENTS.md 第 6 条对 SMTP 口令的处理):

- 令牌明文只在创建时返回一次,之后只保留 sha256 哈希;
- 哈希进入 SQLite(进而进入备份),但明文不进库、不进日志、不回显;
- 与 `.env` 里的 `AGENT_API_TOKEN` 取并集鉴权,env 作为兜底/恢复通道,
  这样既兼容已有部署,也保证丢了数据库仍能用 .env 里的令牌应急。
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentToken
from app.settings_store import SettingValidationError
from app.timeutil import utcnow

# 前缀只是让人一眼认出「这是 SisiCalendar 的令牌」,不参与鉴权
TOKEN_PREFIX = "sisi_"
MAX_LABEL = 100


def generate_token() -> str:
    """生成一枚新的随机令牌明文(token_urlsafe(32) ≈ 43 字符熵)。"""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """令牌明文 → sha256 十六进制摘要。令牌是高熵随机串,无需加盐或慢哈希。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def fingerprint_of_hash(token_hash: str) -> str:
    """给界面与审计对账用的指纹 = 哈希前 16 位,与 `AgentPrincipal.fingerprint` 一致。"""
    return token_hash[:16]


def _validate_label(label: object) -> str:
    text = str(label or "").strip()
    if not text:
        raise SettingValidationError({"label": "令牌名称不能为空"})
    if len(text) > MAX_LABEL:
        raise SettingValidationError({"label": f"令牌名称最多 {MAX_LABEL} 个字符"})
    return text


def create_token(session: Session, label: object) -> tuple[str, AgentToken]:
    """生成一枚新令牌,返回 ``(明文, 记录)``。

    明文只在这里出现一次,调用方拿到后应立即交给用户(界面一次提示),
    不要落库、不要写日志。事务由调用方提交。
    """
    name = _validate_label(label)
    plaintext = generate_token()
    row = AgentToken(
        label=name,
        token_hash=hash_token(plaintext),
        enabled=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return plaintext, row


def list_tokens(session: Session) -> list[AgentToken]:
    """全部令牌(含已停用),按创建顺序返回,供管理界面渲染。

    同一秒内创建的多枚令牌仅靠 `created_at` 无法定序,再以 id 兜底,保证渲染稳定。
    """
    return list(
        session.execute(
            select(AgentToken).order_by(AgentToken.created_at.asc(), AgentToken.id.asc())
        ).scalars()
    )


def enabled_hashes(session: Session) -> list[str]:
    """所有启用令牌的哈希,供鉴权时做常量时间比对。"""
    return [
        digest
        for (digest,) in session.execute(
            select(AgentToken.token_hash).where(AgentToken.enabled.is_(True))
        )
    ]


def get_token(session: Session, token_id: str) -> AgentToken | None:
    return session.get(AgentToken, token_id)


def set_enabled(session: Session, token: AgentToken, enabled: bool) -> None:
    token.enabled = enabled
    token.updated_at = utcnow()
    session.flush()


def rename(session: Session, token: AgentToken, label: object) -> None:
    token.label = _validate_label(label)
    token.updated_at = utcnow()
    session.flush()


def delete(session: Session, token: AgentToken) -> None:
    session.delete(token)
    session.flush()


__all__ = [
    "MAX_LABEL",
    "TOKEN_PREFIX",
    "create_token",
    "delete",
    "enabled_hashes",
    "fingerprint_of_hash",
    "generate_token",
    "get_token",
    "hash_token",
    "list_tokens",
    "rename",
    "set_enabled",
]
