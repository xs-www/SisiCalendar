"""SQLAlchemy 数据模型。

对应开发文档第 7 节。SQLite 是日程数据的唯一事实来源,`calendar.ics` 只是派生
发布文件,因此这里不保存任何 ICS 正文,只保存生成状态与版本快照索引。

多用户模型(2026-10):

- `users` 是账号;`calendars` 是日历,分「个人日历」(每人一个,仅本人可见可写)
  与「家庭共享日历」(成员可见,写权限由 `calendar_members.role` 决定)。
- 每个事件必须属于一个日历(`events.calendar_id`);可见性与写权限都由日历推导。
- 订阅地址对应 `feed_tokens` 的一行:`user_id` 有值表示「某个成员可见范围」的合并日历,
  为 `NULL` 表示「家庭共享日历」范围(给没有账号的外部收件人/长辈用)。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.timeutil import utcnow

UID_DOMAIN = "sisi-calendar"

EVENT_STATUSES = ("confirmed", "tentative", "cancelled")

# ---------- 账号角色 ----------
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
USER_ROLES = (ROLE_ADMIN, ROLE_MEMBER)

# ---------- 日历类型 ----------
CALENDAR_KIND_PERSONAL = "personal"
CALENDAR_KIND_SHARED = "shared"
CALENDAR_KINDS = (CALENDAR_KIND_PERSONAL, CALENDAR_KIND_SHARED)

# ---------- 日历成员权限 ----------
MEMBER_ROLE_OWNER = "owner"
MEMBER_ROLE_EDITOR = "editor"
MEMBER_ROLE_VIEWER = "viewer"
CALENDAR_MEMBER_ROLES = (MEMBER_ROLE_OWNER, MEMBER_ROLE_EDITOR, MEMBER_ROLE_VIEWER)
#: 能写这个日历的角色(个人日历的写权限只看归属,不看这张表)
WRITE_MEMBER_ROLES = (MEMBER_ROLE_OWNER, MEMBER_ROLE_EDITOR)

#: 订阅令牌的作用域:user_id 为 NULL 时代表「家庭共享日历」范围
FAMILY_FEED_LABEL = "家庭共享日历"

#: 新建账号时按顺序取的默认颜色,保证同一家庭的日历在界面上可区分
DEFAULT_USER_COLORS = (
    "#2563eb",
    "#db2777",
    "#059669",
    "#f59e0b",
    "#7c3aed",
    "#0891b2",
)

# 事件来源:本机管理界面 / 远程 Agent
EVENT_SOURCE_WEB = "web"
EVENT_SOURCE_AGENT = "agent"
EVENT_SOURCES = (EVENT_SOURCE_WEB, EVENT_SOURCE_AGENT)

CHANGE_CREATED = "created"
CHANGE_UPDATED = "updated"
CHANGE_DELETED = "deleted"
CHANGE_MANUAL = "manual"
CHANGE_TYPES = (CHANGE_CREATED, CHANGE_UPDATED, CHANGE_DELETED, CHANGE_MANUAL)

DELIVERY_PENDING = "pending"
DELIVERY_SENDING = "sending"
DELIVERY_SENT = "sent"
DELIVERY_RETRY_WAIT = "retry_wait"
DELIVERY_FAILED = "failed"
DELIVERY_STATUSES = (
    DELIVERY_PENDING,
    DELIVERY_SENDING,
    DELIVERY_SENT,
    DELIVERY_RETRY_WAIT,
    DELIVERY_FAILED,
)
DELIVERY_TERMINAL_STATUSES = (DELIVERY_SENT, DELIVERY_FAILED)


class Base(DeclarativeBase):
    pass


def new_event_uid() -> str:
    """生成永久稳定的 ICS UID,创建后不得更改。"""
    return f"{uuid.uuid4()}@{UID_DOMAIN}"


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_username(value: str) -> str:
    """账号名统一小写并去空白,避免 `Mama` 与 `mama` 变成两个账号。"""
    return value.strip().lower()


class User(Base):
    """家庭账号。3–5 人的小规模场景:没有邀请、没有找回流程,由管理员重置口令。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_MEMBER)
    #: 界面与 ICS 里给这个人的日历用的颜色,个人日历跟随该值
    color: Mapped[str] = mapped_column(String(16), nullable=False, default=DEFAULT_USER_COLORS[0])
    #: 该成员自己的时区;NULL 表示跟随应用默认时区
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 管理员创建/重置口令后置位,首次登录强制改密
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 邮箱验证:通过时间。为空且账号有邮箱 = 未验证(成员在验证前不能使用系统)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 验证码的 HMAC(服务端密钥做键),不存明文;有效期、尝试次数与上次发送时间一并记录
    email_verify_code_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email_verify_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    email_verify_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    email_verify_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("role IN ('admin','member')", name="ck_users_role"),
        Index("ix_users_enabled", "enabled"),
    )

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def label(self) -> str:
        return self.display_name or self.username

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<User {self.username} role={self.role}>"


