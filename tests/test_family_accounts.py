"""家庭版领域层测试:账号、日历归属、权限、邀请码、订阅令牌与引导逻辑。

这些用例直接操作数据库(不经过 HTTP),覆盖「谁能看见/修改哪个日历」这条主线,
以及升级到家庭版时对历史数据的处理。
"""

from __future__ import annotations

import pytest
from app import feed_tokens, invites
from app.accounts import (
    AccountError,
    calendar_access,
    calendar_members_map,
    create_shared_calendar,
    create_user,
    delete_user,
    family_calendar_ids,
    get_user_by_username,
    list_shared_calendars,
    membership_role,
    owner_user,
    personal_calendar,
    recipient_scope_user_id,
    set_membership,
    set_user_enabled,
    set_user_role,
    sync_user_recipient,
    update_profile,
    user_timezone,
    visible_calendar_ids,
)
from app.bootstrap import ensure_family_setup
from app.events import create_event, get_event, query_events
from app.models import (
    MEMBER_ROLE_EDITOR,
    MEMBER_ROLE_OWNER,
    MEMBER_ROLE_VIEWER,
    ROLE_ADMIN,
    ROLE_MEMBER,
    AgentToken,
    Event,
    SyncRecipient,
)
from app.schemas import EventWrite
from app.settings_store import effective_settings
from app.timeutil import utcnow
from app.token_store import create_token, hash_token
from sqlalchemy import select


