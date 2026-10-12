"""家庭版 HTTP 层测试:页面权限、事件 API 的可见性/写入权限、注册流程与订阅地址。

这里的核心是**越权**用例:成员之间、成员与管理员之间都必须读不到对方的私人日历,
而且写不进去。判定规则是「读不到 → 404,写不动 → 403」。
"""

from __future__ import annotations

import re

import pytest
from app import invites
from app.accounts import create_user, get_user_by_username, personal_calendar, set_membership
from app.events import create_event
from app.feed_tokens import create_feed_token
from app.models import MEMBER_ROLE_EDITOR
from app.schemas import EventWrite
from app.settings_store import effective_settings
from app.sync_service import mark_export_dirty
from fastapi.testclient import TestClient

API = "/api/v1/events"


def _csrf_of(client: TestClient) -> str:
    page = client.get("/admin")
    assert page.status_code == 200, page.text
    match = re.search(r'data-csrf="([^"]+)"', page.text)
    assert match, "日历页缺少 data-csrf"
    return match.group(1)


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": _csrf_of(client)}


def _hidden_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "表单缺少 csrf_token"
    return match.group(1)


def _make_event(
    session, settings, *, title: str, calendar_id: str | None, user_id: str | None
):
    """直接建事件时也要标记导出待更新——生产里这一步由 `publish()` 负责。

    否则订阅文件会停在旧版本:`_ensure_current` 只看 dirty 标志,不会去比较内容哈希。
    """
    event = create_event(
        session,
        EventWrite(
            title=title,
            start_at="2026-11-03T14:00:00",
            end_at="2026-11-03T15:00:00",
            timezone="UTC",
        ),
        settings=settings,
        calendar_id=calendar_id,
        user_id=user_id,
    )
    mark_export_dirty(session)
    session.commit()
    return event


def _seed_private_and_shared(app_session, app_family, *, username: str = "member"):
    """预置三条事件:管理员的私事、成员的私事、家庭共享。

    默认复用 `member_client` 夹具建立的那个成员账号(它已登录,是最方便的被测主体);
    没有该夹具的用例会现场建一个同名成员。
    """
    settings = effective_settings(app_session)
    member = get_user_by_username(app_session, username)
    if member is None:
        member = create_user(
            app_session,
            username=username,
            password="member-password",
            display_name="妈妈",
            email="mama@example.test",
        )
        app_session.commit()
    member_calendar = personal_calendar(app_session, member)

    events = {
        "owner_private": _make_event(
            app_session,
            settings,
            title="爸爸的私事",
            calendar_id=app_family.personal_id,
            user_id=app_family.owner_id,
        ),
        "member_private": _make_event(
            app_session,
            settings,
            title="妈妈的私事",
            calendar_id=member_calendar.id,
            user_id=member.id,
        ),
        "shared": _make_event(
            app_session,
            settings,
            title="家庭聚餐",
            calendar_id=app_family.shared_id,
            user_id=app_family.owner_id,
        ),
    }
    return member, member_calendar, events


