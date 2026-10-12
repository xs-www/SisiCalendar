"""家庭版邮件与远程 Agent 的作用域测试。

邮件部分验证「每人只收到自己可见范围里尚未结束的日程」:账号收件人收到个人 + 共享,
外部邮箱只收到家庭共享,任何一封邮件都不得包含别人的私人日程。
Agent 部分验证令牌绑定的账号与日历:越权读取返回 404,跨账号的 `client_ref` 返回 409。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from app import mailqueue
from app.accounts import (
    create_user,
    personal_calendar,
    set_membership,
    set_user_enabled,
    sync_user_recipient,
    update_profile,
)
from app.config import Settings
from app.events import create_event
from app.exporter import (
    FAMILY_TARGET_KEY,
    feed_path_for,
    feed_target_key,
    mail_snapshot_path_for,
    resolve_targets,
)
from app.mailqueue import ensure_delivery_tasks, process_queue
from app.models import CHANGE_CREATED, MEMBER_ROLE_EDITOR, Event, MailDelivery, SyncRecipient
from app.schemas import EventWrite
from app.settings_store import effective_settings
from app.sync_service import publish
from app.timeutil import utcnow
from app.token_store import create_token
from fastapi.testclient import TestClient
from sqlalchemy import select

AGENT_API = "/api/agent/events"


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


def _add_event(session, settings, *, title: str, calendar_id: str, user_id: str):
    return create_event(
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


def _ics_titles(payload: bytes) -> set[str]:
    titles: set[str] = set()
    for line in payload.decode("utf-8").splitlines():
        if line.startswith("SUMMARY:"):
            titles.add(line[len("SUMMARY:") :].strip())
    return titles


def _publish(session, settings, *, title: str = "事件", event=None) -> int:
    outcome = publish(
        session,
        settings=settings,
        change_type=CHANGE_CREATED,
        event=event,
        event_title=title,
    )
    assert outcome.ok, outcome.error
    session.commit()
    return int(outcome.version or 0)


# --------------------------------------------------------------------------
# 目标解析
# --------------------------------------------------------------------------
def test_resolve_targets_covers_each_enabled_user_and_family(
    session, settings, family, add_member
) -> None:
    mama = add_member("mama")
    papa = add_member("papa")
    targets = {target.key: target for target in resolve_targets(session)}
    assert FAMILY_TARGET_KEY in targets
    assert targets[FAMILY_TARGET_KEY].user_id is None
    assert targets[FAMILY_TARGET_KEY].calendar_ids == (family.shared_id,)
    assert f"user-{mama.id}" in targets
    assert f"user-{papa.id}" in targets
    # 每个账号的目标 = 自己的个人日历 + 共享日历
    assert set(targets[f"user-{mama.id}"].calendar_ids) == {
        personal_calendar(session, mama).id,
        family.shared_id,
    }


def test_disabled_user_is_dropped_from_targets(
    session, settings, family, add_member
) -> None:
    mama = add_member("mama")
    assert f"user-{mama.id}" in {target.key for target in resolve_targets(session)}
    set_user_enabled(session, mama, False)
    session.commit()
    assert f"user-{mama.id}" not in {target.key for target in resolve_targets(session)}


# --------------------------------------------------------------------------
# 邮件范围
# --------------------------------------------------------------------------
def test_external_recipient_only_gets_family_calendar(
    session, settings, family, add_member, mail_settings, sent
) -> None:
    add_member("mama")
    session.add(SyncRecipient(email="guest@example.test", enabled=True))
    shared_event = _add_event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    _add_event(
        session,
        settings,
        title="爸爸的私事",
        calendar_id=family.personal_id,
        user_id=family.owner_id,
    )
    session.commit()

    version = _publish(session, mail_settings, event=shared_event)
    process_queue(session, settings=mail_settings)
    session.commit()

    assert len(sent) == 1
    titles = _ics_titles(sent[0]["attachment"]["payload"])
    assert titles == {"家庭聚餐"}
    assert "家庭共享日历" in sent[0]["body"]
    assert "私人" not in sent[0]["body"] or "不含" in sent[0]["body"]

    delivery = session.execute(select(MailDelivery)).scalar_one()
    assert Path(delivery.snapshot_path).name == (
        f"calendar-v{version:06d}-{FAMILY_TARGET_KEY}-upcoming.ics"
    )


def test_account_recipient_gets_own_visible_scope(
    session, settings, family, add_member, mail_settings, sent
) -> None:
    """账号收件人:个人 + 共享都在自己的附件里,但看不到别人的私人日历。"""
    mama = add_member("mama", email="mama@example.test")
    papa = add_member("papa", email="papa@example.test")
    sync_user_recipient(session, mama)
    sync_user_recipient(session, papa)
    session.commit()

    shared_event = _add_event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    _add_event(
        session,
        settings,
        title="妈妈的瑜伽",
        calendar_id=personal_calendar(session, mama).id,
        user_id=mama.id,
    )
    _add_event(
        session,
        settings,
        title="爸爸的球赛",
        calendar_id=family.personal_id,
        user_id=family.owner_id,
    )
    session.commit()

    _publish(session, mail_settings, event=shared_event)
    process_queue(session, settings=mail_settings)
    session.commit()

    by_recipient = {entry["to"]: entry for entry in sent}
    assert set(by_recipient) == {"mama@example.test", "papa@example.test"}
    assert _ics_titles(by_recipient["mama@example.test"]["attachment"]["payload"]) == {
        "家庭聚餐",
        "妈妈的瑜伽",
    }
    # papa 没有自己的事件,只看到共享日历
    assert _ics_titles(by_recipient["papa@example.test"]["attachment"]["payload"]) == {
        "家庭聚餐",
    }
    for entry in sent:
        assert "爸爸的球赛" not in _ics_titles(entry["attachment"]["payload"])
        assert "爸爸的球赛" not in entry["attachment"]["payload"].decode("utf-8")


def test_owner_recipient_gets_his_own_fixed_address(
    session, settings, family, mail_settings, sent
) -> None:
    """账号收件人收到的是**自己**那条固定地址(由服务端密钥派生,永远不变)。"""
    from app import feed_tokens
    from app.accounts import owner_user

    owner = owner_user(session)
    assert owner is not None
    update_profile(session, owner, email="admin@example.test")
    owner.email_verified_at = utcnow()
    sync_user_recipient(session, owner)
    session.commit()

    event = _add_event(
        session, settings, title="全家出游", calendar_id=family.shared_id, user_id=owner.id
    )
    session.commit()

    _publish(session, mail_settings, event=event)
    process_queue(session, settings=mail_settings)
    session.commit()

    body = sent[0]["body"]
    fixed = feed_tokens.account_feed_token(mail_settings, owner.id)
    assert f"{mail_settings.public_base_url}/{fixed}.ics" in body
    assert "你可见的日历" in body
    # 升级前的无令牌地址彻底消失(它会把管理员的可见范围暴露给任何拿到地址的人)
    assert f"{mail_settings.public_base_url}\n" not in body


def test_legacy_external_recipient_keeps_owner_scope_attachment(
    session, settings, family, mail_settings, sent
) -> None:
    """单用户版带上来的历史邮箱:收信范围仍是首位管理员的可见范围。

    它**不是**账号收件人(`user_id` 为空),所以正文里既不能说成「你可见的日历」,
    也不该附带任何订阅地址(地址只发给账号本人)。
    """
    session.add(
        SyncRecipient(
            email="legacy@example.test", enabled=True, scope_user_id=family.owner_id
        )
    )
    session.commit()

    shared = _add_event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    _add_event(
        session,
        settings,
        title="爸爸的球赛",
        calendar_id=family.personal_id,
        user_id=family.owner_id,
    )
    session.commit()

    _publish(session, mail_settings, event=shared)
    process_queue(session, settings=mail_settings)
    session.commit()

    assert len(sent) == 1
    # 范围 = 首位管理员的个人 + 家庭共享(与升级前一致)
    assert _ics_titles(sent[0]["attachment"]["payload"]) == {"家庭聚餐", "爸爸的球赛"}
    assert "家庭管理员可见的日历" in sent[0]["body"]
    assert "你可见的日历" not in sent[0]["body"]
    assert "sisi_feed_" not in sent[0]["body"]


def test_member_recipient_gets_only_his_own_address(
    session, settings, family, add_member, mail_settings, sent
) -> None:
    """其他成员收到的是**他自己**的固定地址,绝不能出现别人的地址。"""
    from app import feed_tokens

    mama = add_member("mama", email="mama@example.test")
    sync_user_recipient(session, mama)
    session.commit()
    event = _add_event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    session.commit()

    _publish(session, mail_settings, event=event)
    process_queue(session, settings=mail_settings)
    session.commit()

    body = sent[0]["body"]
    mama_fixed = feed_tokens.account_feed_token(mail_settings, mama.id)
    owner_fixed = feed_tokens.account_feed_token(mail_settings, family.owner_id)
    assert f"{mail_settings.public_base_url}/{mama_fixed}.ics" in body
    assert owner_fixed not in body
    assert "你的固定订阅地址" in body


def test_each_recipient_attachment_comes_from_its_own_snapshot(
    session, settings, family, add_member, mail_settings
) -> None:
    mama = add_member("mama", email="mama@example.test")
    sync_user_recipient(session, mama)
    session.add(SyncRecipient(email="guest@example.test", enabled=True))
    session.commit()

    event = _add_event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    session.commit()
    version = _publish(session, mail_settings, event=event)

    names = {
        Path(delivery.snapshot_path).name
        for delivery in session.execute(select(MailDelivery)).scalars()
    }
    assert names == {
        mail_snapshot_path_for(
            mail_settings, version, target_key=feed_target_key(user_id=mama.id)
        ).name,
        mail_snapshot_path_for(
            mail_settings, version, target_key=FAMILY_TARGET_KEY
        ).name,
    }


def test_account_and_external_recipients_coexist_and_are_idempotent(
    session, settings, family, add_member, mail_settings
) -> None:
    mama = add_member("mama", email="mama@example.test")
    sync_user_recipient(session, mama)
    session.add(SyncRecipient(email="guest@example.test", enabled=True))
    session.commit()

    event = _add_event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    session.commit()
    version = publish(
        session, settings=mail_settings, change_type=CHANGE_CREATED, event=event
    ).version
    session.commit()

    assert ensure_delivery_tasks(
        session, export_version=version, change_type=CHANGE_CREATED, settings=mail_settings
    ) == 0, "同一版本对同一收件人只允许一个任务"
    assert len(session.execute(select(MailDelivery)).scalars().all()) == 2


def test_clearing_email_stops_delivery(
    session, settings, family, add_member, mail_settings, sent
) -> None:
    mama = add_member("mama", email="mama@example.test")
    sync_user_recipient(session, mama)
    session.commit()

    # 清空邮箱 → 账号收件人记录被删除
    update_profile(session, mama, email="")
    sync_user_recipient(session, mama)
    session.commit()
    assert session.execute(select(SyncRecipient)).scalars().all() == []

    event = _add_event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    session.commit()
    _publish(session, mail_settings, event=event)
    process_queue(session, settings=mail_settings)
    session.commit()
    assert sent == []


def test_feed_files_exist_for_every_target_after_export(
    session, settings, family, add_member, mail_settings
) -> None:
    mama = add_member("mama")
    _add_event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    session.commit()
    _publish(session, mail_settings)

    assert feed_path_for(mail_settings, user_id=None).exists()
    assert feed_path_for(mail_settings, user_id=mama.id).exists()
    assert feed_path_for(mail_settings, user_id=family.owner_id).exists()


def test_target_files_are_deterministic_across_wall_clock(
    session, settings, family, add_member, mail_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """按目标发布的文件同样只能取决于数据库状态,不能取决于导出时刻。

    并集有专门用例守着(不变量 3),但对外发布的其实是 `export/feeds/<目标键>.ics`;
    这里把时钟拨到第二天再导一次,每一份目标文件的字节都必须不变。
    """
    import app.exporter as exporter_module
    import app.ics as ics_module

    mama = add_member("mama", email="mama@example.test")
    sync_user_recipient(session, mama)
    _add_event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    _add_event(
        session,
        settings,
        title="爸爸的私事",
        calendar_id=family.personal_id,
        user_id=family.owner_id,
    )
    session.commit()

    from app.exporter import export_calendar

    export_calendar(session, settings=mail_settings)
    before = {path.name: path.read_bytes() for path in mail_settings.feed_dir.glob("*.ics")}
    assert before, "应当至少有一份目标文件"

    later = utcnow() + timedelta(days=1, seconds=7)
    monkeypatch.setattr(ics_module, "utcnow", lambda: later)
    monkeypatch.setattr(exporter_module, "utcnow", lambda: later)
    export_calendar(session, settings=mail_settings, force_new_version=True)

    after = {path.name: path.read_bytes() for path in mail_settings.feed_dir.glob("*.ics")}
    assert after == before, "目标文件的内容不得随导出时刻变化"


def test_mail_window_defaults_to_local_today(
    session, settings, family, mail_settings, sent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省窗口起点必须是**应用默认时区的今天**。

    回归用例:起点若用 UTC 的日期去套当地零点,UTC+10 这类时区在当地刚过午夜时
    「今天」会被算成前一天,于是「昨天」的全天事件重新出现在邮件附件里。
    """
    import app.events as events_module
    import app.exporter as exporter_module
    from app.events import build_range
    from app.timeutil import local_to_utc

    tz_name = mail_settings.default_timezone
    local_now = datetime(2026, 10, 8, 0, 30)  # 当地刚过午夜;此时 UTC 还是 10-07
    frozen = local_to_utc(local_now, tz_name)
    monkeypatch.setattr(events_module, "utcnow", lambda: frozen)
    monkeypatch.setattr(exporter_module, "utcnow", lambda: frozen)

    window = build_range(None, None, settings=mail_settings, default_days=2)
    assert window.start_date == local_now.date(), "缺省起点应当是当地的今天"
    assert window.start_utc == local_to_utc(datetime(2026, 10, 8, 0, 0), tz_name)

    session.add(SyncRecipient(email="guest@example.test", enabled=True))
    today = local_now.date()
    yesterday = today - timedelta(days=1)
    for title, start, end in (
        ("今天的全天安排", today, today + timedelta(days=1)),
        ("昨天的全天安排", yesterday, today),
    ):
        create_event(
            session,
            EventWrite(
                title=title,
                all_day=True,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                timezone="UTC",
            ),
            settings=settings,
            calendar_id=family.shared_id,
            user_id=family.owner_id,
        )
    session.commit()

    _publish(session, mail_settings)
    process_queue(session, settings=mail_settings)
    session.commit()

    payload = sent[0]["attachment"]["payload"].decode("utf-8")
    assert "今天的全天安排" in payload
    assert "昨天的全天安排" not in payload, "当地刚过午夜时,昨天的全天事件不得回流"


