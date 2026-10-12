"""日程提醒(VALARM)与备注链接(URL)的行为测试。

对应开发文档「必须保持的不变量」里的两条:

- 导出内容只取决于数据库状态(提醒顺序必须与输入顺序无关,否则哈希会漂移);
- 无实际变化的保存不递增 `sequence`、不产生新版本(所以「不提交 reminders」必须等于「保留原值」)。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from app.config import Settings
from app.events import create_event, update_event
from app.exporter import export_calendar
from app.models import Event
from app.reminders import (
    MAX_REMINDER_MINUTES,
    describe_reminder,
    normalize_reminders,
    parse_reminder_text,
    preset_options,
    serialize_reminders,
)
from app.schemas import EventWrite
from icalendar import Calendar


def _parse(content: bytes) -> Calendar:
    return Calendar.from_ical(content)


def _write(session, settings: Settings, **payload: object) -> Event:
    return create_event(session, EventWrite(**payload), settings=settings)


def _triggers(component) -> list[timedelta]:
    return [alarm["TRIGGER"].dt for alarm in component.walk("VALARM")]


# --------------------------------------------------------------------------
# 纯函数:取值与描述
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (0, "事件开始时"),
        (15, "提前 15 分钟"),
        (60, "提前 1 小时"),
        (90, "提前 90 分钟"),
        (1440, "提前 1 天"),
        (2880, "提前 2 天"),
        (10080, "提前 1 周"),
        (-10, "延后 10 分钟"),
        (-1440, "延后 1 天"),
    ],
)
def test_describe_reminder(minutes: int, expected: str) -> None:
    assert describe_reminder(minutes) == expected


def test_describe_reminder_rejects_out_of_range() -> None:
    assert describe_reminder(MAX_REMINDER_MINUTES + 1) == "无效提醒"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([15, 0, 15], [15, 0]),
        (["15", "1440"], [1440, 15]),
        ("15,1440", [1440, 15]),
        ("[15, 1440]", [1440, 15]),
        ("15,1440,15", [1440, 15]),
        ("", []),
        (None, []),
        ([], []),
    ],
)
def test_normalize_reminders_is_order_and_duplicate_insensitive(value, expected) -> None:
    minutes, error = normalize_reminders(value)
    assert error is None
    assert minutes == expected


@pytest.mark.parametrize("value", ["abc", "15,xyz", 1.5, {"a": 1}, [MAX_REMINDER_MINUTES + 1]])
def test_normalize_reminders_reports_invalid_values(value) -> None:
    minutes, error = normalize_reminders(value)
    assert minutes is None
    assert error


def test_parse_reminder_text_accepts_chinese_separators() -> None:
    assert parse_reminder_text("15,1440;30 5") == [15, 1440, 30, 5]


def test_serialize_reminders_tolerates_corrupt_data() -> None:
    assert serialize_reminders(None) == []
    assert serialize_reminders("not json") == []
    assert serialize_reminders("[999999999]") == []
    assert serialize_reminders("[1440, 0]") == [1440, 0]


def test_preset_options_marks_selection() -> None:
    options = preset_options([15, 2880])
    selected = {item["minutes"] for item in options if item["selected"]}
    assert selected == {15, 2880}
    assert len(options) == len(preset_options([]))


# --------------------------------------------------------------------------
# ICS 导出
# --------------------------------------------------------------------------
def test_reminders_export_as_valarm(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="和牙医的预约",
        start_at="2026-11-03T14:00:00",
        end_at="2026-11-03T15:00:00",
        reminders=[15],
    )
    session.commit()

    content = export_calendar(session, settings=settings).path.read_bytes()
    component = _parse(content).walk("VEVENT")[0]
    alarms = component.walk("VALARM")
    assert len(alarms) == 1
    assert str(alarms[0]["ACTION"]) == "DISPLAY"
    assert str(alarms[0]["DESCRIPTION"]) == "和牙医的预约"
    assert alarms[0]["TRIGGER"].dt == timedelta(minutes=-15)


def test_multiple_reminders_are_exported_newest_first(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="全天出差",
        all_day=True,
        start_date="2026-11-05",
        reminders=[0, 1440, 15],
    )
    session.commit()

    content = export_calendar(session, settings=settings).path.read_bytes()
    component = _parse(content).walk("VEVENT")[0]
    assert _triggers(component) == [
        timedelta(days=-1),
        timedelta(minutes=-15),
        timedelta(0),
    ]


def test_reminder_export_is_independent_of_input_order(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="顺序无关",
        start_at="2026-11-06T09:00:00",
        end_at="2026-11-06T10:00:00",
        reminders=[15, 1440, 0],
    )
    session.commit()
    first = export_calendar(session, settings=settings).path.read_bytes()

    event = session.query(Event).one()
    assert serialize_reminders(event.reminders) == [1440, 15, 0]
    second = export_calendar(session, settings=settings, force_new_version=True).path.read_bytes()
    assert first == second


def test_all_day_reminder_uses_midnight_anchor(session, settings: Settings) -> None:
    """全天事件没有时刻,VALARM 的基准是当天 00:00:提前 1 天即前一天零点。"""
    _write(
        session,
        settings,
        title="纪念日",
        all_day=True,
        start_date="2026-11-07",
        reminders=[1440],
    )
    session.commit()

    content = export_calendar(session, settings=settings).path.read_bytes()
    component = _parse(content).walk("VEVENT")[0]
    assert component["DTSTART"].dt == date(2026, 11, 7)
    assert _triggers(component) == [timedelta(days=-1)]


def test_event_without_reminders_has_no_valarm(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="没有提醒",
        start_at="2026-11-08T09:00:00",
        end_at="2026-11-08T10:00:00",
    )
    session.commit()

    content = export_calendar(session, settings=settings).path.read_bytes()
    component = _parse(content).walk("VEVENT")[0]
    assert component.walk("VALARM") == []


def test_url_is_exported_and_folded(session, settings: Settings) -> None:
    _write(
        session,
        settings,
        title="线上会议",
        start_at="2026-11-09T09:00:00",
        end_at="2026-11-09T10:00:00",
        url="https://example.com/meeting?room=123&slot=abc",
    )
    session.commit()

    content = export_calendar(session, settings=settings).path.read_bytes()
    component = _parse(content).walk("VEVENT")[0]
    assert str(component["URL"]) == "https://example.com/meeting?room=123&slot=abc"
    for raw_line in content.split(b"\r\n"):
        assert len(raw_line) <= 75


def test_reminders_survive_timezone_export(session, settings: Settings) -> None:
    """提醒用相对时长表达,跨时区/夏令时不需要额外换算。"""
    _write(
        session,
        settings,
        title="夏令时提醒",
        timezone="America/New_York",
        start_at="2026-07-01T09:00:00",
        end_at="2026-07-01T10:00:00",
        reminders=[30],
    )
    session.commit()

    content = export_calendar(session, settings=settings).path.read_bytes()
    component = _parse(content).walk("VEVENT")[0]
    assert _triggers(component) == [timedelta(minutes=-30)]


# --------------------------------------------------------------------------
# API 行为
# --------------------------------------------------------------------------
def test_api_create_and_read_reminders(admin_client, api_headers) -> None:
    response = admin_client.post(
        "/api/v1/events",
        headers=api_headers,
        json={
            "title": "带提醒的日程",
            "start_at": "2026-12-01T10:00",
            "duration_minutes": 30,
            "reminders": [15, 1440, 15],
            "url": "https://example.com/doc",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reminders"] == [1440, 15]
    assert body["reminder_labels"] == ["提前 1 天", "提前 15 分钟"]
    assert body["url"] == "https://example.com/doc"

    read = admin_client.get(f"/api/v1/events/{body['id']}", headers=api_headers)
    assert read.status_code == 200
    assert read.json()["reminders"] == [1440, 15]


def test_api_accepts_reminders_as_comma_string(admin_client, api_headers) -> None:
    """`/api/agent/events` 的表单形态与这里共用同一个校验器。"""
    response = admin_client.post(
        "/api/v1/events",
        headers=api_headers,
        json={
            "title": "字符串提醒",
            "start_at": "2026-12-02T10:00",
            "duration_minutes": 30,
            "reminders": "15,1440",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["reminders"] == [1440, 15]


@pytest.mark.parametrize("reminders", ["abc", [MAX_REMINDER_MINUTES + 1], [1.5], [list(range(60))]])
def test_api_rejects_invalid_reminders(admin_client, api_headers, reminders) -> None:
    response = admin_client.post(
        "/api/v1/events",
        headers=api_headers,
        json={
            "title": "非法提醒",
            "start_at": "2026-12-03T10:00",
            "duration_minutes": 30,
            "reminders": reminders,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_patch_without_reminders_keeps_them_and_does_not_bump_version(
    admin_client, api_headers
) -> None:
    created = admin_client.post(
        "/api/v1/events",
        headers=api_headers,
        json={
            "title": "先有提醒",
            "start_at": "2026-12-04T10:00",
            "duration_minutes": 30,
            "reminders": [30],
        },
    ).json()
    version = created["sync"]["version"]

    patched = admin_client.patch(
        f"/api/v1/events/{created['id']}",
        headers=api_headers,
        json={"expected_updated_at": created["updated_at"], "title": "改了标题"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["reminders"] == [30], "未提交 reminders 时必须保留原值"
    assert body["sequence"] == created["sequence"] + 1
    assert body["sync"]["version"] == version + 1

    # 再提交同名标题与同一组提醒:没有任何变化,不得产生新版本
    noop = admin_client.patch(
        f"/api/v1/events/{created['id']}",
        headers=api_headers,
        json={
            "expected_updated_at": body["updated_at"],
            "title": "改了标题",
            "reminders": [30],
        },
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["sequence"] == body["sequence"]
    assert noop.json()["sync"].get("version") is None
    assert noop.json()["sync"]["changed"] is False


def test_patch_can_clear_reminders(admin_client, api_headers) -> None:
    created = admin_client.post(
        "/api/v1/events",
        headers=api_headers,
        json={
            "title": "清除提醒",
            "start_at": "2026-12-05T10:00",
            "duration_minutes": 30,
            "reminders": [30],
        },
    ).json()

    cleared = admin_client.patch(
        f"/api/v1/events/{created['id']}",
        headers=api_headers,
        json={"expected_updated_at": created["updated_at"], "reminders": []},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["reminders"] == []
    assert cleared.json()["sequence"] == created["sequence"] + 1


def test_agent_api_accepts_form_reminders(agent_client, agent_headers) -> None:
    response = agent_client.post(
        "/api/agent/events",
        headers=agent_headers,
        data={
            "title": "快捷指令建的日程",
            "start_at": "2026-12-06T10:00",
            "duration_minutes": "30",
            "reminders": "15,60",
            "url": "https://example.com/shortcut",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reminders"] == [60, 15]
    assert body["url"] == "https://example.com/shortcut"


def test_agent_api_accepts_json_array_string_in_form(agent_client, agent_headers) -> None:
    """表单里直接贴 JSON 数组也接受(有人会从文档里复制这一行)。"""
    response = agent_client.post(
        "/api/agent/events",
        headers=agent_headers,
        data={
            "title": "贴 JSON 数组",
            "start_at": "2026-12-06T12:00",
            "duration_minutes": "30",
            "reminders": "[1440, 15]",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["reminders"] == [1440, 15]


def test_agent_api_rejects_typo_in_form_reminders(agent_client, agent_headers) -> None:
    response = agent_client.post(
        "/api/agent/events",
        headers=agent_headers,
        data={
            "title": "拼错的提醒",
            "start_at": "2026-12-07T10:00",
            "duration_minutes": "30",
            "reminders": "15,abc",
        },
    )
    assert response.status_code == 422, response.text


def test_search_matches_url_and_reminders_are_listed(admin_client, api_headers) -> None:
    admin_client.post(
        "/api/v1/events",
        headers=api_headers,
        json={
            "title": "可搜索的链接",
            "start_at": "2026-12-08T10:00",
            "duration_minutes": 30,
            "description": "备注里的关键词",
            "reminders": [10],
        },
    )
    found = admin_client.get("/api/v1/events?q=备注里的关键词", headers=api_headers)
    assert found.status_code == 200
    assert found.json()["total"] == 1


def test_export_content_stable_across_reopen(admin_client, api_headers) -> None:
    """提交提醒后重新导出,内容哈希必须逐字节稳定(不依赖挂钟时间)。"""
    admin_client.post(
        "/api/v1/events",
        headers=api_headers,
        json={
            "title": "稳定性",
            "start_at": "2026-12-09T10:00",
            "duration_minutes": 30,
            "reminders": [5, 1440],
        },
    )
    first = admin_client.post("/api/v1/sync/calendar", headers=api_headers)
    assert first.status_code in (200, 202), first.text
    second = admin_client.post("/api/v1/sync/calendar", headers=api_headers)
    assert second.status_code in (200, 202), second.text
    first_hash = first.json()["status"]["content_hash"]
    second_hash = second.json()["status"]["content_hash"]
    assert first_hash == second_hash, "同一数据库状态必须导出同一份内容"


# --------------------------------------------------------------------------
# 表单
# --------------------------------------------------------------------------
def test_event_form_renders_reminder_controls(admin_client) -> None:
    page = admin_client.get("/admin/events/new")
    assert page.status_code == 200, page.text
    assert 'name="reminder_preset"' in page.text
    assert 'id="reminder-custom"' in page.text
    assert 'name="url"' in page.text
    assert "VALARM" in page.text, "表单应说明提醒会写进 ICS"


def test_edit_form_marks_existing_reminders(admin_client, api_headers) -> None:
    created = admin_client.post(
        "/api/v1/events",
        headers=api_headers,
        json={
            "title": "表单回填",
            "start_at": "2026-12-10T10:00",
            "duration_minutes": 30,
            "reminders": [15, 45],
            "url": "https://example.com/edit",
        },
    ).json()

    page = admin_client.get(f"/admin/events/{created['id']}")
    assert page.status_code == 200, page.text
    # 15 命中预设勾选项,45 只能出现在自定义输入框里
    assert re.search(r'value="15"\s+checked', page.text), "预设 15 分钟应处于勾选状态"
    assert 'id="reminder-custom"' in page.text
    assert re.search(r'id="reminder-custom"[^>]*value="45"', page.text), "自定义 45 分钟应回填"
    assert "https://example.com/edit" in page.text


def test_update_event_reminder_change_bumps_sequence(session, settings: Settings) -> None:
    event = _write(
        session,
        settings,
        title="递增序号",
        start_at="2026-12-11T10:00:00",
        end_at="2026-12-11T11:00:00",
        reminders=[15],
    )
    session.commit()
    assert event.sequence == 0

    update_event(session, event, EventWrite(reminders=[60]), settings=settings)
    session.commit()
    assert event.sequence == 1
    assert serialize_reminders(event.reminders) == [60]
