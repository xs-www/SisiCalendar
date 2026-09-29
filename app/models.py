"""SQLAlchemy 数据模型。

对应开发文档第 7 节。SQLite 是日程数据的唯一事实来源,`calendar.ics` 只是派生
发布文件,因此这里不保存任何 ICS 正文,只保存生成状态与版本快照索引。
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
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Event {self.id} {self.title!r} all_day={self.all_day}>"


class SyncRecipient(Base):
    """参与邮件同步的人员。会收到完整日历,而不是单个事件。"""

    __tablename__ = "sync_recipients"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
    """

    __tablename__ = "agent_tokens"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
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