# --------------------------------------------------------------------------
# 远程 Agent 作用域
# --------------------------------------------------------------------------
def test_agent_token_writes_into_its_bound_calendar(
    client: TestClient, app_session, app_family
) -> None:
    member = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    member_calendar = personal_calendar(app_session, member)
    plaintext, _ = create_token(
        app_session, "妈妈的快捷指令", user_id=member.id, calendar_id=member_calendar.id
    )
    app_session.commit()

    response = client.post(
        AGENT_API,
        json={
            "title": "妈妈的待办",
            "start_at": "2026-12-02T09:00:00",
            "end_at": "2026-12-02T10:00:00",
        },
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["calendar_id"] == member_calendar.id
    assert response.json()["created_by_user_id"] == member.id


def test_agent_token_cannot_read_another_users_event(
    client: TestClient, app_session, app_family
) -> None:
    settings = effective_settings(app_session)
    member = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    private = _add_event(
        app_session,
        settings,
        title="爸爸的私事",
        calendar_id=app_family.personal_id,
        user_id=app_family.owner_id,
    )
    plaintext, _ = create_token(
        app_session, "妈妈的快捷指令", user_id=member.id, calendar_id=None
    )
    app_session.commit()

    response = client.get(
        f"{AGENT_API}/{private.id}",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 404


def test_agent_client_ref_is_scoped_per_account(
    client: TestClient, app_session, app_family
) -> None:
    """同一个 client_ref 只对自己账号幂等;别的账号用它必须被拒绝,且看不到事件内容。"""
    mama = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    mama_token, _ = create_token(app_session, "妈妈", user_id=mama.id, calendar_id=None)
    owner_token, _ = create_token(
        app_session, "爸爸", user_id=app_family.owner_id, calendar_id=None
    )
    app_session.commit()

    payload = {
        "title": "妈妈的私事",
        "start_at": "2026-12-03T09:00:00",
        "end_at": "2026-12-03T10:00:00",
        "client_ref": "mama-ref-1",
    }
    first = client.post(
        AGENT_API, json=payload, headers={"Authorization": f"Bearer {mama_token}"}
    )
    assert first.status_code == 201, first.text

    # 同一令牌重放:原样返回既有事件,不新建
    replay = client.post(
        AGENT_API, json=payload, headers={"Authorization": f"Bearer {mama_token}"}
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["id"] == first.json()["id"]

    # 换一个账号使用同一个 client_ref:拒绝,且不泄露事件内容
    conflict = client.post(
        AGENT_API, json=payload, headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"
    assert "妈妈的私事" not in conflict.text
    assert first.json()["id"] not in conflict.text

    # 目标事件仍然只属于妈妈的私人日历
    event = app_session.get(Event, first.json()["id"])
    assert event is not None and event.calendar_id == personal_calendar(app_session, mama).id


def test_agent_token_without_write_permission_is_rejected(
    client: TestClient, app_session, app_family
) -> None:
    """成员默认对共享日历只读:令牌即使指向共享日历也不能写。"""
    member = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    plaintext, _ = create_token(
        app_session, "妈妈的快捷指令", user_id=member.id, calendar_id=app_family.shared_id
    )
    app_session.commit()

    response = client.post(
        AGENT_API,
        json={"title": "越权", "start_at": "2026-12-04T09:00:00"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 403


def test_editor_token_can_write_shared_calendar(
    client: TestClient, app_session, app_family
) -> None:
    member = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    set_membership(app_session, app_family.shared, member, MEMBER_ROLE_EDITOR)
    plaintext, _ = create_token(
        app_session, "妈妈的快捷指令", user_id=member.id, calendar_id=app_family.shared_id
    )
    app_session.commit()

    response = client.post(
        AGENT_API,
        json={"title": "外婆生日", "start_at": "2026-12-05T18:00:00"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["calendar_id"] == app_family.shared_id


def test_legacy_env_agent_token_belongs_to_owner(
    agent_client: TestClient, agent_headers: dict[str, str], app_session, app_family
) -> None:
    """.env 里的兜底令牌归属首位管理员,事件落进他的个人日历。"""
    response = agent_client.post(
        AGENT_API,
        json={"title": "兜底令牌", "start_at": "2026-12-06T09:00:00"},
        headers=agent_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["calendar_id"] == app_family.personal_id


def test_agent_token_of_disabled_account_is_rejected(
    client: TestClient, app_session, app_family
) -> None:
    member = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    plaintext, _ = create_token(app_session, "妈妈", user_id=member.id, calendar_id=None)
    set_user_enabled(app_session, member, False)
    app_session.commit()

    response = client.post(
        AGENT_API,
        json={"title": "停用账号", "start_at": "2026-12-07T09:00:00"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 401