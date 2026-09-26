"""SMTP 发信。

安全约定(开发文档 6.5、11 节):

- 口令只来自环境变量,不写数据库、不进日志。
- 每位收件人单独发送,避免在 `To`/`Cc` 中泄露其他同步人员地址。
- 使用默认 TLS 上下文校验服务器证书,禁止跳过验证。
"""

from __future__ import annotations

import contextlib
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from app.config import Settings

logger = logging.getLogger(__name__)


class MailError(RuntimeError):
    """发信失败,消息中不得包含口令或邮件正文。"""


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    security: str
    username: str | None
    password: str | None
    from_address: str
    from_name: str
    timeout: int = 30

    @property
    def from_header(self) -> str:
        return formataddr((self.from_name or "", self.from_address))


def config_from_settings(settings: Settings) -> SmtpConfig | None:
    if not settings.mail_configured:
        return None
    assert settings.smtp_host and settings.smtp_from_address
    return SmtpConfig(
        host=settings.smtp_host,
        port=settings.smtp_port,
        security=settings.smtp_security,
        username=settings.smtp_username,
        password=settings.smtp_password,
        from_address=settings.smtp_from_address,
        from_name=settings.smtp_from_name,
        timeout=settings.smtp_timeout_seconds,
    )


def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _connect(config: SmtpConfig) -> smtplib.SMTP:
    try:
        if config.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                config.host, config.port, timeout=config.timeout, context=_tls_context()
            )
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
            client.ehlo()
            if config.security == "starttls":
                client.starttls(context=_tls_context())
                client.ehlo()
    except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
        raise MailError(f"无法连接 SMTP 服务器:{type(exc).__name__}") from exc

    if config.username:
        try:
            client.login(config.username, config.password or "")
        except (smtplib.SMTPException, OSError) as exc:
            _safe_quit(client)
            raise MailError(f"SMTP 认证失败:{type(exc).__name__}") from exc
    return client


def _safe_quit(client: smtplib.SMTP) -> None:
    try:
        client.quit()
    except Exception:  # noqa: BLE001 - 尽力关闭
        with contextlib.suppress(Exception):
            client.close()


def build_message(
    *,
    config: SmtpConfig,
    to_address: str,
    to_name: str | None,
    subject: str,
    body_text: str,
    attachment: bytes | None = None,
    attachment_name: str = "calendar.ics",
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = config.from_header
    message["To"] = formataddr((to_name or "", to_address))
    message["Subject"] = subject
    message.set_content(body_text, charset="utf-8")
    if attachment is not None:
        message.add_attachment(
            attachment,
            maintype="text",
            subtype="calendar",
            filename=attachment_name,
            params={"charset": "utf-8"},
        )
    return message


def send_message(config: SmtpConfig, message: EmailMessage) -> None:
    client = _connect(config)
    try:
        client.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"投递失败:{type(exc).__name__}") from exc
    finally:
        _safe_quit(client)


def test_connection(config: SmtpConfig) -> str:
    client = _connect(config)
    try:
        code, _ = client.noop()
        return f"SMTP 连接正常(响应码 {code},{config.security})"
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"NOOP 失败:{type(exc).__name__}") from exc
    finally:
        _safe_quit(client)


def send_test_mail(config: SmtpConfig, to_address: str, calendar_name: str) -> None:
    body = (
        f"这是一封来自 {calendar_name} 的测试邮件。\n\n"
        "如果你收到本邮件,说明 SMTP 配置可用,后续日程变更会以完整日历附件的形式发送给你。\n"
    )
    message = build_message(
        config=config,
        to_address=to_address,
        to_name=None,
        subject=f"[测试] {calendar_name} 邮件配置验证",
        body_text=body,
    )
    send_message(config, message)
