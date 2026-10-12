"""邮箱验证:注册必须填邮箱、验证码发信与校验、验证通过后邮件告知固定订阅地址。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from app import db as db_module
from app import invites
from app.accounts import create_user, get_user_by_username, owner_user, sync_user_recipient
from app.config import Settings, reset_settings_cache
from app.feed_tokens import account_feed_token
from app.models import SyncRecipient
from app.timeutil import utcnow
from fastapi.testclient import TestClient
from sqlalchemy import select

SMTP_HOST = "smtp.example.test"
SMTP_FROM = "bot@example.test"
CODE_PATTERN = re.compile(r"\b(\d{6})\b")


# --------------------------------------------------------------------------
# 夹具:配好 SMTP 的应用实例 + 捕获所有发信
# --------------------------------------------------------------------------
@pytest.fixture()
def mail_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """在 `settings` 清空环境变量之后再注入 SMTP 配置。"""
    monkeypatch.setenv("SMTP_HOST", SMTP_HOST)
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_SECURITY", "starttls")
    monkeypatch.setenv("SMTP_FROM_ADDRESS", SMTP_FROM)
    monkeypatch.setenv("SMTP_FROM_NAME", "Sisi Calendar")
    reset_settings_cache()
    return Settings()


@pytest.fixture()
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """捕获 `app.mailer` 发出的所有邮件(验证码与订阅地址都走它)。"""
    captured: list[dict] = []

    def fake_send(config, message):  # noqa: ANN001
        captured.append(
            {
                "to": message["To"],
                "subject": message["Subject"],
                "body": message.get_body(preferencelist=("plain",)).get_content(),
            }
        )

    monkeypatch.setattr("app.mailer.send_message", fake_send)
    return captured


@pytest.fixture()
def mail_client(mail_env: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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


@pytest.fixture()
def anon_client(mail_client: TestClient) -> Iterator[TestClient]:
    """第二个未登录客户端(独立 Cookie),用来换身份登录。"""
    from app.main import create_app

    application = create_app(Settings())
    with TestClient(application, follow_redirects=False) as second:
        yield second


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "页面缺少 csrf_token"
    return match.group(1)


def _code_from(mail: dict) -> str:
    match = CODE_PATTERN.search(mail["body"])
    assert match, f"验证码邮件里没有 6 位验证码:{mail['body']}"
    return match.group(1)


def _reload(app_session, username: str = "mama"):
    """HTTP 请求改的是另一个会话里的对象,读之前必须先失效本地缓存。"""
    app_session.expire_all()
    return get_user_by_username(app_session, username)


def _register(
    client: TestClient,
    app_session,
    owner_id: str,
    *,
    username: str = "mama",
    email: str = "mama@example.test",
) -> None:
    """走 HTTP 注册(邀请码 + 邮箱)。"""
    plaintext, _ = invites.create_invite(
        app_session, label="测试邀请", max_uses=1, created_by_user_id=owner_id
    )
    app_session.commit()

    page = client.get("/register", params={"code": plaintext})
    response = client.post(
        "/register",
        data={
            "code": plaintext,
            "username": username,
            "display_name": "妈妈",
            "email": email,
            "password": "mama-password",
            "password_confirm": "mama-password",
            "csrf_token": _csrf(page.text),
        },
    )
    assert response.status_code == 303, response.text


def _login_admin(client: TestClient) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "test-password",
            "csrf_token": _csrf(page.text),
            "next": "/admin",
        },
    )
    assert response.status_code == 303, response.text


# --------------------------------------------------------------------------
# 注册与闸门
# --------------------------------------------------------------------------
def test_register_requires_email(mail_client: TestClient, app_session, app_family) -> None:
    plaintext, _ = invites.create_invite(
        app_session, label="测试邀请", max_uses=1, created_by_user_id=app_family.owner_id
    )
    app_session.commit()
    page = mail_client.get("/register", params={"code": plaintext})
    response = mail_client.post(
        "/register",
        data={
            "code": plaintext,
            "username": "mama",
            "password": "mama-password",
            "password_confirm": "mama-password",
            "csrf_token": _csrf(page.text),
        },
    )
    assert response.status_code == 422
    assert "邮箱" in response.text
    assert get_user_by_username(app_session, "mama") is None


def test_verify_page_renders_with_strict_undefined(
    mail_client: TestClient, app_session, app_family, outbox, monkeypatch
) -> None:
    """验证页的上下文也必须齐:缺变量时 Jinja 只会渲染成空串,人眼很难发现。"""
    from app.deps import templates
    from jinja2 import StrictUndefined

    _register(mail_client, app_session, app_family.owner_id)
    monkeypatch.setattr(templates.env, "undefined", StrictUndefined)

    page = mail_client.get("/verify-email")
    assert page.status_code == 200, page.text
    assert "mama@example.test" in page.text


def test_unverified_member_is_blocked(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    _register(mail_client, app_session, app_family.owner_id)
    # 注册时就把验证码发出去了
    assert len(outbox) == 1
    assert outbox[0]["to"].endswith("<mama@example.test>")
    assert "验证码" in outbox[0]["subject"]

    # 页面被送到验证页
    blocked = mail_client.get("/admin")
    assert blocked.status_code == 303
    assert "/verify-email" in blocked.headers["location"]
    # JSON 接口给出 403 与明确的错误码
    api = mail_client.get("/api/v1/events")
    assert api.status_code == 403
    assert api.json()["error"]["code"] == "email_not_verified"

    # 验证页自己可以打开(闸门不能把验证页也挡掉)
    page = mail_client.get("/verify-email")
    assert page.status_code == 200
    assert "mama@example.test" in page.text


def test_unverified_admin_is_not_blocked(
    mail_client: TestClient, anon_client: TestClient, app_session, app_family, outbox
) -> None:
    """管理员豁免:本机救急时不能被自己的邮箱验证锁在门外。"""
    owner = owner_user(app_session)
    assert owner is not None
    owner.email = "admin@example.test"
    owner.email_verified_at = None
    app_session.commit()

    _login_admin(anon_client)
    assert anon_client.get("/admin").status_code == 200
    assert anon_client.get("/admin/me").status_code == 200
    # 未验证的邮箱也不会成为收件人(不发日程)
    app_session.expire_all()
    assert (
        app_session.execute(
            select(SyncRecipient).where(SyncRecipient.user_id == owner.id)
        ).scalars().all()
        == []
    )
    assert outbox == []


def test_full_verification_flow_mails_the_fixed_address(
    mail_client: TestClient, app_session, app_family, mail_env: Settings, outbox
) -> None:
    _register(mail_client, app_session, app_family.owner_id)
    code = _code_from(outbox[0])

    page = mail_client.get("/verify-email")
    verified = mail_client.post(
        "/verify-email",
        data={"code": code, "csrf_token": _csrf(page.text)},
    )
    assert verified.status_code == 303, verified.text
    assert "/admin/me" in verified.headers["location"]

    # 验证通过 → 账号可用
    assert mail_client.get("/admin").status_code == 200

    member = _reload(app_session)
    assert member is not None
    assert member.email_verified_at is not None
    # 验证通过后才建账号收件人
    recipients = app_session.execute(
        select(SyncRecipient).where(SyncRecipient.user_id == member.id)
    ).scalars().all()
    assert len(recipients) == 1

    # 第二封信:固定订阅地址
    assert len(outbox) == 2, [mail["subject"] for mail in outbox]
    address_mail = outbox[1]
    assert "固定订阅地址" in address_mail["subject"]
    token = account_feed_token(mail_env, member.id)
    expected = f"{mail_env.public_base_url}/{token}.ics"
    assert expected in address_mail["body"]
    # 页面上也能看到同一条地址(它由服务端派生,不需要保存明文)
    me = mail_client.get("/admin/partials/feeds")
    assert expected in me.text


def test_wrong_code_is_counted_and_then_invalidated(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    _register(mail_client, app_session, app_family.owner_id)
    code = _code_from(outbox[0])
    wrong = "000000" if code != "000000" else "111111"

    for attempt in range(1, 5):
        page = mail_client.get("/verify-email")
        response = mail_client.post(
            "/verify-email", data={"code": wrong, "csrf_token": _csrf(page.text)}
        )
        assert response.status_code == 422, response.text
        assert "不正确" in response.text
        member = _reload(app_session)
        assert member is not None and member.email_verify_attempts == attempt

    # 第 5 次错误直接把验证码作废,之后只能重新发送
    page = mail_client.get("/verify-email")
    last = mail_client.post(
        "/verify-email", data={"code": wrong, "csrf_token": _csrf(page.text)}
    )
    assert last.status_code == 422
    member = _reload(app_session)
    assert member is not None and member.email_verify_code_hash is None

    page = mail_client.get("/verify-email")
    after = mail_client.post(
        "/verify-email", data={"code": code, "csrf_token": _csrf(page.text)}
    )
    assert after.status_code == 422
    assert "重新发送" in after.text


def test_expired_code_is_rejected(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    _register(mail_client, app_session, app_family.owner_id)
    code = _code_from(outbox[0])

    member = _reload(app_session)
    assert member is not None
    member.email_verify_expires_at = utcnow() - timedelta(minutes=1)
    app_session.commit()

    page = mail_client.get("/verify-email")
    response = mail_client.post(
        "/verify-email", data={"code": code, "csrf_token": _csrf(page.text)}
    )
    assert response.status_code == 422
    assert "过期" in response.text


def test_resend_cooldown_then_new_code(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    _register(mail_client, app_session, app_family.owner_id)
    first_code = _code_from(outbox[0])

    page = mail_client.get("/verify-email")
    too_soon = mail_client.post(
        "/verify-email/resend", data={"csrf_token": _csrf(page.text)}
    )
    assert too_soon.status_code == 429
    assert "频繁" in too_soon.text

    # 把「上次发送时间」推到冷却之前就能重发;重发后旧验证码立即失效
    member = _reload(app_session)
    assert member is not None
    member.email_verify_sent_at = utcnow() - timedelta(minutes=5)
    app_session.commit()

    page = mail_client.get("/verify-email")
    resent = mail_client.post("/verify-email/resend", data={"csrf_token": _csrf(page.text)})
    assert resent.status_code == 200, resent.text
    assert len(outbox) == 2
    new_code = _code_from(outbox[1])
    assert new_code != first_code

    page = mail_client.get("/verify-email")
    stale = mail_client.post(
        "/verify-email", data={"code": first_code, "csrf_token": _csrf(page.text)}
    )
    assert stale.status_code == 422

    page = mail_client.get("/verify-email")
    fresh = mail_client.post(
        "/verify-email", data={"code": new_code, "csrf_token": _csrf(page.text)}
    )
    assert fresh.status_code == 303


def test_can_fix_a_mistyped_email_from_the_verify_page(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    _register(mail_client, app_session, app_family.owner_id, email="typo@example.test")
    assert outbox[0]["to"].endswith("<typo@example.test>")

    page = mail_client.get("/verify-email")
    fixed = mail_client.post(
        "/verify-email/email",
        data={"email": "mama@example.test", "csrf_token": _csrf(page.text)},
    )
    assert fixed.status_code == 200, fixed.text
    assert len(outbox) == 2
    assert outbox[1]["to"].endswith("<mama@example.test>")

    member = _reload(app_session)
    assert member is not None and member.email == "mama@example.test"

    page = mail_client.get("/verify-email")
    done = mail_client.post(
        "/verify-email", data={"code": _code_from(outbox[1]), "csrf_token": _csrf(page.text)}
    )
    assert done.status_code == 303
    assert mail_client.get("/admin").status_code == 200


def test_verified_member_who_changes_email_must_verify_again(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    _register(mail_client, app_session, app_family.owner_id)
    page = mail_client.get("/verify-email")
    assert (
        mail_client.post(
            "/verify-email",
            data={"code": _code_from(outbox[0]), "csrf_token": _csrf(page.text)},
        ).status_code
        == 303
    )
    assert mail_client.get("/admin").status_code == 200

    # 换邮箱 → 立刻回到待验证状态,并给新地址发一枚新验证码
    me = mail_client.get("/admin/partials/me")
    changed = mail_client.post(
        "/admin/actions/profile",
        data={
            "display_name": "妈妈",
            "email": "new@example.test",
            "color": "#336699",
            "timezone": "",
            "csrf_token": _csrf(me.text),
        },
    )
    assert changed.status_code == 200, changed.text
    assert mail_client.get("/admin").status_code == 303

    member = _reload(app_session)
    assert member is not None
    assert member.email == "new@example.test"
    assert member.email_verified_at is None
    assert outbox[-1]["to"].endswith("<new@example.test>")


def test_unverified_account_never_becomes_a_mail_recipient(
    mail_client: TestClient, app_session, app_family, settings
) -> None:
    """未验证的账号不发日历邮件:连收件人记录都不会建。"""
    member = create_user(
        app_session,
        username="papa",
        password="papa-password",
        email="papa@example.test",
        settings=settings,
    )
    app_session.commit()
    assert sync_user_recipient(app_session, member) is None
    app_session.commit()
    rows = app_session.execute(select(SyncRecipient)).scalars().all()
    assert [row.email for row in rows] == []

    # 验证通过后立刻可以成为收件人
    member.email_verified_at = utcnow()
    app_session.commit()
    recipient = sync_user_recipient(app_session, member)
    assert recipient is not None and recipient.user_id == member.id


def test_event_creation_by_unverified_account_is_rejected(
    mail_client: TestClient, app_session, app_family, outbox
) -> None:
    """闸门也覆盖写接口:未验证的成员不能建日程。"""
    _register(mail_client, app_session, app_family.owner_id)
    response = mail_client.post(
        "/api/v1/events",
        json={
            "title": "偷偷写一条",
            "start_at": "2026-12-01T09:00:00",
            "end_at": "2026-12-01T10:00:00",
        },
        headers={"X-CSRF-Token": _csrf(mail_client.get("/verify-email").text)},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_not_verified"