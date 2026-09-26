"""事件 CRUD API 的集成测试:CRUD → SQLite → ICS → HTTP 响应完整链路。"""

from __future__ import annotations

from datetime import timedelta

from app.timeutil import utcnow
from fastapi.testclient import TestClient
from icalendar import Calendar

TIMED_EVENT = {
    "title": "团队周会",
    "start_at": "2025-03-04T09:00:00",
    "end_at": "2025-03-04T10:00:00",
    "location": "会议室 A",
    "timezone": "Australia/Brisbane",
}


def _create(client: TestClient, headers: dict[str, str], **overrides) -> dict:
    payload = {**TIMED_EVENT, **overrides}
    response = client.post("/api/v1/events", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_requires_login_for_event_api(client: TestClient) -> None:
    response = client.get("/api/v1/events")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_mutation_requires_csrf(admin_client: TestClient) -> None:
    response = admin_client.post("/api/v1/events", json=TIMED_EVENT)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def _current_version(client: TestClient) -> int:
    """启动时的一致性检查会生成一份空日历,因此版本号不固定从 1 开始。"""
    return int(client.get("/api/v1/sync/status").json()["version"])


def test_create_event_generates_ics_immediately(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    baseline = _current_version(admin_client)
    body = _create(admin_client, api_headers)
    assert body["sync"]["ok"] is True
    assert body["sync"]["changed"] is True
    assert body["sync"]["version"] == baseline + 1

    feed = admin_client.get("/calendar")
    assert feed.status_code == 200
    calendar = Calendar.from_ical(feed.content)
    titles = [str(component["SUMMARY"]) for component in calendar.walk("VEVENT")]
    assert titles == ["团队周会"]


def test_list_events_expands_range(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    response = admin_client.get(
        "/api/v1/events", params={"start": "2025-03-01", "end": "2025-03-31"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["start"].startswith("2025-03-0")


def test_list_events_keyword_and_status_filter(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers, title="牙医预约", location="诊所")
    _create(admin_client, api_headers, title="团队周会")

    hit = admin_client.get("/api/v1/events", params={"q": "牙医"}).json()
    assert [item["title"] for item in hit["items"]] == ["牙医预约"]

    miss = admin_client.get("/api/v1/events", params={"q": "不存在的关键词"}).json()
    assert miss["items"] == []

    filtered = admin_client.get("/api/v1/events", params={"status": "tentative"}).json()
    assert filtered["items"] == []


def test_optimistic_lock_conflict(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    created = _create(admin_client, api_headers)
    stale = created["updated_at"]

    first = admin_client.patch(
        f"/api/v1/events/{created['id']}",
        json={"title": "第一次修改", "expected_updated_at": stale},
        headers=api_headers,
    )
    assert first.status_code == 200

    second = admin_client.patch(
        f"/api/v1/events/{created['id']}",
        json={"title": "旧页面覆盖", "expected_updated_at": stale},
        headers=api_headers,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


def test_update_increments_sequence_and_version(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    baseline = _current_version(admin_client)
    created = _create(admin_client, api_headers)
    assert created["sequence"] == 0

    updated = admin_client.patch(
        f"/api/v1/events/{created['id']}",
        json={"title": "改过的标题", "expected_updated_at": created["updated_at"]},
        headers=api_headers,
    ).json()
    assert updated["sequence"] == 1
    assert updated["sync"]["version"] == baseline + 2

    calendar = Calendar.from_ical(admin_client.get("/calendar").content)
    component = calendar.walk("VEVENT")[0]
    assert str(component["SUMMARY"]) == "改过的标题"
    assert int(component["SEQUENCE"]) == 1


def test_noop_update_does_not_create_new_version(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    created = _create(admin_client, api_headers)
    response = admin_client.patch(
        f"/api/v1/events/{created['id']}",
        json={"title": "团队周会", "expected_updated_at": created["updated_at"]},
        headers=api_headers,
    ).json()
    assert response["sync"]["changed"] is False
    assert response["sync"]["version"] is None


def test_delete_is_soft_and_removes_from_ics(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    created = _create(admin_client, api_headers)
    other = _create(admin_client, api_headers, title="保留的事件")

    response = admin_client.delete(
        f"/api/v1/events/{created['id']}", headers=api_headers
    )
    assert response.status_code == 200
    assert response.json()["sync"]["ok"] is True

    calendar = Calendar.from_ical(admin_client.get("/calendar").content)
    uids = [str(component["UID"]) for component in calendar.walk("VEVENT")]
    assert uids == [other["uid"]]

    assert admin_client.get(f"/api/v1/events/{created['id']}").status_code == 404


def test_all_day_event_validation(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    created = _create(
        admin_client,
        api_headers,
        title="全天培训",
        all_day=True,
        start_date="2025-05-01",
        end_date="2025-05-03",
        start_at=None,
        end_at=None,
    )
    assert created["start_date"] == "2025-05-01"
    assert created["end_date_exclusive"] == "2025-05-03"
    assert created["start_at_utc"] is None

    calendar = Calendar.from_ical(admin_client.get("/calendar").content)
    component = calendar.walk("VEVENT")[0]
    assert component["DTSTART"].params.get("VALUE") == "DATE"


def test_field_level_validation_errors(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/events",
        json={"title": "  ", "start_at": "2025-03-04T10:00:00", "end_at": "2025-03-04T09:00:00"},
        headers=api_headers,
    )
    assert response.status_code == 422
    fields = response.json()["error"]["fields"]
    assert fields["title"] == "标题不能为空"
    assert "结束时间" in fields["end_at"]


def test_invalid_timezone_rejected(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/events",
        json={**TIMED_EVENT, "timezone": "Mars/Olympus"},
        headers=api_headers,
    )
    assert response.status_code == 422
    assert "timezone" in response.json()["error"]["fields"]


def test_naive_input_uses_event_timezone(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    """不带偏移量的 09:00 应按事件时区解释,即 UTC 前一天的 23:00。"""
    created = _create(admin_client, api_headers)
    assert created["start_at_utc"] == "2025-03-03T23:00:00Z"
    assert created["start_local"].startswith("2025-03-04T09:00")


def test_recurring_event_occurrences_expanded(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(
        admin_client,
        api_headers,
        title="每日站会",
        start_at="2025-06-02T09:00:00",
        end_at="2025-06-02T09:15:00",
        recurrence="daily",
        recurrence_until="2025-06-04",
    )

    payload = admin_client.get(
        "/api/v1/events", params={"start": "2025-06-01", "end": "2025-06-08"}
    ).json()
    starts = sorted(item["start"] for item in payload["items"])
    assert len(starts) == 3, starts
    assert all(item["recurring"] for item in payload["items"])
    # 同一重复系列的实例必须能看到同一个基础事件 id
    base_ids = {item["event_id"] for item in payload["items"]}
    assert len(base_ids) == 1


def test_recurring_event_open_ended_is_bounded_by_range(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(
        admin_client,
        api_headers,
        title="无限周会",
        start_at="2025-01-06T09:00:00",
        end_at="2025-01-06T10:00:00",
        recurrence="weekly",
    )
    payload = admin_client.get(
        "/api/v1/events", params={"start": "2025-07-01", "end": "2025-08-01"}
    ).json()
    assert len(payload["items"]) == 4, [item["start"] for item in payload["items"]]


def test_long_description_rejected(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/events",
        json={**TIMED_EVENT, "description": "字" * 10001},
        headers=api_headers,
    )
    assert response.status_code == 422
    assert "description" in response.json()["error"]["fields"]


def test_deleted_event_cannot_be_updated(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    created = _create(admin_client, api_headers)
    admin_client.delete(f"/api/v1/events/{created['id']}", headers=api_headers)
    response = admin_client.patch(
        f"/api/v1/events/{created['id']}", json={"title": "改名"}, headers=api_headers
    )
    assert response.status_code == 404


def test_sync_status_endpoint(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    payload = admin_client.get("/api/v1/sync/status").json()
    assert payload["version"] >= 1
    assert payload["dirty"] is False
    assert payload["file_exists"] is True
    assert payload["content_hash"]
    assert payload["recipients_total"] == 0


def test_readyz_reports_ok(admin_client: TestClient, api_headers: dict[str, str]) -> None:
    _create(admin_client, api_headers)
    response = admin_client.get("/readyz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert {check["name"] for check in payload["checks"]} == {"database", "data_dir", "export"}


def test_healthz_is_public(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_admin_pages_redirect_to_login(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_calendar_page_and_form_pages_render(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    page = admin_client.get("/admin")
    assert page.status_code == 200
    assert "fullcalendar" in page.text

    created = _create(admin_client, api_headers)
    edit_page = admin_client.get(f"/admin/events/{created['id']}")
    assert edit_page.status_code == 200
    assert "团队周会" in edit_page.text

    new_page = admin_client.get("/admin/events/new", params={"start": "2025-03-04T09:00:00"})
    assert new_page.status_code == 200

    assert admin_client.get("/admin/settings").status_code == 200
    assert admin_client.get("/admin/sync").status_code == 200


def test_deleted_event_keeps_row_for_audit(
    admin_client: TestClient, api_headers: dict[str, str], settings
) -> None:
    created = _create(admin_client, api_headers)
    admin_client.delete(f"/api/v1/events/{created['id']}", headers=api_headers)

    from app.db import get_session_factory
    from app.models import Event

    session = get_session_factory()()
    try:
        stored = session.get(Event, created["id"])
        assert stored is not None
        assert stored.deleted_at is not None
        assert stored.uid == created["uid"]
    finally:
        session.close()


def test_events_in_future_and_past_within_range(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    soon = utcnow() + timedelta(days=1)
    _create(
        admin_client,
        api_headers,
        title="明天的事",
        start_at=soon.strftime("%Y-%m-%dT09:00:00"),
        end_at=soon.strftime("%Y-%m-%dT10:00:00"),
    )
    payload = admin_client.get("/api/v1/events").json()
    assert payload["total"] == 1