def _event(session, settings, *, title: str, calendar_id: str | None, user_id: str | None):
    return create_event(
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


# --------------------------------------------------------------------------
# 引导:账号与日历
# --------------------------------------------------------------------------
def test_bootstrap_creates_first_admin_and_two_calendars(session, settings, family) -> None:
    assert family.owner.username == "admin"
    assert family.owner.role == ROLE_ADMIN
    assert family.personal.kind == "personal"
    assert family.personal.name == "家庭管理员的日历"
    assert family.personal.owner_user_id == family.owner_id
    assert family.shared.kind == "shared"
    assert family.shared.name == "家庭共享日历"
    assert family.shared.owner_user_id is None
    assert membership_role(session, family.shared, family.owner) == MEMBER_ROLE_OWNER


def test_bootstrap_is_idempotent(session, settings, family) -> None:
    """每次启动都会跑引导,但不得重复建账号或日历。"""
    before = (len(list_shared_calendars(session)), family.owner.id)
    ensure_family_setup(session, settings)
    session.commit()
    assert len(list_shared_calendars(session)) == before[0]
    assert owner_user(session).id == before[1]


def test_legacy_events_are_moved_to_owner_personal_calendar(
    session, settings, family
) -> None:
    """升级前没有归属日历的事件,归入首位管理员的个人日历(私人,不暴露给家庭)。"""
    legacy = _event(session, settings, title="历史日程", calendar_id=None, user_id=None)
    session.commit()
    assert legacy.calendar_id is None

    ensure_family_setup(session, settings)
    session.commit()

    refreshed = session.get(Event, legacy.id)
    assert refreshed.calendar_id == family.personal_id
    assert refreshed.created_by_user_id == family.owner_id
    assert refreshed.id not in family_calendar_ids(session)


def test_legacy_recipients_keep_owner_scope_on_first_upgrade(session, settings) -> None:
    """首次升级:历史外部收件人绑到首位管理员的**范围**上,保持「收到全部日程」的行为。"""
    session.add(
        SyncRecipient(email="old@example.test", enabled=True)
    )
    session.commit()

    ensure_family_setup(session, settings)
    session.commit()

    recipient = session.execute(select(SyncRecipient)).scalar_one()
    owner = owner_user(session)
    assert recipient.scope_user_id == owner.id, "范围绑到首位管理员"
    assert recipient.user_id is None, "身份仍是外部邮箱:账号收件人每个账号只能有一行"
    assert recipient_scope_user_id(recipient) == owner.id


def test_new_external_recipient_stays_family_scoped(session, settings, family) -> None:
    session.add(SyncRecipient(email="guest@example.test", enabled=True))
    session.commit()
    recipient = session.execute(
        select(SyncRecipient).where(SyncRecipient.email == "guest@example.test")
    ).scalar_one()
    assert recipient.user_id is None


def test_legacy_agent_token_is_bound_to_owner(session, settings) -> None:
    session.add(AgentToken(label="旧令牌", token_hash=hash_token("sisi_agent_legacy")))
    session.commit()

    ensure_family_setup(session, settings)
    session.commit()

    token = session.execute(select(AgentToken)).scalar_one()
    assert token.user_id == owner_user(session).id


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------
def test_new_member_gets_personal_calendar_and_viewer_role(session, family, add_member) -> None:
    member = add_member("mama", display_name="妈妈")
    assert member.role == ROLE_MEMBER
    personal = personal_calendar(session, member)
    assert personal.owner_user_id == member.id
    assert membership_role(session, family.shared, member) == MEMBER_ROLE_VIEWER


def test_duplicate_username_is_rejected(session, family, add_member) -> None:
    add_member("mama")
    with pytest.raises(AccountError) as excinfo:
        create_user(session, username="MAMA", password="another-password")
    assert "username" in excinfo.value.fields


def test_email_must_be_unique(session, family, add_member) -> None:
    add_member("mama", email="mama@example.test")
    with pytest.raises(AccountError) as excinfo:
        create_user(
            session, username="papa", password="another-password", email="MAMA@example.test"
        )
    assert "email" in excinfo.value.fields


def test_disabled_account_cannot_authenticate(session, family, add_member) -> None:
    from app.accounts import authenticate_user

    member = add_member("mama")
    assert authenticate_user(session, "mama", "member-password") is not None
    set_user_enabled(session, member, False)
    session.commit()
    assert authenticate_user(session, "mama", "member-password") is None


def test_profile_update_syncs_mail_recipient(session, family, add_member) -> None:
    member = add_member("mama")
    assert sync_user_recipient(session, member) is None  # 没有邮箱就不该有收件人记录

    update_profile(session, member, display_name="妈妈", email="mama@example.test")
    session.commit()
    # 新邮箱要**验证通过**之后才会成为收件人(否则等于把日程发给一个没确认归属的地址)
    assert sync_user_recipient(session, member) is None
    assert session.execute(select(SyncRecipient)).scalars().all() == []

    member.email_verified_at = utcnow()
    session.commit()
    recipient = sync_user_recipient(session, member)
    assert recipient is not None
    assert recipient.user_id == member.id
    assert recipient.email == "mama@example.test"

    # 清空邮箱即停止收信
    update_profile(session, member, email="")
    session.commit()
    assert sync_user_recipient(session, member) is None
    assert session.execute(select(SyncRecipient)).scalars().all() == []


def test_account_recipient_claims_matching_external_row(session, family, add_member) -> None:
    """外部收件人后来注册了账号(同邮箱),应认领原记录而不是产生两条。"""
    session.add(SyncRecipient(email="mama@example.test", enabled=True))
    session.commit()

    member = add_member("mama", email="mama@example.test")
    recipient = sync_user_recipient(session, member)
    session.commit()

    rows = session.execute(select(SyncRecipient)).scalars().all()
    assert len(rows) == 1
    assert recipient is not None and recipient.user_id == member.id


def test_user_timezone_falls_back_to_default(session, settings, family, add_member) -> None:
    member = add_member("mama")
    assert user_timezone(member, settings) == settings.default_timezone
    update_profile(session, member, timezone_name="Asia/Tokyo")
    session.commit()
    assert user_timezone(member, settings) == "Asia/Tokyo"
    # 非法时区不让保存,旧值保持不变
    with pytest.raises(AccountError):
        update_profile(session, member, timezone_name="Mars/Olympus")
    assert user_timezone(member, settings) == "Asia/Tokyo"


def test_owner_is_first_admin(session, family, add_member) -> None:
    add_member("papa", role=ROLE_ADMIN)
    assert owner_user(session).id == family.owner_id


def test_delete_user_removes_personal_calendar_but_keeps_shared_events(
    session, settings, family, add_member
) -> None:
    member = add_member("mama")
    personal = personal_calendar(session, member)
    private = _event(
        session, settings, title="妈妈的私事", calendar_id=personal.id, user_id=member.id
    )
    shared = _event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=member.id
    )
    session.commit()

    delete_user(session, member)
    session.commit()
    # 外键的 ON DELETE SET NULL 由数据库执行,刷新会话才能看到结果
    session.expire_all()

    assert get_user_by_username(session, "mama") is None
    assert get_event(session, private.id) is None, "个人日历删除后事件随之消失"
    kept = session.get(Event, shared.id)
    assert kept is not None, "共享日历里的历史事件必须保留"
    assert kept.created_by_user_id is None, "创建人字段置空,避免删人抹掉家庭日程"