class Calendar(Base):
    """一个日历。个人日历每人一个;家庭共享日历由管理员创建、成员按角色访问。"""

    __tablename__ = "calendars"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CALENDAR_KIND_PERSONAL, index=True
    )
    #: 个人日历的归属人;共享日历为 NULL
    owner_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    color: Mapped[str] = mapped_column(String(16), nullable=False, default=DEFAULT_USER_COLORS[0])
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        CheckConstraint("kind IN ('personal','shared')", name="ck_calendars_kind"),
    )

    @property
    def is_personal(self) -> bool:
        return self.kind == CALENDAR_KIND_PERSONAL

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Calendar {self.name} kind={self.kind}>"


class CalendarMember(Base):
    """共享日历的成员与角色。个人日历不使用这张表(可见性由归属决定)。"""

    __tablename__ = "calendar_members"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    calendar_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("calendars.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MEMBER_ROLE_VIEWER
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("calendar_id", "user_id", name="uq_calendar_members_pair"),
        CheckConstraint(
            "role IN ('owner','editor','viewer')", name="ck_calendar_members_role"
        ),
    )


class InviteCode(Base):
    """邀请码:成员自助注册的唯一入口。

    与远程接入令牌同级处理:库里只存 `sha256(明文)`,明文只在创建成功那一次响应里出现。
    """

    __tablename__ = "invite_codes"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    code_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    @property
    def remaining_uses(self) -> int:
        return max(0, int(self.max_uses or 0) - int(self.used_count or 0))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<InviteCode {self.label} used={self.used_count}/{self.max_uses}>"


class FeedToken(Base):
    """只读订阅令牌:一枚令牌对应一个订阅地址。

    `user_id` 有值 → 该成员可见范围(个人 + 所有共享日历)的合并日历;
    `user_id` 为 `NULL` → 家庭共享日历范围,给没有账号的外部收件人订阅。
    与 `agent_tokens` 一样只存 `sha256`,明文只在创建时返回一次。
    """

    __tablename__ = "feed_tokens"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Event(Base):
    """日程事件。"""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    uid: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, default=new_event_uid, nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 备注里的链接(iPhone 日历的「URL」字段),导出为 VEVENT 的 URL 属性
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # 归属日历与创建人。可见性与写权限都由日历推导,见 app/accounts.py。
    # 历史数据在启动引导时回填到首位管理员的个人日历,因此这里允许 NULL。
    calendar_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("calendars.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # 提醒:提前分钟数的 JSON 列表,如 "[1440, 15, 0]";空表示不提醒。
    # 语义与合法取值见 app/reminders.py,导出为 VEVENT 下的多个 VALARM。
    reminders: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 非全天事件:UTC 绝对时间
    start_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 全天事件:日期区间,结束日期不含在事件内
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date_exclusive: Mapped[date | None] = mapped_column(Date, nullable=True)

    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rrule: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")

    # 来源与 Agent 幂等键:client_ref 由调用方提供,重复提交返回同一事件而不是新建
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EVENT_SOURCE_WEB, index=True
    )
    client_ref: Mapped[str | None] = mapped_column(
        String(200), nullable=True, unique=True, index=True
    )

    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('confirmed','tentative','cancelled')", name="ck_events_status"
        ),
        CheckConstraint(
            "(all_day = 0 AND start_at_utc IS NOT NULL AND end_at_utc IS NOT NULL)"
            " OR (all_day = 1 AND start_date IS NOT NULL AND end_date_exclusive IS NOT NULL)",
            name="ck_events_time_shape",
        ),
        Index("ix_events_active_start", "deleted_at", "start_at_utc"),
        Index("ix_events_active_date", "deleted_at", "start_date"),
        Index("ix_events_calendar_start", "calendar_id", "deleted_at", "start_at_utc"),
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Event {self.id} {self.title!r} all_day={self.all_day}>"


