"""公开面地址前缀:成员从 `/calendar/...` 访问,本机仍可用裸路径。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app import db as db_module
from app import urls
from app.config import Settings, reset_settings_cache
from fastapi.testclient import TestClient

BROWSER = {"Accept": "text/html,application/xhtml+xml"}
CALENDAR_CLIENT = {"Accept": "*/*"}


@pytest.fixture()
def app_session(client: TestClient) -> Iterator[object]:
    factory = db_module.get_session_factory()
    db_session = factory()
    try:
        yield db_session
    finally:
        db_session.rollback()
        db_session.close()


@pytest.fixture()
def app_family(app_session: object):
    from app.accounts import list_shared_calendars, owner_user, personal_calendar

    from conftest import Family

    owner = owner_user(app_session)
    assert owner is not None
    return Family(
        owner=owner,
        personal=personal_calendar(app_session, owner),
        shared=list_shared_calendars(app_session)[0],
    )


def _csrf(html: str) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "页面缺少 csrf_token"
    return match.group(1)


# --------------------------------------------------------------------------
# 公开面:带前缀的入口与静态资源
# --------------------------------------------------------------------------
def test_public_surface_is_served_under_the_prefix(client: TestClient) -> None:
    login = client.get("/calendar/login")
    assert login.status_code == 200
    # 页面里的链接必须跟着入口走,否则家人点一下就被甩回只在本机可达的裸路径
    assert '/calendar/static/css/app.css' in login.text
    assert 'action="/calendar/login"' in login.text
    assert 'href="/calendar/register"' in login.text

    register = client.get("/calendar/register")
    assert register.status_code == 200
    assert '/calendar/static/css/app.css' in register.text

    for asset in ("/calendar/static/css/app.css", "/calendar/static/js/calendar.js"):
        assert client.get(asset).status_code == 200, asset


def test_local_bare_surface_still_works(client: TestClient) -> None:
    login = client.get("/login")
    assert login.status_code == 200
    assert '/static/css/app.css' in login.text
    assert 'action="/admin/login"' not in login.text
    assert '/calendar/static' not in login.text
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/register").status_code == 200


def test_prefixed_login_redirects_back_into_the_prefix(client: TestClient) -> None:
    """未登录时被送到**同一个前缀**下的登录页,并且带回跳地址。"""
    response = client.get("/calendar/admin")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/calendar/login?next=")
    assert "next=/calendar/admin" in response.headers["location"]


def test_prefixed_json_api_reports_401_in_json(client: TestClient) -> None:
    response = client.get("/calendar/api/v1/events")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_prefix_root_serves_text_404_to_calendar_clients(client: TestClient) -> None:
    """旧的无令牌订阅地址不再提供日历:机器要不到 HTML,人也不会看到机器话。"""
    machine = client.get("/calendar", headers=CALENDAR_CLIENT)
    assert machine.status_code == 404
    assert machine.headers["content-type"].startswith("text/plain")
    assert "不再提供日历" in machine.text

    human = client.get("/calendar/", headers=BROWSER)
    assert human.status_code == 303
    assert human.headers["location"] == "/calendar/admin"


def test_admin_pages_under_prefix_link_within_the_prefix(
    admin_client: TestClient, app_family
) -> None:
    """管理员从 `http://localhost:443/calendar/admin` 进来时,导航仍然指向 /calendar。"""
    page = admin_client.get("/calendar/admin")
    assert page.status_code == 200, page.text
    assert 'href="/calendar/admin/me"' in page.text
    assert 'href="/calendar/admin/members"' in page.text
    assert 'href="/admin/members"' not in page.text


def test_subscription_url_comes_from_public_base_url(
    client: TestClient, settings: Settings, app_family
) -> None:
    from app.feed_tokens import account_feed_token

    token = account_feed_token(settings, app_family.owner_id)
    assert urls.subscription_url(settings, token) == f"{settings.public_base_url}/{token}.ics"
    assert settings.public_base_url == "https://example.test/calendar"
    # 地址里那条路径在本机也真的能取到内容(挂载路径 == 后端路径,不做前缀剥离)
    assert client.get(f"/calendar/{token}.ics").status_code == 200


def test_invite_link_contains_public_base_url(admin_client: TestClient) -> None:
    """邀请链接是可直接复制粘贴的完整地址(带公开基地址)。"""
    csrf = _csrf(admin_client.get("/admin/partials/members").text)
    created = admin_client.post(
        "/admin/partials/invites",
        data={"label": "给妈妈", "max_uses": "1", "valid_days": "0", "csrf_token": csrf},
    )
    assert created.status_code == 200, created.text
    assert "https://example.test/calendar/register?code=sisi_invite_" in created.text
    # 列表里给出同样的前缀提示(明文只在创建那一次出现)
    listing = admin_client.get("/admin/partials/members")
    assert "https://example.test/calendar/register?code=" in listing.text


def test_base_url_without_path_falls_back_to_bare_paths(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把基地址配成没有路径的域名时,整套路由退回裸路径(不留半个 /calendar 面)。"""
    from app.main import create_app

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://calendar.example.test")
    reset_settings_cache()
    application = create_app(Settings())
    try:
        with TestClient(application, follow_redirects=False) as bare:
            assert bare.get("/login").status_code == 200
            assert bare.get("/calendar/login").status_code == 404
    finally:
        db_module.dispose_engine()
        reset_settings_cache()