# --------------------------------------------------------------------------
# 可见性
# --------------------------------------------------------------------------
def test_member_cannot_see_owner_personal_calendar(session, settings, family, add_member) -> None:
    member = add_member("mama")
    private = _event(
        session,
        settings,
        title="爸爸的私事",
        calendar_id=family.personal_id,
        user_id=family.owner_id,
    )
    shared = _event(
        session, settings, title="家庭聚餐", calendar_id=family.shared_id, user_id=family.owner_id
    )
    session.commit()

    access = calendar_access(session, member)
    assert family.personal_id not in access.viewable_ids()
    assert family.shared_id in access.viewable_ids()
    assert access.can_write(family.shared_id) is False, "成员对共享日历默认只读"
    assert access.can_write(personal_calendar(session, member).id) is True

    visible, total = query_events(session, calendar_ids=access.viewable_ids())
    ids = {event.id for event in visible}
    assert shared.id in ids
    assert private.id not in ids
    assert total == 1


def test_admin_cannot_see_member_personal_calendar(session, settings, family, add_member) -> None:
    """管理员能管账号,但读不到别人的私人日历——这是核心隐私约束。"""
    member = add_member("mama")
    private = _event(
        session,
        settings,
        title="妈妈的私事",
        calendar_id=personal_calendar(session, member).id,
        user_id=member.id,
    )
    session.commit()

    access = calendar_access(session, family.owner)
    assert get_event(session, private.id, calendar_ids=access.viewable_ids()) is None
    assert access.can_write(private.calendar_id) is False


def test_editor_role_can_write_shared_calendar(session, settings, family, add_member) -> None:
    member = add_member("mama")
    set_membership(session, family.shared, member, MEMBER_ROLE_EDITOR)
    session.commit()

    access = calendar_access(session, member)
    assert access.can_write(family.shared_id) is True
    assert access.role_for(family.shared_id) == MEMBER_ROLE_EDITOR


def test_default_write_calendar_prefers_personal(session, family, add_member) -> None:
    member = add_member("mama")
    mine = personal_calendar(session, member).id
    access = calendar_access(session, member)
    assert access.default_write_calendar_id() == mine

    # 即使被提升为共享日历的编辑,新建仍默认落在自己的个人日历(不打扰家庭日历)
    set_membership(session, family.shared, member, MEMBER_ROLE_EDITOR)
    session.commit()
    promoted = calendar_access(session, member)
    assert promoted.default_write_calendar_id() == mine
    assert set(promoted.writable_ids()) == {mine, family.shared_id}


def test_family_scope_contains_only_shared_calendars(session, family, add_member) -> None:
    add_member("mama")
    assert family_calendar_ids(session) == [family.shared_id]
    assert family.personal_id not in family_calendar_ids(session)
    assert visible_calendar_ids(session, family.owner) != family_calendar_ids(session)


def test_membership_validation(session, family, add_member) -> None:
    member = add_member("mama")
    with pytest.raises(AccountError):
        set_membership(session, family.shared, member, "superuser")
    with pytest.raises(AccountError):
        create_shared_calendar(session, name="   ")
    with pytest.raises(AccountError):
        set_user_role(session, member, "root")


def test_shared_calendar_creator_becomes_owner_and_members_are_viewers(
    session, family, add_member
) -> None:
    member = add_member("mama", role=ROLE_ADMIN)
    calendar = create_shared_calendar(session, name="祖父母", creator=member)
    session.commit()

    roles = calendar_members_map(session, calendar)
    assert roles[member.id] == MEMBER_ROLE_OWNER
    # 其他管理员同样是 owner,普通成员只读
    assert roles[family.owner_id] == MEMBER_ROLE_OWNER
    assert len(list_shared_calendars(session)) == 2


# --------------------------------------------------------------------------
# 邀请码
# --------------------------------------------------------------------------
def test_invite_registration_creates_member(session, settings, family) -> None:
    plaintext, invite = invites.create_invite(
        session,
        label="给妈妈",
        max_uses=1,
        created_by_user_id=family.owner_id,
    )
    session.commit()

    user = invites.register_with_invite(
        session,
        code=plaintext,
        username="mama",
        password="mama-password",
        display_name="妈妈",
    )
    session.commit()

    assert user.role == ROLE_MEMBER
    assert membership_role(session, family.shared, user) == MEMBER_ROLE_VIEWER
    assert invite.used_count == 1
    assert invite.enabled is False, "一次性邀请码用完即停用"


