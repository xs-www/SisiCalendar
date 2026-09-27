"""ICS 生成测试:对应开发文档 13.1 节的单元测试要求。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from app.config import Settings
from app.events import create_event
from app.exporter import export_calendar
from app.models import Event
from app.schemas import EventWrite
from app.timeutil import utcnow
from icalendar import Calendar


def _write(session, settings: Settings, **payload: object) -> Event:
    return create_event(session, EventWrite(**payload), settings=settings)


def _parse(content: bytes) -> Calendar:
    return Calendar.from_ical(content)


def test_timed_event_uses_tzid_and_vtimezone(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="团队周会,含逗号;与分号",
        start_at="2025-03-04T09:00:00",
        end_at="2025-03-04T10:00:00",
        location="会议室 A",
        description="第一行\n第二行\\反斜线",
    )
    session.commit()

    content = export_calendar(session, settings=settings).path.read_bytes()
    calendar = _parse(content)

    assert calendar.get("VERSION") == "2.0"
    assert calendar.get("METHOD") is None, "快照不得携带 METHOD"
    assert calendar.get("CALSCALE") == "GREGORIAN"
    assert b"\r\n" in content, "必须使用 CRLF"

    events = calendar.walk("VEVENT")
    assert len(events) == 1
    component = events[0]
    assert str(component["SUMMARY"]) == "团队周会,含逗号;与分号"
    assert component["DTSTART"].params["TZID"] == "Australia/Brisbane"
    assert component["DTSTART"].dt.strftime("%Y-%m-%d %H:%M") == "2025-03-04 09:00"
    assert str(component["UID"]) in content.decode()
    assert component["DTSTAMP"].dt.tzinfo is not None
    assert [c for c in calendar.subcomponents if c.name == "VTIMEZONE"]


def test_all_day_event_uses_value_date_and_exclusive_end(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="全天会议",
        all_day=True,
        start_date="2025-05-01",
        end_date="2025-05-03",
    )
    session.commit()

    calendar = _parse(export_calendar(session, settings=settings).path.read_bytes())
    component = calendar.walk("VEVENT")[0]

    assert component["DTSTART"].dt == date(2025, 5, 1)
    assert component["DTEND"].dt == date(2025, 5, 3)
    assert component["DTSTART"].params.get("VALUE") == "DATE"


def test_sequence_and_last_modified_increment_on_change(session, settings: Settings) -> None:
    event = _write(
        session,
        settings,
        title="原始标题",
        start_at="2025-06-01T08:00:00",
        end_at="2025-06-01T09:00:00",
    )
    session.commit()
    assert event.sequence == 0

    from app.events import update_event

    update_event(session, event, EventWrite(title="修改后的标题"), settings=settings)
    session.commit()
    assert event.sequence == 1

    calendar = _parse(export_calendar(session, settings=settings).path.read_bytes())
    component = calendar.walk("VEVENT")[0]
    assert str(component["SUMMARY"]) == "修改后的标题"
    assert int(component["SEQUENCE"]) == 1
    assert component["LAST-MODIFIED"].dt is not None


def test_soft_deleted_event_is_removed_from_export(session, settings: Settings) -> None:
    from app.events import soft_delete_event

    keep = _write(
        session,
        settings,
        title="保留",
        start_at="2025-07-01T08:00:00",
        end_at="2025-07-01T09:00:00",
    )
    drop = _write(
        session,
        settings,
        title="删除",
        start_at="2025-07-02T08:00:00",
        end_at="2025-07-02T09:00:00",
    )
    session.commit()
    uid_keep = keep.uid
    soft_delete_event(session, drop)
    session.commit()

    calendar = _parse(export_calendar(session, settings=settings).path.read_bytes())
    events = calendar.walk("VEVENT")
    assert len(events) == 1
    assert str(events[0]["UID"]) == uid_keep
    assert drop.deleted_at is not None


def test_uid_is_stable_across_exports(session, settings: Settings) -> None:
    event = _write(
        session,
        settings,
        title="稳定 UID",
        start_at="2025-08-01T08:00:00",
        end_at="2025-08-01T09:00:00",
    )
    session.commit()
    first = _parse(export_calendar(session, settings=settings).path.read_bytes())
    second = _parse(
        export_calendar(session, settings=settings, force_new_version=True).path.read_bytes()
    )

    assert str(first.walk("VEVENT")[0]["UID"]) == str(second.walk("VEVENT")[0]["UID"]) == event.uid


def test_recurring_event_rrule_exported(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="每周站会",
        start_at="2025-09-01T09:30:00",
        end_at="2025-09-01T10:00:00",
        recurrence="weekly",
        recurrence_until="2025-12-31",
    )
    session.commit()

    calendar = _parse(export_calendar(session, settings=settings).path.read_bytes())
    component = calendar.walk("VEVENT")[0]
    rule = component["RRULE"]
    assert rule["FREQ"] == ["WEEKLY"]
    until = rule["UNTIL"][0]
    assert until.strftime("%Y-%m-%d") == "2025-12-31"


def test_all_day_recurring_uses_date_until(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="每天记录",
        all_day=True,
        start_date="2025-09-01",
        recurrence="daily",
        recurrence_until="2025-09-30",
    )
    session.commit()

    calendar = _parse(export_calendar(session, settings=settings).path.read_bytes())
    rule = calendar.walk("VEVENT")[0]["RRULE"]
    assert rule["FREQ"] == ["DAILY"]
    until = rule["UNTIL"][0]
    assert until == date(2025, 9, 30), "全天事件的 UNTIL 必须是 DATE"


def test_long_line_is_folded(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="长标题" + "很长的中文内容" * 20,
        start_at="2025-10-01T08:00:00",
        end_at="2025-10-01T09:00:00",
    )
    session.commit()

    content = export_calendar(session, settings=settings).path.read_bytes()
    for raw_line in content.split(b"\r\n"):
        assert len(raw_line) <= 75, f"存在超过 75 字节的内容行:{raw_line[:80]!r}"


def test_content_is_deterministic(session, settings: Settings) -> None:
    for index in range(3):
        _write(
            session,
            settings,
            title=f"事件 {index}",
            start_at=f"2025-11-0{index + 1}T08:00:00",
            end_at=f"2025-11-0{index + 1}T09:00:00",
        )
    session.commit()

    first = export_calendar(session, settings=settings).path.read_bytes()
    second = export_calendar(session, settings=settings, force_new_version=True).path.read_bytes()
    assert first == second


def test_content_is_deterministic_across_wall_clock(
    session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导出内容不得依赖“导出时刻”。

    导出器用内容哈希判断是否需要新版本。若 DTSTAMP 之类字段取自挂钟时间,跨秒重新导出
    就会得到不同哈希,于是没有改动的保存也会递增版本,并向所有同步人员重发一次完整日历。
    这里把时钟拨到第二天,内容必须仍逐字节一致。
    """
    _write(
        session,
        settings,
        title="跨秒稳定性",
        start_at="2025-11-10T08:00:00",
        end_at="2025-11-10T09:00:00",
    )
    session.commit()

    import app.ics as ics_module

    first = export_calendar(session, settings=settings).path.read_bytes()
    later = utcnow() + timedelta(days=1, seconds=5)
    monkeypatch.setattr(ics_module, "utcnow", lambda: later)
    second = export_calendar(session, settings=settings, force_new_version=True).path.read_bytes()

    assert first == second, "导出内容必须只取决于数据库状态,不能取决于导出时刻"


