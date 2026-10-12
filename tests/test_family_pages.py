"""家庭版页面与 HTMX 局部片段的渲染/权限测试。

覆盖三件事:

1. 所有页面与片段都能渲染(模板与后端上下文对得上,不会 500);
2. 角色边界:成员进不了管理员页面,也拿不到管理员的片段;
3. 一次性明文(令牌/邀请码/临时口令)只在创建那一次出现,列表不回显;
   以及页面里没有任何内联脚本(CSP 是 `script-src 'self'`)。
"""

from __future__ import annotations

import re

import pytest
from app import invites
from app.accounts import (
    calendar_access,
    create_user,
    get_user_by_username,
    list_users,
    membership_role,
    personal_calendar,
    sync_user_recipient,
)
from app.events import create_event
from app.feed_tokens import create_feed_token
from app.models import (
    CALENDAR_KIND_SHARED,
    MEMBER_ROLE_EDITOR,
    MEMBER_ROLE_OWNER,
    MEMBER_ROLE_VIEWER,
    Calendar,
    SyncRecipient,
)
from app.schemas import EventWrite
from app.settings_store import effective_settings
from fastapi.testclient import TestClient
from sqlalchemy import select

ADMIN_PAGES = ["/admin", "/admin/me", "/admin/members", "/admin/settings", "/admin/sync"]
ADMIN_PARTIALS = [
    "/admin/partials/me",
    "/admin/partials/feeds",
    "/admin/partials/family-feeds",
    "/admin/partials/members",
    "/admin/partials/invites",
    "/admin/partials/recipients",
    "/admin/partials/tokens",
    "/admin/partials/sync",
    "/admin/partials/settings",
]
MEMBER_FORBIDDEN = [
    "/admin/members",
    "/admin/settings",
    "/admin/sync",
    "/admin/partials/members",
    "/admin/partials/invites",
    "/admin/partials/recipients",
    "/admin/partials/tokens",
    "/admin/partials/family-feeds",
    "/admin/partials/sync",
    "/admin/partials/settings",
]

INLINE_HANDLER = re.compile(r"\son(click|change|submit|load|input)\s*=", re.IGNORECASE)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE)


def _csrf(client: TestClient) -> str:
    page = client.get("/admin")
    assert page.status_code == 200, page.text
    match = re.search(r'data-csrf="([^"]+)"', page.text)
    assert match, "日历页缺少 data-csrf"
    return match.group(1)


def _assert_no_inline_script(html: str, *, where: str) -> None:
    assert not INLINE_HANDLER.search(html), f"{where} 里出现内联事件处理器"
    assert not INLINE_SCRIPT.search(html), f"{where} 里出现内联 <script>"


# --------------------------------------------------------------------------
# 渲染与角色边界
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_admin_pages_render(admin_client: TestClient, path: str) -> None:
    response = admin_client.get(path)
    assert response.status_code == 200, response.text
    _assert_no_inline_script(response.text, where=path)


@pytest.mark.parametrize("path", ADMIN_PARTIALS)
def test_admin_partials_render(admin_client: TestClient, path: str) -> None:
    response = admin_client.get(path)
    assert response.status_code == 200, response.text
    _assert_no_inline_script(response.text, where=path)


def test_footer_and_sync_partial_show_the_public_base_url(
    admin_client: TestClient, settings
) -> None:
    """页面脚注与同步面板要显示**公开面基地址**(旧的无令牌订阅地址已经不存在)。"""
    page = admin_client.get("/admin")
    assert page.status_code == 200
    assert f"{settings.public_base_url}/login" in page.text
    assert "本机管理地址" in page.text

    sync = admin_client.get("/admin/partials/sync")
    assert sync.status_code == 200
    assert settings.public_base_url in sync.text
    assert "公开面基地址" in sync.text


