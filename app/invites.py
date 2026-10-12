"""邀请码:家庭成员自助注册的唯一入口。

安全基调与远程令牌一致:**明文只显示一次,库里只存 `sha256`**。

一张邀请码可以限制使用次数与有效期。注册成功后即时消耗一次计数,用完即失效;
管理员可随时停用或撤销。注册接口另有基于来源地址的限流(见 `app/routers/auth.py`),
避免有人拿一枚有效邀请码去暴力猜用户名。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import AccountError, create_user
from app.config import Settings
from app.models import InviteCode, User
from app.settings_store import SettingValidationError
from app.timeutil import utcnow
from app.token_store import generate_token, hash_token

INVITE_TOKEN_PREFIX = "sisi_invite_"
MAX_LABEL = 100
MAX_USES = 20
#: 邀请链接里的参数名,注册页与页面提示都用它
INVITE_PARAM = "code"


def _validate_label(label: object) -> str:
    text = str(label or "").strip()
    if not text:
        raise SettingValidationError({"label": "邀请码名称不能为空"})
    if len(text) > MAX_LABEL:
        raise SettingValidationError({"label": f"邀请码名称最多 {MAX_LABEL} 个字符"})
    return text


def create_invite(
    session: Session,
    *,
    label: object,
    max_uses: int = 1,
    expires_at: datetime | None = None,
    created_by_user_id: str | None = None,
) -> tuple[str, InviteCode]:
    """生成一张邀请码,返回 ``(明文, 记录)``。明文只出现这一次。"""
    name = _validate_label(label)
    try:
        uses = int(max_uses)
    except (TypeError, ValueError) as exc:
        raise SettingValidationError({"max_uses": "可用次数必须是整数"}) from exc
    if not 1 <= uses <= MAX_USES:
        raise SettingValidationError({"max_uses": f"可用次数必须在 1 到 {MAX_USES} 之间"})

    plaintext = generate_token(prefix=INVITE_TOKEN_PREFIX)
    row = InviteCode(
        code_hash=hash_token(plaintext),
        label=name,
        max_uses=uses,
        used_count=0,
        expires_at=expires_at,
        enabled=True,
        created_by_user_id=created_by_user_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return plaintext, row


def list_invites(session: Session) -> list[InviteCode]:
    return list(
        session.execute(
            select(InviteCode).order_by(InviteCode.created_at.asc(), InviteCode.id.asc())
        ).scalars()
    )


def get_invite(session: Session, invite_id: str) -> InviteCode | None:
    return session.get(InviteCode, invite_id)


def find_by_code(session: Session, code: str) -> InviteCode | None:
    text = (code or "").strip()
    if not text:
        return None
    digest = hash_token(text)
    return session.execute(
        select(InviteCode).where(InviteCode.code_hash == digest)
    ).scalar_one_or_none()


def unusable_reason(invite: InviteCode | None, *, now: datetime | None = None) -> str | None:
    """返回不可用原因;可用时返回 None。文案直接展示给注册者。

    先判「次数用完」再判「已停用」:用完的邀请码会被自动停用,这时告诉对方
    「次数已用完」比「已停用」更有指导意义。
    """
    if invite is None:
        return "邀请码无效,请向家庭管理员确认。"
    if invite.remaining_uses <= 0:
        return "该邀请码的使用次数已用完,请向家庭管理员索取新的邀请码。"
    if not invite.enabled:
        return "该邀请码已被停用,请向家庭管理员索取新的邀请码。"
    moment = now or utcnow()
    if invite.expires_at is not None and invite.expires_at <= moment:
        return "该邀请码已过期,请向家庭管理员索取新的邀请码。"
    return None


def consume(session: Session, invite: InviteCode) -> None:
    invite.used_count = int(invite.used_count or 0) + 1
    if invite.remaining_uses <= 0:
        invite.enabled = False
    invite.updated_at = utcnow()
    session.flush()


def set_enabled(session: Session, invite: InviteCode, enabled: bool) -> None:
    invite.enabled = enabled
    invite.updated_at = utcnow()
    session.flush()


def delete(session: Session, invite: InviteCode) -> None:
    session.delete(invite)
    session.flush()


def register_with_invite(
    session: Session,
    *,
    code: str,
    username: object,
    password: object,
    display_name: object = None,
    email: object = None,
    settings: Settings | None = None,
) -> User:
    """用邀请码注册成员账号并消耗一次使用次数。

    失败时抛出 `AccountError`(字段错误)或 `ValueError`(邀请码不可用,文案可直接展示)。
    调用方负责提交事务。
    """
    invite = find_by_code(session, code)
    reason = unusable_reason(invite)
    if reason is not None:
        raise ValueError(reason)
    assert invite is not None  # unusable_reason 已排除 None

    try:
        user = create_user(
            session,
            username=username,
            password=password,
            display_name=display_name,
            email=email,
            role="member",
            settings=settings,
        )
    except AccountError:
        raise
    consume(session, invite)
    return user


__all__ = [
    "INVITE_PARAM",
    "INVITE_TOKEN_PREFIX",
    "MAX_USES",
    "consume",
    "create_invite",
    "delete",
    "find_by_code",
    "get_invite",
    "list_invites",
    "register_with_invite",
    "set_enabled",
    "unusable_reason",
]