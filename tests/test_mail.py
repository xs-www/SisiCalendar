"""邮件同步测试:幂等、独立投递、退避重试与旧版附件隔离。"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from app import db as db_module
from app import mailqueue
from app.config import Settings, reset_settings_cache
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
from app.sync_service import event_time_text, input_time_text
from app.timeutil import utc_to_local, utcnow
from fastapi.testclient import TestClient
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


@pytest.fixture(autouse=True)
def events_land_in_shared_calendar(request, monkeypatch: pytest.MonkeyPatch):
    """本模块的收件人都是「外部邮箱」,附件范围是**家庭共享日历**。

    因此对直接操作数据库的用例,把 `create_event` 包一层:没有显式指定日历的事件
    一律落进家庭共享日历。走 HTTP 的用例不做处理——它们的 `agent_client` 已经跑过
    lifespan 引导,拉 `session` 夹具反而会与 Alembic 迁移冲突。
    """
    if "session" not in request.fixturenames:
        return
    request.getfixturevalue("patch_create_event_to_shared")


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


def _window(*, days: int, hours: int = 1) -> tuple[str, str]:
    """相对当前时间的时间窗口(`days` 为负表示过去,`hours` 是时长)。

    邮件附件只含**尚未结束**的日程,所以测试数据必须显式落在过去或未来,不能写死日期。
    """
    start = (utcnow() + timedelta(days=days)).replace(microsecond=0)
    return start.isoformat(), (start + timedelta(hours=hours)).isoformat()


def _publish(session, settings: Settings, *, title: str = "事件", days: int = 30) -> int:
    start_at, end_at = _window(days=days)
    create_event(
        session,
        EventWrite(
            title=title,
            start_at=start_at,
            end_at=end_at,
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


def test_each_recipient_gets_separate_message_with_future_attachment(
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
        assert "尚未结束的日程" in entry["body"]
        # 当前的正文措辞是「附件是…尚未结束的日程快照。完整历史(含过去事件)始终可以从长期
        # 订阅地址获得。」——也就是「已结束的不在附件里」这层意思(旧措辞已随 2026-10 的
        # 家庭版文案改写消失,这里断言现在的说法)
        assert "完整历史(含过去事件)" in entry["body"]
        # 家庭版:外部邮箱只是替别人收信,正文里不带任何订阅地址(那会泄露账号的地址),
        # 只提示地址由各自的账号本人查看
        assert "sisi_feed_" not in entry["body"]
        assert "长期订阅地址" in entry["body"]


def test_attachment_hash_matches_task_snapshot(
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
    assert delivery.snapshot_path.endswith("-upcoming.ics"), "附件应取自邮件专用快照"


def test_attachment_drops_past_events_but_feed_keeps_them(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    """邮件附件只含尚未结束的日程;发布文件(订阅源)必须原样保留历史。"""
    _add_recipients(session, "a@example.test")
    past_start, past_end = _window(days=-30)
    create_event(
        session,
        EventWrite(
            title="上个月的复盘", start_at=past_start, end_at=past_end, timezone="UTC"
        ),
        settings=mail_settings,
    )
    session.commit()

    version = _publish(session, mail_settings, title="下周的评审")
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    payload = sent[0]["attachment"]["payload"].decode("utf-8")
    assert "下周的评审" in payload
    assert "上个月的复盘" not in payload, "过去事件不得出现在邮件附件里"

    feed = mail_settings.calendar_path.read_text(encoding="utf-8")
    assert "上个月的复盘" in feed, "订阅源必须保留历史事件"
    assert "下周的评审" in feed


def test_attachment_keeps_ongoing_events(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    """尚未结束的事件要保留:起点在过去、但离结束还有一段时间(跨天也算)。"""
    _add_recipients(session, "a@example.test")
    ongoing_start, ongoing_end = _window(days=-1, hours=48)
    create_event(
        session,
        EventWrite(
            title="进行中的工作坊",
            start_at=ongoing_start,
            end_at=ongoing_end,
            timezone="UTC",
        ),
        settings=mail_settings,
    )
    done_start, done_end = _window(days=-3, hours=24)
    create_event(
        session,
        EventWrite(
            title="前天就结束了", start_at=done_start, end_at=done_end, timezone="UTC"
        ),
        settings=mail_settings,
    )
    session.commit()

    version = _publish(session, mail_settings, title="明天的事", days=1)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    payload = sent[0]["attachment"]["payload"].decode("utf-8")
    assert "进行中的工作坊" in payload
    assert "明天的事" in payload
    assert "前天就结束了" not in payload


def test_attachment_drops_events_that_ended_today(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    """分界是**此刻**,不是「今天 00:00」:当天早些时候结束的事件同样不进附件。"""
    _add_recipients(session, "a@example.test")
    earlier_start, earlier_end = _window(days=-1, hours=18)  # 昨天开始、6 小时前结束
    create_event(
        session,
        EventWrite(
            title="今天上午开完的会",
            start_at=earlier_start,
            end_at=earlier_end,
            timezone="UTC",
        ),
        settings=mail_settings,
    )
    session.commit()

    version = _publish(session, mail_settings, title="今晚的事", days=1)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    payload = sent[0]["attachment"]["payload"].decode("utf-8")
    assert "今晚的事" in payload
    assert "今天上午开完的会" not in payload, "已结束的事件不得进附件,哪怕就是今天结束的"
    assert "今天上午开完的会" in mail_settings.calendar_path.read_text(encoding="utf-8")


def test_attachment_keeps_all_day_events_of_today(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    """全天事件没有时刻可言,仍按当地日期比较:今天的全天事件保留,昨天的丢弃。"""
    _add_recipients(session, "a@example.test")
    today = utc_to_local(utcnow(), mail_settings.default_timezone).date()
    create_event(
        session,
        EventWrite(
            title="今天的全天安排",
            all_day=True,
            start_date=today.isoformat(),
            end_date=(today + timedelta(days=1)).isoformat(),
            timezone="UTC",
        ),
        settings=mail_settings,
    )
    yesterday = today - timedelta(days=1)
    create_event(
        session,
        EventWrite(
            title="昨天的全天安排",
            all_day=True,
            start_date=yesterday.isoformat(),
            end_date=today.isoformat(),
            timezone="UTC",
        ),
        settings=mail_settings,
    )
    session.commit()

    version = _publish(session, mail_settings, title="明天的事", days=1)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    payload = sent[0]["attachment"]["payload"].decode("utf-8")
    assert "今天的全天安排" in payload, "今天的全天事件尚未「结束」"
    assert "昨天的全天安排" not in payload


def test_attachment_keeps_recurring_series_started_in_the_past(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    """起点很早但仍在重复的事件不能丢:它的 DTSTART 属于这个序列。"""
    _add_recipients(session, "a@example.test")
    start_at, end_at = _window(days=-60)
    create_event(
        session,
        EventWrite(
            title="每周站会",
            start_at=start_at,
            end_at=end_at,
            timezone="UTC",
            recurrence="weekly",
        ),
        settings=mail_settings,
    )
    session.commit()

    version = _publish(session, mail_settings, title="未来的一次性事件", days=7)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    payload = sent[0]["attachment"]["payload"].decode("utf-8")
    assert "每周站会" in payload
    assert "RRULE" in payload


def test_attachment_drops_recurring_series_that_already_ended(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    _add_recipients(session, "a@example.test")
    start_at, end_at = _window(days=-120)
    until = (utcnow() - timedelta(days=90)).date().isoformat()
    create_event(
        session,
        EventWrite(
            title="早就结束的每日记录",
            start_at=start_at,
            end_at=end_at,
            timezone="UTC",
            recurrence="daily",
            recurrence_until=until,
        ),
        settings=mail_settings,
    )
    session.commit()

    version = _publish(session, mail_settings, title="未来的一次性事件", days=7)
    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    payload = sent[0]["attachment"]["payload"].decode("utf-8")
    assert "未来的一次性事件" in payload
    assert "早就结束的每日记录" not in payload


def test_attachment_falls_back_to_version_snapshot(
    session, mail_settings: Settings, sent: list[dict]
) -> None:
    """邮件快照缺失(升级前的旧版本)时回退到版本快照,不能让任务失败。"""
    _add_recipients(session, "a@example.test")
    version = _publish(session, mail_settings, title="升级前的日程")

    from pathlib import Path

    from app.exporter import FAMILY_TARGET_KEY, mail_snapshot_path_for

    # 外部邮箱的附件范围是家庭共享日历,邮件专用快照按目标分别落盘
    mail_path = mail_snapshot_path_for(
        mail_settings, version, target_key=FAMILY_TARGET_KEY
    )
    assert mail_path.exists()
    mail_path.unlink()

    ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    delivery = session.execute(select(MailDelivery)).scalar_one()
    assert Path(delivery.snapshot_path).name == f"calendar-v{version:06d}-{FAMILY_TARGET_KEY}.ics"
    assert delivery.status == "sent"
    assert "升级前的日程" in sent[0]["attachment"]["payload"].decode("utf-8")


def test_delete_notification_subject(session, mail_settings: Settings, sent: list[dict]) -> None:
    _add_recipients(session, "a@example.test")
    start_at, end_at = _window(days=5)
    event = create_event(
        session,
        EventWrite(
            title="要删掉的事",
            start_at=start_at,
            end_at=end_at,
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


# --------------------------------------------------------------------------
# 邮件正文里的「事件时间」按提交时区表述
# --------------------------------------------------------------------------
def test_event_time_text_uses_submitted_offset(session, settings: Settings) -> None:
    """提交时间带偏移量时按该偏移表述;不带偏移量时仍按事件的参照时区。"""
    event = create_event(
        session,
        EventWrite(
            title="悉尼的会",
            start_at="2026-11-03T14:00:00+11:00",
            end_at="2026-11-03T15:00:00+11:00",
            timezone="Asia/Shanghai",
        ),
        settings=settings,
    )
    session.commit()

    assert event.timezone == "Asia/Shanghai", "事件的参照时区由 timezone 决定"
    assert (
        input_time_text(event, "2026-11-03T14:00:00+11:00", "2026-11-03T15:00:00+11:00")
        == "2026-11-03 14:00–15:00(UTC+11:00)"
    )
    assert (
        event_time_text(event) == "2026-11-03 11:00–12:00(Asia/Shanghai)"
    ), "没带偏移量时保持原行为"


def test_utc_offset_is_not_treated_as_missing(session, settings: Settings) -> None:
    """`Z` 的偏移量是 timedelta(0),不能被当成「没给偏移量」。"""
    event = create_event(
        session,
        EventWrite(
            title="UTC 的会",
            start_at="2026-11-03T04:00:00Z",
            end_at="2026-11-03T05:00:00Z",
            timezone="Asia/Shanghai",
        ),
        settings=settings,
    )
    session.commit()

    assert input_time_text(event, "2026-11-03T04:00:00Z") == "2026-11-03 04:00–05:00(UTC)"


@pytest.fixture()
def smtp_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """必须在应用实例构造之前配好 SMTP,否则 Agent 接口不会建发信任务。"""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_ADDRESS", "bot@example.test")
    reset_settings_cache()


def test_agent_submitted_offset_reaches_the_mail_body(
    settings: Settings,
    smtp_env: None,
    agent_client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    """走完整的 Agent HTTP 链路,确认偏移量真的被带进了发信任务记录。

    这里只能用应用自己的会话工厂:测试的 `session` 夹具会先建表,与应用 lifespan
    里的 Alembic 迁移冲突。
    """
    factory = db_module.get_session_factory()
    prepared = factory()
    try:
        prepared.add(
            SyncRecipient(
                email="a@example.test", enabled=True, created_at=utcnow(), updated_at=utcnow()
            )
        )
        prepared.commit()
    finally:
        prepared.close()

    response = agent_client.post(
        "/api/agent/events",
        json={
            "title": "悉尼的会",
            "start_at": "2026-11-03T14:00:00+11:00",
            "end_at": "2026-11-03T15:00:00+11:00",
        },
        headers=agent_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["timezone"] == settings.default_timezone, "参照时区仍取服务器默认值"

    check = factory()
    try:
        delivery = check.execute(select(MailDelivery)).scalar_one()
        assert delivery.event_time_text == "2026-11-03 14:00–15:00(UTC+11:00)"
    finally:
        check.close()