def test_all_pages_render_with_strict_undefined(
    admin_client: TestClient, member_client: TestClient, client: TestClient, monkeypatch
) -> None:
    """模板引用了不存在的上下文变量时,Jinja 默认渲染成空串而不会报错。

    这里把所有页面的 `undefined` 换成 `StrictUndefined`:上下文与模板对不上就会当场炸,
    避免出现「页面上某一栏悄悄变成空白」这种只有人眼能发现的回归。
    """
    from app.deps import templates
    from jinja2 import StrictUndefined

    monkeypatch.setattr(templates.env, "undefined", StrictUndefined)

    failures: list[str] = []

    def check(client: TestClient, path: str, *, allowed: tuple[int, ...] = (200,)) -> None:
        try:
            response = client.get(path)
        except Exception as exc:  # noqa: BLE001 - 这里就是要收集模板异常
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
            return
        if response.status_code not in allowed:
            failures.append(f"{path}: HTTP {response.status_code}")

    for path in [*ADMIN_PAGES, *ADMIN_PARTIALS, "/admin/events/new"]:
        check(admin_client, path)
    for path in ("/admin", "/admin/me", "/admin/events/new"):
        check(member_client, path)
    for path in ("/login", "/register", "/"):
        check(client, path, allowed=(200, 303))

    assert not failures, "模板里有未定义的上下文变量:\n" + "\n".join(failures)


def test_calendar_page_exposes_calendars_for_the_sidebar(admin_client: TestClient) -> None:
    page = admin_client.get("/admin")
    assert 'data-calendars="' in page.text or "data-calendars='" in page.text
    assert "家庭共享日历" in page.text
    assert "家庭管理员的日历" in page.text


def test_sidebar_primary_action_is_a_proper_button(admin_client: TestClient) -> None:
    """侧栏那块的排版:主按钮带图标、日历清单有独立小标题(曾经是一个贴边的蓝条)。"""
    page = admin_client.get("/admin")
    assert 'class="button primary wide sidebar-cta"' in page.text
    assert 'class="icon"' in page.text, "新建日程按钮应有图标"
    assert "sidebar-label" in page.text and "显示哪些日历" in page.text
    assert 'class="calendar-list"' in page.text


def test_static_assets_carry_a_version_stamp(admin_client: TestClient) -> None:
    """静态资源必须带指纹,否则浏览器会拿旧 CSS 渲染出错版页面。"""
    page = admin_client.get("/admin")
    css = re.search(r'href="(/static/css/app\.css\?v=[0-9a-f]+-[0-9a-f]+)"', page.text)
    js = re.search(r'src="(/static/js/copy\.js\?v=[0-9a-f]+-[0-9a-f]+)"', page.text)
    assert css, page.text[:400]
    assert js, page.text[:400]
    assert admin_client.get(css.group(1)).status_code == 200

    asset = admin_client.get(js.group(1))
    assert asset.status_code == 200
    # 复制逻辑:优先剪贴板 API,同时保留 execCommand 兜底;不使用内联处理器
    assert "navigator.clipboard" in asset.text
    assert "execCommand" in asset.text
    assert "addEventListener" in asset.text


def test_feed_cards_offer_a_copy_button(member_client: TestClient) -> None:
    """固定订阅地址必须有一键复制(以前只能手动选中那串长地址)。"""
    page = member_client.get("/admin/partials/feeds")
    assert page.status_code == 200, page.text
    assert 'id="fixed-feed-url"' in page.text
    assert 'data-copy="#fixed-feed-url"' in page.text

    me = member_client.get("/admin/me").text
    assert 'data-copy="#fixed-feed-url"' in me, "整页 /admin/me 也要有复制按钮"


def test_new_token_reveals_offer_a_copy_button(member_client: TestClient) -> None:
    csrf = _csrf(member_client)
    created = member_client.post(
        "/admin/partials/feeds", data={"label": "我的 iPad", "csrf_token": csrf}
    )
    assert created.status_code == 200, created.text
    assert 'id="new-feed-url"' in created.text
    assert 'data-copy="#new-feed-url"' in created.text


