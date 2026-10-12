"""pytest 夹具:每个测试使用独立的数据目录与数据库。"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from app import db as db_module
from app.config import Settings, reset_settings_cache
from app.models import ROLE_MEMBER, Base
from app.security import rate_limiter
from fastapi.testclient import TestClient

TEST_TIMEZONE = "Australia/Brisbane"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "test-password"
MEMBER_USERNAME = "member"
MEMBER_PASSWORD = "member-password"
AGENT_TOKEN = "test-agent-token-abcdefghijklmnop"


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """把数据目录指向临时路径,并清空所有可能干扰测试的环境变量。"""
    for key in list(os.environ):
        if key.startswith(
            (
                "DATA_DIR",
                "ADMIN_",
                "SESSION_",
                "SMTP_",
                "CALENDAR_",
                "DEFAULT_",
                "MAIL_",
                "PUBLIC_BASE_URL",
                "SCHEDULER_",
                "FEED_",
                "BACKUP_",
                "AGENT_",
                "SISI_ENV_FILE",
            )
        ):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEFAULT_TIMEZONE", TEST_TIMEZONE)
    monkeypatch.setenv("CALENDAR_NAME", "Sisi 测试日历")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test/calendar")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # 关键:把配置文件指向不存在的路径,否则测试会读到开发机上的 .env
    # (例如 AGENT_API_TOKEN、SMTP_* 会意外生效,让用例结果依赖本机环境)
    monkeypatch.setenv("SISI_ENV_FILE", str(tmp_path / "no-such.env"))
    reset_settings_cache()
    # 限流器是模块级单例,必须逐测试重置,否则前一个用例会消耗掉后一个的配额
    rate_limiter.reset()

    resolved = Settings()
    resolved.ensure_directories()
    yield resolved
    rate_limiter.reset()
    reset_settings_cache()


@pytest.fixture()
def engine(settings: Settings) -> Iterator[object]:
    created = db_module.init_engine(settings, use_null_pool=True)
    Base.metadata.create_all(created)
    yield created
    db_module.dispose_engine()


@pytest.fixture()
def session(engine: object) -> Iterator[object]:
    factory = db_module.get_session_factory()
    db_session = factory()
    try:
        yield db_session
    finally:
        db_session.rollback()
        db_session.close()


# --------------------------------------------------------------------------
# 家庭夹具:账号、个人日历、家庭共享日历
# --------------------------------------------------------------------------
@dataclass
class Family:
    """最小家庭模型:一位管理员 + 一个家庭共享日历。"""

    owner: object
    personal: object
    shared: object

    @property
    def owner_id(self) -> str:
        return self.owner.id

    @property
    def personal_id(self) -> str:
        return self.personal.id

    @property
    def shared_id(self) -> str:
        return self.shared.id


@pytest.fixture()
def patch_create_event_to_shared(request, family: Family, monkeypatch: pytest.MonkeyPatch):
    """把当前测试模块里的 `create_event` 包一层:未指定日历的事件落进家庭共享日历。

    外部邮箱收件人的附件范围就是家庭共享日历,所以邮件类测试需要事件真的落在那个日历里
    (等价于界面上「成员在共享日历里新建日程」)。仅对直接操作数据库的用例使用:
    走 HTTP 的用例由 lifespan 的引导逻辑负责,不能在这里拉起 `session` 夹具
    (它会 `create_all`,与 lifespan 的 Alembic 迁移冲突)。
    """
    module = request.module
    real = module.create_event

    def create_event(session_, payload, **kwargs):  # noqa: ANN001, ANN202
        if kwargs.get("calendar_id") is None:
            kwargs["calendar_id"] = family.shared_id
        return real(session_, payload, **kwargs)

    monkeypatch.setattr(module, "create_event", create_event)
    return create_event


@pytest.fixture()
def family(session: object, settings: Settings) -> Family:
    """把 `session` 夹具的库补齐成家庭版(等价于应用启动时的引导逻辑)。

    走真实的 `ensure_family_setup`,这样测试用的结构与生产完全一致:
    首位管理员、个人日历「家庭管理员」、共享日历「家庭共享日历」。
    """
    from app.accounts import list_shared_calendars, owner_user, personal_calendar
    from app.bootstrap import ensure_family_setup

    ensure_family_setup(session, settings)
    session.commit()
    owner = owner_user(session)
    assert owner is not None
    shared = list_shared_calendars(session)[0]
    return Family(owner=owner, personal=personal_calendar(session, owner), shared=shared)


@pytest.fixture()
def add_member(session: object, family: Family, settings: Settings):
    """在同一个库里直接建成员账号(绕过注册接口,用于领域层测试)。

    默认把邮箱标记为「已验证」:验证闸门与发信范围另有专门的用例覆盖
    (`tests/test_email_verify.py`),其余用例只关心成员本来的能力。
    """
    from app.accounts import create_user
    from app.timeutil import utcnow

    def _add(
        username: str,
        *,
        password: str = MEMBER_PASSWORD,
        email: str | None = None,
        role: str = ROLE_MEMBER,
        display_name: str | None = None,
        verified: bool = True,
    ):
        user = create_user(
            session,
            username=username,
            password=password,
            display_name=display_name,
            email=email,
            role=role,
            settings=settings,
        )
        if email and verified:
            user.email_verified_at = utcnow()
        session.flush()
        from app.accounts import sync_user_recipient

        sync_user_recipient(session, user)
        session.commit()
        return user

    return _add


@pytest.fixture()
def owner_feed_path(family: Family, settings: Settings) -> str:
    """首位管理员的**固定**订阅地址(站内路径),领域层用例用它。

    HTTP 层用例请用 `app_owner_feed_path`(不能混用 `session` 与 `client`)。
    """
    from app.feed_tokens import account_feed_token

    token = account_feed_token(settings, family.owner_id)
    return f"/calendar/{token}.ics"


# --------------------------------------------------------------------------
# HTTP 层夹具:走完整 lifespan(含 Alembic 迁移)
# --------------------------------------------------------------------------
@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    from app.main import create_app

    application = create_app(settings)
    with TestClient(application, follow_redirects=False) as test_client:
        yield test_client
    db_module.dispose_engine()


def _hidden_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "登录页缺少 csrf_token"
    return match.group(1)


@pytest.fixture()
def admin_client(client: TestClient) -> TestClient:
    page = client.get("/login")
    assert page.status_code == 200
    response = client.post(
        "/login",
        data={
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
            "csrf_token": _hidden_csrf(page.text),
            "next": "/admin",
        },
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture()
def session_csrf(admin_client: TestClient) -> str:
    page = admin_client.get("/admin")
    assert page.status_code == 200, page.text
    match = re.search(r'data-csrf="([^"]+)"', page.text)
    assert match, "日历页缺少 data-csrf"
    return match.group(1)


@pytest.fixture()
def api_headers(session_csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": session_csrf}


# --------------------------------------------------------------------------
# 家庭版的 HTTP 夹具:直接操作运行中应用的数据库 / 以成员身份登录
# --------------------------------------------------------------------------
@pytest.fixture()
def app_session(client: TestClient) -> Iterator[object]:
    """连到**运行中应用**同一个引擎的会话,用来在测试里预置数据。

    不能和 `session` 夹具混用:`session` 会重建引擎,导致两个应用实例用不同的连接池。
    """
    factory = db_module.get_session_factory()
    db_session = factory()
    try:
        yield db_session
    finally:
        db_session.rollback()
        db_session.close()


@pytest.fixture()
def app_family(app_session: object) -> Family:
    """运行中应用里的家庭模型(账号与两个日历)。"""
    from app.accounts import list_shared_calendars, owner_user, personal_calendar

    owner = owner_user(app_session)
    assert owner is not None, "应用启动时应已创建首位管理员"
    shared = list_shared_calendars(app_session)[0]
    return Family(owner=owner, personal=personal_calendar(app_session, owner), shared=shared)


@pytest.fixture()
def app_owner_feed_path(app_family: Family, settings: Settings) -> str:
    """运行中应用里首位管理员的固定订阅地址(HTTP 层用例)。"""
    from app.feed_tokens import account_feed_token

    token = account_feed_token(settings, app_family.owner_id)
    return f"/calendar/{token}.ics"


@pytest.fixture()
def client2(client: TestClient) -> Iterator[TestClient]:
    """第二个**未登录**的客户端(独立 Cookie),用来模拟另一个人/另一台设备。

    依赖 `client` 只是为了保证 lifespan 已经跑过一次(迁移与引导都已完成),
    随后用同一份配置再起一个应用实例,指向同一个数据目录。
    """
    from app.main import create_app

    application = create_app(Settings())
    with TestClient(application, follow_redirects=False) as second:
        yield second


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


@pytest.fixture()
def login_as(client: TestClient):
    """让 `client` 换一个身份登录(同一浏览器换账号)。"""

    def _login_as(username: str, password: str = MEMBER_PASSWORD) -> TestClient:
        client.get("/logout")
        _login(client, username, password)
        return client

    return _login_as


@pytest.fixture()
def member_client(
    client: TestClient, admin_client: TestClient, app_session: object
) -> Iterator[TestClient]:
    """创建成员账号并返回以该成员身份登录的**独立**客户端。

    独立客户端很关键:它有自己的 Cookie,因此可以在同一个测试里既用管理员身份
    操作、又用成员身份验证权限,而不会互相顶掉会话。
    """
    from app.accounts import create_user
    from app.main import create_app
    from app.timeutil import utcnow

    member = create_user(
        app_session,
        username=MEMBER_USERNAME,
        password=MEMBER_PASSWORD,
        display_name="测试成员",
        email="member@example.test",
        settings=Settings(),
    )
    # 邮箱验证另有专门用例;这里让成员直接可用,免得每个权限用例都被闸门挡住
    member.email_verified_at = utcnow()
    app_session.flush()
    from app.accounts import sync_user_recipient

    sync_user_recipient(app_session, member)
    app_session.commit()

    member_app = create_app(Settings())
    with TestClient(member_app, follow_redirects=False) as test_client:
        _login(test_client, MEMBER_USERNAME, MEMBER_PASSWORD)
        yield test_client


# --------------------------------------------------------------------------
# 远程 Agent 接入夹具
# --------------------------------------------------------------------------
@pytest.fixture()
def agent_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """配置了 AGENT_API_TOKEN 的应用实例。"""
    from app.main import create_app

    monkeypatch.setenv("AGENT_API_TOKEN", AGENT_TOKEN)
    reset_settings_cache()
    application = create_app(Settings())
    with TestClient(application, follow_redirects=False) as test_client:
        yield test_client
    db_module.dispose_engine()
    reset_settings_cache()


@pytest.fixture()
def agent_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AGENT_TOKEN}"}
