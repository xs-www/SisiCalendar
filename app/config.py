"""应用配置。

所有可配置项都来自环境变量或 `.env`(路径可用环境变量 `SISI_ENV_FILE` 覆盖,
设为不存在的路径即可完全忽略 `.env`,测试就是这么隔离的)。SMTP 密码等敏感信息
只允许通过环境注入,绝不写入 SQLite、日志或镜像。
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 默认配置文件;测试会把它指向一个不存在的路径,避免读到你本机的 .env
DEFAULT_ENV_FILE = ".env"


class Settings(BaseSettings):
    """从环境变量加载的运行时配置。

    注意:`env_file` 刻意不写进 model_config,而是在 `get_settings()` 里显式传入,
    这样直接 `Settings()` 构造出来的实例只依赖进程环境变量,行为可预测。
    """

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 运行与数据 ----------
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    # ---------- 管理员与安全 ----------
    admin_username: str = "admin"
    admin_password: str | None = None
    admin_password_hash: str | None = None
    session_secret: str | None = None
    session_max_age_seconds: int = 12 * 60 * 60

    # ---------- 日历默认值 ----------
    calendar_name: str = "Sisi Calendar"
    default_timezone: str = "Australia/Brisbane"
    default_event_duration_minutes: int = 60
    #: 对外公开的基地址。路径部分就是公开面的挂载前缀(`https://节点/calendar` → 前缀
    #: `/calendar`):邀请链接、每人的订阅地址、邮件正文里的地址都从它派生。
    #: 本机管理页不受影响,仍可用裸路径 `http://localhost:443/admin` 访问。
    public_base_url: str = "https://sisi-laptop.tail921448.ts.net/calendar"
    feed_cache_seconds: int = 300

    # ---------- SMTP ----------
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str | None = None
    smtp_from_name: str = "Sisi Calendar"
    smtp_timeout_seconds: int = 30

    # ---------- 邮箱验证 ----------
    #: 验证码有效期、可尝试次数与重发冷却(单位:秒)
    email_code_ttl_seconds: int = 30 * 60
    email_code_max_attempts: int = 5
    email_code_resend_seconds: int = 60

    # ---------- 邮件重试 ----------
    mail_max_attempts: int = 5
    mail_backoff_minutes: str = "1,5,15,60,180"
    export_snapshot_retention_days: int = 30
    scheduler_interval_seconds: int = 60
    scheduler_enabled: bool = True

    # ---------- 远程 Agent 接入 ----------
    # 逗号分隔可配置多个 token,便于分批轮换。留空则 /api/agent 视为不存在(返回 404)。
    agent_api_token: str | None = None
    agent_rate_limit_per_minute: int = 60

    # ---------- 备份 ----------
    backup_retention_days: int = 14

    # ---------- 派生路径 ----------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "calendar.db"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "export"

    @property
    def calendar_path(self) -> Path:
        """导出的**并集**文件(内部一致性产物),不允许用户直接编辑,也不对外暴露。"""
        return self.export_dir / "calendar.ics"

    @property
    def feed_dir(self) -> Path:
        """按订阅目标发布的 ICS:`user-<id>.ics` 与 `family.ics`。

        对外只暴露这些文件(经 `/calendar/<令牌>.ics`),并集文件留在 `export/` 里。
        """
        return self.export_dir / "feeds"

    @property
    def snapshot_dir(self) -> Path:
        """导出版本快照,供邮件重试时附上正确的历史版本。"""
        return self.data_dir / "snapshots"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def secret_dir(self) -> Path:
        return self.data_dir

    @property
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path.as_posix()}"

    @property
    def backoff_minutes(self) -> list[int]:
        values: list[int] = []
        for chunk in self.mail_backoff_minutes.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            values.append(max(0, int(chunk)))
        return values or [1, 5, 15, 60, 180]

    @property
    def public_base_path(self) -> str:
        """公开面的挂载前缀,取自 `PUBLIC_BASE_URL` 的路径部分(如 `/calendar`)。"""
        from app.urls import public_base_path

        return public_base_path(self)

    @property
    def mail_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from_address)

    @property
    def agent_tokens(self) -> tuple[str, ...]:
        """可用的 Agent 访问令牌。支持逗号分隔多个,便于轮换。"""
        raw = self.agent_api_token or ""
        return tuple(chunk.strip() for chunk in raw.split(",") if chunk.strip())

    @property
    def agent_enabled(self) -> bool:
        return bool(self.agent_tokens)

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value

    @field_validator("smtp_security")
    @classmethod
    def _check_smtp_security(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"starttls", "ssl", "none"}:
            raise ValueError("SMTP_SECURITY 必须是 starttls、ssl 或 none")
        return normalized

    # ---------- 目录准备 ----------
    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.export_dir,
            self.feed_dir,
            self.snapshot_dir,
            self.backup_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ---------- 密钥 ----------
    def load_or_create_session_secret(self) -> str:
        """返回会话签名密钥。

        优先使用环境变量;否则在数据目录生成并复用,避免每次重启导致登录态失效。
        """
        if self.session_secret:
            return self.session_secret
        self.ensure_directories()
        secret_file = self.secret_dir / "session_secret"
        if secret_file.exists():
            stored = secret_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        generated = secrets.token_urlsafe(48)
        secret_file.write_text(generated, encoding="utf-8")
        _harden(secret_file)
        logger.warning("已生成新的会话密钥并保存到 %s", secret_file)
        return generated


def _harden(path: Path) -> None:
    """尽力收紧敏感文件权限(Windows 上仅依赖目录 ACL)。"""
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def env_file_path() -> str:
    """配置文件路径。指向不存在的文件时等价于「忽略 .env」。"""
    return os.environ.get("SISI_ENV_FILE", DEFAULT_ENV_FILE) or ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(_env_file=env_file_path())


def reset_settings_cache() -> None:
    """测试用:清空配置缓存。"""
    get_settings.cache_clear()