def test_member_pages_render(member_client: TestClient) -> None:
    for path in ("/admin", "/admin/me", "/admin/events/new"):
        response = member_client.get(path)
        assert response.status_code == 200, response.text
        _assert_no_inline_script(response.text, where=path)


def test_member_partials_render(member_client: TestClient) -> None:
    for path in ("/admin/partials/me", "/admin/partials/feeds"):
        response = member_client.get(path)
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("path", MEMBER_FORBIDDEN)
def test_member_is_locked_out_of_admin_surfaces(member_client: TestClient, path: str) -> None:
    response = member_client.get(path)
    assert response.status_code in (403, 404), f"{path} 不应向成员开放"


def test_member_nav_hides_admin_links(member_client: TestClient, admin_client: TestClient) -> None:
    member_page = member_client.get("/admin").text
    assert "/admin/me" in member_page
    for hidden in ("/admin/members", "/admin/settings", "/admin/sync"):
        assert hidden not in member_page, f"成员页面不该出现 {hidden} 入口"

    admin_page = admin_client.get("/admin").text
    for link in ("/admin/members", "/admin/settings", "/admin/sync"):
        assert link in admin_page


def test_event_form_for_shared_calendar_is_read_only_for_member(
    member_client: TestClient, admin_client: TestClient, client: TestClient,
    app_session, app_family
) -> None:
    """成员打开共享日历里的事件:能看,不能存,也不渲染删除按钮。"""
    settings = effective_settings(app_session)
    member = get_user_by_username(app_session, "member")
    assert member is not None
    event = create_event(
        app_session,
        EventWrite(
            title="家庭聚餐",
            start_at="2026-12-01T09:00:00",
            end_at="2026-12-01T10:00:00",
            timezone="UTC",
        ),
        settings=settings,
        calendar_id=app_family.shared_id,
        user_id=app_family.owner_id,
    )
    app_session.commit()

    page = member_client.get(f"/admin/events/{event.id}")
    assert page.status_code == 200, page.text
    assert "没有修改该事件的权限" in page.text

    # 自己的个人日历里的事件则可以编辑
    mine = create_event(
        app_session,
        EventWrite(
            title="妈妈的瑜伽",
            start_at="2026-12-02T09:00:00",
            end_at="2026-12-02T10:00:00",
            timezone="UTC",
        ),
        settings=settings,
        calendar_id=personal_calendar(app_session, member).id,
        user_id=member.id,
    )
    app_session.commit()
    own_page = member_client.get(f"/admin/events/{mine.id}")
    assert own_page.status_code == 200
    assert "没有修改该事件的权限" not in own_page.text


# --------------------------------------------------------------------------
# 一次性明文
# --------------------------------------------------------------------------
PLAINTEXT_PATTERNS = {
    # Agent 令牌沿用历史的 `sisi_` 前缀(与订阅令牌/邀请码区分)
    "agent": re.compile(r"sisi_(?!feed_|invite_)[A-Za-z0-9_-]{8,}"),
    "feed": re.compile(r"sisi_feed_[A-Za-z0-9_-]{8,}"),
    "invite": re.compile(r"sisi_invite_[A-Za-z0-9_-]{8,}"),
}


def _plaintext(html: str, kind: str) -> str:
    """从响应 HTML 里取出一次性明文(模板只用 class 不写 name,故按前缀找)。"""
    match = PLAINTEXT_PATTERNS[kind].search(html)
    assert match, f"响应里没有 {kind} 明文"
    return match.group(0)


def test_agent_token_plaintext_is_shown_only_once(admin_client: TestClient) -> None:
    csrf = _csrf(admin_client)
    created = admin_client.post(
        "/admin/partials/tokens",
        data={"label": "客厅平板", "acknowledge": "1", "csrf_token": csrf},
    )
    assert created.status_code == 200, created.text
    plaintext = _plaintext(created.text, "agent")

    listing = admin_client.get("/admin/partials/tokens")
    assert plaintext not in listing.text, "明文令牌不得在列表里回显"
    assert "客厅平板" in listing.text


