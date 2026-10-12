"""账号、日历与访问权限。

这一层是「谁看得见什么、谁改得了什么」的唯一判定处,路由与导出都必须经过它,
不允许各自拼 SQL 判断权限。

核心规则(2026-10 家庭版):

1. **个人日历**:每人一个,只有归属人能看见、能写。管理员也不行——管理员管的是账号、
   邀请码与全局设置,不是别人的私人日程。
2. **家庭共享日历**:成员按 `calendar_members.role` 访问。新建成员默认 `viewer`,
   也就是「看得到、改不了」;管理员是 `owner`,可以改。把某个成员提升为 `editor`
   就等于允许他改这个共享日历。
3. 每个事件必须属于一个日历(`events.calendar_id`);权限全部由日历推导,
   事件本身不单独授权。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.email_verify import is_verified
from app.models import (
    CALENDAR_KIND_PERSONAL,
    CALENDAR_KIND_SHARED,
    DEFAULT_USER_COLORS,
    MEMBER_ROLE_EDITOR,
    MEMBER_ROLE_OWNER,
    MEMBER_ROLE_VIEWER,
    ROLE_ADMIN,
    ROLE_MEMBER,
    USER_ROLES,
    WRITE_MEMBER_ROLES,
    Calendar,
    CalendarMember,
    SyncRecipient,
    User,
    normalize_email,
    normalize_username,
)
from app.security import hash_password, verify_password
from app.timeutil import is_valid_timezone, utcnow

logger = logging.getLogger(__name__)

MAX_USERNAME = 64
MAX_DISPLAY_NAME = 100
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 200
MAX_CALENDAR_NAME = 100

USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: 订阅令牌作用域关键字:家庭共享日历(没有归属账号)
FAMILY_TARGET = "family"


class AccountError(ValueError):
    """字段级校验失败,由路由转成 422 结构化错误。"""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {value}" for key, value in fields.items()))
        self.fields = fields


class RegistrationError(ValueError):
    """注册失败(邀请码无效、用户名被占用等),文案直接给用户看。"""


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def validate_username(raw: object) -> str:
    username = normalize_username(str(raw or ""))
    if not username:
        raise AccountError({"username": "用户名不能为空"})
    if len(username) > MAX_USERNAME:
        raise AccountError({"username": f"用户名最多 {MAX_USERNAME} 个字符"})
    if not USERNAME_PATTERN.match(username):
        raise AccountError(
            {"username": "用户名只能用小写字母、数字、点、下划线和连字符,且以字母或数字开头"}
        )
    return username


def validate_password(raw: object, *, field: str = "password") -> str:
    password = str(raw or "")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError({field: f"口令至少 {MIN_PASSWORD_LENGTH} 位"})
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AccountError({field: f"口令最多 {MAX_PASSWORD_LENGTH} 位"})
    if password.strip() != password:
        raise AccountError({field: "口令首尾不能有空白字符"})
    return password


def normalize_optional_email(raw: object) -> str | None:
    """邮箱可选;给了就必须像邮箱。"""
    text = normalize_email(str(raw or ""))
    if not text:
        return None
    if len(text) > 320 or not EMAIL_PATTERN.match(text):
        raise AccountError({"email": "请输入有效的邮箱地址"})
    return text


def validate_display_name(raw: object) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) > MAX_DISPLAY_NAME:
        raise AccountError({"display_name": f"显示名最多 {MAX_DISPLAY_NAME} 个字符"})
    return text


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------
def get_user(session: Session, user_id: str | None) -> User | None:
    if not user_id:
        return None
    return session.get(User, user_id)


def get_user_by_username(session: Session, username: str) -> User | None:
    normalized = normalize_username(username)
    if not normalized:
        return None
    return session.execute(
        select(User).where(User.username == normalized)
    ).scalar_one_or_none()


def list_users(session: Session) -> list[User]:
    """全部账号,管理员在前、再按创建顺序,保证渲染稳定。"""
    return list(
        session.execute(
            select(User).order_by(User.created_at.asc(), User.id.asc())
        ).scalars()
    )


def list_members(session: Session) -> list[User]:
    """只列成员(不含管理员),用于成员管理页。"""
    return [user for user in list_users(session) if user.role != ROLE_ADMIN]


def owner_user(session: Session) -> User | None:
    """首位管理员。历史订阅地址 `/calendar` 与 `AGENT_API_TOKEN` 都绑在他身上。"""
    users = list_users(session)
    for user in users:
        if user.role == ROLE_ADMIN:
            return user
    return users[0] if users else None


def next_color(session: Session) -> str:
    """按账号数量循环取一个默认颜色,保证同家庭成员的日历颜色可区分。"""
    used = len(list_users(session))
    return DEFAULT_USER_COLORS[used % len(DEFAULT_USER_COLORS)]


def create_user(
    session: Session,
    *,
    username: object,
    password: object,
    display_name: object = None,
    email: object = None,
    role: str = ROLE_MEMBER,
    color: str | None = None,
    timezone_name: str | None = None,
    must_change_password: bool = False,
    settings: Settings | None = None,
) -> User:
    """创建账号:个人日历、共享日历成员关系、账号收件人与**固定订阅地址**一次到位。

    `settings` 决定固定订阅地址(它由服务端密钥派生),调用方应尽量传入;
    传 `None` 时跳过,由启动引导 `ensure_family_setup` 兜底补上(每次启动都会检查)。
    调用方负责提交事务。用户名冲突与字段非法都抛 `AccountError`。
    """
    errors: dict[str, str] = {}
    resolved_username = ""
    resolved_password = ""
    resolved_email: str | None = None
    resolved_display: str | None = None
    try:
        resolved_username = validate_username(username)
    except AccountError as exc:
        errors.update(exc.fields)
    try:
        resolved_password = validate_password(password)
    except AccountError as exc:
        errors.update(exc.fields)
    try:
        resolved_email = normalize_optional_email(email)
    except AccountError as exc:
        errors.update(exc.fields)
    try:
        resolved_display = validate_display_name(display_name)
    except AccountError as exc:
        errors.update(exc.fields)

    if role not in USER_ROLES:
        errors["role"] = "角色只能是 admin 或 member"

    if resolved_username and get_user_by_username(session, resolved_username) is not None:
        errors["username"] = "该用户名已被占用"
    if resolved_email and email_taken(session, resolved_email):
        errors["email"] = "该邮箱已被其他账号使用"

    if errors:
        raise AccountError(errors)

    now = utcnow()
    user = User(
        username=resolved_username,
        display_name=resolved_display,
        email=resolved_email,
        password_hash=hash_password(resolved_password),
        role=role,
        color=(color or next_color(session)),
        timezone=timezone_name or None,
        enabled=True,
        must_change_password=must_change_password,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.flush()

    personal_calendar(session, user)
    join_shared_calendars(session, user, role=MEMBER_ROLE_VIEWER)
    sync_user_recipient(session, user)
    if settings is not None:
        from app.feed_tokens import ensure_account_feed_token

        ensure_account_feed_token(session, settings, user)
    logger.info("已创建账号 username=%s role=%s", user.username, user.role)
    return user


def email_taken(session: Session, email: str, *, exclude_user_id: str | None = None) -> bool:
    stmt = select(User).where(User.email == email)
    if exclude_user_id:
        stmt = stmt.where(User.id != exclude_user_id)
    return session.execute(stmt).first() is not None


def set_password(
    session: Session, user: User, password: object, *, must_change: bool = False
) -> None:
    user.password_hash = hash_password(validate_password(password))
    user.must_change_password = must_change
    user.updated_at = utcnow()
    session.flush()


def verify_user_password(user: User, password: str) -> bool:
    return verify_password(user.password_hash, password)


def authenticate_user(session: Session, username: str, password: str) -> User | None:
    """校验账号口令。用户名不存在时也走一次哈希校验,避免用响应时间探测账号。"""
    user = get_user_by_username(session, username)
    if user is None:
        verify_password("$argon2id$v=19$m=65536,t=3,p=4$invalid$invalid", password)
        return None
    if not user.enabled:
        verify_password(user.password_hash, password)
        return None
    if not verify_user_password(user, password):
        return None
    return user


def mark_login(session: Session, user: User) -> None:
    user.last_login_at = utcnow()
    session.flush()


def set_user_enabled(session: Session, user: User, enabled: bool) -> None:
    user.enabled = enabled
    user.updated_at = utcnow()
    session.flush()


def set_user_role(session: Session, user: User, role: str) -> None:
    if role not in USER_ROLES:
        raise AccountError({"role": "角色只能是 admin 或 member"})
    user.role = role
    user.updated_at = utcnow()
    session.flush()


def update_profile(
    session: Session,
    user: User,
    *,
    display_name: object = None,
    email: object = None,
    timezone_name: object = None,
    color: object = None,
    display_name_given: bool = True,
    email_given: bool = True,
    timezone_given: bool = True,
    color_given: bool = True,
) -> None:
    """更新个人资料。只处理显式提交的字段(表单里留空表示清除)。"""
    errors: dict[str, str] = {}
    if display_name_given:
        try:
            user.display_name = validate_display_name(display_name)
        except AccountError as exc:
            errors.update(exc.fields)
    if email_given:
        try:
            resolved = normalize_optional_email(email)
        except AccountError as exc:
            errors.update(exc.fields)
        else:
            if resolved and email_taken(session, resolved, exclude_user_id=user.id):
                errors["email"] = "该邮箱已被其他账号使用"
            else:
                user.email = resolved
    if timezone_given:
        text = str(timezone_name or "").strip()
        if text and not is_valid_timezone(text):
            errors["timezone"] = "请选择合法的 IANA 时区(例如 Asia/Shanghai)"
        else:
            user.timezone = text or None
    if color_given:
        text = str(color or "").strip()
        if text and not text.startswith("#"):
            errors["color"] = "颜色必须以 # 开头"
        elif text and len(text) not in (4, 7):
            errors["color"] = "颜色格式应为 #RGB 或 #RRGGBB"
        else:
            user.color = text or user.color
    if errors:
        raise AccountError(errors)
    user.updated_at = utcnow()
    session.flush()
    sync_user_recipient(session, user)


def delete_user(session: Session, user: User) -> None:
    """删除账号,连带删除他的个人日历(含其中的私人日程)、订阅令牌与账号收件人。

    共享日历里的历史事件保留(创建人字段置空),避免删人就把家庭日程抹掉。
    个人日历里的日程必须一起删掉:否则会留下 `calendar_id` 为空的孤儿事件,
    它们既不属于任何人的可见范围,又会被并集导出(内部一致性文件)带出去。

    `created_by_user_id` 用**显式赋值**清空,而不是依赖外键的 `ON DELETE SET NULL`:
    SQLite 的 `batch_alter_table` 加列时不会补外键,升级上来的库里没有这条约束,
    光靠外键会留下指向已删除账号的悬空创建人。
    """
    from app.models import AgentToken, Event, FeedToken

    for token in session.execute(
        select(FeedToken).where(FeedToken.user_id == user.id)
    ).scalars():
        session.delete(token)
    for token in session.execute(
        select(AgentToken).where(AgentToken.user_id == user.id)
    ).scalars():
        session.delete(token)
    for recipient in session.execute(
        select(SyncRecipient).where(SyncRecipient.user_id == user.id)
    ).scalars():
        session.delete(recipient)

    personal_ids = [
        calendar.id
        for calendar in session.execute(
            select(Calendar).where(Calendar.owner_user_id == user.id)
        ).scalars()
    ]
    if personal_ids:
        for event in session.execute(
            select(Event).where(Event.calendar_id.in_(personal_ids))
        ).scalars():
            session.delete(event)
        for calendar in session.execute(
            select(Calendar).where(Calendar.owner_user_id == user.id)
        ).scalars():
            session.delete(calendar)

    session.flush()
    for event in session.execute(
        select(Event).where(Event.created_by_user_id == user.id)
    ).scalars():
        event.created_by_user_id = None
    # 别人(例如历史外部邮箱)把收信范围挂在这个账号上时,删账号后要退回家庭共享范围,
    # 否则它的附件目标会指向一个不存在的账号。
    for recipient in session.execute(
        select(SyncRecipient).where(
            SyncRecipient.scope_user_id == user.id,
            SyncRecipient.user_id.is_(None),
        )
    ).scalars():
        recipient.scope_user_id = None
    session.flush()

    session.delete(user)
    session.flush()


# --------------------------------------------------------------------------
# 日历
# --------------------------------------------------------------------------
def personal_calendar(session: Session, user: User) -> Calendar:
    """取该用户的个人日历,不存在就建一个(幂等)。"""
    existing = session.execute(
        select(Calendar).where(
            Calendar.kind == CALENDAR_KIND_PERSONAL,
            Calendar.owner_user_id == user.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    calendar = Calendar(
        name=f"{user.label}的日历",
        kind=CALENDAR_KIND_PERSONAL,
        owner_user_id=user.id,
        color=user.color or DEFAULT_USER_COLORS[0],
        sort_order=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(calendar)
    session.flush()
    return calendar


def list_shared_calendars(session: Session) -> list[Calendar]:
    """全部共享日历,按排序与创建时间。"""
    return list(
        session.execute(
            select(Calendar)
            .where(Calendar.kind == CALENDAR_KIND_SHARED)
            .order_by(Calendar.sort_order.asc(), Calendar.created_at.asc(), Calendar.id.asc())
        ).scalars()
    )


def create_shared_calendar(
    session: Session,
    *,
    name: object,
    color: str | None = None,
    creator: User | None = None,
) -> Calendar:
    """新建共享日历:creator 是 owner,其余成员默认 viewer。

    默认 viewer 是刻意的:文档定的规则是「成员看得到家庭日历,但只有管理员能改」,
    要放开某个成员,由管理员在成员管理页把他提升为 editor。
    """
    text = str(name or "").strip()
    if not text:
        raise AccountError({"name": "日历名称不能为空"})
    if len(text) > MAX_CALENDAR_NAME:
        raise AccountError({"name": f"日历名称最多 {MAX_CALENDAR_NAME} 个字符"})

    existing = list_shared_calendars(session)
    calendar = Calendar(
        name=text,
        kind=CALENDAR_KIND_SHARED,
        owner_user_id=None,
        color=color or DEFAULT_USER_COLORS[len(existing) % len(DEFAULT_USER_COLORS)],
        sort_order=len(existing),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(calendar)
    session.flush()

    for user in list_users(session):
        role = MEMBER_ROLE_OWNER if (creator is not None and user.id == creator.id) else None
        if role is None:
            role = MEMBER_ROLE_OWNER if user.role == ROLE_ADMIN else MEMBER_ROLE_VIEWER
        set_membership(session, calendar, user, role)
    return calendar


def join_shared_calendars(session: Session, user: User, *, role: str = MEMBER_ROLE_VIEWER) -> None:
    """把新账号加入所有共享日历(已存在的成员关系不动)。"""
    for calendar in list_shared_calendars(session):
        if membership_role(session, calendar, user) is None:
            set_membership(session, calendar, user, role)


def set_membership(
    session: Session, calendar: Calendar, user: User, role: str
) -> CalendarMember:
    if role not in (MEMBER_ROLE_OWNER, MEMBER_ROLE_EDITOR, MEMBER_ROLE_VIEWER):
        raise AccountError({"role": "共享日历角色只能是 owner、editor 或 viewer"})
    member = session.execute(
        select(CalendarMember).where(
            CalendarMember.calendar_id == calendar.id,
            CalendarMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    if member is None:
        member = CalendarMember(
            calendar_id=calendar.id,
            user_id=user.id,
            role=role,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(member)
    else:
        member.role = role
        member.updated_at = utcnow()
    session.flush()
    return member


def membership_role(session: Session, calendar: Calendar, user: User) -> str | None:
    if calendar.is_personal:
        return MEMBER_ROLE_OWNER if calendar.owner_user_id == user.id else None
    row = session.execute(
        select(CalendarMember).where(
            CalendarMember.calendar_id == calendar.id,
            CalendarMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    return row.role if row is not None else None


def calendar_members_map(session: Session, calendar: Calendar) -> dict[str, str]:
    """共享日历的 user_id → role 映射,用于成员管理页渲染。"""
    return {
        row.user_id: row.role
        for row in session.execute(
            select(CalendarMember).where(CalendarMember.calendar_id == calendar.id)
        ).scalars()
    }


# --------------------------------------------------------------------------
# 访问权限
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CalendarAccess:
    """一次请求内的可见/可写日历快照,避免权限判断反复查库。"""

    user: User
    calendars: tuple[Calendar, ...]
    roles: dict[str, str]

    @property
    def by_id(self) -> dict[str, Calendar]:
        return {calendar.id: calendar for calendar in self.calendars}

    def get(self, calendar_id: str | None) -> Calendar | None:
        if not calendar_id:
            return None
        return self.by_id.get(calendar_id)

    def can_view(self, calendar_id: str | None) -> bool:
        return self.get(calendar_id) is not None

    def can_write(self, calendar_id: str | None) -> bool:
        return self.role_for(calendar_id) in WRITE_MEMBER_ROLES

    def role_for(self, calendar_id: str | None) -> str | None:
        if not calendar_id:
            return None
        return self.roles.get(calendar_id)

    @property
    def writable_calendars(self) -> tuple[Calendar, ...]:
        return tuple(
            calendar
            for calendar in self.calendars
            if self.roles.get(calendar.id) in WRITE_MEMBER_ROLES
        )

    @property
    def shared_calendars(self) -> tuple[Calendar, ...]:
        return tuple(calendar for calendar in self.calendars if not calendar.is_personal)

    def viewable_ids(self) -> list[str]:
        return [calendar.id for calendar in self.calendars]

    def writable_ids(self) -> list[str]:
        return [calendar.id for calendar in self.writable_calendars]

    def default_write_calendar_id(self) -> str | None:
        """新建事件时的默认日历:优先个人日历,其次第一个可写的共享日历。"""
        for calendar in self.calendars:
            if calendar.is_personal and self.can_write(calendar.id):
                return calendar.id
        writable = self.writable_calendars
        return writable[0].id if writable else None

    def describe(self, calendar_id: str | None) -> str:
        calendar = self.get(calendar_id)
        return calendar.name if calendar is not None else "未知日历"


def calendar_access(session: Session, user: User) -> CalendarAccess:
    """算出该用户可见的日历与各自角色。

    个人日历只有归属人能看见;共享日历按成员表。`roles` 里同时包含个人日历的
    `owner`,这样 `can_write` 的判断只有一处。
    """
    calendars: list[Calendar] = []
    roles: dict[str, str] = {}

    personal = session.execute(
        select(Calendar)
        .where(Calendar.kind == CALENDAR_KIND_PERSONAL, Calendar.owner_user_id == user.id)
        .order_by(Calendar.created_at.asc())
    ).scalars()
    for calendar in personal:
        calendars.append(calendar)
        roles[calendar.id] = MEMBER_ROLE_OWNER

    shared = list_shared_calendars(session)
    memberships = {
        row.calendar_id: row.role
        for row in session.execute(
            select(CalendarMember).where(CalendarMember.user_id == user.id)
        ).scalars()
    }
    for calendar in shared:
        role = memberships.get(calendar.id)
        if role is None:
            continue
        calendars.append(calendar)
        roles[calendar.id] = role

    return CalendarAccess(user=user, calendars=tuple(calendars), roles=roles)


def visible_calendar_ids(session: Session, user: User) -> list[str]:
    return calendar_access(session, user).viewable_ids()


def family_calendar_ids(session: Session) -> list[str]:
    """家庭共享范围 = 所有共享日历。外部收件人与家庭订阅地址都用它。"""
    return [calendar.id for calendar in list_shared_calendars(session)]


def target_calendar_ids(session: Session, *, user_id: str | None) -> list[str]:
    """订阅/邮件目标的日历集合。

    `user_id` 非空 → 该成员的可见范围;为空 → 家庭共享范围。
    """
    if not user_id:
        return family_calendar_ids(session)
    user = get_user(session, user_id)
    if user is None:
        return []
    return visible_calendar_ids(session, user)


def target_name(session: Session, *, user_id: str | None, base_name: str) -> str:
    """订阅文件里的 `X-WR-CALNAME`。只取决于数据库状态,保证导出确定性。"""
    if not user_id:
        return f"{base_name} · 家庭共享"
    user = get_user(session, user_id)
    if user is None:
        return base_name
    return f"{base_name} · {user.label}"


# --------------------------------------------------------------------------
# 账号收件人同步
# --------------------------------------------------------------------------
def sync_user_recipient(session: Session, user: User) -> SyncRecipient | None:
    """把账号邮箱同步成一条邮件收件人记录。

    规则:

    - 账号没有邮箱 → 移除该账号的收件人记录(清空邮箱即停止收信);
    - **邮箱还没验证** → 同样不建记录:验证前不发任何邮件,避免把日程发到一个
      还没确认归属的地址(验证通过时会再同步一次,见 `app/routers/auth.py`);
    - 邮箱已被一条**外部**收件人占用 → 直接认领那条记录(保留历史发送时间);
    - 否则新建或更新,`user_id` 指向该账号,收信范围即该账号的可见日历。

    调用方负责提交事务。
    """
    rows = list(
        session.execute(
            select(SyncRecipient).where(SyncRecipient.user_id == user.id)
        ).scalars()
    )
    if len(rows) > 1:
        logger.warning(
            "账号 %s 名下有 %s 条收件人记录(历史缺陷),按邮箱匹配识别账号收件人",
            user.username,
            len(rows),
        )
    if len(rows) == 1:
        existing = rows[0]
    else:
        existing = next(
            (row for row in rows if user.email and row.email == user.email), None
        )

    if not user.email or not is_verified(user):
        if existing is not None:
            session.delete(existing)
            session.flush()
        return None

    claimed = session.execute(
        select(SyncRecipient).where(SyncRecipient.email == user.email)
    ).scalar_one_or_none()
    if claimed is not None and claimed is not existing:
        if existing is not None:
            session.delete(existing)
        claimed.user_id = user.id
        claimed.scope_user_id = user.id
        if not claimed.display_name:
            claimed.display_name = user.label
        claimed.updated_at = utcnow()
        session.flush()
        return claimed

    if existing is None:
        existing = SyncRecipient(
            email=user.email,
            display_name=user.label,
            enabled=True,
            user_id=user.id,
            scope_user_id=user.id,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(existing)
    else:
        existing.email = user.email
        existing.scope_user_id = user.id
        if not existing.display_name:
            existing.display_name = user.label
        existing.updated_at = utcnow()
    session.flush()
    return existing


def recipient_scope_user_id(recipient: SyncRecipient) -> str | None:
    """这条收件人记录该按**谁的可见范围**取附件。

    - 账号收件人 → 该账号本人;
    - 历史外部邮箱(单用户版升级上来的)→ 绑定时指定的账号,保持原有收信范围;
    - 其余外部邮箱 → `None`,即只有家庭共享日历。
    """
    return recipient.scope_user_id or recipient.user_id or None


def user_timezone(user: User | None, settings: Settings) -> str:
    """该账号展示时间用的时区:自己的设置优先,非法或未设置时回退应用默认时区。"""
    if user is not None and user.timezone and is_valid_timezone(user.timezone):
        return user.timezone
    return settings.default_timezone


__all__ = [
    "AccountError",
    "CalendarAccess",
    "FAMILY_TARGET",
    "MAX_CALENDAR_NAME",
    "MAX_DISPLAY_NAME",
    "MAX_USERNAME",
    "MIN_PASSWORD_LENGTH",
    "RegistrationError",
    "authenticate_user",
    "calendar_access",
    "calendar_members_map",
    "create_shared_calendar",
    "create_user",
    "delete_user",
    "family_calendar_ids",
    "get_user",
    "get_user_by_username",
    "join_shared_calendars",
    "list_members",
    "list_shared_calendars",
    "list_users",
    "mark_login",
    "membership_role",
    "next_color",
    "owner_user",
    "personal_calendar",
    "recipient_scope_user_id",
    "set_membership",
    "set_password",
    "set_user_enabled",
    "set_user_role",
    "sync_user_recipient",
    "target_calendar_ids",
    "target_name",
    "update_profile",
    "user_timezone",
    "validate_display_name",
    "validate_password",
    "validate_username",
    "verify_user_password",
    "visible_calendar_ids",
]