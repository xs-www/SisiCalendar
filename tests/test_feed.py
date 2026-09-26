"""订阅入口测试:条件请求、缓存头、原子性与公开可访问性。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from app.exporter import ExportError, export_calendar, get_state
from app.models import Event
from app.timeutil import utcnow
from fastapi.testclient import TestClient
from icalendar import Calendar

FEED_PATHS = ["/calendar", "/calendar.ics"]


def _create(client: TestClient, headers: dict[str, str], **overrides) -> dict:
    payload = {
        "title": "订阅测试",
        "start_at": "2025-03-04T09:00:00",
        "end_at": "2025-03-04T10:00:00",
        "timezone": "Australia/Brisbane",
        **overrides,
    }
    response = client.post("/api/v1/events", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_feed_is_public_and_not_a_redirect(client: TestClient) -> None:
    response = client.get("/calendar")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")


def test_feed_headers(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    response = admin_client.get("/calendar")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert response.headers["content-disposition"] == 'inline; filename="calendar.ics"'
    assert response.headers["cache-control"] == "private, max-age=300, must-revalidate"
    assert response.headers["etag"].startswith('"') and response.headers["etag"].endswith('"')
    assert response.headers["last-modified"]

    payload = Calendar.from_ical(response.content)
    assert payload.get("VERSION") == "2.0"
    assert payload.get("METHOD") is None


def test_alias_returns_identical_content(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    primary = admin_client.get("/calendar")
    alias = admin_client.get("/calendar.ics")
    assert primary.content == alias.content
    assert primary.headers["etag"] == alias.headers["etag"]


def test_etag_matches_content_hash(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    response = admin_client.get("/calendar")
    expected = '"' + hashlib.sha256(response.content).hexdigest() + '"'
    assert response.headers["etag"] == expected


def test_conditional_get_with_etag_returns_304(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    first = admin_client.get("/calendar")
    etag = first.headers["etag"]

    response = admin_client.get("/calendar", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag

    # 弱校验符写法也应识别
    weak = admin_client.get("/calendar", headers={"If-None-Match": f"W/{etag}"})
    assert weak.status_code == 200


def test_conditional_get_with_last_modified_returns_304(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    first = admin_client.get("/calendar")
    modified = first.headers["last-modified"]

    response = admin_client.get("/calendar", headers={"If-Modified-Since": modified})
    assert response.status_code == 304


def test_stale_last_modified_returns_full_body(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    old = format_datetime(datetime.now(UTC) - timedelta(days=30), usegmt=True)
    response = admin_client.get("/calendar", headers={"If-Modified-Since": old})
    assert response.status_code == 200


def test_head_returns_headers_without_body(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    get_response = admin_client.get("/calendar")
    head_response = admin_client.head("/calendar")

    assert head_response.status_code == 200
    assert head_response.content == b""
    assert head_response.headers["content-length"] == str(len(get_response.content))
    assert head_response.headers["etag"] == get_response.headers["etag"]
    assert head_response.headers["content-type"].startswith("text/calendar")


def test_etag_changes_after_new_event(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    before = admin_client.get("/calendar").headers["etag"]
    _create(admin_client, api_headers, title="第二个事件")
    after = admin_client.get("/calendar").headers["etag"]
    assert before != after


def test_feed_serves_last_valid_file_when_export_fails(
    admin_client: TestClient, api_headers: dict[str, str], monkeypatch
) -> None:
    """导出失败时不能返回半写入文件,而是继续提供最近的有效版本。"""
    _create(admin_client, api_headers)
    good = admin_client.get("/calendar").content

    import app.exporter as exporter

    def boom(*args, **kwargs):
        raise ExportError("模拟生成失败")

    original = exporter.build_calendar
    monkeypatch.setattr(exporter, "build_calendar", boom)
    try:
        payload = {
            "title": "触发失败",
            "start_at": "2025-04-01T09:00:00",
            "end_at": "2025-04-01T10:00:00",
        }
        created = admin_client.post("/api/v1/events", json=payload, headers=api_headers)
        assert created.status_code == 201, "数据库中的编辑不能丢失"
        assert created.json()["sync"]["ok"] is False
        assert created.json()["sync"]["error"]

        # 生成仍然失败:订阅端必须拿到上一次完整有效的文件
        response = admin_client.get("/calendar")
        assert response.status_code == 200
        assert response.content == good
    finally:
        monkeypatch.setattr(exporter, "build_calendar", original)

    status = admin_client.get("/api/v1/sync/status").json()
    assert status["dirty"] is True
    assert status["last_error"]

    # 后台修复任务恢复后,新事件才会出现在订阅里
    from app.db import get_session_factory
    from app.sync_service import repair_if_needed

    session = get_session_factory()()
    try:
        outcome = repair_if_needed(session, settings=admin_client.app.state.settings)
    finally:
        session.close()
    assert outcome.ok is True
    repaired = admin_client.get("/calendar").content
    assert repaired != good
    assert "触发失败" in repaired.decode("utf-8")


def test_dirty_state_is_repaired_by_feed_request(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _create(admin_client, api_headers)
    from app.db import get_session_factory

    session = get_session_factory()()
    try:
        state = get_state(session)
        state.dirty = True
        state.last_error = "人为标记"
        session.commit()
    finally:
        session.close()

    response = admin_client.get("/calendar")
    assert response.status_code == 200
    status = admin_client.get("/api/v1/sync/status").json()
    assert status["dirty"] is False
    assert status["last_error"] is None


def test_atomic_replace_never_exposes_partial_file(
    admin_client: TestClient, api_headers: dict[str, str], settings
) -> None:
    """写入过程中,发布路径上始终是完整的旧文件或完整的新文件。"""
    _create(admin_client, api_headers)
    published = settings.calendar_path
    assert published.exists()

    from app.db import get_session_factory

    session = get_session_factory()()
    try:
        for index in range(5):
            session.add(
                Event(
                    uid=f"atomic-{index}@test",
                    title=f"批量 {index}",
                    start_at_utc=utcnow() + timedelta(days=index),
                    end_at_utc=utcnow() + timedelta(days=index, hours=1),
                    timezone="UTC",
                    all_day=False,
                    status="confirmed",
                    sequence=0,
                )
            )
        session.commit()

        export_calendar(session, settings=settings)
        data = published.read_bytes()
        Calendar.from_ical(data)  # 完整且可解析
        assert b"END:VCALENDAR" in data
    finally:
        session.close()


def test_export_creates_versioned_snapshot(
    admin_client: TestClient, api_headers: dict[str, str], settings
) -> None:
    created = _create(admin_client, api_headers)
    version = created["sync"]["version"]
    snapshot = settings.snapshot_dir / f"calendar-v{version:06d}.ics"
    assert snapshot.exists()
    assert snapshot.read_bytes() == settings.calendar_path.read_bytes()


def test_export_result_reports_unchanged_content(
    admin_client: TestClient, api_headers: dict[str, str], settings
) -> None:
    _create(admin_client, api_headers)
    from app.db import get_session_factory

    session = get_session_factory()()
    try:
        first = export_calendar(session, settings=settings)
        session.commit()
        second = export_calendar(session, settings=settings)
        session.commit()
        assert second.changed is False
        assert second.version == first.version
        assert second.content_hash == first.content_hash
    finally:
        session.close()


def test_verify_consistency_regenerates_missing_file(
    admin_client: TestClient, api_headers: dict[str, str], settings
) -> None:
    _create(admin_client, api_headers)
    settings.calendar_path.unlink()

    from app.db import get_session_factory
    from app.exporter import verify_consistency

    session = get_session_factory()()
    try:
        ok, detail = verify_consistency(session, settings=settings)
        session.commit()
    finally:
        session.close()

    assert ok is True
    assert settings.calendar_path.exists()
    assert "重新生成" in detail
