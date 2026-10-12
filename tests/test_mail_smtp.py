"""SMTP 传输层测试。

`test_mail.py` 用注入的假 `send_message` 验证队列逻辑;这里补的是它没有覆盖的一层:
真实的 `smtplib` 对话、MIME 编码与 TLS 配置。用一个本地假 SMTP 服务器承接连接,
所以不依赖任何外部账号。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import socket
import ssl
import threading
from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy

import pytest
from app import mailqueue
from app.config import Settings
from app.events import create_event
from app.exporter import export_calendar
from app.mailer import MailError, SmtpConfig, _tls_context, config_from_settings
from app.mailer import test_connection as check_smtp_connection
from app.mailqueue import ensure_delivery_tasks, process_queue
from app.models import CHANGE_CREATED, MailDelivery, SyncRecipient
from app.schemas import EventWrite
from app.timeutil import utcnow
from sqlalchemy import select


@pytest.fixture(autouse=True)
def events_land_in_shared_calendar(request):
    """本模块的收件人都是外部邮箱,附件范围是家庭共享日历(详见 conftest 同名夹具)。"""
    if "session" not in request.fixturenames:
        return
    request.getfixturevalue("patch_create_event_to_shared")


class FakeSmtpServer:
    """够用的本地 SMTP 服务器:EHLO / AUTH PLAIN / MAIL / RCPT / DATA / QUIT。"""

    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(5)
        self.port = int(self._socket.getsockname()[1])
        self.messages: list[dict] = []
        self.credentials: list[tuple[str, str]] = []
        self.envelopes: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._socket.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        stream = connection.makefile("rwb")

        def reply(line: str) -> None:
            stream.write(line.encode() + b"\r\n")
            stream.flush()

        reply("220 fake.local ESMTP ready")
        in_data = False
        chunks: list[bytes] = []
        envelope: dict[str, object] = {"to": []}
        awaiting_auth_user = False
        awaiting_auth_pass = False

        while True:
            raw = stream.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").rstrip("\r\n")

            if in_data:
                if text == ".":
                    in_data = False
                    self.messages.append({"raw": b"\r\n".join(chunks)})
                    self.envelopes.append(dict(envelope))
                    chunks = []
                    envelope = {"to": []}
                    reply("250 2.0.0 Ok: queued")
                else:
                    # 还原 dot-stuffing
                    if text.startswith(".."):
                        text = text[1:]
                    chunks.append(text.encode("utf-8"))
                continue

            if awaiting_auth_user:
                awaiting_auth_user = False
                awaiting_auth_pass = True
                envelope["auth_user"] = base64.b64decode(text).decode()
                reply("334 UGFzc3dvcmQ6")
                continue
            if awaiting_auth_pass:
                awaiting_auth_pass = False
                self.credentials.append(
                    (str(envelope.get("auth_user", "")), base64.b64decode(text).decode())
                )
                reply("235 2.7.0 Authentication successful")
                continue

            command = text.upper()
            if command.startswith("EHLO"):
                reply("250-fake.local")
                reply("250-AUTH PLAIN LOGIN")
                reply("250-8BITMIME")
                reply("250 SIZE 52428800")
            elif command.startswith("HELO"):
                reply("250 fake.local")
            elif command.startswith("AUTH PLAIN"):
                parts = text.split()
                if len(parts) >= 3:
                    decoded = base64.b64decode(parts[2]).decode()
                    _, user, password = decoded.split("\x00")
                    self.credentials.append((user, password))
                    reply("235 2.7.0 Authentication successful")
                else:
                    reply("334 ")
                    raw = stream.readline()
                    decoded = base64.b64decode(raw.decode().strip()).decode()
                    _, user, password = decoded.split("\x00")
                    self.credentials.append((user, password))
                    reply("235 2.7.0 Authentication successful")
            elif command.startswith("AUTH LOGIN"):
                reply("334 VXNlcm5hbWU6")
                awaiting_auth_user = True
            elif command.startswith("MAIL FROM"):
                envelope["from"] = text
                reply("250 2.1.0 Ok")
            elif command.startswith("RCPT TO"):
                envelope["to"].append(text)  # type: ignore[union-attr]
                reply("250 2.1.5 Ok")
            elif command.startswith("DATA"):
                in_data = True
                reply("354 End data with <CR><LF>.<CR><LF>")
            elif command.startswith("QUIT"):
                reply("221 2.0.0 Bye")
                break
            elif command.startswith(("NOOP", "RSET")):
                reply("250 2.0.0 Ok")
            else:
                reply("250 2.0.0 Ok")

        with contextlib.suppress(OSError):
            connection.close()


@pytest.fixture()
def smtp_server():
    server = FakeSmtpServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture()
def smtp_settings(settings: Settings, smtp_server: FakeSmtpServer) -> Settings:
    return settings.model_copy(
        update={
            "smtp_host": "127.0.0.1",
            "smtp_port": smtp_server.port,
            "smtp_security": "none",
            "smtp_username": "robot@example.test",
            "smtp_password": "app-specific-password",
            "smtp_from_address": "robot@example.test",
            "smtp_from_name": "Sisi Calendar",
            "smtp_timeout_seconds": 10,
        }
    )


# --------------------------------------------------------------------------
# TLS 配置
# --------------------------------------------------------------------------
def test_tls_context_verifies_certificates() -> None:
    """文档要求「连接必须验证服务器证书,禁止默认跳过 TLS 验证」。"""
    context = _tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_ssl_mode_uses_smtps(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    class RecordingSmtpSsl:
        def __init__(self, host, port, timeout=None, context=None):  # noqa: ANN001
            calls.append((host, port, context))
            self.context = context

        def noop(self):
            return (250, b"OK")

        def quit(self):
            return (221, b"Bye")

    monkeypatch.setattr("app.mailer.smtplib.SMTP_SSL", RecordingSmtpSsl)
    config = SmtpConfig(
        host="smtp.example.test",
        port=465,
        security="ssl",
        username=None,
        password=None,
        from_address="a@example.test",
        from_name="A",
    )
    detail = check_smtp_connection(config)
    assert "SMTP 连接正常" in detail
    assert calls and calls[0][1] == 465
    assert calls[0][2].verify_mode == ssl.CERT_REQUIRED, "SSL 模式也必须校验证书"


def test_config_requires_host_and_from_address(settings: Settings) -> None:
    assert config_from_settings(settings) is None
    partial = settings.model_copy(update={"smtp_host": "smtp.example.test"})
    assert config_from_settings(partial) is None, "只有主机没有发件地址时不算配置完整"
    complete = settings.model_copy(
        update={"smtp_host": "smtp.example.test", "smtp_from_address": "a@example.test"}
    )
    assert config_from_settings(complete) is not None


# --------------------------------------------------------------------------
# 真实 smtplib 对话
# --------------------------------------------------------------------------
def _prepare_delivery(session, settings: Settings, email: str) -> MailDelivery:
    session.add(
        SyncRecipient(email=email, enabled=True, created_at=utcnow(), updated_at=utcnow())
    )
    create_event(
        session,
        EventWrite(
            title="邮件链路验证",
            start_at="2026-11-20T09:00:00",
            end_at="2026-11-20T10:00:00",
            timezone="UTC",
            location="会议室",
        ),
        settings=settings,
    )
    session.commit()
    version = export_calendar(session, settings=settings).version
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=settings
    )
    session.commit()
    return session.execute(select(MailDelivery)).scalar_one()


def test_real_smtp_conversation(
    session, smtp_settings: Settings, smtp_server: FakeSmtpServer
) -> None:
    """走真实 smtplib:EHLO → AUTH → MAIL → RCPT → DATA,并校验最终 MIME 结构。"""
    delivery = _prepare_delivery(session, smtp_settings, "someone@example.test")

    summary = process_queue(session, settings=smtp_settings)
    session.commit()

    assert summary.sent == 1, summary.as_dict()
    assert smtp_server.credentials == [("robot@example.test", "app-specific-password")]
    assert len(smtp_server.messages) == 1

    envelope = smtp_server.envelopes[0]
    assert any("someone@example.test" in item for item in envelope["to"])  # type: ignore[union-attr]
    assert "robot@example.test" in str(envelope["from"])

    message: Message = message_from_bytes(smtp_server.messages[0]["raw"], policy=default_policy)
    assert message["Subject"]
    assert message["From"] == "Sisi Calendar <robot@example.test>"
    assert message["To"] == "someone@example.test"
    assert message.get_content_type() == "multipart/mixed"

    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    text = body.get_content()
    assert "尚未结束的日程" in text
    # 家庭版:外部邮箱只是替别人收信,正文里不带任何订阅地址(那会泄露账号的地址)
    assert "sisi_feed_" not in text
    assert "长期订阅地址" in text

    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    part = attachments[0]
    assert part.get_filename() == "calendar.ics"
    assert part.get_content_type() == "text/calendar"
    raw = part.get_payload(decode=True)
    assert raw is not None
    # 附件必须与任务记录的不可变快照逐字节一致
    assert hashlib.sha256(raw).hexdigest() == delivery.attachment_hash
    assert b"BEGIN:VCALENDAR" in raw
    assert "邮件链路验证".encode() in raw

    assert delivery.status == "sent"
    assert delivery.sent_at is not None


def test_two_recipients_get_separate_messages(
    session, smtp_settings: Settings, smtp_server: FakeSmtpServer
) -> None:
    for address in ("a@example.test", "b@example.test"):
        session.add(
            SyncRecipient(
                email=address, enabled=True, created_at=utcnow(), updated_at=utcnow()
            )
        )
    create_event(
        session,
        EventWrite(
            title="分别发送",
            start_at="2026-11-21T09:00:00",
            end_at="2026-11-21T10:00:00",
            timezone="UTC",
        ),
        settings=smtp_settings,
    )
    session.commit()
    version = export_calendar(session, settings=smtp_settings).version
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=smtp_settings
    )
    session.commit()

    process_queue(session, settings=smtp_settings)
    session.commit()

    assert len(smtp_server.messages) == 2
    recipients = set()
    for item in smtp_server.messages:
        message = message_from_bytes(item["raw"], policy=default_policy)
        recipients.add(message["To"])
        # 每封邮件只能出现一个收件人,不得在 To/Cc 中泄露其他人
        assert "@example.test" not in str(message["Cc"] or "")
    assert recipients == {"a@example.test", "b@example.test"}


def test_without_credentials_skips_auth(
    session, settings: Settings, smtp_server: FakeSmtpServer
) -> None:
    anonymous = settings.model_copy(
        update={
            "smtp_host": "127.0.0.1",
            "smtp_port": smtp_server.port,
            "smtp_security": "none",
            "smtp_username": None,
            "smtp_password": None,
            "smtp_from_address": "robot@example.test",
        }
    )
    _prepare_delivery(session, anonymous, "someone@example.test")
    summary = process_queue(session, settings=anonymous)
    session.commit()

    assert summary.sent == 1
    assert smtp_server.credentials == [], "未配置用户名时不应尝试认证"


def test_connection_error_is_reported_and_retried(
    session, settings: Settings
) -> None:
    """端口无人监听时应给出明确错误并进入重试,而不是抛到调用方。"""
    dead = settings.model_copy(
        update={
            "smtp_host": "127.0.0.1",
            "smtp_port": 9,  # discard 端口,通常无人监听
            "smtp_security": "none",
            "smtp_from_address": "robot@example.test",
            "smtp_timeout_seconds": 3,
        }
    )
    delivery = _prepare_delivery(session, dead, "someone@example.test")
    summary = process_queue(session, settings=dead)
    session.commit()

    assert summary.sent == 0
    assert summary.retry_wait == 1
    assert delivery.status == "retry_wait"
    assert delivery.error_message and "无法连接" in delivery.error_message


def test_auth_failure_is_reported(
    session, smtp_settings: Settings, smtp_server: FakeSmtpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务器拒绝认证时应记录失败原因,且不泄露口令。"""
    delivery = _prepare_delivery(session, smtp_settings, "someone@example.test")

    original = mailqueue.send_message

    def reject(config, message):  # noqa: ANN001
        raise MailError("SMTP 认证失败:SMTPAuthenticationError")

    monkeypatch.setattr(mailqueue, "send_message", reject)
    summary = process_queue(session, settings=smtp_settings)
    session.commit()
    monkeypatch.setattr(mailqueue, "send_message", original)

    assert summary.retry_wait == 1
    assert "认证失败" in (delivery.error_message or "")
    assert "app-specific-password" not in (delivery.error_message or "")