def test_utc_event_has_no_timezone_component(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="UTC 事件",
        timezone="UTC",
        start_at="2025-12-01T00:00:00+00:00",
        end_at="2025-12-01T01:00:00+00:00",
    )
    session.commit()
    calendar = _parse(export_calendar(session, settings=settings).path.read_bytes())
    assert [c for c in calendar.subcomponents if c.name == "VTIMEZONE"] == []


@pytest.mark.parametrize("tz_name", ["America/New_York", "Europe/Berlin"])
def test_dst_timezone_roundtrip(session, settings: Settings, tz_name: str) -> None:
    """跨夏令时的两个时区:输入当地 09:00,导出后仍须是当地 09:00。"""
    _write(
        session,
        settings,
        title=f"{tz_name} 夏季",
        timezone=tz_name,
        start_at="2025-07-01T09:00:00",
        end_at="2025-07-01T10:00:00",
    )
    _write(
        session,
        settings,
        title=f"{tz_name} 冬季",
        timezone=tz_name,
        start_at="2025-01-15T09:00:00",
        end_at="2025-01-15T10:00:00",
    )
    session.commit()

    calendar = _parse(export_calendar(session, settings=settings).path.read_bytes())
    starts = sorted(
        (str(c["SUMMARY"]), c["DTSTART"].dt.strftime("%H:%M"))
        for c in calendar.walk("VEVENT")
    )
    assert [value for _, value in starts] == ["09:00", "09:00"]
    zones = {c["TZID"] for c in calendar.subcomponents if c.name == "VTIMEZONE"}
    assert tz_name in zones


def test_created_at_is_exported(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="带创建时间",
        start_at="2025-12-05T08:00:00",
        end_at="2025-12-05T09:00:00",
    )
    session.commit()
    calendar = _parse(export_calendar(session, settings=settings).path.read_bytes())
    component = calendar.walk("VEVENT")[0]
    assert component["CREATED"].dt is not None
    assert component["CREATED"].dt <= utcnow().replace(tzinfo=component["CREATED"].dt.tzinfo)