def test_invite_is_stored_hashed_and_looked_up_by_hash(session, family) -> None:
    plaintext, invite = invites.create_invite(session, label="测试", max_uses=2)
    session.commit()
    assert invite.code_hash != plaintext
    assert invites.find_by_code(session, plaintext).id == invite.id
    assert invites.find_by_code(session, "sisi_invite_nope") is None


def test_invite_respects_max_uses(session, family) -> None:
    plaintext, invite = invites.create_invite(session, label="两位", max_uses=2)
    session.commit()
    for index in range(2):
        invites.register_with_invite(
            session, code=plaintext, username=f"user{index}", password="password-123"
        )
        session.commit()
    assert invite.remaining_uses == 0

    with pytest.raises(ValueError, match="使用次数已用完"):
        invites.register_with_invite(
            session, code=plaintext, username="user9", password="password-123"
        )


def test_disabled_and_expired_invites_are_rejected(session, family) -> None:
    from datetime import timedelta

    from app.timeutil import utcnow

    plaintext, invite = invites.create_invite(session, label="停用", max_uses=1)
    session.commit()
    invites.set_enabled(session, invite, False)
    session.commit()
    with pytest.raises(ValueError, match="停用"):
        invites.register_with_invite(
            session, code=plaintext, username="mama", password="password-123"
        )

    expired_code, expired = invites.create_invite(
        session, label="过期", max_uses=1, expires_at=utcnow() - timedelta(days=1)
    )
    session.commit()
    with pytest.raises(ValueError, match="过期"):
        invites.register_with_invite(
            session, code=expired_code, username="mama", password="password-123"
        )

    with pytest.raises(ValueError, match="无效"):
        invites.register_with_invite(
            session, code="sisi_invite_garbage", username="mama", password="password-123"
        )


def test_invite_max_uses_bounds(session, family) -> None:
    from app.settings_store import SettingValidationError

    with pytest.raises(SettingValidationError):
        invites.create_invite(session, label="太多", max_uses=0)
    with pytest.raises(SettingValidationError):
        invites.create_invite(session, label="太多", max_uses=invites.MAX_USES + 1)
    with pytest.raises(SettingValidationError):
        invites.create_invite(session, label="", max_uses=1)


# --------------------------------------------------------------------------
# 订阅令牌
# --------------------------------------------------------------------------
def test_feed_token_roundtrip(session, settings, family, add_member) -> None:
    member = add_member("mama")
    plaintext, token = feed_tokens.create_feed_token(session, user_id=member.id, label="妈妈的手机")
    session.commit()

    assert plaintext.startswith(feed_tokens.FEED_TOKEN_PREFIX)
    assert token.token_hash != plaintext, "令牌必须只存哈希"
    resolved = feed_tokens.resolve_feed_token(session, plaintext)
    assert resolved is not None and resolved.id == token.id
    assert resolved.user_id == member.id
    assert feed_tokens.resolve_feed_token(session, plaintext + "x") is None


def test_family_feed_token_has_no_user(session, settings, family) -> None:
    plaintext, token = feed_tokens.create_feed_token(session, user_id=None, label="客厅平板")
    session.commit()
    assert token.user_id is None
    assert feed_tokens.default_token(session, user_id=None).id == token.id
    assert feed_tokens.feed_scope_label(None) == "家庭共享日历"
    assert feed_tokens.feed_scope_label(family.owner_id) == "个人日历"


def test_disabled_feed_token_cannot_be_resolved(session, settings, family) -> None:
    plaintext, token = feed_tokens.create_feed_token(session, user_id=family.owner_id, label="iPad")
    session.commit()
    feed_tokens.set_enabled(session, token, False)
    session.commit()
    assert feed_tokens.resolve_feed_token(session, plaintext) is None
    # 停用的临时令牌不进比对表;账号的**固定**地址仍在(它由服务端派生,不受影响)
    enabled = feed_tokens.enabled_feed_hashes(session)
    assert hash_token(plaintext) not in [digest for digest, _ in enabled]
    fixed = feed_tokens.account_feed_token(settings, family.owner_id)
    assert feed_tokens.resolve_feed_token(session, fixed) is not None


