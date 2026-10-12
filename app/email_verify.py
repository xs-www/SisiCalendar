"""邮箱验证:6 位验证码 + 验证通过后的固定订阅地址邮件。

流程(见 `AGENTS.md` 不变量 22):

1. 注册必须填邮箱;提交成功后立刻发一封验证码邮件,账号进入**待验证**状态;
2. 待验证的成员**不能使用系统**:任何需要登录的页面/接口都会被送到 `/verify-email`
   (管理员豁免——本机救急时不能把管理员自己锁在门外);
3. 验证码 6 位数字,有效期默认 30 分钟,最多试 5 次,重发冷却 60 秒;
4. 验证通过 → 置 `email_verified_at`,`sync_user_recipient()` 才会为它建账号收件人,
   并**立刻发一封信告知该账号固定的订阅地址**。

安全要点:

- 库里只存验证码的 **HMAC**(键是服务端密钥,见 `load_or_create_session_secret`),
  不存明文;日志、审计里也不出现验证码或明文地址;
- 每次重发都换新码并把尝试次数清零,旧码立即失效;
- 验证码与邮箱绑定:改了邮箱就重新发、重新验。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import urls
from app.config import Settings
from app.mailer import (
    MailError,
    config_from_settings,
    send_subscription_address,
    send_verification_code,
)
from app.models import User
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

CODE_LENGTH = 6
CODE_DIGITS = "0123456789"


def generate_code() -> str:
    """6 位数字验证码(允许前导零,一律按字符串比较)。"""
    return "".join(secrets.choice(CODE_DIGITS) for _ in range(CODE_LENGTH))


def code_hash(settings: Settings, *, user_id: str, code: str) -> str:
    """验证码的 HMAC:即使数据库泄漏,没有服务端密钥也无法反推(验证码空间很小)。"""
    secret = settings.load_or_create_session_secret().encode("utf-8")
    payload = f"verify:{user_id}:{code.strip()}".encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def needs_email_verification(user: User) -> bool:
    """这个账号是否被验证挡住。

    规则:**有邮箱且未验证**的普通成员被挡住;管理员豁免(本机救急入口);
    没有邮箱的账号不挡(升级上来的老账号可能没填),由界面持续提醒补填。
    """
    if user.is_admin:
        return False
    if not (user.email or "").strip():
        return False
    return user.email_verified_at is None


def is_verified(user: User) -> bool:
    return user.email_verified_at is not None


def mark_unverified(session: Session, user: User) -> None:
    """邮箱变更后重新进入待验证状态(旧验证码一并作废)。"""
    user.email_verified_at = None
    user.email_verify_code_hash = None
    user.email_verify_expires_at = None
    user.email_verify_attempts = 0
    user.email_verify_sent_at = None
    user.updated_at = utcnow()
    session.flush()


def issue_code(
    session: Session, settings: Settings, user: User, *, now: datetime | None = None
) -> str:
    """生成并登记一枚新验证码,返回明文(只用于发信,绝不记日志)。

    无论发信是否成功,验证码都已登记;发信失败由调用方提示「重新发送」。
    """
    moment = now or utcnow()
    code = generate_code()
    user.email_verify_code_hash = code_hash(settings, user_id=user.id, code=code)
    user.email_verify_expires_at = moment + timedelta(
        seconds=max(60, settings.email_code_ttl_seconds)
    )
    user.email_verify_attempts = 0
    user.email_verify_sent_at = moment
    user.updated_at = moment
    session.flush()
    return code


def resend_wait_seconds(
    user: User, settings: Settings, *, now: datetime | None = None
) -> int:
    """距离可以再次发送还差多少秒(0 = 现在就能发)。"""
    sent_at = user.email_verify_sent_at
    if sent_at is None:
        return 0
    cooldown = max(0, settings.email_code_resend_seconds)
    if cooldown == 0:
        return 0
    elapsed = ((now or utcnow()) - sent_at).total_seconds()
    return max(0, int(cooldown - elapsed))


def check_code(
    session: Session,
    settings: Settings,
    user: User,
    code: str,
    *,
    now: datetime | None = None,
) -> str:
    """校验验证码,返回 `ok` / `missing` / `expired` / `mismatch` / `too_many`。

    校验通过时置 `email_verified_at`,并保留尝试次数供审计;失败时递增尝试次数,
    超过上限直接作废(必须重新发送)。
    """
    moment = now or utcnow()
    stored = user.email_verify_code_hash
    expires_at = user.email_verify_expires_at
    if not stored or expires_at is None:
        return "missing"
    if expires_at <= moment:
        return "expired"
    attempts = int(user.email_verify_attempts or 0)
    if attempts >= max(1, settings.email_code_max_attempts):
        return "too_many"

    candidate = code_hash(settings, user_id=user.id, code=code or "")
    if not hmac.compare_digest(candidate, stored):
        user.email_verify_attempts = attempts + 1
        user.updated_at = moment
        if user.email_verify_attempts >= max(1, settings.email_code_max_attempts):
            # 次数用完:直接作废,防止继续暴力尝试
            user.email_verify_code_hash = None
            user.email_verify_expires_at = None
        session.flush()
        return "mismatch"

    user.email_verified_at = moment
    user.email_verify_code_hash = None
    user.email_verify_expires_at = None
    user.email_verify_attempts = 0
    user.updated_at = moment
    session.flush()
    return "ok"


def send_code_mail(settings: Settings, user: User, code: str) -> bool:
    """发验证码邮件。返回是否发出;失败只记日志(不抛给调用方)。"""
    config = config_from_settings(settings)
    address = (user.email or "").strip()
    if config is None or not address:
        logger.warning("SMTP 未配置或账号没有邮箱,跳过验证码邮件")
        return False
    try:
        send_verification_code(
            config,
            to_address=address,
            to_name=user.label,
            code=code,
            calendar_name=settings.calendar_name,
            ttl_minutes=max(1, settings.email_code_ttl_seconds // 60),
            public_base_url=urls.public_base_url(settings),
        )
    except MailError as exc:
        logger.warning("验证码邮件发送失败(%s),可稍后重发", type(exc).__name__)
        return False
    return True


def send_subscription_mail(settings: Settings, user: User, *, subscription_url: str) -> bool:
    """把固定订阅地址发给本人。返回是否发出。"""
    config = config_from_settings(settings)
    address = (user.email or "").strip()
    if config is None or not address:
        return False
    try:
        send_subscription_address(
            config,
            to_address=address,
            to_name=user.label,
            subscription_url=subscription_url,
            calendar_name=settings.calendar_name,
        )
    except MailError as exc:
        logger.warning("订阅地址邮件发送失败(%s)", type(exc).__name__)
        return False
    return True


__all__ = [
    "CODE_LENGTH",
    "check_code",
    "code_hash",
    "generate_code",
    "is_verified",
    "issue_code",
    "mark_unverified",
    "needs_email_verification",
    "resend_wait_seconds",
    "send_code_mail",
    "send_subscription_mail",
]