def test_my_feed_is_stable_and_temporary_token_shown_once(
    member_client: TestClient, app_session, settings
) -> None:
    """固定地址永远可见;临时地址的明文只在创建那一次出现。"""
    from app import feed_tokens

    member = get_user_by_username(app_session, "member")
    assert member is not None, "member_client 应已创建成员账号"
    fixed = feed_tokens.account_feed_token(settings, member.id)

    csrf = _csrf(member_client)
    created = member_client.post(
        "/admin/partials/feeds", data={"label": "我的 iPhone", "csrf_token": csrf}
    )
    assert created.status_code == 200, created.text
    tokens = set(re.findall(r"sisi_feed_[A-Za-z0-9_-]+", created.text))
    assert fixed in tokens, "固定地址应始终显示"
    temporary = tokens - {fixed}
    assert len(temporary) == 1, "创建响应里应恰好出现一条新的临时地址"
    plaintext = temporary.pop()
    assert f"/{plaintext}.ics" in created.text

    listing = member_client.get("/admin/partials/feeds")
    assert fixed in listing.text, "固定地址刷新后仍然可见(它由服务端派生,不需要保存)"
    assert plaintext not in listing.text, "临时地址的明文不得在列表里回显"


def test_fixed_address_cannot_be_disabled_or_revoked(
    member_client: TestClient, app_session, settings
) -> None:
    """固定地址是订阅端长期填着的那条,界面上不能停用或撤销它。"""
    from app import feed_tokens

    member = get_user_by_username(app_session, "member")
    assert member is not None
    fixed_row = feed_tokens.fixed_account_token(app_session, settings, user_id=member.id)
    assert fixed_row is not None
    csrf = _csrf(member_client)

    toggled = member_client.post(
        f"/admin/partials/feeds/{fixed_row.id}/toggle", data={"csrf_token": csrf}
    )
    assert toggled.status_code == 409
    revoked = member_client.post(
        f"/admin/partials/feeds/{fixed_row.id}/delete", data={"csrf_token": csrf}
    )
    assert revoked.status_code == 409

    app_session.expire_all()
    still_there = app_session.get(type(fixed_row), fixed_row.id)
    assert still_there is not None and still_there.enabled is True


