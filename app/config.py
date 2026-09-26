"""应用配置。

所有可配置项都来自环境变量或 `.env`,SMTP 密码等敏感信息只允许通过环境注入,
绝不写入 SQLite、日志或镜像。
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """从环境变量加载的运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
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
    subscription_url: str = "https://sisi-laptop.tail921448.ts.net/calendar"
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

    # ---------- 邮件重试 ----------
    mail_max_attempts: int = 5
    mail_backoff_minutes: str = "1,5,15,60,180"
    export_snapshot_retention_days: int = 30
    scheduler_interval_seconds: int = 60
    scheduler_enabled: bool = True

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
        """对外发布的派生文件,不允许用户直接编辑。"""
        return self.export_dir / "calendar.ics"

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
    def mail_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from_address)

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用:清空配置缓存。"""
    get_settings.cache_clear()
