"""事件领域逻辑:输入校验、查询、写入与重复事件展开。

设计要点:

- 输入时间带偏移量时按偏移解释,不带偏移量时按事件的 IANA 时区解释,
  最终统一以 naive UTC 落库。
- 重复事件在本地墙钟时间上展开,保证跨夏令时后 09:00 仍然是 09:00。
- 修改时只有在字段确实变化才递增 `sequence`(RFC 5545 语义)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from dateutil.rrule import rrulestr
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import EVENT_SOURCE_WEB, EVENT_STATUSES, Event, new_event_uid
from app.recurrence import (
    RRuleError,
    build_rrule_from_form,
    describe_rrule,
    rebuild_rule,
    validate_rrule,
)
from app.schemas import EventWrite
from app.timeutil import (
    as_utc,
    get_zone,
    is_valid_timezone,
    iso_utc,
    local_to_utc,
    normalize_datetime_text,
    parse_date,
    utc_to_local,
    utcnow,
)

logger = logging.getLogger(__name__)

MAX_TITLE = 200
MAX_DESCRIPTION = 10000
MAX_LOCATION = 500

# 单次范围查询展开的重复实例上限,避免无限重复规则拖垮界面
MAX_OCCURRENCES = 2000


class EventValidationError(ValueError):
    """字段级校验失败,由路由转换为 422 结构化错误。"""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {value}" for key, value in fields.items()))
        self.fields = fields


class EventConflictError(RuntimeError):
    """乐观锁冲突。"""


# --------------------------------------------------------------------------
# 输入校验
# --------------------------------------------------------------------------
def _parse_input_datetime(text: str, tz_name: str) -> datetime | None:
    """把输入时间解析为 naive UTC。无偏移量时按事件时区解释。

    接受 ISO 8601 及其常见变体:空格分隔、缺秒、纯日期、斜杠或中文日期分隔符。
    """
    value = normalize_datetime_text(text)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return local_to_utc(parsed, tz_name)
    return as_utc(parsed)


def _default_timezone(settings: Settings) -> str:
    return settings.default_timezone if is_valid_timezone(settings.default_timezone) else "UTC"


def normalize_event_input(
    payload: EventWrite,
    *,
    existing: Event | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    """把请求体合并到既有事件上,返回可直接写入列的字典。

    校验失败时抛出 `EventValidationError`,一次性返回所有字段错误。
    """
    resolved = settings or get_settings()
    provided = payload.model_fields_set
    errors: dict[str, str] = {}

    if "rrule" in provided and "recurrence" in provided:
        errors["rrule"] = "rrule 与 recurrence 不能同时提交"

    def current(attribute: str, default: object = None) -> object:
        if existing is not None:
            return getattr(existing, attribute)
        return default

    # ---------- 文本字段 ----------
    if "title" in provided:
        title = (payload.title or "").strip()
        if not title:
            errors["title"] = "标题不能为空"
        elif len(title) > MAX_TITLE:
            errors["title"] = f"标题最多 {MAX_TITLE} 个字符"
    else:
        title = current("title")
        if title is None and existing is None:
            errors["title"] = "标题不能为空"

    description: str | None
    if "description" in provided:
        description = (payload.description or "").strip() or None
        if description and len(description) > MAX_DESCRIPTION:
            errors["description"] = f"说明最多 {MAX_DESCRIPTION} 个字符"
    else:
        description = current("description")  # type: ignore[assignment]

    location: str | None
    if "location" in provided:
        location = (payload.location or "").strip() or None
        if location and len(location) > MAX_LOCATION:
            errors["location"] = f"地点最多 {MAX_LOCATION} 个字符"
    else:
        location = current("location")  # type: ignore[assignment]

    # ---------- 基本枚举 ----------
    if "status" in provided and payload.status is not None:
        status = payload.status
    else:
        status = str(current("status", "confirmed"))
    if status not in EVENT_STATUSES:
        errors["status"] = "status 只能是 confirmed、tentative 或 cancelled"

    all_day = bool(payload.all_day) if "all_day" in provided else bool(current("all_day", False))

    if "timezone" in provided and payload.timezone is not None:
        timezone = payload.timezone.strip()
    else:
        timezone = str(current("timezone") or _default_timezone(resolved))
    if not is_valid_timezone(timezone):
        errors["timezone"] = f"未知的 IANA 时区名:{timezone}"

    # ---------- 时间 ----------
    start_at_utc: datetime | None = None
    end_at_utc: datetime | None = None
    start_date: date | None = None
    end_date_exclusive: date | None = None

    if all_day:
        if "start_date" in provided:
            raw_start = (payload.start_date or "").strip()
            start_date = parse_date(raw_start) if raw_start else None
            if raw_start and start_date is None:
                errors["start_date"] = "开始日期格式应为 YYYY-MM-DD"
        elif existing is not None and existing.all_day:
            start_date = existing.start_date
        elif existing is None and payload.start_at:
            parsed = _parse_input_datetime(payload.start_at, timezone)
            start_date = utc_to_local(parsed, timezone).date() if parsed else None

        if start_date is None:
            errors.setdefault("start_date", "全天事件必须提供开始日期")

        if "end_date" in provided:
            raw_end = (payload.end_date or "").strip()
            end_date_exclusive = parse_date(raw_end) if raw_end else None
            if raw_end and end_date_exclusive is None:
                errors["end_date"] = "结束日期格式应为 YYYY-MM-DD"
        elif existing is not None and existing.all_day:
            end_date_exclusive = existing.end_date_exclusive
        if end_date_exclusive is None and start_date is not None:
            end_date_exclusive = start_date + timedelta(days=1)

        if (
            start_date is not None
            and end_date_exclusive is not None
            and end_date_exclusive <= start_date
        ):
            errors["end_date"] = "结束日期必须晚于开始日期(结束日期不含在事件内)"
    else:
        if "start_at" in provided:
            raw = (payload.start_at or "").strip()
            start_at_utc = _parse_input_datetime(raw, timezone) if raw else None
            if raw and start_at_utc is None:
                errors["start_at"] = "开始时间格式应为 ISO 8601"
        elif existing is not None and not existing.all_day:
            start_at_utc = existing.start_at_utc
        elif existing is None and payload.start_date:
            # 允许只给日期:按当天本地时间 00:00 处理
            parsed_date = parse_date(payload.start_date)
            if parsed_date:
                start_at_utc = local_to_utc(
                    datetime(parsed_date.year, parsed_date.month, parsed_date.day), timezone
                )

        if "end_at" in provided:
            raw = (payload.end_at or "").strip()
            end_at_utc = _parse_input_datetime(raw, timezone) if raw else None
            if raw and end_at_utc is None:
                errors["end_at"] = "结束时间格式应为 ISO 8601"
        elif existing is not None and not existing.all_day:
            end_at_utc = existing.end_at_utc
        if end_at_utc is None and start_at_utc is not None:
            requested = payload.duration_minutes if "duration_minutes" in provided else None
            minutes = max(1, requested or resolved.default_event_duration_minutes)
            end_at_utc = start_at_utc + timedelta(minutes=minutes)

        if start_at_utc is None:
            errors.setdefault("start_at", "非全天事件必须提供开始时间")
        if end_at_utc is None:
            errors.setdefault("end_at", "非全天事件必须提供结束时间")
        if (
            start_at_utc is not None
            and end_at_utc is not None
            and end_at_utc <= start_at_utc
        ):
            errors["end_at"] = "结束时间必须晚于开始时间"

    # ---------- 重复规则 ----------
    rrule_value: str | None
    if "rrule" in provided and "recurrence" not in provided:
        raw_rule = (payload.rrule or "").strip()
        if not raw_rule:
            rrule_value = None
        else:
            try:
                rrule_value = validate_rrule(raw_rule).raw
            except RRuleError as exc:
                errors["rrule"] = str(exc)
                rrule_value = current("rrule")  # type: ignore[assignment]
    elif "recurrence" in provided:
        try:
            rrule_value = build_rrule_from_form(
                payload.recurrence or "none", payload.recurrence_until
            )
        except RRuleError as exc:
            errors["recurrence"] = str(exc)
            rrule_value = current("rrule")  # type: ignore[assignment]
    else:
        rrule_value = current("rrule")  # type: ignore[assignment]

    if errors:
        raise EventValidationError(errors)

    return {
        "title": title,
        "description": description,
        "location": location,
        "all_day": all_day,
        "timezone": timezone,
        "status": status,
        "start_at_utc": start_at_utc,
        "end_at_utc": end_at_utc,
        "start_date": start_date,
        "end_date_exclusive": end_date_exclusive,
        "rrule": rrule_value,
    }


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------
def create_event(
    session: Session,
    payload: EventWrite,
    *,
    settings: Settings | None = None,
    source: str = EVENT_SOURCE_WEB,
    client_ref: str | None = None,
) -> Event:
    values = normalize_event_input(payload, existing=None, settings=settings)
    now = utcnow()
    event = Event(
        uid=new_event_uid(),
        sequence=0,
        created_at=now,
        updated_at=now,
        source=source,
        client_ref=(client_ref or None),
        **values,  # type: ignore[arg-type]
    )
    session.add(event)
    session.flush()
    return event


def update_event(
    session: Session,
    event: Event,
    payload: EventWrite,
    *,
    settings: Settings | None = None,
) -> bool:
    """应用修改。返回是否真的有字段变化(用于决定是否递增 sequence)。"""
    if payload.expected_updated_at:
        expected = _parse_expected_updated_at(payload.expected_updated_at)
        if expected is not None and event.updated_at is not None and expected != event.updated_at:
            raise EventConflictError("事件已被其他页面修改,请刷新后重试")

    values = normalize_event_input(payload, existing=event, settings=settings)
    changed = False
    for key, value in values.items():
        if getattr(event, key) != value:
            setattr(event, key, value)
            changed = True
    if changed:
        event.sequence = (event.sequence or 0) + 1
        event.updated_at = utcnow()
        session.flush()
    return changed


def _parse_expected_updated_at(value: str) -> datetime | None:
    """解析乐观锁时间戳。

    这里刻意不走 `as_utc`,因为后者会把微秒归零,而 `updated_at` 的并发判定
    需要微秒级精度。
    """
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def soft_delete_event(session: Session, event: Event) -> None:
    event.deleted_at = utcnow()
    event.updated_at = event.deleted_at
    event.sequence = (event.sequence or 0) + 1
    session.flush()


def get_event(session: Session, event_id: str, *, include_deleted: bool = False) -> Event | None:
    stmt = select(Event).where(Event.id == event_id)
    if not include_deleted:
        stmt = stmt.where(Event.deleted_at.is_(None))
    return session.execute(stmt).scalar_one_or_none()


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
@dataclass
class DateRange:
    """查询范围。`end_*` 为不含边界。"""

    start_utc: datetime
    end_utc: datetime
    start_date: date
    end_date: date
    settings: Settings = field(repr=False, default_factory=get_settings)


def build_range(
    start: str | None,
    end: str | None,
    *,
    settings: Settings | None = None,
    default_days: int = 42,
) -> DateRange:
    """把 API 传入的 start/end 解析成查询范围。

    纯日期按应用默认时区的当地午夜解释,`end` 为不含边界。
    """
    resolved = settings or get_settings()
    tz_name = _default_timezone(resolved)

    start_dt = _parse_range_bound(start, tz_name, is_end=False)
    end_dt = _parse_range_bound(end, tz_name, is_end=True)
    if start_dt is None:
        start_dt = local_to_utc(datetime.combine(utcnow().date(), datetime.min.time()), tz_name)
    if end_dt is None:
        end_dt = start_dt + timedelta(days=default_days)
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(days=1)

    return DateRange(
        start_utc=start_dt,
        end_utc=end_dt,
        start_date=utc_to_local(start_dt, tz_name).date(),
        end_date=utc_to_local(end_dt, tz_name).date(),
        settings=resolved,
    )


def _parse_range_bound(value: str | None, tz_name: str, *, is_end: bool) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if "T" not in text and " " not in text:
        parsed_date = parse_date(text)
        if parsed_date is None:
            return None
        return local_to_utc(
            datetime(parsed_date.year, parsed_date.month, parsed_date.day), tz_name
        )
    parsed = _parse_input_datetime(text, tz_name)
    if parsed is None:
        return None
    if is_end and parsed.time() == datetime.min.time():
        return parsed
    return parsed


def query_events(
    session: Session,
    *,
    date_range: DateRange | None = None,
    keyword: str | None = None,
    status: str | None = None,
    source: str | None = None,
    all_day: bool | None = None,
    recurring: bool | None = None,
    include_deleted: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[Event], int]:
    """按条件查询事件,返回 (结果, 命中总数)。"""
    conditions = []
    if not include_deleted:
        conditions.append(Event.deleted_at.is_(None))

    if date_range is not None:
        conditions.append(
            or_(
                (
                    (Event.all_day.is_(False))
                    & (Event.start_at_utc < date_range.end_utc)
                    & (Event.end_at_utc > date_range.start_utc)
                ),
                (
                    (Event.all_day.is_(True))
                    & (Event.start_date < date_range.end_date)
                    & (Event.end_date_exclusive > date_range.start_date)
                ),
                # 重复事件的首个实例可能早于范围起点,但仍会落在范围内
                (
                    (Event.rrule.is_not(None))
                    & (Event.deleted_at.is_(None))
                    & (
                        (
                            (Event.all_day.is_(False))
                            & (Event.start_at_utc < date_range.end_utc)
                        )
                        | (
                            (Event.all_day.is_(True))
                            & (Event.start_date < date_range.end_date)
                        )
                    )
                ),
            )
        )

    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                Event.title.ilike(pattern),
                Event.description.ilike(pattern),
                Event.location.ilike(pattern),
            )
        )

    if status:
        conditions.append(Event.status == status)
    if source:
        conditions.append(Event.source == source)
    if all_day is not None:
        conditions.append(Event.all_day.is_(all_day))
    if recurring is not None:
        conditions.append(
            Event.rrule.is_not(None) if recurring else Event.rrule.is_(None)
        )

    count_stmt = select(func.count()).select_from(Event)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = int(session.execute(count_stmt).scalar_one())

    stmt = select(Event)
    if conditions:
        stmt = stmt.where(*conditions)
    stmt = stmt.order_by(Event.all_day.desc(), Event.start_at_utc.asc(), Event.start_date.asc())
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)

    events = list(session.execute(stmt).scalars())
    return events, total


# --------------------------------------------------------------------------
# 重复事件展开
# --------------------------------------------------------------------------
@dataclass
class Occurrence:
    event: Event
    start_utc: datetime | None
    end_utc: datetime | None
    start_date: date | None
    end_date_exclusive: date | None
    is_instance: bool = False

    @property
    def key(self) -> str:
        if self.event.all_day and self.start_date is not None:
            return f"{self.event.id}:{self.start_date.isoformat()}"
        if self.start_utc is not None:
            return f"{self.event.id}:{self.start_utc.strftime('%Y%m%dT%H%M%S')}"
        return self.event.id


def _rrule_for_local(rule: str, tz_name: str) -> str:
    """把 UNTIL 统一成本地墙钟时间,使展开在本地时间轴上正确。

    纯日期形式的 UNTIL 本身就表示“当地这一天结束”,不做时区换算。
    """
    try:
        parsed = validate_rrule(rule)
    except RRuleError:
        return rule
    if parsed.until is None:
        return parsed.raw
    if parsed.until_is_date:
        local_until = parsed.until
    else:
        local_until = utc_to_local(parsed.until, tz_name).replace(tzinfo=None)
    return rebuild_rule(parsed, local_until.strftime("%Y%m%dT%H%M%S"))


def expand_occurrences(
    event: Event,
    date_range: DateRange,
    *,
    limit: int = MAX_OCCURRENCES,
) -> list[Occurrence]:
    """把事件在查询范围内展开成若干实例。非重复事件最多返回一个。"""
    if not event.rrule:
        if _overlaps(event, date_range):
            return [
                Occurrence(
                    event=event,
                    start_utc=event.start_at_utc,
                    end_utc=event.end_at_utc,
                    start_date=event.start_date,
                    end_date_exclusive=event.end_date_exclusive,
                )
            ]
        return []

    tz_name = event.timezone or _default_timezone(date_range.settings)
    local_rule = _rrule_for_local(event.rrule, tz_name)

    if event.all_day:
        anchor_date = event.start_date or date_range.start_date
        dtstart = datetime(anchor_date.year, anchor_date.month, anchor_date.day)
        duration = timedelta(days=1) if event.end_date_exclusive is None else (
            event.end_date_exclusive - anchor_date
        )
    else:
        start_utc = event.start_at_utc or date_range.start_utc
        dtstart = utc_to_local(start_utc, tz_name).replace(tzinfo=None)
        duration = (event.end_at_utc or start_utc) - start_utc

    try:
        rule = rrulestr(local_rule, dtstart=dtstart)
    except Exception as exc:  # noqa: BLE001 - 规则可能来自历史数据
        logger.warning("无法展开重复规则 event=%s rule=%s err=%s", event.id, local_rule, exc)
        return [
            Occurrence(
                event=event,
                start_utc=event.start_at_utc,
                end_utc=event.end_at_utc,
                start_date=event.start_date,
                end_date_exclusive=event.end_date_exclusive,
            )
        ]

    window_start = utc_to_local(date_range.start_utc, tz_name).replace(tzinfo=None)
    window_end = utc_to_local(date_range.end_utc, tz_name).replace(tzinfo=None)
    # 往前多看一个实例的时长,避免跨越范围起点的事件被漏掉
    lookback = window_start - (duration if duration > timedelta(0) else timedelta(days=1))

    occurrences: list[Occurrence] = []
    for index, moment in enumerate(rule.between(lookback, window_end, inc=True)):
        if index >= limit:
            logger.warning("重复事件展开超过上限 event=%s limit=%s", event.id, limit)
            break
        if event.all_day:
            occ_start_date = moment.date()
            occ_end_date = occ_start_date + duration
            if occ_end_date <= date_range.start_date or occ_start_date >= date_range.end_date:
                continue
            occurrences.append(
                Occurrence(
                    event=event,
                    start_utc=None,
                    end_utc=None,
                    start_date=occ_start_date,
                    end_date_exclusive=occ_end_date,
                    is_instance=True,
                )
            )
        else:
            occ_start_utc = local_to_utc(moment, tz_name)
            occ_end_utc = occ_start_utc + duration
            if occ_end_utc <= date_range.start_utc or occ_start_utc >= date_range.end_utc:
                continue
            occurrences.append(
                Occurrence(
                    event=event,
                    start_utc=occ_start_utc,
                    end_utc=occ_end_utc,
                    start_date=None,
                    end_date_exclusive=None,
                    is_instance=True,
                )
            )
    return occurrences


def _overlaps(event: Event, date_range: DateRange) -> bool:
    if event.all_day:
        if event.start_date is None or event.end_date_exclusive is None:
            return False
        return (
            event.end_date_exclusive > date_range.start_date
            and event.start_date < date_range.end_date
        )
    if event.start_at_utc is None or event.end_at_utc is None:
        return False
    return event.end_at_utc > date_range.start_utc and event.start_at_utc < date_range.end_utc


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------
def serialize_event(event: Event) -> dict[str, object]:
    tz_name = event.timezone or "UTC"
    start_local = (
        utc_to_local(event.start_at_utc, tz_name).isoformat()
        if event.start_at_utc is not None
        else None
    )
    end_local = (
        utc_to_local(event.end_at_utc, tz_name).isoformat()
        if event.end_at_utc is not None
        else None
    )
    return {
        "id": event.id,
        "uid": event.uid,
        "title": event.title,
        "description": event.description,
        "location": event.location,
        "all_day": bool(event.all_day),
        "timezone": tz_name,
        "status": event.status,
        "source": event.source or EVENT_SOURCE_WEB,
        "client_ref": event.client_ref,
        "rrule": event.rrule,
        "rrule_label": describe_rrule(event.rrule),
        "start_at_utc": iso_utc(event.start_at_utc),
        "end_at_utc": iso_utc(event.end_at_utc),
        "start_local": start_local,
        "end_local": end_local,
        "start_date": event.start_date.isoformat() if event.start_date else None,
        "end_date_exclusive": (
            event.end_date_exclusive.isoformat() if event.end_date_exclusive else None
        ),
        "sequence": int(event.sequence or 0),
        "created_at": iso_utc(event.created_at),
        "updated_at": iso_utc(event.updated_at),
        "deleted_at": iso_utc(event.deleted_at),
    }


def serialize_occurrence(occurrence: Occurrence) -> dict[str, object]:
    """给日历界面用的形态:start/end 直接对齐 FullCalendar 约定。"""
    event = occurrence.event
    payload = serialize_event(event)
    is_instance = occurrence.is_instance
    payload["id"] = occurrence.key
    payload["event_id"] = event.id
    payload["is_instance"] = is_instance
    payload["recurring"] = bool(event.rrule)

    if event.all_day:
        assert occurrence.start_date is not None
        payload["start"] = occurrence.start_date.isoformat()
        payload["end"] = (
            occurrence.end_date_exclusive.isoformat()
            if occurrence.end_date_exclusive
            else None
        )
        payload["allDay"] = True
    else:
        payload["start"] = iso_utc(occurrence.start_utc)
        payload["end"] = iso_utc(occurrence.end_utc)
        payload["allDay"] = False
        if occurrence.start_utc is not None:
            tz_name = event.timezone or "UTC"
            payload["instance_start_local"] = utc_to_local(
                occurrence.start_utc, tz_name
            ).isoformat()
    return payload


def serialize_events_with_occurrences(
    events: list[Event],
    date_range: DateRange | None,
) -> list[dict[str, object]]:
    if date_range is None:
        return [serialize_event(event) for event in events]
    payloads: list[dict[str, object]] = []
    for event in events:
        for occurrence in expand_occurrences(event, date_range):
            payloads.append(serialize_occurrence(occurrence))
    payloads.sort(key=lambda item: (str(item.get("start") or ""), str(item.get("title") or "")))
    return payloads


def zone_label(tz_name: str) -> str:
    """给界面显示的时区标签。"""
    zone = get_zone(tz_name)
    return str(zone)