def test_member_cannot_touch_another_users_feed_token(
    member_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    """订阅令牌是私人物品:成员不能停用或撤销别人的(管理员也不行,他没有入口)。"""
    csrf = _csrf(member_client)
    _, owner_token = create_feed_token(
        app_session, user_id=app_family.owner_id, label="爸爸的"
    )
    app_session.commit()

    toggled = member_client.post(
        f"/admin/partials/feeds/{owner_token.id}/toggle", data={"csrf_token": csrf}
    )
    assert toggled.status_code == 404

    revoked = member_client.post(
        f"/admin/partials/feeds/{owner_token.id}/delete", data={"csrf_token": csrf}
    )
    assert revoked.status_code == 404

    app_session.expire_all()
    still_there = app_session.get(type(owner_token), owner_token.id)
    assert still_there is not None and still_there.enabled is True


def test_invite_plaintext_is_shown_once_and_register_link_works(
    admin_client: TestClient, client2: TestClient
) -> None:
    csrf = _csrf(admin_client)
    created = admin_client.post(
        "/admin/partials/invites",
        data={"label": "给妈妈", "max_uses": "1", "valid_days": "0", "csrf_token": csrf},
    )
    assert created.status_code == 200, created.text
    plaintext = _plaintext(created.text, "invite")
    assert plaintext.startswith("sisi_invite_"), created.text
    assert f"/register?code={plaintext}" in created.text

    listing = admin_client.get("/admin/partials/invites")
    assert plaintext not in listing.text

    # 明文能直接用来注册
    page = client2.get("/register", params={"code": plaintext})
    assert page.status_code == 200


def test_reset_member_password_shows_one_time_password_once(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    member = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    csrf = _csrf(admin_client)

    reset = admin_client.post(
        f"/admin/partials/members/{member.id}/reset", data={"csrf_token": csrf}
    )
    assert reset.status_code == 200, reset.text
    assert "的临时口令只显示这一次" in reset.text
    match = re.search(r'value="([^"]+)"\s*\n?\s*aria-label="临时口令"', reset.text)
    assert match, "重置响应里应给出一次性临时口令"
    temporary = match.group(1)
    assert len(temporary) >= 8
    assert "mama" in reset.text

    listing = admin_client.get("/admin/partials/members")
    assert temporary not in listing.text, "临时口令不得在成员列表里回显"


def test_recipient_partial_shows_scope(
    admin_client: TestClient, client: TestClient, app_session, app_family, settings
) -> None:
    from app.timeutil import utcnow

    member = create_user(
        app_session,
        username="mama",
        password="member-password",
        display_name="妈妈",
        email="mama@example.test",
        settings=settings,
    )
    member.email_verified_at = utcnow()
    app_session.commit()
    sync_user_recipient(app_session, member)
    app_session.add(SyncRecipient(email="guest@example.test", enabled=True))
    app_session.commit()

    page = admin_client.get("/admin/partials/recipients")
    assert page.status_code == 200, page.text
    assert "mama@example.test" in page.text
    assert "guest@example.test" in page.text
    assert "本人可见日历" in page.text
    assert "家庭共享日历" in page.text


def test_token_partial_lists_owner_and_shared_calendars(admin_client: TestClient) -> None:
    page = admin_client.get("/admin/partials/tokens")
    assert page.status_code == 200, page.text
    assert "家庭共享日历" in page.text, "默认日历下拉应列出共享日历"
    assert "个人日历" in page.text


# --------------------------------------------------------------------------
# 我的设置:资料与口令
# --------------------------------------------------------------------------
def test_profile_update_starts_email_verification(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    """改邮箱 → 资料保存成功、但邮箱进入待验证;验证前不建收件人(不发日程)。"""
    csrf = _csrf(admin_client)
    response = admin_client.post(
        "/admin/actions/profile",
        data={
            "display_name": "爸爸",
            "email": "admin@example.test",
            "color": "#336699",
            "timezone": "Asia/Tokyo",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200, response.text
    assert "验证" in response.text, "页面应提示去验证邮箱"

    app_session.expire_all()
    from app.accounts import owner_user

    owner = owner_user(app_session)
    assert owner is not None
    assert owner.display_name == "爸爸"
    assert owner.timezone == "Asia/Tokyo"
    assert owner.email == "admin@example.test"
    assert owner.email_verified_at is None, "新邮箱必须重新验证"
    assert owner.email_verify_code_hash, "应已登记一枚验证码"
    assert app_session.execute(select(SyncRecipient)).scalars().all() == []


def test_profile_rejects_invalid_timezone(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    csrf = _csrf(admin_client)
    response = admin_client.post(
        "/admin/actions/profile",
        data={"display_name": "爸爸", "timezone": "Mars/Olympus", "csrf_token": csrf},
    )
    assert response.status_code == 422
    assert "IANA" in response.text


def test_password_change_requires_current_password(admin_client: TestClient) -> None:
    csrf = _csrf(admin_client)
    wrong = admin_client.post(
        "/admin/actions/password",
        data={
            "current_password": "not-the-password",
            "new_password": "new-password-1",
            "new_password_confirm": "new-password-1",
            "csrf_token": csrf,
        },
    )
    assert wrong.status_code == 401

    mismatch = admin_client.post(
        "/admin/actions/password",
        data={
            "current_password": "test-password",
            "new_password": "new-password-1",
            "new_password_confirm": "new-password-2",
            "csrf_token": csrf,
        },
    )
    assert mismatch.status_code == 422

    ok = admin_client.post(
        "/admin/actions/password",
        data={
            "current_password": "test-password",
            "new_password": "new-password-1",
            "new_password_confirm": "new-password-1",
            "csrf_token": csrf,
        },
    )
    assert ok.status_code == 200, ok.text
    assert "口令已更新" in ok.text


def test_profile_cannot_steal_another_accounts_email(
    admin_client: TestClient, client: TestClient, app_session, app_family, member_client: TestClient
) -> None:
    csrf = _csrf(admin_client)
    response = admin_client.post(
        "/admin/actions/profile",
        data={"email": "member@example.test", "csrf_token": csrf},
    )
    assert response.status_code == 422
    assert "已被其他账号使用" in response.text


# --------------------------------------------------------------------------
# 管理员操作:成员与共享日历
# --------------------------------------------------------------------------
def test_admin_can_disable_and_promote_member(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    member = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    csrf = _csrf(admin_client)

    disabled = admin_client.post(
        f"/admin/partials/members/{member.id}/toggle", data={"csrf_token": csrf}
    )
    assert disabled.status_code == 200 and "已停用" in disabled.text

    promoted = admin_client.post(
        f"/admin/partials/members/{member.id}/calendar-role",
        data={"calendar_id": app_family.shared_id, "role": MEMBER_ROLE_EDITOR, "csrf_token": csrf},
    )
    assert promoted.status_code == 200 and "可编辑" in promoted.text

    app_session.expire_all()
    assert member.enabled is False


def test_admin_cannot_disable_or_delete_self(
    admin_client: TestClient, app_session, app_family
) -> None:
    csrf = _csrf(admin_client)
    assert (
        admin_client.post(
            f"/admin/partials/members/{app_family.owner_id}/toggle", data={"csrf_token": csrf}
        ).status_code
        == 409
    )
    assert (
        admin_client.post(
            f"/admin/partials/members/{app_family.owner_id}/delete", data={"csrf_token": csrf}
        ).status_code
        == 409
    )


def test_admin_creates_shared_calendar_with_viewer_defaults(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    member = create_user(
        app_session, username="mama", password="member-password", display_name="妈妈"
    )
    app_session.commit()
    csrf = _csrf(admin_client)

    response = admin_client.post(
        "/admin/partials/shared-calendars",
        data={"name": "祖父母", "color": "#112233", "csrf_token": csrf},
    )
    assert response.status_code == 200, response.text
    assert "祖父母" in response.text
    assert "只读" in response.text

    created = _shared_calendar_named(app_session, "祖父母")
    assert membership_role(app_session, created, member) == MEMBER_ROLE_VIEWER
    assert membership_role(app_session, created, app_family.owner) == MEMBER_ROLE_OWNER


def _shared_calendar_named(session, name: str) -> Calendar:
    return session.execute(
        select(Calendar).where(Calendar.kind == CALENDAR_KIND_SHARED, Calendar.name == name)
    ).scalar_one()


def test_admin_deleting_member_keeps_last_admin(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    """把另一个成员提升成管理员后,不能把管理员删到只剩零个。"""
    admin2 = create_user(
        app_session, username="papa", password="member-password", display_name="爸爸", role="admin"
    )
    app_session.commit()
    csrf = _csrf(admin_client)

    # 删掉第二个管理员是允许的(还剩自己)
    removed = admin_client.post(
        f"/admin/partials/members/{admin2.id}/delete", data={"csrf_token": csrf}
    )
    assert removed.status_code == 200 and "已删除账号" in removed.text
    assert get_user_by_username(app_session, "papa") is None

    # 自己不能被删,所以管理员数量永远不会归零
    assert (
        admin_client.post(
            f"/admin/partials/members/{app_family.owner_id}/delete", data={"csrf_token": csrf}
        ).status_code
        == 409
    )


def test_recipient_api_reports_scope_and_blocks_account_rows(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    from app.accounts import owner_user
    from app.timeutil import utcnow

    owner = owner_user(app_session)
    assert owner is not None
    owner.email = "admin@example.test"
    owner.email_verified_at = utcnow()  # 已确认归属的邮箱才会成为收件人
    app_session.commit()
    sync_user_recipient(app_session, owner)
    app_session.add(SyncRecipient(email="guest@example.test", enabled=True))
    app_session.commit()

    listing = admin_client.get("/api/v1/sync-recipients").json()
    scopes = {item["email"]: item["scope"] for item in listing["items"]}
    assert scopes == {"admin@example.test": "account", "guest@example.test": "family"}
    account_row = next(
        item for item in listing["items"] if item["email"] == "admin@example.test"
    )
    assert account_row["username"] == owner.username
    assert account_row["scope_label"].startswith("本人可见日历")

    csrf = _csrf(admin_client)
    blocked = admin_client.delete(
        f"/api/v1/sync-recipients/{account_row['id']}", headers={"X-CSRF-Token": csrf}
    )
    assert blocked.status_code == 409
    assert "账号邮箱" in blocked.text


def test_member_cannot_use_admin_apis(member_client: TestClient) -> None:
    csrf = _csrf(member_client)
    headers = {"X-CSRF-Token": csrf}
    assert member_client.get("/api/v1/sync-recipients").status_code == 403
    assert member_client.get("/api/v1/sync/status").status_code == 403
    assert member_client.get("/api/v1/settings").status_code == 403
    assert (
        member_client.post("/api/v1/sync/calendar", headers=headers).status_code == 403
    )
    assert (
        member_client.patch(
            "/api/v1/settings", json={"calendar_name": "改名"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        member_client.post(
            "/api/v1/sync-recipients",
            json={"email": "x@example.test", "acknowledge_full_calendar": True},
            headers=headers,
        ).status_code
        == 403
    )


def test_invite_creation_validates_inputs(admin_client: TestClient) -> None:
    csrf = _csrf(admin_client)
    bad = admin_client.post(
        "/admin/partials/invites",
        data={"label": "坏天数", "max_uses": "1", "valid_days": "abc", "csrf_token": csrf},
    )
    assert bad.status_code == 422 and "整数" in bad.text

    out_of_range = admin_client.post(
        "/admin/partials/invites",
        data={"label": "太多", "max_uses": "999", "valid_days": "0", "csrf_token": csrf},
    )
    assert out_of_range.status_code == 422


def test_invite_list_marks_used_up_invites(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    plaintext, invite = invites.create_invite(
        session=app_session, label="一次性", max_uses=1, created_by_user_id=app_family.owner_id
    )
    app_session.commit()
    from app.invites import consume

    consume(app_session, invite)
    app_session.commit()

    page = admin_client.get("/admin/partials/invites")
    assert page.status_code == 200
    assert "一次性" in page.text
    assert plaintext not in page.text


def test_members_page_lists_all_accounts(
    admin_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    create_user(
        app_session,
        username="mama",
        password="member-password",
        display_name="妈妈",
        email="mama@example.test",
    )
    app_session.commit()
    page = admin_client.get("/admin/members")
    assert page.status_code == 200
    for username in ("admin", "mama"):
        assert username in page.text
    assert len(list_users(app_session)) == 2


def test_member_calendar_role_change_requires_admin(
    member_client: TestClient, client: TestClient, app_session, app_family
) -> None:
    target = get_user_by_username(app_session, "member")
    assert target is not None
    csrf = _csrf(member_client)
    response = member_client.post(
        f"/admin/partials/members/{target.id}/calendar-role",
        data={"calendar_id": app_family.shared_id, "role": MEMBER_ROLE_EDITOR, "csrf_token": csrf},
    )
    assert response.status_code in (403, 404)

    app_session.expire_all()
    # 权限没有被改动:成员默认仍是只读
    assert membership_role(app_session, app_family.shared, target) == MEMBER_ROLE_VIEWER
    assert not calendar_access(app_session, target).can_write(app_family.shared_id)