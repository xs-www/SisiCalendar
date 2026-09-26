"""邮件同步测试:幂等、独立投递、退避重试与旧版附件隔离。"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from app import mailqueue
from app.config import Settings
from app.events import create_event, soft_delete_event
from app.exporter import export_calendar
from app.mailer import MailError
from app.mailqueue import (
    ensure_delivery_tasks,
    process_queue,
    queue_counts,
    recover_stuck_deliveries,
    retry_delivery,
)
from app.models import CHANGE_CREATED, CHANGE_DELETED, MailDelivery, SyncRecipient
from app.schemas import EventWrite
from app.timeutil import utcnow
from sqlalchemy import select


@pytest.fixture()
def mail_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "smtp_host": "smtp.example.test",
            "smtp_port": 587,
            "smtp_security": "starttls",
            "smtp_username": "bot@example.test",
            "smtp_password": "super-secret",
            "smtp_from_address": "bot@example.test",
            "smtp_from_name": "Sisi Calendar",
        }
    )


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """拦截真实 SMTP 发送,记录每封邮件。"""
    captured: list[dict] = []

    def fake_send(config, message):  # noqa: ANN001
        attachment = None
        for part in message.iter_attachments():
            attachment = {
                "filename": part.get_filename(),
                "content_type": part.get_content_type(),
                "payload": part.get_payload(decode=True),
            }
        captured.append(
            {
                "to": message["To"],
                "subject": message["Subject"],
                "body": message.get_body(preferencelist=("plain",)).get_content(),
                "attachment": attachment,
            }
        )

    monkeypatch.setattr(mailqueue, "send_message", fake_send)
    return captured


def _add_recipients(session, *emails: str) -> list[SyncRecipient]:
    recipients = []
    for email in emails:
        recipient = SyncRecipient(
            email=email, enabled=True, created_at=utcnow(), updated_at=utcnow()
        )
        session.add(recipient)
        recipients.append(recipient)
    session.commit()
    return recipients


def _publish(session, settings: Settings, *, title: str = "事件") -> int:
    create_event(
        session,
        EventWrite(
            title=title,
            start_at="2025-03-04T09:00:00",
            end_at="2025-03-04T10:00:00",
            timezone="UTC",
        ),
        settings=settings,
    )
    session.commit()
    return export_calendar(session, settings=settings).version


def test_no_tasks_when_smtp_not_configured(session, settings: Settings) -> None:
    _add_recipients(session, "a@example.test")
    version = _publish(session, settings)
    created = ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=settings
    )
    assert created == 0
    assert queue_counts(session)["pending"] == 0


def test_task_creation_is_idempotent(session, mail_settings: Settings) -> None:
    _add_recipients(session, "a@example.test", "b@example.test")
    version = _publish(session, mail_settings)

    first = ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    second = ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()

    assert first == 2
    assert second == 0, "同一导出版本对同一收件人只能有一个任务"
    rows = list(session.execute(select(MailDelivery)).scalars())
    assert len(rows) == 2


def test_disabled_recipient_is_skipped(session, mail_settings: Settings) -> None:
    _add_recipients(session, "on@example.test")
    off = SyncRecipient(
        email="off@example.test", enabled=False, created_at=utcnow(), updated_at=utcnow()
    )
    session.add(off)
    session.commit()

    version = _publish(session, mail_settings)
    created = ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()
    assert created == 1


def test_each_recipient_gets_separate_message_with_full_attachment(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    _add_recipients(session, "a@example.test", "b@example.test")
    version = _publish(session, mail_settings, title="团队周会")
    ensure_delivery_tasks(
        session,
        export_version=version,
        change_type=CHANGE_CREATED,
        event_title="团队周会",
        settings=mail_settings,
    )
    session.commit()

    summary = process_queue(session, settings=mail_settings)
    session.commit()

    assert summary.sent == 2
    assert len(sent) == 2
    recipients = {entry["to"] for entry in sent}
    assert len(recipients) == 2, "每位收件人必须单独发送,不能互相看到地址"

    for entry in sent:
        assert entry["subject"] == "日程已更新:团队周会"
        assert entry["attachment"]["filename"] == "calendar.ics"
        assert entry["attachment"]["content_type"] == "text/calendar"
        digest = hashlib.sha256(entry["attachment"]["payload"]).hexdigest()
        assert digest, "附件内容必须存在"
        assert "完整日历快照" in entry["body"]
        assert mail_settings.subscription_url in entry["body"]


def test_attachment_hash_matches_export_version(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    _add_recipients(session, "a@example.test")
    version = _publish(session, mail_settings)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    delivery = session.execute(select(MailDelivery)).scalar_one()
    digest = hashlib.sha256(sent[0]["attachment"]["payload"]).hexdigest()
    assert digest == delivery.attachment_hash


def test_delete_notification_subject(session, mail_settings: Settings, sent: list[dict]) -> None:
    _add_recipients(session, "a@example.test")
    event = create_event(
        session,
        EventWrite(
            title="要删掉的事",
            start_at="2025-03-04T09:00:00",
            end_at="2025-03-04T10:00:00",
            timezone="UTC",
        ),
        settings=mail_settings,
    )
    session.commit()
    result = export_calendar(session, settings=mail_settings)
    soft_delete_event(session, event)
    session.commit()
    result = export_calendar(session, settings=mail_settings)
    ensure_delivery_tasks(
        session,
        export_version=result.version,
        change_type=CHANGE_DELETED,
        event_title="要删掉的事",
        settings=mail_settings,
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    assert sent[0]["subject"] == "日程已移除:要删掉的事"
    assert "要删掉的事" not in sent[0]["attachment"]["payload"].decode("utf-8")


def test_failure_schedules_retry_with_backoff(
    session, mail_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_recipients(session, "a@example.test")
    version = _publish(session, mail_settings)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()

    def always_fail(config, message):  # noqa: ANN001
        raise MailError("投递失败:SMTPRecipientsRefused")

    monkeypatch.setattr(mailqueue, "send_message", always_fail)

    moment = utcnow()
    summary = process_queue(session, settings=mail_settings, now=moment)
    session.commit()

    delivery = session.execute(select(MailDelivery)).scalar_one()
    assert summary.retry_wait == 1
    assert delivery.status == "retry_wait"
    assert delivery.attempt_count == 1
    assert delivery.next_attempt_at is not None
    # 第一档退避为 1 分钟
    assert timedelta(minutes=1) - timedelta(seconds=2) <= (
        delivery.next_attempt_at - moment
    ) <= timedelta(minutes=1, seconds=2)
    assert "SMTPRecipientsRefused" in (delivery.error_message or "")


def test_max_attempts_marks_failed(
    session, mail_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_recipients(session, "a@example.test")
    version = _publish(session, mail_settings)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()

    def always_fail(config, message):  # noqa: ANN001
        raise MailError("投递失败")

    monkeypatch.setattr(mailqueue, "send_message", always_fail)

    moment = utcnow()
    for _ in range(mail_settings.mail_max_attempts):
        process_queue(session, settings=mail_settings, now=moment)
        session.commit()
        delivery = session.execute(select(MailDelivery)).scalar_one()
        if delivery.next_attempt_at is not None:
            moment = delivery.next_attempt_at

    delivery = session.execute(select(MailDelivery)).scalar_one()
    assert delivery.attempt_count == mail_settings.mail_max_attempts
    assert delivery.status == "failed"
    assert delivery.next_attempt_at is None


def test_one_failure_does_not_block_others(
    session, mail_settings: Settings, sent: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_recipients(session, "bad@example.test", "good@example.test")
    version = _publish(session, mail_settings)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()

    def selective(config, message):  # noqa: ANN001
        if "bad@example.test" in message["To"]:
            raise MailError("投递失败")
        sent.append({"to": message["To"], "subject": message["Subject"]})

    monkeypatch.setattr(mailqueue, "send_message", selective)
    summary = process_queue(session, settings=mail_settings)
    session.commit()

    assert summary.sent == 1
    assert summary.retry_wait == 1
    assert len(sent) == 1


def test_retry_of_old_version_keeps_old_attachment(
    session, mail_settings: Settings, sent: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧版本重试必须继续附上旧版快照,不能冒充新版。"""
    _add_recipients(session, "a@example.test")
    first_version = _publish(session, mail_settings, title="第一版")
    ensure_delivery_tasks(
        session,
        export_version=first_version,
        change_type=CHANGE_CREATED,
        event_title="第一版",
        settings=mail_settings,
    )
    session.commit()

    def always_fail(config, message):  # noqa: ANN001
        raise MailError("投递失败")

    monkeypatch.setattr(mailqueue, "send_message", always_fail)
    process_queue(session, settings=mail_settings)
    session.commit()

    delivery = session.execute(select(MailDelivery)).scalar_one()
    old_hash = delivery.attachment_hash

    # 产生更新的版本
    second_version = _publish(session, mail_settings, title="第二版")
    assert second_version > first_version

    # 恢复发送能力后重试旧任务
    def ok(config, message):  # noqa: ANN001
        for part in message.iter_attachments():
            sent.append({"payload": part.get_payload(decode=True)})

    monkeypatch.setattr(mailqueue, "send_message", ok)
    status = retry_delivery(session, delivery, settings=mail_settings)
    session.commit()

    assert status == "sent"
    payload = sent[0]["payload"].decode("utf-8")
    assert "第一版" in payload
    assert "第二版" not in payload
    assert hashlib.sha256(sent[0]["payload"]).hexdigest() == old_hash


