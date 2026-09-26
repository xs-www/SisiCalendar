"""同步人员与设置 API 测试:隐私确认、唯一性、限流前提与脱敏。"""

from __future__ import annotations

from fastapi.testclient import TestClient

ACK = {"acknowledge_full_calendar": True}


def _add(client: TestClient, headers: dict[str, str], email: str, **extra) -> dict:
    response = client.post(
        "/api/v1/sync-recipients", json={"email": email, **ACK, **extra}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_requires_acknowledgement_before_adding(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/sync-recipients",
        json={"email": "a@example.test"},
        headers=api_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "confirmation_required"


def test_create_and_list_recipient(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    created = _add(admin_client, api_headers, "Someone@Example.Test", display_name="小明")
    assert created["email"] == "someone@example.test", "邮箱需要规范化"
    assert created["enabled"] is True
    assert created["masked_email"] != created["email"]

    listed = admin_client.get("/api/v1/sync-recipients").json()
    assert listed["total"] == 1
    assert listed["enabled"] == 1


def test_duplicate_email_conflicts(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _add(admin_client, api_headers, "dup@example.test")
    again = admin_client.post(
        "/api/v1/sync-recipients",
        json={"email": "DUP@example.test", **ACK},
        headers=api_headers,
    )
    assert again.status_code == 409


def test_invalid_email_rejected(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/sync-recipients",
        json={"email": "not-an-email", **ACK},
        headers=api_headers,
    )
    assert response.status_code == 422
    assert "email" in response.json()["error"]["fields"]


def test_disable_then_enable_requires_acknowledgement(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    created = _add(admin_client, api_headers, "t@example.test")

    disabled = admin_client.patch(
        f"/api/v1/sync-recipients/{created['id']}",
        json={"enabled": False},
        headers=api_headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    without_ack = admin_client.patch(
        f"/api/v1/sync-recipients/{created['id']}",
        json={"enabled": True},
        headers=api_headers,
    )
    assert without_ack.status_code == 422

    with_ack = admin_client.patch(
        f"/api/v1/sync-recipients/{created['id']}",
        json={"enabled": True, **ACK},
        headers=api_headers,
    )
    assert with_ack.status_code == 200
    assert with_ack.json()["enabled"] is True


def test_remove_recipient(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    created = _add(admin_client, api_headers, "bye@example.test")
    response = admin_client.delete(
        f"/api/v1/sync-recipients/{created['id']}", headers=api_headers
    )
    assert response.status_code == 200
    assert admin_client.get("/api/v1/sync-recipients").json()["total"] == 0


def test_recipient_mutations_require_csrf(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/api/v1/sync-recipients", json={"email": "x@example.test", **ACK}
    )
    assert response.status_code == 403


def test_settings_update_and_validation(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    ok = admin_client.patch(
        "/api/v1/settings",
        json={"calendar_name": "我的私人日历", "default_timezone": "Asia/Shanghai"},
        headers=api_headers,
    )
    assert ok.status_code == 200
    assert ok.json()["calendar_name"] == "我的私人日历"

    bad = admin_client.patch(
        "/api/v1/settings",
        json={"default_timezone": "Nowhere/Land"},
        headers=api_headers,
    )
    assert bad.status_code == 422
    assert "default_timezone" in bad.json()["error"]["fields"]

    # 名称覆盖会反映到订阅文件里
    feed = admin_client.get("/calendar")
    assert b"X-WR-CALNAME" in feed.content


def test_settings_never_exposes_smtp_password(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    payload = admin_client.get("/api/v1/settings").json()
    text = str(payload)
    assert "password" not in text or "password_configured" in text
    assert "smtp_password" not in text


def test_mail_test_without_smtp_returns_conflict(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post("/api/v1/settings/mail/test", headers=api_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mail_not_configured"


def test_send_latest_requires_confirmation(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/sync/mail/send-latest", json={"confirm": False}, headers=api_headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "confirmation_required"


def test_send_latest_without_smtp_returns_conflict(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/sync/mail/send-latest", json={"confirm": True}, headers=api_headers
    )
    assert response.status_code == 409


def test_regenerate_creates_new_version(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    before = admin_client.get("/api/v1/sync/status").json()["version"]
    response = admin_client.post("/api/v1/sync/calendar", headers=api_headers)
    assert response.status_code == 200
    assert response.json()["sync"]["ok"] is True
    assert response.json()["status"]["version"] == before + 1


def test_retry_unknown_delivery_returns_404(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.post(
        "/api/v1/mail-deliveries/does-not-exist/retry", headers=api_headers
    )
    assert response.status_code == 404


def test_settings_page_renders_smtp_status(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    page = admin_client.get("/admin/settings")
    assert page.status_code == 200
    assert "SMTP 发信配置" in page.text
    assert "未配置" in page.text
