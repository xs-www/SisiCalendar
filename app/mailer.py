"""SMTP 发信。

安全约定(开发文档 6.5、11 节):

- 口令只来自环境变量,不写数据库、不进日志。
- 每位收件人单独发送,避免在 `To`/`Cc` 中泄露其他收件人地址。
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
        "如果你收到本邮件,说明 SMTP 配置可用,后续日程变更会以「尚未结束的日程」快照附件"
        "发送给你;附件范围按你的收信身份决定(账号邮箱=本人可见的日历,外部邮箱=家庭共享日历)。\n"
    )
    message = build_message(
        config=config,
        to_address=to_address,
        to_name=None,
        subject=f"[测试] {calendar_name} 邮件配置验证",
        body_text=body,
    )
    send_message(config, message)


def send_verification_code(
    config: SmtpConfig,
    *,
    to_address: str,
    to_name: str | None,
    code: str,
    calendar_name: str,
    ttl_minutes: int,
    public_base_url: str,
) -> None:
    """发邮箱验证码。

    **验证码只出现在正文里**:日志、审计、数据库都不得留明文(库里只存 HMAC)。
    """
    body = "\n".join(
        [
            f"{to_name or to_address},你好:",
            "",
            f"这是 {calendar_name} 的邮箱验证码,请在验证页面填入:",
            "",
            f"    {code}",
            "",
            f"验证码 {ttl_minutes} 分钟内有效。",
            "如果这不是你本人的操作,忽略本邮件即可,账号在你验证前不会被启用。",
            "",
            f"验证页面:{public_base_url}/verify-email",
        ]
    )
    message = build_message(
        config=config,
        to_address=to_address,
        to_name=to_name,
        subject=f"[{calendar_name}] 邮箱验证码",
        body_text=body,
    )
    send_message(config, message)


def send_subscription_address(
    config: SmtpConfig,
    *,
    to_address: str,
    to_name: str | None,
    subscription_url: str,
    calendar_name: str,
) -> None:
    """邮箱验证通过后,把该账号**固定的订阅地址**发给本人。"""
    body = "\n".join(
        [
            f"{to_name or to_address},你好:",
            "",
            f"你的邮箱已验证,{calendar_name} 账号现在可以正常使用了。",
            "",
            "下面是属于你的固定订阅地址(只会看到你自己的可见日历:个人日历 + 你能看到的共享日历),"
            "复制到 iPhone 的「设置 → 日历 → 账户 → 添加账户 → 其他 → 添加已订阅的日历」即可:",
            "",
            f"    {subscription_url}",
            "",
            "地址等同于密码,请不要发到公开的群里或群里转发。"
        ]
    )
    message = build_message(
        config=config,
        to_address=to_address,
        to_name=to_name,
        subject=f"[{calendar_name}] 你的固定订阅地址",
        body_text=body,
    )
    send_message(config, message)