def test_message_body_includes_change_summary(
    session, smtp_settings: Settings, smtp_server: FakeSmtpServer
) -> None:
    session.add(
        SyncRecipient(
            email="someone@example.test",
            enabled=True,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    create_event(
        session,
        EventWrite(
            title="牙医预约",
            start_at="2026-11-22T14:00:00",
            end_at="2026-11-22T15:00:00",
            timezone="Australia/Brisbane",
        ),
        settings=smtp_settings,
    )
    session.commit()
    version = export_calendar(session, settings=smtp_settings).version
    ensure_delivery_tasks(
        session,
        export_version=version,
        change_type=CHANGE_CREATED,
        event_title="牙医预约",
        event_time_text="2026-11-22 14:00–15:00(Australia/Brisbane)",
        settings=smtp_settings,
    )
    session.commit()
    process_queue(session, settings=smtp_settings)
    session.commit()

    message = message_from_bytes(smtp_server.messages[0]["raw"], policy=default_policy)
    assert message["Subject"] == "日程已更新:牙医预约"
    text = message.get_body(preferencelist=("plain",)).get_content()
    assert "新增日程" in text
    assert "牙医预约" in text
    assert "2026-11-22 14:00–15:00(Australia/Brisbane)" in text
    assert "日历生成时间(UTC)" in text


def test_send_message_requires_reachable_server(settings: Settings) -> None:
    config = SmtpConfig(
        host="127.0.0.1",
        port=9,
        security="none",
        username=None,
        password=None,
        from_address="a@example.test",
        from_name="A",
        timeout=3,
    )
    with pytest.raises(MailError):
        check_smtp_connection(config)
