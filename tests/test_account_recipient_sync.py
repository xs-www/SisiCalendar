"""账号收件人自动同步 + 邮件按可见范围投递。

需求(用户反馈):共享日历里日程变动时,没法按家庭可见范围把这次变更同步到
**范围内用户已认证的邮箱**,「设置里的同步邮箱」成了唯一出口。本轮固定住四件事:

1. 账号邮箱是**派生**的:启用 + 已验证 → 一定有一条启用的账号收件人,
   历史库里被删掉/停用过的行会在发布前与启动时自动补回来;
2. 设置页只维护**外部邮箱**,账号邮箱既不能在那里删除也不能在那里停用(409);
3. 发信按**这次改动落在谁的可见范围里**筛选:看不见这条日程的人不发信;
4. 手动群发(向当前人员发送最新版 / 重建 ICS / CLI 导出)拿不到具体日历,仍然发给所有人。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app import db as db_module
from app import mailqueue
from app.accounts import (
    create_user,
    sync_account_recipients,
    sync_user_recipient,
    update_profile,
)
from app.bootstrap import ensure_family_setup
from app.config import Settings
from app.events import create_event
from app.mailqueue import ensure_delivery_tasks
from app.models import CHANGE_CREATED, CHANGE_MANUAL, MailDelivery, SyncRecipient
from app.schemas import EventWrite
from app.sync_service import publish
from app.timeutil import utcnow
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
    captured: list[dict] = []

    def fake_send(config, message):  # noqa: ANN001
        attachment = None
        for part in message.iter_attachments():
            attachment = {
                "filename": part.get_filename(),
                "payload": part.get_payload(decode=True),
            }
        captured.append(
            {
                "to": message["To"],
                "body": message.get_body(preferencelist=("plain",)).get_content(),
                "attachment": attachment,
            }
        )

    monkeypatch.setattr(mailqueue, "send_message", fake_send)
    return captured


def _add_event(session, settings, *, title: str, calendar_id: str, user_id: str | None = None):
    event = create_event(
        session,
        EventWrite(
            title=title,
            start_at="2026-12-01T09:00:00",
            end_at="2026-12-01T10:00:00",
            timezone="UTC",
        ),
        settings=settings,
        calendar_id=calendar_id,
        user_id=user_id,
    )
    session.commit()
    return event


def _add_external(session, email: str, *, scope_user_id: str | None = None) -> None:
    session.add(
        SyncRecipient(
            email=email,
            enabled=True,
            scope_user_id=scope_user_id,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )
    session.commit()


def _delivered(session) -> set[str]:
    return {
        row.recipient_email for row in session.execute(select(MailDelivery)).scalars()
    }


def _csv(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "页面缺少 csrf_token"
    return match.group(1)


def _data_csrf(client: TestClient) -> str:
    page = client.get("/admin")
    assert page.status_code == 200, page.text
    match = re.search(r'data-csrf="([^"]+)"', page.text)
    assert match, "日历页缺少 data-csrf"
    return match.group(1)


# --------------------------------------------------------------------------
# 账号收件人是派生结果:能自愈
# --------------------------------------------------------------------------
def test_publish_restores_a_deleted_account_recipient(
    session, settings, family, add_member, mail_settings
) -> None:
    """账号收件人行被历史操作删掉后,下一次发布自动补回来并正常收信。"""
    mama = add_member("mama", email="mama@example.test")
    row = session.execute(
        select(SyncRecipient).where(SyncRecipient.user_id == mama.id)
    ).scalar_one()
    session.delete(row)
    session.commit()
    assert session.execute(select(SyncRecipient)).scalars().all() == []

    event = _add_event(
        session,
        settings,
        title="家庭聚餐",
        calendar_id=family.shared_id,
        user_id=family.owner_id,
    )
    outcome = publish(session, settings=mail_settings, change_type=CHANGE_CREATED, event=event)
    assert outcome.ok, outcome.error

    restored = session.execute(
        select(SyncRecipient).where(SyncRecipient.user_id == mama.id)
    ).scalar_one()
    assert restored.email == "mama@example.test"
    assert restored.enabled is True
    assert Path(
        session.execute(select(MailDelivery)).scalar_one().snapshot_path
    ).name.endswith(f"user-{mama.id}-upcoming.ics")


def test_publish_reactivates_a_disabled_account_recipient(
    session, settings, family, add_member, mail_settings
) -> None:
    """被停用过的账号收件人行同样会被恢复:账号邮箱不受「设置页停用」影响。"""
    mama = add_member("mama", email="mama@example.test")
    row = session.execute(
        select(SyncRecipient).where(SyncRecipient.user_id == mama.id)
    ).scalar_one()
    row.enabled = False
    session.commit()

    event = _add_event(
        session,
        settings,
        title="家庭聚餐",
        calendar_id=family.shared_id,
        user_id=family.owner_id,
    )
    publish(session, settings=mail_settings, change_type=CHANGE_CREATED, event=event)

    assert row.enabled is True
    assert _delivered(session) == {"mama@example.test"}


def test_disabled_account_stops_receiving(session, settings, family, add_member) -> None:
    """停用账号 = 停止收信;重新启用后自动恢复。"""
    from app.accounts import set_user_enabled

    mama = add_member("mama", email="mama@example.test")
    set_user_enabled(session, mama, False)
    assert sync_account_recipients(session) >= 1
    row = session.execute(
        select(SyncRecipient).where(SyncRecipient.user_id == mama.id)
    ).scalar_one()
    assert row.enabled is False

    set_user_enabled(session, mama, True)
    assert sync_account_recipients(session) >= 1
    assert row.enabled is True


def test_bootstrap_repairs_and_commits_a_missing_account_recipient(
    session, settings, family, add_member
) -> None:
    """启动引导的修复必须落库,不能只留在内存里(另一个会话要能看见)。"""
    mama = add_member("mama", email="mama@example.test")
    row = session.execute(
        select(SyncRecipient).where(SyncRecipient.user_id == mama.id)
    ).scalar_one()
    session.delete(row)
    session.commit()

    ensure_family_setup(session, settings)  # 不额外 commit,由引导自己保证

    other = db_module.get_session_factory()()
    try:
        found = other.execute(
            select(SyncRecipient).where(SyncRecipient.user_id == mama.id)
        ).scalar_one_or_none()
        assert found is not None, "引导里的账号收件人修复没有提交"
        assert found.enabled is True
    finally:
        other.close()


# --------------------------------------------------------------------------
# 发信按「这次改动落在谁的可见范围里」筛选
# --------------------------------------------------------------------------
def test_shared_calendar_change_mails_every_in_scope_verified_account(
    session, settings, family, add_member, mail_settings
) -> None:
    """共享日历变动 → 范围内**已认证**的账号邮箱全部收到;没验证的不发信。"""
    add_member("mama", email="mama@example.test")
    papa = add_member("papa", email="papa@example.test", verified=False)
    _add_external(session, "granny@example.test")

    event = _add_event(
        session,
        settings,
        title="家庭聚餐",
        calendar_id=family.shared_id,
        user_id=family.owner_id,
    )
    publish(session, settings=mail_settings, change_type=CHANGE_CREATED, event=event)

    assert _delivered(session) == {"mama@example.test", "granny@example.test"}
    assert (
        session.execute(
            select(SyncRecipient).where(SyncRecipient.user_id == papa.id)
        ).scalar_one_or_none()
        is None
    ), "邮箱未验证的账号不能成为收件人(不变量 22)"

    mailqueue.process_queue(session, settings=mail_settings)
    session.commit()


def test_personal_calendar_change_skips_out_of_scope_recipients(
    session, settings, family, add_member, mail_settings, sent
) -> None:
    """改动落在管理员的私人日历里 → 看不见它的成员与外部邮箱都不收信。"""
    owner = family.owner
    update_profile(session, owner, email="admin@example.test")
    owner.email_verified_at = utcnow()
    sync_user_recipient(session, owner)
    session.commit()

    add_member("mama", email="mama@example.test")
    _add_external(session, "granny@example.test")
    _add_external(session, "legacy@example.test", scope_user_id=owner.id)

    event = _add_event(
        session,
        settings,
        title="爸爸的私事",
        calendar_id=family.personal_id,
        user_id=owner.id,
    )
    publish(session, settings=mail_settings, change_type=CHANGE_CREATED, event=event)

    # 只有「看得见这条私人日程」的收件人:管理员自己的账号邮箱 + 绑定了他范围的历史邮箱
    assert _delivered(session) == {"admin@example.test", "legacy@example.test"}

    mailqueue.process_queue(session, settings=mail_settings)
    session.commit()
    assert {entry["to"] for entry in sent} == {"admin@example.test", "legacy@example.test"}
    for entry in sent:
        assert "爸爸的私事" in entry["attachment"]["payload"].decode("utf-8")


def test_multiple_changed_calendars_notify_every_affected_scope(
    session, settings, family, add_member, mail_settings
) -> None:
    """事件在两条日历之间移动:新旧两边的可见者都要收到通知。"""
    add_member("mama", email="mama@example.test")
    _add_external(session, "granny@example.test")

    event = _add_event(
        session,
        settings,
        title="搬家的日程",
        calendar_id=family.personal_id,
        user_id=family.owner_id,
    )
    publish(
        session,
        settings=mail_settings,
        change_type=CHANGE_CREATED,
        event=event,
        changed_calendar_ids=[family.personal_id, family.shared_id],
    )

    assert _delivered(session) == {"mama@example.test", "granny@example.test"}


def test_manual_sync_ignores_the_scope_filter(
    session, settings, family, add_member, mail_settings
) -> None:
    """手动群发没有「具体改动的日历」:仍然发给全部启用收件人。"""
    add_member("mama", email="mama@example.test")
    _add_external(session, "granny@example.test")
    _add_event(
        session,
        settings,
        title="爸爸的私事",
        calendar_id=family.personal_id,
        user_id=family.owner_id,
    )

    outcome = publish(session, settings=mail_settings, change_type=CHANGE_MANUAL)
    assert outcome.ok, outcome.error
    assert _delivered(session) == {"mama@example.test", "granny@example.test"}


def test_explicit_calendar_filter_still_applies_to_manual_calls(
    session, settings, family, add_member, mail_settings
) -> None:
    """`ensure_delivery_tasks` 拿到日历集合时,范围外的收件人一个任务都不建。"""
    from app.accounts import personal_calendar
    from app.exporter import export_calendar

    mama = add_member("mama", email="mama@example.test")
    _add_external(session, "granny@example.test")
    version = export_calendar(session, settings=mail_settings).version
    session.commit()

    created = ensure_delivery_tasks(
        session,
        export_version=version,
        change_type=CHANGE_MANUAL,
        changed_calendar_ids=[personal_calendar(session, mama).id],
        settings=mail_settings,
    )
    session.commit()
    assert created == 1, "只有妈妈的账号邮箱看得见这条日历"
    assert _delivered(session) == {"mama@example.test"}


def test_account_email_reaches_its_own_scope_only(
    session, settings, family, add_member, mail_settings, sent
) -> None:
    """成员自己的私人日历变动 → 只有他自己收到,附件里也只有他自己的日程。"""
    from app.accounts import personal_calendar

    mama = add_member("mama", email="mama@example.test")
    _add_external(session, "granny@example.test")

    event = _add_event(
        session,
        settings,
        title="妈妈的瑜伽",
        calendar_id=personal_calendar(session, mama).id,
        user_id=mama.id,
    )
    publish(session, settings=mail_settings, change_type=CHANGE_CREATED, event=event)

    assert _delivered(session) == {"mama@example.test"}
    mailqueue.process_queue(session, settings=mail_settings)
    session.commit()
    assert "妈妈的瑜伽" in sent[0]["attachment"]["payload"].decode("utf-8")


# --------------------------------------------------------------------------
# 设置页:账号邮箱只读,外部邮箱才可维护
# --------------------------------------------------------------------------
def _member_with_email(app_session, username: str, email: str, display_name: str = "成员"):
    member = create_user(
        app_session,
        username=username,
        password="member-password",
        display_name=display_name,
        email=email,
        settings=Settings(),
    )
    member.email_verified_at = utcnow()
    sync_user_recipient(app_session, member)
    app_session.commit()
    return member


def _account_row(app_session, member) -> SyncRecipient:
    return app_session.execute(
        select(SyncRecipient).where(SyncRecipient.user_id == member.id)
    ).scalar_one()


def test_settings_page_cannot_remove_or_disable_an_account_recipient(
    admin_client: TestClient, app_session, app_family
) -> None:
    member = _member_with_email(app_session, "sisi", "sisi@example.test")
    row = _account_row(app_session, member)
    csrf = _csv(admin_client.get("/admin/settings").text)

    for action in ("toggle", "delete"):
        response = admin_client.post(
            f"/admin/partials/recipients/{row.id}/{action}",
            data={"csrf_token": csrf},
        )
        assert response.status_code == 409, response.text
        assert "账号邮箱" in response.text

    app_session.expire_all()
    still = _account_row(app_session, member)
    assert still.enabled is True, "账号邮箱不能被设置页停用"


def test_recipients_partial_lists_account_mail_as_read_only(
    admin_client: TestClient, app_session, app_family
) -> None:
    """账号邮箱出现在只读列表里,但不带任何增删改操作。"""
    member = _member_with_email(app_session, "sisi", "sisi@example.test", "思思")
    row = _account_row(app_session, member)
    csrf = _csv(admin_client.get("/admin/settings").text)
    added = admin_client.post(
        "/admin/partials/recipients",
        data={
            "csrf_token": csrf,
            "email": "granny@example.test",
            "display_name": "外婆",
            "acknowledge": "1",
        },
    )
    assert added.status_code == 200, added.text

    page = admin_client.get("/admin/partials/recipients")
    assert page.status_code == 200, page.text
    text = page.text
    assert "granny@example.test" in text, "外部邮箱要能在设置页维护"
    assert "sisi@example.test" in text, "账号邮箱要在只读列表里可见"
    assert "思思" in text
    assert "自动同步中" in text
    assert f"/admin/partials/recipients/{row.id}/delete" not in text
    assert f"/admin/partials/recipients/{row.id}/toggle" not in text


def test_api_rejects_disabling_an_account_recipient(
    admin_client: TestClient, app_session, app_family
) -> None:
    member = _member_with_email(app_session, "sisi", "sisi@example.test")
    row = _account_row(app_session, member)
    response = admin_client.patch(
        f"/api/v1/sync-recipients/{row.id}",
        json={"enabled": False},
        headers={"X-CSRF-Token": _data_csrf(admin_client)},
    )
    assert response.status_code == 409, response.text
    assert "账号邮箱" in response.text


def test_event_move_hands_both_calendars_to_publish(
    admin_client: TestClient, app_session, app_family, monkeypatch: pytest.MonkeyPatch
) -> None:
    """日历间移动事件时,新旧两条日历都要参与「通知谁」的判定。"""
    import app.routers.api_events as api_events

    seen: list[list[str] | None] = []
    real_publish = api_events.publish

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        seen.append(list(kwargs.get("changed_calendar_ids") or []))
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(api_events, "publish", spy)
    csrf = _data_csrf(admin_client)

    created = admin_client.post(
        "/api/v1/events",
        json={
            "title": "搬家的日程",
            "start_at": "2026-12-09T09:00:00",
            "end_at": "2026-12-09T10:00:00",
            "timezone": "UTC",
            "calendar_id": app_family.shared_id,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    assert seen[-1] == [app_family.shared_id]

    moved = admin_client.patch(
        f"/api/v1/events/{created.json()['id']}",
        json={
            "title": "搬家的日程",
            "start_at": "2026-12-09T09:00:00",
            "end_at": "2026-12-09T10:00:00",
            "timezone": "UTC",
            "calendar_id": app_family.personal_id,
            "expected_updated_at": created.json()["updated_at"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert moved.status_code == 200, moved.text
    assert set(seen[-1] or []) == {app_family.shared_id, app_family.personal_id}
