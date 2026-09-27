"""Pydantic 输入/输出模型。

对外字段名与开发文档第 8 节的 REST API 对应;时间字段一律接受 ISO 8601,
带偏移量时按其偏移解释,不带偏移量时按事件的 `timezone` 字段解释。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RecurrenceChoice = Literal["none", "daily", "weekly", "monthly", "yearly"]


class ErrorDetail(BaseModel):
    code: str
    message: str
    fields: dict[str, str] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class EventWrite(BaseModel):
    """新建或修改事件的请求体。PATCH 时只提交需要改动的字段。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    location: str | None = None
    all_day: bool | None = None
    timezone: str | None = None
    status: Literal["confirmed", "tentative", "cancelled"] | None = None

    # 非全天事件
    start_at: str | None = None
    end_at: str | None = None

    # 全天事件,end_date 不含在事件内
    start_date: str | None = None
    end_date: str | None = None

    # 标准 RRULE(不含 "RRULE:" 前缀)
    rrule: str | None = None
    # 表单便捷输入
    recurrence: RecurrenceChoice | None = None
    recurrence_until: str | None = None

    # 乐观锁:提交上一次读取到的 updated_at,不匹配则返回 409
    expected_updated_at: str | None = None

    # 幂等键:远程 Agent 重试时带上同一个值,服务端返回已存在的事件而不是重复创建
    client_ref: str | None = Field(default=None, max_length=200)


class EventRead(BaseModel):
    id: str
    uid: str
    title: str
    description: str | None = None
    location: str | None = None
    all_day: bool
    timezone: str
    status: str
    source: str = "web"
    client_ref: str | None = None
    rrule: str | None = None
    rrule_label: str = "不重复"

    start_at_utc: str | None = None
    end_at_utc: str | None = None
    start_local: str | None = None
    end_local: str | None = None
    start_date: str | None = None
    end_date_exclusive: str | None = None

    sequence: int
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class EventListResponse(BaseModel):
    items: list[EventRead]
    total: int


class RecipientWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    display_name: str | None = None
    enabled: bool | None = None
    # 新增或启用人员时必须显式确认“会收到完整日历”
    acknowledge_full_calendar: bool = False


class RecipientRead(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    enabled: bool
    created_at: str
    updated_at: str
    last_sent_at: str | None = None
    masked_email: str = ""


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar_name: str | None = None
    default_timezone: str | None = None
    default_event_duration_minutes: int | None = None
    mail_max_attempts: int | None = None
    mail_backoff_minutes: str | None = None


class MailTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str | None = None


class SendLatestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(default=False, description="必须显式确认才会向全部启用人员发送")


class DeliveryRead(BaseModel):
    id: str
    export_version: int
    recipient_id: str
    recipient_email: str
    change_type: str
    status: str
    attempt_count: int
    next_attempt_at: str | None = None
    error_message: str | None = None
    created_at: str
    sent_at: str | None = None
    attachment_hash: str


class SyncStatusRead(BaseModel):
    version: int
    content_hash: str | None = None
    generated_at: str | None = None
    dirty: bool
    last_error: str | None = None
    size_bytes: int = 0
    file_exists: bool = False
    subscription_url: str
    mail_configured: bool
    recipients_total: int = 0
    recipients_enabled: int = 0
    deliveries_pending: int = 0
    deliveries_sent: int = 0
    deliveries_retry_wait: int = 0
    deliveries_failed: int = 0
    recent_deliveries: list[DeliveryRead] = Field(default_factory=list)
    last_delivery_success_at: str | None = None


class HealthRead(BaseModel):
    status: str
    version: str


class ReadyCheck(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class ReadyRead(BaseModel):
    status: str
    checks: list[ReadyCheck]
