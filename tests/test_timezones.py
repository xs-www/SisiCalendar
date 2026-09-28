"""时区清单与「序号作为索引」测试。

覆盖两件事:

1. `app.timezones.resolve_timezone` 能把序号、中文名、IANA 名统一解析成 IANA 名;
2. 设置页下拉框与 event 接口(含远程 Agent 接口)都走同一条解析路径,
   邮件队列页面把创建/发送时间按默认时区展示。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from app.deps import templates
from app.models import MailDelivery
from app.timezones import (
    MAX_INDEX,
    TIMEZONE_CHOICES,
    known_iana_names,
    resolve_timezone,
    timezone_groups,
)
from fastapi.testclient import TestClient

BEIJING = "Asia/Shanghai"
SYDNEY = "Australia/Sydney"


# --------------------------------------------------------------------------
# 清单本身
# --------------------------------------------------------------------------
def test_indexes_are_contiguous_and_unique() -> None:
    indexes = [choice.index for choice in TIMEZONE_CHOICES]
    assert indexes == list(range(1, MAX_INDEX + 1)), "序号必须从 1 连续编号"
    assert len(known_iana_names()) == len(TIMEZONE_CHOICES), "INAIA 名不得重复"


def test_beijing_and_sydney_are_in_the_list() -> None:
    """用户点名要的两个时区,序号与标签都要稳定。"""
    assert resolve_timezone(1) == BEIJING
    assert resolve_timezone(11) == SYDNEY
    labels = {choice.label for choice in TIMEZONE_CHOICES}
    assert {"北京时间", "悉尼时间"} <= labels


def test_groups_cover_every_choice() -> None:
    grouped = [choice for _, items in timezone_groups() for choice in items]
    assert grouped == list(TIMEZONE_CHOICES)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, BEIJING),
        ("1", BEIJING),
        (11, SYDNEY),
        ("11", SYDNEY),
        ("#11", SYDNEY),
        ("序号11", SYDNEY),
        ("第 11 项", SYDNEY),
        ("第11项", SYDNEY),
        ("11号", SYDNEY),
        (0, BEIJING),  # 0 视为第一项,兼容从 0 开始数的调用方
        ("北京时间", BEIJING),
        ("北京", BEIJING),
        ("悉尼时间", SYDNEY),
        ("悉尼", SYDNEY),
        ("11. 悉尼时间(Australia/Sydney)", SYDNEY),
        ("3. 布里斯班时间", "Australia/Brisbane"),
        ("悉尼时间(Australia/Sydney)", SYDNEY),
        ("Asia/Shanghai", BEIJING),
        ("europe/london", "Europe/London"),  # 大小写不敏感,但返回规范写法
    ],
)
def test_resolve_timezone(value: object, expected: str | None) -> None:
    assert resolve_timezone(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        True,
        False,
        "Mars/Olympus",
        "Nowhere/Land",
        MAX_INDEX + 1,
        -1,
        1.5,
        "+10:00",
        "GMT+10",
        "AEST",
    ],
)
def test_resolve_timezone_rejects_unknown(value: object) -> None:
    """偏移量与缩写一律拒绝:存进库里的必须是可还原的 IANA 名。"""
    assert resolve_timezone(value) is None


# --------------------------------------------------------------------------
# 接口层:序号可以直接当 timezone 用
# --------------------------------------------------------------------------
def test_events_api_accepts_timezone_index(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/events",
        json={
            "title": "用序号指定时区",
            "start_at": "2026-11-03T14:00:00",
            "end_at": "2026-11-03T15:00:00",
            "timezone": 1,
        },
        headers=api_headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["timezone"] == BEIJING, "JSON 里的数字序号要能落成 IANA 名"
    assert body["start_at_utc"] == "2026-11-03T06:00:00Z", "14:00 北京时间即 06:00 UTC"


def test_agent_api_accepts_timezone_index(
    agent_client: TestClient, agent_headers: dict[str, str]
) -> None:
    response = agent_client.post(
        "/api/agent/events",
        json={
            "title": "快捷指令用序号",
            "start_at": "2026-11-03T14:00:00",
            "duration_minutes": 60,
            "timezone": "11",  # 表单形态下只能是字符串
        },
        headers=agent_headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["timezone"] == SYDNEY
    assert body["start_at_utc"] == "2026-11-03T03:00:00Z", "14:00 悉尼夏令时(+11)= 03:00 UTC"


def test_agent_api_accepts_chinese_timezone_label(
    agent_client: TestClient, agent_headers: dict[str, str]
) -> None:
    response = agent_client.post(
        "/api/agent/events",
        json={
            "title": "中文时区名",
            "start_at": "2026-11-03T14:00:00",
            "duration_minutes": 60,
            "timezone": "北京时间",
        },
        headers=agent_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["timezone"] == BEIJING


def test_invalid_timezone_error_mentions_index(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/events",
        json={
            "title": "错误时区",
            "start_at": "2026-11-03T14:00:00",
            "end_at": "2026-11-03T15:00:00",
            "timezone": "Mars/Olympus",
        },
        headers=api_headers,
    )
    assert response.status_code == 422
    message = response.json()["error"]["fields"]["timezone"]
    assert "序号" in message and "北京时间" in message, "报错要告诉用户还能填序号"


def test_settings_api_accepts_timezone_index(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.patch(
        "/api/v1/settings",
        json={"default_timezone": 1},
        headers=api_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["default_timezone"] == BEIJING


def test_settings_api_exposes_timezone_choices(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    payload = admin_client.get("/api/v1/settings").json()
    choices = payload["timezone_choices"]
    assert choices[0] == {"index": 1, "iana": BEIJING, "label": "北京时间", "region": "亚洲"}
    assert len(choices) == MAX_INDEX


def test_event_without_timezone_uses_settings_default(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    """默认时区通过序号改成北京后,不带 timezone 的事件也按北京解释。"""
    changed = admin_client.patch(
        "/api/v1/settings", json={"default_timezone": 1}, headers=api_headers
    )
    assert changed.status_code == 200

    response = admin_client.post(
        "/api/v1/events",
        json={
            "title": "跟随默认时区",
            "start_at": "2026-11-03T14:00:00",
            "end_at": "2026-11-03T15:00:00",
        },
        headers=api_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["timezone"] == BEIJING
    assert response.json()["start_at_utc"] == "2026-11-03T06:00:00Z"


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
def test_settings_page_renders_timezone_dropdown(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    page = admin_client.get("/admin/settings")
    assert page.status_code == 200
    text = page.text
    assert 'name="default_timezone"' in text
    assert "<select" in text and "<optgroup" in text
    assert "1. 北京时间(Asia/Shanghai)" in text
    assert "11. 悉尼时间(Australia/Sydney)" in text
    # 当前生效的默认时区必须是选中项,而不是每次打开都跳回第一项
    assert 'value="Australia/Brisbane" selected' in text


def test_settings_page_keeps_custom_timezone(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    """清单外的时区(如 Asia/Kathmandu)不能被下拉框吞掉。"""
    saved = admin_client.patch(
        "/api/v1/settings",
        json={"default_timezone": "Asia/Kathmandu"},
        headers=api_headers,
    )
    assert saved.status_code == 200

    page = admin_client.get("/admin/settings")
    assert page.status_code == 200
    assert 'value="Asia/Kathmandu" selected' in page.text
    assert "不在常用清单中" in page.text


def test_event_form_mentions_timezone_index(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    page = admin_client.get("/admin/events/new")
    assert page.status_code == 200
    assert "1. 北京时间" in page.text
    assert "序号" in page.text


# --------------------------------------------------------------------------
# 邮件队列的操作时间
# --------------------------------------------------------------------------
def test_mail_queue_shows_operation_and_sent_time() -> None:
    """队列里要能看到「操作时间」与发送时间,并按默认时区换算。"""
    delivery = MailDelivery(
        export_version=7,
        recipient_id="r-1",
        recipient_email="someone@example.test",
        change_type="created",
        attachment_hash="hash",
        snapshot_path="/tmp/x.ics",
        status="sent",
        attempt_count=1,
        created_at=datetime(2026, 9, 28, 1, 30),
        sent_at=datetime(2026, 9, 28, 1, 31),
    )
    html = templates.get_template("partials/sync_status.html").render(
        csrf_token="csrf",
        sync_state={
            "dirty": False,
            "version": 7,
            "generated_at": "2026-09-28T01:30:00Z",
            "content_hash": "0" * 64,
            "size_bytes": 128,
            "file_exists": True,
            "subscription_url": "https://example.test/calendar",
            "mail_configured": True,
            "recipients_enabled": 1,
            "recipients_total": 1,
            "last_error": None,
            "last_delivery_success_at": "2026-09-28T01:31:00Z",
            "deliveries_pending": 0,
            "deliveries_retry_wait": 0,
            "deliveries_sent": 1,
            "deliveries_failed": 0,
        },
        deliveries=[delivery],
        status_labels={"sent": "已发送"},
        change_labels={"created": "新增"},
        default_timezone="Australia/Brisbane",
        notice=None,
        error=None,
    )

    assert "操作时间" in html
    assert "发送时间" in html
    # UTC 01:30 / 01:31 → 布里斯班(+10)11:30 / 11:31
    assert "2026-09-28 11:30" in html
    assert "2026-09-28 11:31" in html
    assert "2026-09-28 01:30" not in html, "不能把 UTC 原样当本地时间展示"
    assert "Australia/Brisbane" in html, "页面要说明时间用的是哪个时区"
