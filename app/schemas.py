"""Pydantic 输入/输出模型。

对外字段名与开发文档第 8 节的 REST API 对应;时间字段一律接受 ISO 8601,
带偏移量时按其偏移解释,不带偏移量时按事件的 `timezone` 字段解释。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.reminders import (
    MAX_REMINDER_MINUTES,
    SEPARATOR_CODEPOINTS,
    normalize_reminders,
    parse_reminder_text,
)

RecurrenceChoice = Literal["none", "daily", "weekly", "monthly", "yearly"]

# 单个事件的提醒条数上限:挡住明显的脚本错误,同时远大于任何真实用量
MAX_REMINDER_COUNT = 50

REMINDER_FORMAT_HINT = (
    "提醒只能是整数分钟数,多个值用逗号分隔(如 15,1440);"
    f"0 表示事件开始时,正数表示提前(最多 {MAX_REMINDER_MINUTES} 分钟),负数表示延后"
)


def _reminders_before(value: Any) -> Any:
    """允许表单直接提交 `reminders=15,1440` 这样的字符串。

    JSON 形态仍然应该是数组;只有字符串会被解析成列表——空字符串按「未提供」处理,
    这样 `/api/agent/events` 的表单形态不会因为多带一个空字段而报错。
    拼错的片段**必须报错**(与「拼错参数应当失败」的既定约定一致),错误文案用中文,
    范围、去重与排序仍由 `app/events.py::normalize_event_input` 负责。
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        # 有人在表单里直接贴了 JSON 数组:交给 normalize 解析,失败按格式错误处理
        minutes, error = normalize_reminders(text)
        if error is not None or minutes is None:
            raise ValueError(REMINDER_FORMAT_HINT)
        return minutes
    separators = {ord(char): " " for char in SEPARATOR_CODEPOINTS}
    for chunk in text.translate(separators).split():
        if parse_reminder_text(chunk) == []:
            raise ValueError(REMINDER_FORMAT_HINT)
    return parse_reminder_text(text)


def _timezone_before(value: Any) -> Any:
    """允许把「序号」当字符串收下。

    `timezone` 既接受 IANA 名,也接受设置页列出的序号(如 `1`)或中文名(如「北京时间」)。
    JSON 里的数字先转成字符串,后续统一由 `app.timezones.resolve_timezone` 解析,
    这样 `"timezone": 1` 与 `"timezone": "1"` 行为一致。
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if float(value).is_integer() else str(value)
    return value


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
    # 备注里的链接(如会议地址、文档页),导出为 ICS 的 URL 属性
    url: str | None = Field(default=None, max_length=1000)
    # 提醒:提前分钟数的列表,0 表示事件开始时;非法值由 app/reminders.py 统一报 422
    reminders: list[int] | None = Field(default=None, max_length=MAX_REMINDER_COUNT)
    # 非全天事件
    start_at: str | None = None
    end_at: str | None = None
    # 便捷写法:只给开始时间 + 时长,或干脆省略(用默认时长)。
    # 主要给 iOS 快捷指令这类不方便算结束时间的调用方使用。
    duration_minutes: int | None = Field(default=None, ge=1, le=10080)

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

    # 归属日历:新建时决定事件落到哪个日历(必须可写),修改时相当于「移动到日历」。
    # 不给时:新建用默认可写日历,修改沿用原日历。
    calendar_id: str | None = Field(default=None, max_length=36)

    # 幂等键:远程 Agent 重试时带上同一个值,服务端返回已存在的事件而不是重复创建
    client_ref: str | None = Field(default=None, max_length=200)

    # timezone 可以填序号(如 1)或中文名(如「北京时间」),这里先收下再统一解析
    _normalize_timezone = field_validator("timezone", mode="before")(_timezone_before)
    # 表单形态的提醒是逗号分隔字符串,先归一成列表
    _normalize_reminders = field_validator("reminders", mode="before")(_reminders_before)


class EventRead(BaseModel):
    id: str
    uid: str
    title: str
    description: str | None = None
    location: str | None = None
    url: str | None = None
    all_day: bool
    timezone: str
    status: str
    source: str = "web"
    client_ref: str | None = None
    # 归属日历与可见性(带 access 序列化时才有 calendar_name / calendar_color / can_write)
    calendar_id: str | None = None
    calendar_name: str | None = None
    calendar_color: str | None = None
    calendar_kind: str | None = None
    can_write: bool = False
    created_by_user_id: str | None = None
    rrule: str | None = None
    rrule_label: str = "不重复"
    # 提醒:提前分钟数与对应中文描述(如 15 → 「提前 15 分钟」)
    reminders: list[int] = Field(default_factory=list)
    reminder_labels: list[str] = Field(default_factory=list)

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
    # 新增或启用外部收件人时必须显式确认收信范围(只有家庭共享日历)
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

    # 同 EventWrite.timezone:默认时区也允许填序号或中文名
    _normalize_timezone = field_validator("default_timezone", mode="before")(_timezone_before)


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
    #: 发起这次操作的账号显示名(快照);升级前的历史任务为 None
    actor_label: str | None = None
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
    public_base_url: str
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