def test_feed_tokens_are_listed_per_scope(session, settings, family, add_member) -> None:
    member = add_member("mama")
    feed_tokens.create_feed_token(session, user_id=member.id, label="妈妈的")
    feed_tokens.create_feed_token(session, user_id=family.owner_id, label="爸爸的")
    feed_tokens.create_feed_token(session, user_id=None, label="家庭的")
    session.commit()

    # 建号时就自动生成的那条固定地址永远排在列表第一位
    assert [row.label for row in feed_tokens.list_feed_tokens(session, user_id=member.id)] == [
        feed_tokens.ACCOUNT_FEED_LABEL,
        "妈妈的",
    ]
    owner_labels = [
        row.label
        for row in feed_tokens.list_feed_tokens(session, user_id=family.owner_id)
    ]
    assert owner_labels == [feed_tokens.ACCOUNT_FEED_LABEL, "爸爸的"]
    assert [row.label for row in feed_tokens.list_feed_tokens(session, user_id=None)] == ["家庭的"]


def test_account_feed_token_is_stable_and_derived(session, settings, family, add_member) -> None:
    """账号的固定地址:同一个账号永远算出同一个明文,库里只存 sha256。"""
    first = feed_tokens.account_feed_token(settings, family.owner_id)
    assert first == feed_tokens.account_feed_token(settings, family.owner_id)
    assert first.startswith(feed_tokens.FEED_TOKEN_PREFIX)

    row = feed_tokens.fixed_account_token(session, settings, user_id=family.owner_id)
    assert row is not None
    assert row.label == feed_tokens.ACCOUNT_FEED_LABEL
    assert row.token_hash == hash_token(first)
    assert feed_tokens.resolve_feed_token(session, first).id == row.id
    assert feed_tokens.is_fixed_account_token(session, settings, row) is True

    # 幂等:重复确保不会多出第二条固定地址
    again = feed_tokens.ensure_account_feed_token(session, settings, family.owner)
    session.commit()
    assert again.id == row.id
    assert len(feed_tokens.list_feed_tokens(session, user_id=family.owner_id)) == 1

    # 换台机器/换个人:地址不同
    member = add_member("mama")
    assert feed_tokens.account_feed_token(settings, member.id) != first


def test_subscription_url_uses_configured_base(session, settings, family) -> None:
    plaintext, _ = feed_tokens.create_feed_token(session, user_id=family.owner_id, label="iPad")
    session.commit()
    url = feed_tokens.subscription_url_for(settings, plaintext)
    assert url == f"{settings.public_base_url.rstrip('/')}/{plaintext}.ics"
    assert feed_tokens.subscription_path_for(plaintext) == f"/{plaintext}.ics"


def test_agent_token_carries_scope(session, family, add_member) -> None:
    member = add_member("mama")
    plaintext, token = create_token(
        session,
        "妈妈的快捷指令",
        user_id=member.id,
        calendar_id=personal_calendar(session, member).id,
    )
    session.commit()
    assert token.user_id == member.id
    assert token.calendar_id == personal_calendar(session, member).id
    assert plaintext not in token.token_hash


def test_events_are_isolated_per_calendar(session, settings, family, add_member) -> None:
    """两个成员各自的私人日程互不可见,但都能看到家庭共享日历。"""
    mama = add_member("mama")
    papa = add_member("papa")
    mama_calendar = personal_calendar(session, mama).id
    papa_calendar = personal_calendar(session, papa).id

    each = {
        "mama": _event(
            session, settings, title="妈妈的瑜伽", calendar_id=mama_calendar, user_id=mama.id
        ),
        "papa": _event(
            session, settings, title="爸爸的球赛", calendar_id=papa_calendar, user_id=papa.id
        ),
        "shared": _event(
            session,
            settings,
            title="全家出游",
            calendar_id=family.shared_id,
            user_id=family.owner_id,
        ),
    }
    session.commit()

    mama_visible, _ = query_events(session, calendar_ids=visible_calendar_ids(session, mama))
    mama_ids = {event.id for event in mama_visible}
    assert each["mama"].id in mama_ids
    assert each["shared"].id in mama_ids
    assert each["papa"].id not in mama_ids

    # 空的可见范围 = 一条都查不到,而不是「不过滤」
    empty, total = query_events(session, calendar_ids=[])
    assert empty == [] and total == 0


def test_effective_settings_unchanged_by_accounts(session, family, add_member) -> None:
    """家庭版不应改变全局设置的默认值(时区/日历名仍来自应用配置)。"""
    settings = effective_settings(session)
    add_member("mama")
    assert effective_settings(session).default_timezone == settings.default_timezone
    assert effective_settings(session).calendar_name == settings.calendar_name