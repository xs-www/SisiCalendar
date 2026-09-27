"""远程 Agent 接入接口测试。

覆盖:默认关闭、令牌鉴权、幂等重放、完整同步链路、限流与来源标记。
"""

from __future__ import annotations

from app.models import EVENT_SOURCE_AGENT, EVENT_SOURCE_WEB
from fastapi.testclient import TestClient
from icalendar import Calendar

AGENT_PAYLOAD = {
    "title": "Agent 安排的会议",
    "start_at": "2026-11-03T14:00:00",
    "end_at": "2026-11-03T15:00:00",
    "timezone": "Australia/Brisbane",
    "location": "线上",
    "description": "由远程 Agent 创建",
}


def _post(client: TestClient, headers: dict[str, str], **overrides) -> object:
    return client.post("/api/agent/events", json={**AGENT_PAYLOAD, **overrides}, headers=headers)


def test_endpoint_hidden_when_token_not_configured(client: TestClient) -> None:
    """未配置令牌时,整个 /api/agent 等同不存在,不能泄露 401。"""
    response = client.post("/api/agent/events", json=AGENT_PAYLOAD)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_requires_bearer_token(agent_client: TestClient) -> None:
    missing = agent_client.post("/api/agent/events", json=AGENT_PAYLOAD)
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")

    wrong = agent_client.post(
        "/api/agent/events",
        json=AGENT_PAYLOAD,
        headers={"Authorization": "Bearer not-the-right-token"},
    )
    assert wrong.status_code == 401

    bad_scheme = agent_client.post(
        "/api/agent/events",
        json=AGENT_PAYLOAD,
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert bad_scheme.status_code == 401


def test_session_cookie_does_not_authorize_agent_endpoint(agent_client: TestClient) -> None:
    """Agent 接口只认令牌:已登录的管理员会话也不能代替令牌。

    这条约束同时也是「该接口没有 CSRF 面」的依据——它不接受 Cookie 鉴权。
    """
    page = agent_client.get("/login")
    token = page.text.split('name="csrf_token" value="')[1].split('"')[0]
    agent_client.post(
        "/login",
        data={
            "username": "admin",
            "password": "test-password",
            "csrf_token": token,
            "next": "/admin",
        },
    )
    assert agent_client.get("/admin").status_code == 200, "会话本身有效"
    assert agent_client.post("/api/agent/events", json=AGENT_PAYLOAD).status_code == 401


def test_agent_creates_event_and_syncs(
    agent_client: TestClient, agent_headers: dict[str, str]
) -> None:
    response = _post(agent_client, agent_headers)
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["title"] == AGENT_PAYLOAD["title"]
    assert body["source"] == EVENT_SOURCE_AGENT
    assert body["duplicate"] is False
    assert body["sync"]["ok"] is True
    assert body["start_at_utc"] == "2026-11-03T04:00:00Z", "本地时间应按事件时区换算"

    feed = agent_client.get("/calendar")
    assert feed.status_code == 200
    calendar = Calendar.from_ical(feed.content)
    titles = [str(component["SUMMARY"]) for component in calendar.walk("VEVENT")]
    assert titles == [AGENT_PAYLOAD["title"]]


def test_client_ref_makes_retry_idempotent(
    agent_client: TestClient, agent_headers: dict[str, str]
) -> None:
    first = _post(agent_client, agent_headers, client_ref="meeting-2026-11-03")
    assert first.status_code == 201
    created = first.json()

    replay = _post(agent_client, agent_headers, client_ref="meeting-2026-11-03")
    assert replay.status_code == 200, "重放应返回既有事件而不是再建一条"
    replayed = replay.json()
    assert replayed["duplicate"] is True
    assert replayed["id"] == created["id"]
    assert replayed["uid"] == created["uid"]
    assert replayed["sync"] is None, "重放不得触发新的导出或发信"

    listed = agent_client.get("/api/v1/events", params={"source": "agent"})
    # 未登录的列表接口不可用,这里改用订阅内容确认只有一条
    assert listed.status_code == 401
    calendar = Calendar.from_ical(agent_client.get("/calendar").content)
    assert len(calendar.walk("VEVENT")) == 1


def test_different_client_ref_creates_separate_events(
    agent_client: TestClient, agent_headers: dict[str, str]
) -> None:
    _post(agent_client, agent_headers, client_ref="ref-a")
    _post(agent_client, agent_headers, client_ref="ref-b")
    calendar = Calendar.from_ical(agent_client.get("/calendar").content)
    assert len(calendar.walk("VEVENT")) == 2


def test_validation_errors_are_structured(
    agent_client: TestClient, agent_headers: dict[str, str]
) -> None:
    response = _post(agent_client, agent_headers, title="  ")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["fields"]["title"] == "标题不能为空"

    bad_time = _post(agent_client, agent_headers, end_at="2026-11-03T13:00:00")
    assert bad_time.status_code == 422
    assert "end_at" in bad_time.json()["error"]["fields"]

    bad_tz = _post(agent_client, agent_headers, timezone="Nowhere/Land")
    assert bad_tz.status_code == 422
    assert "timezone" in bad_tz.json()["error"]["fields"]


def test_all_day_event_via_agent(
    agent_client: TestClient, agent_headers: dict[str, str]
) -> None:
    response = _post(
        agent_client,
        agent_headers,
        all_day=True,
        start_date="2026-11-05",
        end_date="2026-11-06",
        start_at=None,
        end_at=None,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["all_day"] is True
    assert body["start_date"] == "2026-11-05"

    calendar = Calendar.from_ical(agent_client.get("/calendar").content)
    assert calendar.walk("VEVENT")[0]["DTSTART"].params.get("VALUE") == "DATE"


def test_agent_read_back_own_event(
    agent_client: TestClient, agent_headers: dict[str, str]
) -> None:
    created = _post(agent_client, agent_headers).json()
    fetched = agent_client.get(f"/api/agent/events/{created['id']}", headers=agent_headers)
    assert fetched.status_code == 200
    assert fetched.json()["uid"] == created["uid"]

    assert (
        agent_client.get("/api/agent/events/missing-id", headers=agent_headers).status_code
        == 404
    )


def test_agent_cannot_read_web_created_event(
    admin_client: TestClient, api_headers: dict[str, str], agent_client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    """Agent 只能回查自己创建的事件,读不到本机录入的日程。"""
    created = admin_client.post(
        "/api/v1/events",
        json={
            "title": "本机录入的私事",
            "start_at": "2026-11-10T09:00:00",
            "end_at": "2026-11-10T10:00:00",
            "timezone": "Australia/Brisbane",
        },
        headers=api_headers,
    ).json()
    assert agent_client.get(
        f"/api/agent/events/{created['id']}", headers=agent_headers
    ).status_code == 404


def test_rate_limit(agent_client: TestClient, agent_headers: dict[str, str]) -> None:
    """默认每分钟 60 次,超限返回 429 且带 Retry-After。"""
    statuses = []
    for index in range(62):
        response = _post(agent_client, agent_headers, client_ref=f"burst-{index}")
        statuses.append(response.status_code)
    assert 429 in statuses
    assert statuses.count(429) >= 1
    limited = [s for s in statuses if s == 429]
    assert len(limited) == 2, "第 61、62 次应被限流"


def test_source_is_recorded_and_filterable(
    admin_client: TestClient, api_headers: dict[str, str], agent_client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    _post(agent_client, agent_headers, client_ref="source-check")
    admin_client.post(
        "/api/v1/events",
        json={
            "title": "本机录入",
            "start_at": "2026-12-01T09:00:00",
            "end_at": "2026-12-01T10:00:00",
            "timezone": "Australia/Brisbane",
        },
        headers=api_headers,
    )

    everything = admin_client.get("/api/v1/events").json()
    by_id = {item["title"]: item["source"] for item in everything["items"]}
    assert by_id["Agent 安排的会议"] == EVENT_SOURCE_AGENT
    assert by_id["本机录入"] == EVENT_SOURCE_WEB

    only_agent = admin_client.get("/api/v1/events", params={"source": "agent"}).json()
    assert [item["title"] for item in only_agent["items"]] == ["Agent 安排的会议"]

    only_web = admin_client.get("/api/v1/events", params={"source": "web"}).json()
    assert [item["title"] for item in only_web["items"]] == ["本机录入"]


def test_agent_token_never_appears_in_response(
    agent_client: TestClient, agent_headers: dict[str, str]
) -> None:
    from tests.conftest import AGENT_TOKEN

    created = _post(agent_client, agent_headers)
    assert AGENT_TOKEN not in created.text
    status = agent_client.get("/api/v1/sync/status")
    assert AGENT_TOKEN not in status.text


def test_agent_event_editable_from_admin_ui(
    admin_client: TestClient, api_headers: dict[str, str], agent_client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    created = _post(agent_client, agent_headers, client_ref="editable").json()

    page = admin_client.get(f"/admin/events/{created['id']}")
    assert page.status_code == 200
    assert "远程 Agent 创建" in page.text

    updated = admin_client.patch(
        f"/api/v1/events/{created['id']}",
        json={"title": "人工改过的标题", "expected_updated_at": created["updated_at"]},
        headers=api_headers,
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["source"] == EVENT_SOURCE_AGENT, "来源标记应保留"
    assert body["client_ref"] == "editable", "幂等键应保留"
    assert body["sequence"] == 1
