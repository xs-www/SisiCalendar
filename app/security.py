"""认证、会话、CSRF 与限流。

管理入口虽然只绑定回环地址,仍按文档第 11 节作为第二层保护:Argon2id 口令哈希、
签名会话 Cookie、双提交 CSRF 令牌,以及对敏感动作的速率限制。

家庭版(2026-10)起,登录主体是 `users` 表里的账号:

- 会话里带 `user_id`,每次请求都重新读一次账号,以保证「停用账号立即失效」;
- 环境变量里的 `ADMIN_PASSWORD`/`ADMIN_PASSWORD_HASH` 只在**首次启动**用来创建首位
  管理员,不再是长期登录凭据(`app/bootstrap.py::ensure_family_setup`)。
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Settings, get_settings
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "sisi_session"
LOGIN_CSRF_COOKIE_NAME = "sisi_login_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
#: 家庭版换了会话载荷(user_id),升到 v2 让旧 Cookie 一律失效,避免出现「用户名在、
#: 用户 id 不在」的半升级会话。
SESSION_SALT = "sisi-calendar-session-v2"
LOGIN_CSRF_SALT = "sisi-calendar-login-csrf-v1"

_hasher = PasswordHasher()


# --------------------------------------------------------------------------
# 口令
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:  # pragma: no cover - 异常哈希
        return True


def _admin_hash_file(settings: Settings) -> Path:
    return settings.secret_dir / "admin.hash"


def ensure_admin_credential(settings: Settings | None = None) -> str | None:
    """确保存在管理员口令哈希。

    优先级:环境变量 `ADMIN_PASSWORD_HASH` > 环境变量 `ADMIN_PASSWORD` >
    数据目录中已有的 `admin.hash` > 随机生成一个新口令。

    返回新生成的明文口令(仅首次生成时返回),调用方负责打印一次,不落盘。
    """
    resolved = settings or get_settings()
    if resolved.admin_password_hash:
        return None
    if resolved.admin_password:
        _admin_hash_file(resolved).write_text(
            hash_password(resolved.admin_password), encoding="utf-8"
        )
        return None

    resolved.ensure_directories()
    hash_file = _admin_hash_file(resolved)
    if hash_file.exists() and hash_file.read_text(encoding="utf-8").strip():
        return None

    generated = secrets.token_urlsafe(12)
    hash_file.write_text(hash_password(generated), encoding="utf-8")
    with contextlib.suppress(OSError):  # pragma: no cover - 平台差异
        hash_file.chmod(0o600)
    return generated


def load_admin_hash(settings: Settings | None = None) -> str | None:
    resolved = settings or get_settings()
    if resolved.admin_password_hash:
        return resolved.admin_password_hash
    hash_file = _admin_hash_file(resolved)
    if hash_file.exists():
        stored = hash_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    return None


def authenticate(username: str, password: str, settings: Settings | None = None) -> bool:
    """校验**环境变量里的管理员口令**(仅用于兼容与排错)。

    家庭版的登录走 `app/accounts.py::authenticate_user`(users 表);这个函数只在
    「数据库里的账号全部丢失、需要确认 env 兜底口令是否正确」时使用。
    """
    resolved = settings or get_settings()
    if not hmac.compare_digest(username.strip(), resolved.admin_username):
        # 仍然执行一次哈希校验,避免通过响应时间区分用户名是否存在
        verify_password(load_admin_hash(resolved) or "", password)
        return False
    stored = load_admin_hash(resolved)
    if not stored:
        return False
    return verify_password(stored, password)


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------
def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.load_or_create_session_secret(), salt=SESSION_SALT)


@dataclass(frozen=True)
class SessionData:
    user_id: str
    username: str
    csrf_token: str
    issued_at: int


def create_session(
    *, user_id: str, username: str, settings: Settings | None = None
) -> tuple[str, SessionData]:
    resolved = settings or get_settings()
    csrf_token = secrets.token_urlsafe(32)
    data = SessionData(
        user_id=user_id,
        username=username,
        csrf_token=csrf_token,
        issued_at=int(time.time()),
    )
    token = _serializer(resolved).dumps(
        {"uid": data.user_id, "sub": data.username, "csrf": data.csrf_token, "iat": data.issued_at}
    )
    return token, data


def read_session(token: str | None, settings: Settings | None = None) -> SessionData | None:
    if not token:
        return None
    resolved = settings or get_settings()
    try:
        payload = _serializer(resolved).loads(
            token, max_age=resolved.session_max_age_seconds
        )
    except (BadSignature, SignatureExpired):
        return None
    except Exception:  # pragma: no cover - 防御性
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("uid")
    username = payload.get("sub")
    csrf = payload.get("csrf")
    if not isinstance(user_id, str) or not isinstance(username, str) or not isinstance(csrf, str):
        return None
    return SessionData(
        user_id=user_id,
        username=username,
        csrf_token=csrf,
        issued_at=int(payload.get("iat") or 0),
    )


def verify_csrf(session: SessionData | None, provided: str | None) -> bool:
    if session is None or not provided:
        return False
    return hmac.compare_digest(session.csrf_token, provided)


# --------------------------------------------------------------------------
# 登录前的 CSRF(双提交 Cookie)
# --------------------------------------------------------------------------
def _login_serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.load_or_create_session_secret(), salt=LOGIN_CSRF_SALT)


def create_login_csrf(settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    return _login_serializer(resolved).dumps(secrets.token_urlsafe(16))


def read_login_csrf(token: str | None, settings: Settings | None = None) -> str | None:
    if not token:
        return None
    resolved = settings or get_settings()
    try:
        return str(_login_serializer(resolved).loads(token, max_age=3600))
    except Exception:  # noqa: BLE001 - 过期或篡改都视为无效
        return None


def verify_login_csrf(
    cookie_token: str | None, form_token: str | None, settings: Settings | None = None
) -> bool:
    """双提交校验:先确认 Cookie 是我们签发的,再与表单值逐字节比较。"""
    if read_login_csrf(cookie_token, settings) is None:
        return False
    if not form_token:
        return False
    return hmac.compare_digest(cookie_token or "", form_token)


# --------------------------------------------------------------------------
# 限流:进程内滑动窗口,足够家庭规模(3-5 人)的单进程部署
# --------------------------------------------------------------------------
class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        """记录一次尝试。返回 (是否允许, 需要等待的秒数)。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] > window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = int(window_seconds - (now - bucket[0])) + 1
                return False, max(1, retry_after)
            bucket.append(now)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


rate_limiter = RateLimiter()


class AuditLog:
    """极简审计日志:只记录动作与结果,绝不记录口令、会话或邮件正文。"""

    @staticmethod
    def record(action: str, outcome: str, *, actor: str | None = None, detail: str = "") -> None:
        logger.info(
            "audit action=%s outcome=%s actor=%s detail=%s at=%s",
            action,
            outcome,
            actor or "-",
            detail or "-",
            utcnow().isoformat(timespec="seconds"),
        )