def test_snapshot_hash_mismatch_refuses_to_send(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    _add_recipients(session, "a@example.test")
    version = _publish(session, mail_settings)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()

    delivery = session.execute(select(MailDelivery)).scalar_one()
    delivery.attachment_hash = "0" * 64
    session.commit()

    summary = process_queue(session, settings=mail_settings)
    session.commit()

    assert summary.sent == 0
    assert session.execute(select(MailDelivery)).scalar_one().error_message.startswith(
        "快照内容哈希"
    )
    assert sent == []


def test_missing_snapshot_is_reported(session, mail_settings: Settings) -> None:
    _add_recipients(session, "a@example.test")
    version = _publish(session, mail_settings)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()

    delivery = session.execute(select(MailDelivery)).scalar_one()
    from pathlib import Path

    Path(delivery.snapshot_path).unlink()

    summary = process_queue(session, settings=mail_settings)
    session.commit()
    assert summary.sent == 0
    assert "快照缺失" in (session.execute(select(MailDelivery)).scalar_one().error_message or "")


def test_sent_task_is_not_resent(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    _add_recipients(session, "a@example.test")
    version = _publish(session, mail_settings)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()

    process_queue(session, settings=mail_settings)
    session.commit()
    second = process_queue(session, settings=mail_settings)
    session.commit()

    assert len(sent) == 1
    assert second.attempted == 0, "已成功的任务不得重复发送"


def test_stuck_sending_is_requeued(session, mail_settings: Settings) -> None:
    _add_recipients(session, "a@example.test")
    version = _publish(session, mail_settings)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()

    delivery = session.execute(select(MailDelivery)).scalar_one()
    delivery.status = "sending"
    delivery.next_attempt_at = utcnow() - timedelta(hours=1)
    session.commit()

    recovered = recover_stuck_deliveries(session, settings=mail_settings)
    session.commit()
    assert recovered == 1
    assert session.execute(select(MailDelivery)).scalar_one().status == "retry_wait"


def test_smtp_password_is_never_persisted(
    session, mail_settings: Settings
) -> None:
    """口令只能来自环境变量,不得出现在数据库的配置表里。"""
    from app.models import AppSetting
    from app.settings_store import save_overrides

    save_overrides(session, {"calendar_name": "我的日历"})
    session.commit()

    rows = list(session.execute(select(AppSetting)).scalars())
    dumped = " ".join(f"{row.key}={row.value}" for row in rows)
    assert "super-secret" not in dumped
    assert all("smtp" not in (row.key or "").lower() for row in rows)
