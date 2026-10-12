"""只读订阅令牌:每人一条**固定**订阅地址。

设计要点:

- 每个账号在**创建时**自动得到一条固定地址,之后永不改变:
  `token = HMAC(服务端密钥, "feed:<账号id>")`,因此服务端随时可以重新算出它并再次告知
  本人(界面直接展示、验证通过后邮件告知),而库里仍然只保存 `sha256`——
  数据库或备份单独泄漏并不能还原出地址(还需要 `data/session_secret`)。
  代价:轮换服务端密钥会让所有固定地址失效(需要重新告知)。
- 其余令牌仍是随机生成、明文只显示一次(`create_feed_token`),供「换手机」「链接外泄」
  时临时发一条,可以单独停用/撤销;
- `user_id` 有值 → 该成员可见范围(个人 + 共享)的合并日历;
  `user_id` 为 `NULL` → 家庭共享日历范围,专门给没有账号的外部订阅者;
- 升级前的无令牌地址 `/calendar` 与 `/calendar.ics` 已取消,见 `app/routers/feed.py`。
"""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import urls
from app.config import Settings
from app.models import FAMILY_FEED_LABEL, FeedToken, User
from app.settings_store import SettingValidationError
from app.timeutil import utcnow
from app.token_store import fingerprint_of_hash, generate_token, hash_token

#: 前缀只为人眼辨识,不参与鉴权
FEED_TOKEN_PREFIX = "sisi_feed_"
MAX_LABEL = 100
#: 账号固定地址的记录名,界面与文档都按它识别
ACCOUNT_FEED_LABEL = "账号订阅地址(固定)"
#: 派生令牌的长度(hex 字符数),40 个 hex = 160 bit,足够抵抗穷举
ACCOUNT_TOKEN_HEX = 40


