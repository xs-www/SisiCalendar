"""首位管理员「初始口令」的救援通道(2026-10-14 新增)。

背景:首位管理员的口令若由 `app/security.py::ensure_admin_credential` **随机生成**,
明文只在启动日志里出现一次;`data/admin.hash` 与 `users.password_hash` 里都只有
Argon2id 哈希,**丢了就再也算不出来**。真机上真的发生过:`docker logs` 卡住,
初始口令永久丢失,除了重置数据库没有别的路。

本模块把这条通道换成**一个文件**:

- `<DATA_DIR>/bootstrap_code`(权限 600)保存一枚高熵引导码,它**不以日志为唯一载体**:
  宿主机上的 `./data/bootstrap_code`,或容器内 `cat /data/bootstrap_code`,随时可读;
- 它的有效期恰好等于「首位管理员仍带 `must_change_password`」——口令一旦被设置就
  自然失效(并且文件被删掉),所以它**不是**常驻后门;
- 浏览器里用 `/bootstrap?code=...` 提交新口令,成功后立刻删除该文件。

CLI 兜底(容器内一条命令,不需要口令、不需要看日志):

    python -m app.cli bootstrap                  # 查看/生成引导码与完整引导地址
    python -m app.cli bootstrap --rotate         # 轮换(旧码立即失效)
    python -m app.cli reset-admin-password       # 直接给首位管理员设一个新口令

不变量:

- 引导码只在「首位管理员处于初始口令状态」时有效;`pending_owner()` 是唯一判据,
  它读的是 `users.must_change_password`,不额外引入状态文件;
- 引导码只写这一个文件,不进数据库、不进日志正文(日志只记文件路径,不记内容);
- 比较必须用 `hmac.compare_digest`(与 CSRF/令牌一致);
- 设置口令成功的那一刻必须 `clear_code()`,否则引导通道会继续开着。
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import secrets
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.orm import Session

from app import urls
from app.accounts import owner_user
from app.config import Settings
from app.models import User

logger = logging.getLogger(__name__)

#: 引导码文件名(放在 `settings.secret_dir`,即 `DATA_DIR` 根)
BOOTSTRAP_FILENAME = "bootstrap_code"
#: 引导码的熵:24 字节随机 → 约 32 个 URL 安全字符
BOOTSTRAP_CODE_BYTES = 24
#: 肉眼可识别的前缀,便于确认「这就是引导码」
BOOTSTRAP_CODE_PREFIX = "sisi_boot_"
#: 引导地址里的参数名
BOOTSTRAP_QUERY = "code"


def code_path(settings: Settings) -> Path:
    """引导码文件的位置。"""
    return settings.secret_dir / BOOTSTRAP_FILENAME


def read_code(settings: Settings) -> str | None:
    """读取当前引导码;文件不存在或内容为空时返回 `None`。"""
    try:
        code = code_path(settings).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return code or None


def ensure_code(settings: Settings, *, rotate: bool = False) -> str:
    """确保存在引导码并返回它;`rotate=True` 时重新生成(旧码立即失效)。"""
    if not rotate:
        existing = read_code(settings)
        if existing:
            return existing
    code = f"{BOOTSTRAP_CODE_PREFIX}{secrets.token_urlsafe(BOOTSTRAP_CODE_BYTES)}"
    settings.ensure_directories()
    path = code_path(settings)
    path.write_text(code, encoding="utf-8")
    with contextlib.suppress(OSError):  # pragma: no cover - 平台差异
        path.chmod(0o600)
    return code


def clear_code(settings: Settings) -> bool:
    """删除引导码文件;返回是否真的删掉了。"""
    path = code_path(settings)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:  # pragma: no cover - 权限异常
        logger.warning("引导码文件删除失败:%s", path)
        return False


def verify_code(settings: Settings, candidate: object) -> bool:
    """常量时间比较引导码;任一侧为空一律 `False`。"""
    expected = read_code(settings)
    text = str(candidate or "").strip()
    if not expected or not text:
        return False
    return hmac.compare_digest(expected, text)


def pending_owner(session: Session) -> User | None:
    """仍在用初始口令的首位管理员;没有(或已改过口令)时返回 `None`。

    判据就是 `users.must_change_password`:首位管理员由引导创建时,若口令是随机生成的,
    它被置为真;任何路径改过口令都会把它清掉(见 `app/accounts.py::set_password`)。
    """
    owner = owner_user(session)
    if owner is None or not owner.must_change_password:
        return None
    return owner


def sync_code_state(session: Session, settings: Settings) -> str | None:
    """让引导码文件与「是否仍处于初始口令」保持一致,返回当前引导码。

    - 仍处于初始状态 → 确保文件存在(自愈:被手工删掉也会在下次启动补回来);
    - 已经设过口令 → 删掉文件(例如从备份恢复了一个已改过口令的库,
      不能让旧引导码继续有效)。

    由 `app/bootstrap.py::ensure_family_setup` 在每次启动时调用,幂等。
    """
    if pending_owner(session) is None:
        clear_code(settings)
        return None
    return ensure_code(settings)


def bootstrap_url(settings: Settings, code: str) -> str:
    """给管理员复制粘贴用的完整引导地址;基地址为空时退回相对路径。"""
    base = urls.public_base_url(settings).rstrip("/")
    path = f"/bootstrap?{BOOTSTRAP_QUERY}={quote(code)}"
    return f"{base}{path}" if base else path


__all__ = [
    "BOOTSTRAP_CODE_PREFIX",
    "BOOTSTRAP_FILENAME",
    "BOOTSTRAP_QUERY",
    "bootstrap_url",
    "clear_code",
    "code_path",
    "ensure_code",
    "pending_owner",
    "read_code",
    "sync_code_state",
    "verify_code",
]