# --------------------------------------------------------------------------
# 页面访问权限
# --------------------------------------------------------------------------
def test_anonymous_visitor_is_redirected_to_login(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", ["/admin/settings", "/admin/sync", "/admin/members"])
def test_admin_only_pages_reject_members(
    member_client: TestClient, path: str
) -> None:
    response = member_client.get(path)
    assert response.status_code in (403, 404), response.text


def test_member_can_open_calendar_and_profile(
    member_client: TestClient, app_session, app_family
) -> None:
    assert member_client.get("/admin").status_code == 200
    assert member_client.get("/admin/me").status_code == 200
    assert member_client.get("/admin/events/new").status_code == 200


def test_member_cannot_open_someone_elses_event_page(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    _, _, events = _seed_private_and_shared(app_session, app_family)
    mine = member_client.get(f"/admin/events/{events['member_private'].id}")
    assert mine.status_code == 200

    theirs = member_client.get(f"/admin/events/{events['owner_private'].id}")
    assert theirs.status_code == 404, "成员不得通过页面读到管理员的私人日程"


# --------------------------------------------------------------------------
# 日历清单与事件 API
# --------------------------------------------------------------------------
def test_calendars_endpoint_lists_visible_calendars_with_roles(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    _, member_calendar, _ = _seed_private_and_shared(app_session, app_family)

    listing = member_client.get("/api/v1/calendars")
    assert listing.status_code == 200
    items = {item["id"]: item for item in listing.json()["items"]}
    assert set(items) == {member_calendar.id, app_family.shared_id}
    assert items[app_family.shared_id]["can_write"] is False
    assert items[app_family.shared_id]["role"] == "viewer"
    assert items[member_calendar.id]["can_write"] is True
    assert listing.json()["default_calendar_id"] == member_calendar.id
    assert app_family.personal_id not in items


def test_events_api_hides_other_peoples_personal_calendars(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    _, _, events = _seed_private_and_shared(app_session, app_family)

    response = member_client.get(API, params={"start": "2026-11-01", "end": "2026-11-30"})
    assert response.status_code == 200
    titles = {item["title"] for item in response.json()["items"]}
    assert titles == {"妈妈的私事", "家庭聚餐"}
    assert "爸爸的私事" not in titles

    details = member_client.get(f"{API}/{events['owner_private'].id}")
    assert details.status_code == 404

    # 显式索要别人的日历 id 也不会被满足(忽略而不是报错,避免泄露存在性)
    scoped = member_client.get(
        API,
        params=[
            ("start", "2026-11-01"),
            ("end", "2026-11-30"),
            ("calendar_id", app_family.personal_id),
        ],
    )
    assert scoped.status_code == 200
    assert scoped.json()["items"] == []


def test_admin_cannot_read_member_personal_event(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    _, _, events = _seed_private_and_shared(app_session, app_family)
    assert admin_client.get(f"{API}/{events['member_private'].id}").status_code == 404
    assert admin_client.get(f"{API}/{events['shared'].id}").status_code == 200


def test_events_api_requires_login(client: TestClient) -> None:
    assert client.get(API).status_code == 401


def test_member_cannot_write_to_shared_calendar(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    _seed_private_and_shared(app_session, app_family)
    headers = _headers(member_client)

    created = member_client.post(
        API,
        json={
            "title": "擅自加的家庭日程",
            "start_at": "2026-11-04T10:00:00",
            "end_at": "2026-11-04T11:00:00",
            "calendar_id": app_family.shared_id,
        },
        headers=headers,
    )
    assert created.status_code == 403, created.text
    assert "管理员" in created.text


def test_member_can_create_in_own_personal_calendar(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    _, member_calendar, _ = _seed_private_and_shared(app_session, app_family)

    created = member_client.post(
        API,
        json={
            "title": "妈妈的瑜伽",
            "start_at": "2026-11-04T10:00:00",
            "end_at": "2026-11-04T11:00:00",
        },
        headers=_headers(member_client),
    )
    assert created.status_code == 201, created.text
    assert created.json()["calendar_id"] == member_calendar.id
    assert created.json()["can_write"] is True

    # 管理员看不到这条私人日程
    assert admin_client.get(f"{API}/{created.json()['id']}").status_code == 404


def test_member_cannot_edit_or_delete_shared_event(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    _, _, events = _seed_private_and_shared(app_session, app_family)
    headers = _headers(member_client)

    patched = member_client.patch(
        f"{API}/{events['shared'].id}", json={"title": "被改掉的名字"}, headers=headers
    )
    assert patched.status_code == 403

    removed = member_client.delete(f"{API}/{events['shared'].id}", headers=headers)
    assert removed.status_code == 403


def test_member_cannot_edit_other_peoples_private_event(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    _, _, events = _seed_private_and_shared(app_session, app_family)
    headers = _headers(member_client)
    target = events["owner_private"].id
    patched = member_client.patch(
        f"{API}/{target}", json={"title": "x"}, headers=headers
    )
    assert patched.status_code == 404
    assert member_client.delete(f"{API}/{target}", headers=headers).status_code == 404


def test_member_without_csrf_header_is_rejected(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    _seed_private_and_shared(app_session, app_family)
    response = member_client.post(
        API,
        json={"title": "没有 CSRF", "start_at": "2026-11-04T10:00:00"},
    )
    assert response.status_code == 403


def test_editor_role_can_write_shared_calendar(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    member, _, _ = _seed_private_and_shared(app_session, app_family)
    set_membership(app_session, app_family.shared, member, MEMBER_ROLE_EDITOR)
    app_session.commit()

    created = member_client.post(
        API,
        json={
            "title": "外婆生日",
            "start_at": "2026-11-05T18:00:00",
            "end_at": "2026-11-05T20:00:00",
            "calendar_id": app_family.shared_id,
        },
        headers=_headers(member_client),
    )
    assert created.status_code == 201, created.text
    assert created.json()["calendar_name"] == "家庭共享日历"

    # 管理员(共享日历的 owner)能看到并修改它
    assert admin_client.get(f"{API}/{created.json()['id']}").status_code == 200


def test_admin_can_move_event_between_calendars(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    settings = effective_settings(app_session)
    event = _make_event(
        app_session,
        settings,
        title="要搬家的日程",
        calendar_id=app_family.personal_id,
        user_id=app_family.owner_id,
    )

    moved = admin_client.patch(
        f"{API}/{event.id}",
        json={"calendar_id": app_family.shared_id},
        headers=_headers(admin_client),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["calendar_id"] == app_family.shared_id


def test_admin_cannot_move_event_into_member_private_calendar(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    settings = effective_settings(app_session)
    member = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    member_calendar = personal_calendar(app_session, member)
    event = _make_event(
        app_session,
        settings,
        title="要搬家的日程",
        calendar_id=app_family.shared_id,
        user_id=app_family.owner_id,
    )

    response = admin_client.patch(
        f"{API}/{event.id}",
        json={"calendar_id": member_calendar.id},
        headers=_headers(admin_client),
    )
    assert response.status_code == 404, "别人的私人日历连管理员也不该看见"


# --------------------------------------------------------------------------
# 邀请码注册流程
# --------------------------------------------------------------------------
def _make_invite(app_session, app_family, *, max_uses: int = 1) -> str:
    plaintext, _ = invites.create_invite(
        app_session,
        label="测试邀请",
        max_uses=max_uses,
        created_by_user_id=app_family.owner_id,
    )
    app_session.commit()
    return plaintext


def test_register_page_and_submit(client: TestClient, app_session, app_family) -> None:
    code = _make_invite(app_session, app_family)

    page = client.get("/register", params={"code": code})
    assert page.status_code == 200
    assert code in page.text, "邀请码应预填进注册表单"

    response = client.post(
        "/register",
        data={
            "code": code,
            "username": "mama",
            "display_name": "妈妈",
            "email": "mama@example.test",
            "password": "mama-password",
            "password_confirm": "mama-password",
            "csrf_token": _hidden_csrf(page.text),
        },
    )
    assert response.status_code == 303, response.text
    # 注册成功即登录,但邮箱没验证前不能用:先去验证页
    assert response.headers["location"] == "/verify-email?sent=0"

    home = client.get("/admin")
    assert home.status_code == 303
    assert "/verify-email" in home.headers["location"], "未验证的账号被挡在验证页"
    verify_page = client.get("/verify-email")
    assert verify_page.status_code == 200
    assert "mama@example.test" in verify_page.text

    from app.accounts import membership_role

    created = get_user_by_username(app_session, "mama")
    assert created is not None and created.role == "member"
    assert created.email_verified_at is None
    assert created.email_verify_code_hash, "注册时就应该登记一枚验证码"
    assert membership_role(app_session, app_family.shared, created) == "viewer"


def test_register_rejects_bad_invite_code(client: TestClient, app_session, app_family) -> None:
    page = client.get("/register")
    response = client.post(
        "/register",
        data={
            "code": "sisi_invite_not-real",
            "username": "mama",
            "email": "mama@example.test",
            "password": "mama-password",
            "password_confirm": "mama-password",
            "csrf_token": _hidden_csrf(page.text),
        },
    )
    assert response.status_code == 403
    assert "邀请码" in response.text


def test_register_rejects_password_mismatch(client: TestClient, app_session, app_family) -> None:
    code = _make_invite(app_session, app_family)
    page = client.get("/register", params={"code": code})
    response = client.post(
        "/register",
        data={
            "code": code,
            "username": "mama",
            "email": "mama@example.test",
            "password": "mama-password",
            "password_confirm": "other-password",
            "csrf_token": _hidden_csrf(page.text),
        },
    )
    assert response.status_code == 422


def test_register_consumes_invite_once(
    client: TestClient, client2: TestClient, app_session, app_family
) -> None:
    code = _make_invite(app_session, app_family, max_uses=1)
    first = client.get("/register", params={"code": code})
    ok = client.post(
        "/register",
        data={
            "code": code,
            "username": "mama",
            "email": "mama@example.test",
            "password": "mama-password",
            "password_confirm": "mama-password",
            "csrf_token": _hidden_csrf(first.text),
        },
    )
    assert ok.status_code == 303

    second = client2.get("/register", params={"code": code})
    reused = client2.post(
        "/register",
        data={
            "code": code,
            "username": "papa",
            "email": "papa@example.test",
            "password": "papa-password",
            "password_confirm": "papa-password",
            "csrf_token": _hidden_csrf(second.text),
        },
    )
    assert reused.status_code == 403
    assert "用完" in reused.text


def test_logged_in_user_is_redirected_away_from_register(admin_client: TestClient) -> None:
    response = admin_client.get("/register")
    assert response.status_code == 303


def test_register_is_rate_limited_per_client(
    client: TestClient, app_session, app_family, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注册接口按来源限流:连打 6 次,第 6 次返回 429 并带 Retry-After。

    为了让客户端始终保持未登录(注册成功会自动登录,之后就打不开注册页了),
    这里每次都用已被占用的用户名,请求失败在字段校验上。
    """
    from app.routers import auth as auth_module

    # 限流桶按来源地址分桶;给这个用例一个专属键,免得污染同进程里的其它注册用例
    monkeypatch.setattr(auth_module, "_client_key", lambda request: "register-ratelimit-probe")

    code = _make_invite(app_session, app_family, max_uses=20)
    for attempt in range(1, auth_module.REGISTER_RATE_LIMIT + 2):
        page = client.get("/register", params={"code": code})
        assert page.status_code == 200
        response = client.post(
            "/register",
            data={
                "code": code,
                "username": app_family.owner.username,  # 已占用 → 422,不会登录
                "password": "kid-password-1",
                "password_confirm": "kid-password-1",
                "csrf_token": _hidden_csrf(page.text),
            },
        )
        if attempt <= auth_module.REGISTER_RATE_LIMIT:
            assert response.status_code == 422, f"第 {attempt} 次应命中字段校验:{response.text}"
        else:
            assert response.status_code == 429, response.text
            assert response.headers["Retry-After"].isdigit()
            assert "频繁" in response.text


def test_register_page_gets_csp(client: TestClient) -> None:
    response = client.get("/register")
    assert response.status_code == 200
    assert "content-security-policy" in response.headers
    assert "script-src 'self'" in response.headers["content-security-policy"]


def test_initial_password_login_is_guided_to_change_it(
    client: TestClient, app_session, app_family
) -> None:
    """管理员重置出来的临时口令:首次登录被引导去改口令,并给出提示。"""
    from app.accounts import create_user

    create_user(
        app_session,
        username="mama",
        password="temporary-password-1",
        display_name="妈妈",
        must_change_password=True,
    )
    app_session.commit()

    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": "mama",
            "password": "temporary-password-1",
            "csrf_token": _hidden_csrf(page.text),
        },
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/admin/me")
    assert "notice" in response.headers["location"]

    profile = client.get(response.headers["location"])
    assert profile.status_code == 200
    assert "请先修改初始口令" in profile.text


def test_forbidden_returns_users_language_for_pages_and_json_for_api(
    member_client: TestClient,
) -> None:
    page = member_client.get("/admin/settings")
    assert page.status_code == 403
    assert "403" in page.text and "权限" in page.text

    api = member_client.get("/api/v1/settings")
    assert api.status_code == 403
    assert api.json()["error"]["code"] == "forbidden"


# --------------------------------------------------------------------------
# 订阅地址
# --------------------------------------------------------------------------
def _parse_ics_titles(payload: str) -> set[str]:
    """从 ICS 正文里取出所有 SUMMARY(按行解析,顺手去掉 CRLF 的行尾)。"""
    titles: set[str] = set()
    for line in payload.splitlines():
        if line.startswith("SUMMARY:"):
            titles.add(line[len("SUMMARY:") :].strip())
    return titles


def _feed_titles(client: TestClient, url: str) -> set[str]:
    """取订阅正文里的标题;顺带断言状态码,失败时能直接看到响应体。"""
    response = client.get(url)
    assert response.status_code == 200, response.text
    return _parse_ics_titles(response.text)


def test_member_feed_contains_only_their_visible_events(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    member, _, _ = _seed_private_and_shared(app_session, app_family)
    plaintext, _ = create_feed_token(app_session, user_id=member.id, label="妈妈的手机")
    app_session.commit()

    response = member_client.get(f"/calendar/{plaintext}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    titles = _parse_ics_titles(response.text)
    assert titles == {"妈妈的私事", "家庭聚餐"}


def test_family_feed_excludes_personal_calendars(
    client: TestClient, admin_client: TestClient, app_session, app_family
) -> None:
    _, _, events = _seed_private_and_shared(app_session, app_family)
    plaintext, _ = create_feed_token(app_session, user_id=None, label="客厅平板")
    app_session.commit()

    titles = _feed_titles(client, f"/calendar/{plaintext}.ics")
    assert titles == {"家庭聚餐"}
    assert events["owner_private"].title not in titles


def test_invalid_feed_token_returns_404(client: TestClient) -> None:
    response = client.get("/calendar/sisi_feed_nothing.ics")
    assert response.status_code == 404
    assert "无效" in response.text


def test_disabled_feed_token_returns_404(
    client: TestClient, admin_client: TestClient, app_session, app_family
) -> None:
    from app.feed_tokens import set_enabled

    plaintext, token = create_feed_token(app_session, user_id=app_family.owner_id, label="iPad")
    app_session.commit()
    set_enabled(app_session, token, False)
    app_session.commit()

    assert client.get(f"/calendar/{plaintext}.ics").status_code == 404


def test_owner_fixed_address_serves_owner_scope_not_union(
    client: TestClient, admin_client: TestClient, app_session, app_family, settings
) -> None:
    """首位管理员的固定地址只给他自己的可见范围,绝不包含别人的私人日程。"""
    from app.feed_tokens import account_feed_token

    _seed_private_and_shared(app_session, app_family)

    fixed = account_feed_token(settings, app_family.owner_id)
    response = client.get(f"/calendar/{fixed}.ics")
    assert response.status_code == 200
    titles = _parse_ics_titles(response.text)
    assert titles == {"爸爸的私事", "家庭聚餐"}
    assert "妈妈的私事" not in titles


def test_legacy_tokenless_address_is_gone(
    client: TestClient, admin_client: TestClient, app_session, app_family
) -> None:
    """升级前的无令牌地址不再服务日历,也不做 HTML 跳转(订阅端会被误导)。"""
    _seed_private_and_shared(app_session, app_family)
    for path in ("/calendar", "/calendar/", "/calendar.ics"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"].startswith("text/plain"), path


def test_feed_token_is_touched_on_access(
    client: TestClient, admin_client: TestClient, app_session, app_family
) -> None:
    plaintext, token = create_feed_token(app_session, user_id=app_family.owner_id, label="iPad")
    app_session.commit()
    assert token.last_used_at is None

    assert client.get(f"/calendar/{plaintext}.ics").status_code == 200
    app_session.expire_all()
    refreshed = app_session.get(type(token), token.id)
    assert refreshed is not None and refreshed.last_used_at is not None


def test_feed_token_only_sees_its_own_file(
    member_client: TestClient, client: TestClient, admin_client: TestClient,
    app_session, app_family
) -> None:
    """两个成员的令牌互不相同,内容也各自独立。"""
    member, _, _ = _seed_private_and_shared(app_session, app_family)
    owner_token, _ = create_feed_token(app_session, user_id=app_family.owner_id, label="爸爸的")
    member_token, _ = create_feed_token(app_session, user_id=member.id, label="妈妈的")
    app_session.commit()

    owner_titles = _feed_titles(member_client, f"/calendar/{owner_token}.ics")
    member_titles = _feed_titles(member_client, f"/calendar/{member_token}.ics")
    assert owner_titles == {"爸爸的私事", "家庭聚餐"}
    assert member_titles == {"妈妈的私事", "家庭聚餐"}