def account_feed_token(settings: Settings, user_id: str) -> str:
    """账号的**固定**订阅令牌明文。

    由服务端密钥与账号 ID 派生(不是随机数),因此同一个账号永远得到同一条地址:
    界面随时能展示、验证通过后能邮件告知,而库里只存 `sha256`。服务端密钥轮换会让
    所有固定地址失效——这是有意的取舍,它同时是唯一能还原地址的输入。
    """
    secret = settings.load_or_create_session_secret().encode("utf-8")
    digest = hmac.new(
        secret, f"feed:{user_id}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{FEED_TOKEN_PREFIX}{digest[:ACCOUNT_TOKEN_HEX]}"


def account_feed_hash(settings: Settings, user_id: str) -> str:
    return hash_token(account_feed_token(settings, user_id))


def fixed_account_token(
    session: Session, settings: Settings, *, user_id: str
) -> FeedToken | None:
    """找出账号那条固定地址的记录(不新建)。"""
    digest = account_feed_hash(settings, user_id)
    for row in list_feed_tokens(session, user_id=user_id):
        if hmac.compare_digest(row.token_hash, digest):
            return row
    return None


def ensure_account_feed_token(
    session: Session, settings: Settings, user: User
) -> FeedToken:
    """保证账号有一条固定订阅地址,返回它的记录。幂等:已有就原样返回。"""
    existing = fixed_account_token(session, settings, user_id=user.id)
    if existing is not None:
        if not existing.enabled:
            existing.enabled = True
        if existing.label != ACCOUNT_FEED_LABEL:
            existing.label = ACCOUNT_FEED_LABEL
        existing.updated_at = utcnow()
        session.flush()
        return existing

    row = FeedToken(
        user_id=user.id,
        label=ACCOUNT_FEED_LABEL,
        token_hash=account_feed_hash(settings, user.id),
        enabled=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def is_fixed_account_token(session: Session, settings: Settings, token: FeedToken) -> bool:
    """这条记录是不是账号的固定地址(界面据此隐藏停用/撤销)。"""
    if not token.user_id:
        return False
    return hmac.compare_digest(token.token_hash, account_feed_hash(settings, token.user_id))


def account_subscription_url(session: Session, settings: Settings, user: User) -> str:
    """账号的固定订阅地址(绝对地址,可直接粘贴到 iPhone)。"""
    return urls.subscription_url(settings, account_feed_token(settings, user.id))


def _validate_label(label: object) -> str:
    text = str(label or "").strip()
    if not text:
        raise SettingValidationError({"label": "令牌名称不能为空"})
    if len(text) > MAX_LABEL:
        raise SettingValidationError({"label": f"令牌名称最多 {MAX_LABEL} 个字符"})
    return text


def create_feed_token(
    session: Session, *, user_id: str | None, label: object
) -> tuple[str, FeedToken]:
    """生成一枚订阅令牌,返回 ``(明文, 记录)``。明文只出现这一次。"""
    name = _validate_label(label)
    plaintext = generate_token(prefix=FEED_TOKEN_PREFIX)
    row = FeedToken(
        user_id=user_id or None,
        label=name,
        token_hash=hash_token(plaintext),
        enabled=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return plaintext, row


def list_feed_tokens(session: Session, *, user_id: str | None) -> list[FeedToken]:
    """某个范围的令牌列表。`user_id=None` 查的是家庭共享范围的令牌。"""
    stmt = select(FeedToken)
    if user_id:
        stmt = stmt.where(FeedToken.user_id == user_id)
    else:
        stmt = stmt.where(FeedToken.user_id.is_(None))
    ordered = stmt.order_by(FeedToken.created_at.asc(), FeedToken.id.asc())
    return list(session.execute(ordered).scalars())


def enabled_feed_hashes(session: Session) -> list[tuple[str, str | None]]:
    """所有启用令牌的 `(哈希, user_id)`,供订阅入口做常量时间比对。"""
    return [
        (digest, user_id)
        for digest, user_id in session.execute(
            select(FeedToken.token_hash, FeedToken.user_id).where(
                FeedToken.enabled.is_(True)
            )
        )
    ]


def resolve_feed_token(session: Session, plaintext: str) -> FeedToken | None:
    """按明文找出启用中的令牌记录(逐个常量时间比对,数量只有个位数)。"""
    if not plaintext:
        return None
    digest = hash_token(plaintext)
    stmt = select(FeedToken).where(FeedToken.enabled.is_(True))
    for row in session.execute(stmt).scalars():
        if hmac.compare_digest(digest, row.token_hash):
            return row
    return None


def get_feed_token(session: Session, token_id: str) -> FeedToken | None:
    return session.get(FeedToken, token_id)


def set_enabled(session: Session, token: FeedToken, enabled: bool) -> None:
    token.enabled = enabled
    token.updated_at = utcnow()
    session.flush()


def rename(session: Session, token: FeedToken, label: object) -> None:
    token.label = _validate_label(label)
    token.updated_at = utcnow()
    session.flush()


def delete(session: Session, token: FeedToken) -> None:
    session.delete(token)
    session.flush()


def touch(session: Session, token: FeedToken) -> None:
    """记录最近一次被订阅端拉取的时间;失败不影响响应。"""
    token.last_used_at = utcnow()
    session.flush()


def default_token(session: Session, *, user_id: str | None) -> FeedToken | None:
    """该范围里第一枚启用令牌,用于界面直接展示一条可用订阅地址。"""
    for row in list_feed_tokens(session, user_id=user_id):
        if row.enabled:
            return row
    return None


def feed_scope_label(user_id: str | None) -> str:
    return FAMILY_FEED_LABEL if not user_id else "个人日历"


def subscription_url_for(settings: Settings, plaintext: str) -> str:
    """把令牌明文拼成可直接在 iPhone 里订阅的地址。

    `PUBLIC_BASE_URL` 既是公开面的挂载前缀(如 `https://host/calendar`),也是拼地址的
    基地址,因此地址形如 `https://host/calendar/<令牌>.ics`,与 Tailscale Serve 上唯一
    那个 `/calendar` 挂载天然一致。
    """
    return urls.subscription_url(settings, plaintext)


def subscription_path_for(plaintext: str) -> str:
    """站内相对路径(不带公开前缀),便于测试与内部跳转。"""
    token = (plaintext or "").strip()
    if token.endswith(urls.FEED_SUFFIX):
        token = token[: -len(urls.FEED_SUFFIX)]
    return f"/{token}{urls.FEED_SUFFIX}"


__all__ = [
    "ACCOUNT_FEED_LABEL",
    "FEED_TOKEN_PREFIX",
    "MAX_LABEL",
    "account_feed_hash",
    "account_feed_token",
    "account_subscription_url",
    "create_feed_token",
    "default_token",
    "delete",
    "enabled_feed_hashes",
    "ensure_account_feed_token",
    "feed_scope_label",
    "fingerprint_of_hash",
    "fixed_account_token",
    "get_feed_token",
    "hash_token",
    "is_fixed_account_token",
    "list_feed_tokens",
    "rename",
    "resolve_feed_token",
    "set_enabled",
    "subscription_path_for",
    "subscription_url_for",
    "touch",
]