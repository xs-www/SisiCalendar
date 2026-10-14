"""同步邮件里的「发起人」:谁改的日程,邮件就说谁。

需求(用户反馈):同步邮件只说「本次操作:新增日程」,收信的家人看不出**是谁**改的。
这里守住四条:

1. 具体日程的增删改(网页/API/快捷指令)**都**标出账号显示名,显示名优先于用户名;
2. 「手动同步」(重新生成、向当前人员发送最新版)是管理员主动群发,**不**出现发起人一行;
3. 认不出发起人时正文写「未记录」,不编造名字;
4. 发起人在**建任务那一刻**固化:重试发信时不会改用「现在登录的人」,
   账号被删除后也仍能说明当时是谁操作的(正文用显示名快照)。
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from app import db as db_module
from app import mailqueue
from app.accounts import (
    create_user,
    delete_user,
    get_user_by_username,
    owner_user,
    set_membership,
    sync_user_recipient,
)
from app.config import Settings, reset_settings_cache
from app.models import MEMBER_ROLE_EDITOR, MailDelivery, SyncRecipient
from app.timeutil import utcnow
from fastapi.testclient import TestClient
from sqlalchemy import select, text

SMTP_HOST = "smtp.example.test"
SMTP_FROM = "bot@example.test"

MEMBER_USERNAME = "sisi"
MEMBER_PASSWORD = "member-password"
MEMBER_LABEL = "思思"
MEMBER_EMAIL = "sisi@example.test"


# --------------------------------------------------------------------------
# 夹具:配好 SMTP 的应用实例 + 捕获日历邮件
# --------------------------------------------------------------------------
@pytest.fixture()
def mail_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """在 `settings` 清空环境变量之后再注入 SMTP 配置(否则不会建发信任务)。"""
    monkeypatch.setenv("SMTP_HOST", SMTP_HOST)
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_SECURITY", "starttls")
    monkeypatch.setenv("SMTP_FROM_ADDRESS", SMTP_FROM)
    monkeypatch.setenv("SMTP_FROM_NAME", "Sisi Calendar")
    reset_settings_cache()
    return Settings()


@pytest.fixture()
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """拦截 `app/mailqueue.py` 里的发送函数。

    必须打在 `mailqueue` 上:`from app.mailer import send_message` 在导入时就绑定了引用,
    只 patch `app.mailer` 拦不到日历邮件。
    """
    captured: list[dict] = []

    def fake_send(config, message):  # noqa: ANN001
        captured.append(
            {
                "to": message["To"],
                "subject": message["Subject"],
                "body": message.get_body(preferencelist=("plain",)).get_content(),
            }
        )

    monkeypatch.setattr(mailqueue, "send_message", fake_send)
    return captured


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """领域层用例用的收信记录(与 `tests/test_mail.py` 的 `sent` 同形)。"""
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
                "subject": message["Subject"],
                "body": message.get_body(preferencelist=("plain",)).get_content(),
                "attachment": attachment,
            }
        )

    monkeypatch.setattr(mailqueue, "send_message", fake_send)
    return captured


@pytest.fixture()
def mail_settings(settings: Settings) -> Settings:
    """配好 SMTP 的配置对象,给直接调领域函数的用例(不走 HTTP)。"""
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
def mail_client(mail_env: Settings) -> Iterator[TestClient]:
    from app.main import create_app

    application = create_app(Settings())
    with TestClient(application, follow_redirects=False) as test_client:
        yield test_client
    db_module.dispose_engine()
    reset_settings_cache()


@pytest.fixture()
def app_session(mail_client: TestClient) -> Iterator[object]:
    factory = db_module.get_session_factory()
    db_session = factory()
    try:
        yield db_session
    finally:
        db_session.rollback()
        db_session.close()


def _csrf(client: TestClient) -> str:
    page = client.get("/admin")
    assert page.status_code == 200, page.text
    match = re.search(r'data-csrf="([^"]+)"', page.text)
    assert match, "日历页缺少 data-csrf"
    return match.group(1)


def _hidden_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "页面缺少 csrf_token"
    return match.group(1)


def _login(client: TestClient, username: str, password: str) -> None:
    page = client.get("/login")
    assert page.status_code == 200
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": _hidden_csrf(page.text),
            "next": "/admin",
        },
    )
    assert response.status_code == 303, response.text


def _admin_login(client: TestClient) -> None:
    _login(client, "admin", "test-password")


def _add_sisi(app_session, app_family):
    """建一个已验证、可写共享日历的成员,并让他成为账号收件人。"""
    from app.accounts import list_shared_calendars

    shared = list_shared_calendars(app_session)[0]
    member = create_user(
        app_session,
        username=MEMBER_USERNAME,
        password=MEMBER_PASSWORD,
        display_name=MEMBER_LABEL,
        email=MEMBER_EMAIL,
        settings=Settings(),
    )
    member.email_verified_at = utcnow()
    set_membership(app_session, shared, member, role=MEMBER_ROLE_EDITOR)
    sync_user_recipient(app_session, member)
    app_session.commit()
    return member


def _add_recipient(session, email: str) -> None:
    """预置一个外部收件人:没有收件人时根本不会建发信任务。"""
    session.add(
        SyncRecipient(email=email, enabled=True, created_at=utcnow(), updated_at=utcnow())
    )
    session.commit()


def _last_delivery(session) -> MailDelivery | None:
    return (
        session.execute(
            select(MailDelivery).order_by(
                MailDelivery.created_at.desc(), MailDelivery.id.desc()
            )
        )
        .scalars()
        .first()
    )


def _delivery_for(session, email: str) -> MailDelivery | None:
    return (
        session.execute(
            select(MailDelivery)
            .where(MailDelivery.recipient_email == email)
            .order_by(MailDelivery.created_at.desc(), MailDelivery.id.desc())
        )
        .scalars()
        .first()
    )


def _last_delivery(session) -> MailDelivery | None:
    return (
        session.execute(
            select(MailDelivery).order_by(
                MailDelivery.created_at.desc(), MailDelivery.id.desc()
            )
        )
        .scalars()
        .first()
    )


def _create_event(client: TestClient, **extra) -> dict:
    payload = {
        "title": "全家体检",
        "start_at": "2026-12-01T09:00:00",
        "end_at": "2026-12-01T10:00:00",
        "timezone": "UTC",
    }
    payload.update(extra)
    response = client.post(
        "/api/v1/events", json=payload, headers={"X-CSRF-Token": _csrf(client)}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# 正文:谁改的就写谁
# --------------------------------------------------------------------------
def test_created_event_mail_names_the_admin(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    """管理员在网页/API 里新建日程 → 正文有「发起人:家庭管理员」。"""
    _add_recipient(app_session, "family@example.test")
    _admin_login(mail_client)
    _create_event(mail_client, title="全家体检", calendar_id=app_family.shared_id)

    delivery = _last_delivery(app_session)
    assert delivery is not None
    assert delivery.actor_label == app_family.owner.label
    assert delivery.actor_user_id == app_family.owner_id

    mailqueue.process_queue(app_session, settings=Settings())
    app_session.commit()
    body = outbox[-1]["body"]
    assert f"发起人:{app_family.owner.label}" in body
    assert "本次操作:新增日程" in body


def test_member_edit_mail_names_the_member_display_name(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    """成员(editor)改共享日历里的日程 → 正文写**成员**的显示名,不是用户名。"""
    _add_sisi(app_session, app_family)
    _admin_login(mail_client)
    created = _create_event(
        mail_client, title="家庭聚餐", calendar_id=app_family.shared_id
    )
    app_session.expire_all()

    # 换身份登录同一浏览器:清掉会话 Cookie(相当于退出登录)
    mail_client.cookies.clear()
    _login(mail_client, MEMBER_USERNAME, MEMBER_PASSWORD)
    edited = mail_client.patch(
        f"/api/v1/events/{created['id']}",
        json={
            "title": "家庭聚餐(改到晚上七点)",
            "start_at": "2026-12-02T19:00:00",
            "end_at": "2026-12-02T21:00:00",
            "timezone": "UTC",
            "calendar_id": app_family.shared_id,
            "expected_updated_at": created["updated_at"],
        },
        headers={"X-CSRF-Token": _csrf(mail_client)},
    )
    assert edited.status_code == 200, edited.text

    delivery = _last_delivery(app_session)
    assert delivery is not None
    assert delivery.actor_label == MEMBER_LABEL
    assert delivery.actor_label != MEMBER_USERNAME
    # 关键区分:事件是**管理员**建的,但这次改动是思思做的 —— 发起人是「谁在操作」,
    # 不是「事件是谁建的」,所以正文里不能出现管理员的名字
    assert delivery.actor_user_id != app_family.owner_id
    assert delivery.actor_label != app_family.owner.label

    mailqueue.process_queue(app_session, settings=Settings())
    app_session.commit()
    body = outbox[-1]["body"]
    assert f"发起人:{MEMBER_LABEL}" in body
    assert f"发起人:{app_family.owner.label}" not in body
    assert "本次操作:修改日程" in body


def test_deleted_event_mail_names_the_operator(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    """删除日程也要写出是谁删的(误删时最容易需要这条信息)。"""
    _add_recipient(app_session, "family@example.test")
    _admin_login(mail_client)
    created = _create_event(mail_client, title="待删除的会", calendar_id=app_family.shared_id)

    removed = mail_client.delete(
        f"/api/v1/events/{created['id']}",
        headers={"X-CSRF-Token": _csrf(mail_client)},
    )
    assert removed.status_code == 200, removed.text

    delivery = _last_delivery(app_session)
    assert delivery is not None
    assert delivery.actor_label == app_family.owner.label
    mailqueue.process_queue(app_session, settings=Settings())
    app_session.commit()
    body = outbox[-1]["body"]
    assert f"发起人:{app_family.owner.label}" in body
    assert "本次操作:删除日程" in body


def test_agent_mail_names_the_token_owner(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, outbox
) -> None:
    """快捷指令/Agent 写进来的日程:发起人 = 令牌绑定的账号。"""
    from app.accounts import list_shared_calendars
    from app.main import create_app

    monkeypatch.setenv("SMTP_HOST", SMTP_HOST)
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_SECURITY", "starttls")
    monkeypatch.setenv("SMTP_FROM_ADDRESS", SMTP_FROM)
    monkeypatch.setenv("AGENT_API_TOKEN", "test-agent-token-abcdefghijklmnop")
    reset_settings_cache()
    application = create_app(Settings())
    try:
        with TestClient(application, follow_redirects=False) as client:
            session = db_module.get_session_factory()()
            try:
                _add_recipient(session, "family@example.test")
                # 外部收件人只收家庭共享日历:日程要落在共享日历里才会通知到它
                shared_id = list_shared_calendars(session)[0].id
                response = client.post(
                    "/api/agent/events",
                    json={
                        "title": "快捷指令建的会",
                        "start_at": "2026-12-04T09:00:00",
                        "end_at": "2026-12-04T10:00:00",
                        "timezone": "UTC",
                        "calendar_id": shared_id,
                    },
                    headers={"Authorization": "Bearer test-agent-token-abcdefghijklmnop"},
                )
                assert response.status_code == 201, response.text
                owner = owner_user(session)
                delivery = _last_delivery(session)
                assert delivery is not None and owner is not None
                assert delivery.actor_label == owner.label
            finally:
                session.close()
    finally:
        db_module.dispose_engine()
        reset_settings_cache()


# --------------------------------------------------------------------------
# 手动同步:队列里记,正文里不写
# --------------------------------------------------------------------------
def test_manual_sync_records_actor_but_never_puts_it_in_the_body(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    """「向当前人员发送最新版」是管理员主动群发,正文不出现发起人一行。"""
    _add_sisi(app_session, app_family)
    app_session.expire_all()
    _admin_login(mail_client)
    admin_page = mail_client.get("/admin/sync")
    assert admin_page.status_code == 200, admin_page.text
    response = mail_client.post(
        "/admin/actions/send-latest",
        data={"csrf_token": _hidden_csrf(admin_page.text), "confirm": "1"},
    )
    assert response.status_code == 200, response.text

    delivery = _last_delivery(app_session)
    assert delivery is not None
    assert delivery.change_type == "manual"
    # 队列里仍然记着是谁点的(备查),但正文不写
    assert delivery.actor_label == app_family.owner.label

    body = outbox[-1]["body"]
    assert "本次操作:手动同步" in body
    assert "发起人" not in body
    # 没有任何日程变更时也不得凭空写「未记录」
    assert "未记录" not in body


def test_legacy_task_without_actor_says_unrecorded(
    session, settings: Settings, mail_settings, sent
) -> None:
    """升级前的历史任务没有发起人信息:正文写「未记录」,不编造名字。"""
    from app.events import create_event
    from app.exporter import export_calendar
    from app.mailqueue import ensure_delivery_tasks, process_queue
    from app.models import CHANGE_CREATED
    from app.schemas import EventWrite

    session.add(
        SyncRecipient(
            email="legacy@example.test", enabled=True, created_at=utcnow(), updated_at=utcnow()
        )
    )
    create_event(
        session,
        EventWrite(
            title="历史日程",
            start_at="2026-12-08T09:00:00",
            end_at="2026-12-08T10:00:00",
            timezone="UTC",
        ),
        settings=mail_settings,
    )
    session.commit()
    version = export_calendar(session, settings=mail_settings).version
    ensure_delivery_tasks(
        session,
        export_version=version,
        change_type=CHANGE_CREATED,
        event_title="历史日程",
        settings=mail_settings,
    )
    session.commit()
    process_queue(session, settings=mail_settings)
    session.commit()

    body = sent[-1]["body"]
    assert "发起人:未记录" in body


# --------------------------------------------------------------------------
# 管理面:队列与接口都能看到发起人
# --------------------------------------------------------------------------
def test_queue_page_and_api_expose_the_actor(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    """`/admin/sync` 的队列表格与 `/api/v1/mail-deliveries` 都要能看到发起人。"""
    _add_recipient(app_session, "family@example.test")
    _admin_login(mail_client)
    _create_event(mail_client, title="队列里要看得见", calendar_id=app_family.shared_id)

    sync_page = mail_client.get("/admin/sync")
    assert sync_page.status_code == 200, sync_page.text
    assert "发起人" in sync_page.text
    assert app_family.owner.label in sync_page.text

    api = mail_client.get("/api/v1/mail-deliveries")
    assert api.status_code == 200, api.text
    items = api.json()["items"]
    assert items
    assert items[0]["actor_label"] == app_family.owner.label


def test_queue_page_renders_legacy_tasks_without_an_actor(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    """升级前的历史任务没有发起人:队列显示占位符,不得报错或渲染出 None。"""
    _add_recipient(app_session, "family@example.test")
    _admin_login(mail_client)
    _create_event(mail_client, title="历史任务", calendar_id=app_family.shared_id)

    delivery = _last_delivery(app_session)
    assert delivery is not None
    delivery.actor_user_id = None
    delivery.actor_label = None
    app_session.commit()

    sync_page = mail_client.get("/admin/sync")
    assert sync_page.status_code == 200, sync_page.text
    assert "None" not in sync_page.text


# --------------------------------------------------------------------------
# 发起人固化:重试与删账号
# --------------------------------------------------------------------------
def test_actor_is_frozen_with_the_task_and_survives_account_deletion(
    session, settings: Settings, family, add_member, mail_settings, sent
) -> None:
    """任务建好后删掉发起人账号:邮件照发,正文用显示名快照,不留悬空 id。"""
    from app.exporter import export_calendar
    from app.mailqueue import ensure_delivery_tasks, process_queue
    from app.models import CHANGE_CREATED, SyncRecipient

    # 外部收件人不受删账号影响,用它验证「这封邮件仍然发得出去」
    session.add(
        SyncRecipient(
            email="family@example.test", enabled=True, created_at=utcnow(), updated_at=utcnow()
        )
    )
    member = add_member(MEMBER_USERNAME, email=MEMBER_EMAIL, display_name=MEMBER_LABEL)
    _add_event(session, settings, title="思思建的会", calendar_id=family.shared_id)

    version = export_calendar(session, settings=mail_settings).version
    ensure_delivery_tasks(
        session,
        export_version=version,
        change_type=CHANGE_CREATED,
        event_title="思思建的会",
        actor_user_id=member.id,
        actor_label=MEMBER_LABEL,
        settings=mail_settings,
    )
    session.commit()

    victim = get_user_by_username(session, MEMBER_USERNAME)
    assert victim is not None
    delete_user(session, victim)
    session.commit()
    session.expire_all()

    delivery = _delivery_for(session, "family@example.test")
    assert delivery is not None
    assert delivery.actor_user_id is None, "删账号后不得留下悬空的发起人 id"
    assert delivery.actor_label == MEMBER_LABEL, "显示名快照保留,历史邮件仍说得清是谁"

    process_queue(session, settings=mail_settings)
    session.commit()
    assert f"发起人:{MEMBER_LABEL}" in sent[-1]["body"]
    assert delivery.status == "sent"


def test_actor_name_follows_the_current_display_name(
    session, settings: Settings, family, add_member, mail_settings, sent
) -> None:
    """`actor_user_id` 存在时按现在的显示名渲染:改了显示名,没发的邮件也跟着改。

    显示名快照只是兜底:只要账号还在,正文就用**此刻**的名字,所以改名会立刻生效。
    """
    from app.exporter import export_calendar
    from app.mailqueue import ensure_delivery_tasks, process_queue
    from app.models import CHANGE_CREATED

    member = add_member(MEMBER_USERNAME, email=MEMBER_EMAIL, display_name=MEMBER_LABEL)
    _add_event(session, settings, title="改名前的会", calendar_id=family.shared_id)

    version = export_calendar(session, settings=mail_settings).version
    ensure_delivery_tasks(
        session,
        export_version=version,
        change_type=CHANGE_CREATED,
        event_title="改名前的会",
        actor_user_id=member.id,
        actor_label="旧名字",
        settings=mail_settings,
    )
    session.commit()

    # 直接改库里的显示名:这里要验的是**发信时现查账号**,不是资料接口的行为
    session.execute(
        text("UPDATE users SET display_name = :name WHERE id = :uid"),
        {"name": "新名字", "uid": member.id},
    )
    session.commit()
    session.expire_all()

    process_queue(session, settings=mail_settings)
    session.commit()
    assert "发起人:新名字" in sent[-1]["body"]
    assert "旧名字" not in sent[-1]["body"]


def _add_event(session, settings, *, title: str, calendar_id: str):
    from app.events import create_event
    from app.schemas import EventWrite

    event = create_event(
        session,
        EventWrite(
            title=title,
            start_at="2026-12-08T09:00:00",
            end_at="2026-12-08T10:00:00",
            timezone="UTC",
        ),
        settings=settings,
        calendar_id=calendar_id,
    )
    session.commit()
    return event