class SyncRecipient(Base):
    """邮件收件人。会收到一份日历快照,而不是单个事件。

    收件人有两种身份,范围由 `recipient_scope_user_id()` 统一解释:

    - **账号收件人**(`user_id` 非空):由 `users.email` 派生,附件是该成员可见范围
      (个人 + 共享)里「尚未结束」的日程;每个账号最多一行。
    - **外部收件人**(`user_id` 为空):默认只收家庭共享日历;`scope_user_id` 非空表示
      「沿用某个账号的可见范围」——单用户版升级上来的历史邮箱靠它保持原有收信范围。

    `user_id` 与 `scope_user_id` 必须分开:前者是「这行属于哪个账号」的身份标记(唯一),
    后者是「发给谁看的范围」(可重复)。混用会让多个历史邮箱挤在同一个账号名下,
    既破坏了「一个账号一行账号收件人」,也让启动引导在第二次运行时崩掉。
    """

    __tablename__ = "sync_recipients"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 对应账号;为空表示这是外部邮箱(没有登录能力)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: 收信范围对应的账号;为空表示只收家庭共享日历
    scope_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CalendarExport(Base):
    """不可变的导出版本快照索引,供邮件任务固定附件内容。"""

    __tablename__ = "calendar_exports"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_path: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class MailDelivery(Base):
    """持久化发信任务。幂等键为 export_version + recipient_id。"""

    __tablename__ = "mail_deliveries"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    export_version: Mapped[int] = mapped_column(
        Integer, ForeignKey("calendar_exports.version", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sync_recipients.id", ondelete="CASCADE"), nullable=False
    )
    # 收件人快照:即使之后删除了收件人,历史记录仍可读
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    trigger_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 事件信息快照:重试时仍能还原当时的主语与时间描述
    event_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    event_time_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    change_type: Mapped[str] = mapped_column(String(20), nullable=False, default=CHANGE_MANUAL)
    attachment_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=DELIVERY_PENDING)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "export_version", "recipient_id", name="uq_mail_deliveries_version_recipient"
        ),
        CheckConstraint(
            "status IN ('pending','sending','sent','retry_wait','failed')",
            name="ck_mail_deliveries_status",
        ),
        Index("ix_mail_deliveries_status_next", "status", "next_attempt_at"),
    )


class CalendarExportState(Base):
    """单行表:当前发布版本与一致性状态。"""

    __tablename__ = "calendar_export_state"

    SINGLETON_ID = 1

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=SINGLETON_ID)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dirty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class AppSetting(Base):
    """非敏感键值配置。SMTP 密码不进入此表。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class AgentToken(Base):
    """远程 Agent 接入令牌。

    只存 `sha256(token)` 哈希,明文不在库中:创建时把明文返回给界面一次即丢弃,
    之后只能看到指纹(哈希前 16 位,与审计日志 `agent:<指纹>` 对得上)。
    与 `.env` 里的 `AGENT_API_TOKEN` 取并集鉴权,env 作为兜底/恢复通道。

    每枚令牌绑定一个账号(`user_id`)与一个默认落库日历(`calendar_id`):
    Agent 新建的日程写进该日历,权限按该账号判定,因此不会越权写别人的私人日历。
    """

    __tablename__ = "agent_tokens"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    #: 令牌归属账号;为空的历史令牌在启动引导时补成首位管理员
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: 默认落库日历;为空表示用归属账号的个人日历
    calendar_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
