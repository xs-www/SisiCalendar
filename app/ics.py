"""iCalendar(RFC 5545)导出。

严格按开发文档 6.4 节:

- UTF-8、CRLF 行结束符、按 75 字节折叠(由 icalendar 负责)。
- 根对象含 `VERSION:2.0`、稳定的 `PRODID` 与 `CALSCALE:GREGORIAN`,**不设置 `METHOD`**,
  以表明这是日历快照而不是会议邀请。
- 每个事件使用永久稳定的 `UID`,`DTSTAMP` 为 UTC,修改时递增 `SEQUENCE` 并更新 `LAST-MODIFIED`。
- 全天事件使用 `VALUE=DATE`,`DTEND` 为不含在事件内的次日日期。
- 有时间的事件输出原始 `TZID` 并生成对应 `VTIMEZONE`,保证跨夏令时后墙钟时间不变。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from icalendar import Calendar, Timezone, vRecur
from icalendar import Event as ICalEvent

from app.config import Settings
from app.models import Event
from app.recurrence import RRuleError, rebuild_rule, validate_rrule
from app.timeutil import as_aware_utc, get_zone, local_to_utc, utc_to_local, utcnow

logger = logging.getLogger(__name__)

PRODID = "-//SisiCalendar//Sisi Calendar 1.0//CN"

# 无限重复规则时,VTIMEZONE 需要覆盖的未来年份
VTIMEZONE_FUTURE_YEARS = 5


def _component_rrule(event: Event) -> vRecur | None:
    """把数据库中的 RRULE 转成组件属性。

    RFC 5545 要求:DTSTART 带 TZID 时 UNTIL 必须是 UTC;DTSTART 是 DATE 时
    UNTIL 必须是 DATE。数据库里保存的是可读形式,这里按取值类型还原。
    """
    if not event.rrule:
        return None
    try:
        parsed = validate_rrule(event.rrule)
    except RRuleError:
        logger.warning("跳过无法解析的重复规则 event=%s", event.id)
        return None

    if parsed.until is None:
        rule = parsed.raw
    elif event.all_day:
        rule = rebuild_rule(parsed, f"{parsed.until.date():%Y%m%d}")
    elif parsed.until_is_date:
        tz_name = event.timezone or "UTC"
        utc_until = local_to_utc(parsed.until, tz_name)
        rule = rebuild_rule(parsed, f"{utc_until:%Y%m%dT%H%M%SZ}")
    else:
        rule = rebuild_rule(parsed, f"{parsed.until:%Y%m%dT%H%M%SZ}")

    try:
        return vRecur.from_ical(rule)
    except Exception:  # noqa: BLE001 - 防御性
        logger.warning("无法构造 RRULE event=%s", event.id)
        return None


def _build_vevent(event: Event, tz_name: str) -> ICalEvent:
    component = ICalEvent()
    component.add("uid", event.uid)
    component.add("summary", event.title)
    if event.description:
        component.add("description", event.description)
    if event.location:
        component.add("location", event.location)

    # DTSTAMP 取事件自身的修订时间,而不是“导出时刻”。
    # 导出器靠内容哈希判断是否产生新版本;若 DTSTAMP 用挂钟时间,跨秒重新导出就会得到
    # 不同哈希,于是“没有改动”的保存也会递增版本并向所有同步人员重发完整日历。
    revision = as_aware_utc(event.updated_at) or as_aware_utc(utcnow())
    component.add("dtstamp", revision)
    component.add("created", as_aware_utc(event.created_at) or revision)
    component.add("last-modified", revision)
    component.add("sequence", int(event.sequence or 0))
    component.add("status", (event.status or "confirmed").upper())
    component.add("class", "PRIVATE")
    component.add("x-sisi-event-id", event.id)

    if event.all_day:
        start_date = event.start_date or date.today()
        end_date = event.end_date_exclusive or (start_date + timedelta(days=1))
        component.add("dtstart", start_date)
        component.add("dtend", end_date)
    elif event.start_at_utc is not None and event.end_at_utc is not None:
        zone = get_zone(tz_name)
        local_start = utc_to_local(event.start_at_utc, tz_name).replace(tzinfo=None)
        local_end = utc_to_local(event.end_at_utc, tz_name).replace(tzinfo=None)
        component.add("dtstart", local_start, parameters={"TZID": str(zone)})
        component.add("dtend", local_end, parameters={"TZID": str(zone)})

    rule = _component_rrule(event)
    if rule is not None:
        component.add("rrule", rule)
    return component


def _used_timezones(events: list[Event]) -> dict[str, tuple[date, date]]:
    """收集需要生成 VTIMEZONE 的时区,以及各自需要覆盖的日期区间。"""
    today = utcnow().date()
    zones: dict[str, tuple[date, date]] = {}
    for event in events:
        if event.all_day or event.start_at_utc is None:
            continue
        tz_name = event.timezone or "UTC"
        if tz_name.upper() in {"UTC", "GMT"}:
            continue
        start_local = utc_to_local(event.start_at_utc, tz_name).date()
        end_local = (
            utc_to_local(event.end_at_utc, tz_name).date()
            if event.end_at_utc is not None
            else start_local
        )
        first, last = zones.get(tz_name, (start_local, end_local))
        zones[tz_name] = (min(first, start_local), max(last, end_local))

    result: dict[str, tuple[date, date]] = {}
    for tz_name, (first, last) in zones.items():
        future = last + timedelta(days=365 * VTIMEZONE_FUTURE_YEARS)
        result[tz_name] = (first - timedelta(days=370), max(future, today))
    return result


def _build_vtimezone(tz_name: str, first_date: date, last_date: date) -> Timezone | None:
    try:
        return Timezone.from_tzinfo(
            get_zone(tz_name), tzid=tz_name, first_date=first_date, last_date=last_date
        )
    except Exception as exc:  # noqa: BLE001 - 个别时区数据可能缺失
        logger.warning("无法生成 VTIMEZONE tz=%s err=%s", tz_name, exc)
        return None


def build_calendar(
    events: list[Event],
    *,
    calendar_name: str,
    default_timezone: str,
    settings: Settings | None = None,
    exported_at: datetime | None = None,
) -> bytes:
    """生成完整日历文件内容。"""
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("x-wr-calname", calendar_name or "Sisi Calendar")
    calendar.add("x-wr-timezone", default_timezone or "UTC")
    if settings is not None and settings.feed_cache_seconds > 0:
        minutes = max(1, settings.feed_cache_seconds // 60)
        calendar.add(
            "refresh-interval",
            timedelta(minutes=minutes),
            parameters={"VALUE": "DURATION"},
        )

    for tz_name, (first_date, last_date) in _used_timezones(events).items():
        component = _build_vtimezone(tz_name, first_date, last_date)
        if component is not None:
            calendar.add_component(component)

    for event in events:
        timezone_name = event.timezone or default_timezone or "UTC"
        if not event.all_day and timezone_name.upper() in {"UTC", "GMT"}:
            timezone_name = "UTC"
        calendar.add_component(_build_vevent(event, timezone_name))

    return calendar.to_ical()


def event_sort_key(event: Event) -> tuple[date, str]:
    anchor = event.start_date if event.all_day else (
        event.start_at_utc.date() if event.start_at_utc else date.min
    )
    return anchor or date.min, event.title or ""
