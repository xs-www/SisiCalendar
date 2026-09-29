"""平台内远程接入令牌管理测试。

覆盖:创建时的一次性明文、库内只存哈希、DB 令牌能鉴权、与 .env 令牌并存、
撤销与停用立即生效、管理接口的登录 + CSRF 保护、以及明文绝不进入日志。
"""

from __future__ import annotations

import hashlib
import logging
import re

import pytest
from app import db as db_module
from app.models import EVENT_SOURCE_AGENT, AgentToken
from fastapi.testclient import TestClient
from sqlalchemy import select

AGENT_PAYLOAD = {
    "title": "令牌创建的会议",
    "start_at": "2026-11-04T09:00:00",
    "end_at": "2026-11-04T10:00:00",
    "timezone": "Australia/Brisbane",
}

PLAINTEXT_RE = re.compile(r'class="mono token-reveal" readonly value="([^"]+)"')
CSRF_RE = re.compile(r'data-csrf="([^"]+)"')


def _issue(client: TestClient, csrf: str, label: str = "iPhone 快捷指令") -> str:
    """通过管理界面创建令牌,返回一次性明文。"""
    response = client.post(
        "/admin/partials/tokens",
        data={"csrf_token": csrf, "label": label, "acknowledge": "1"},
    )
    assert response.status_code == 200, response.text
    plaintext = PLAINTEXT_RE.search(response.text)
    assert plaintext, "创建响应必须包含一次性明文"
    return plaintext.group(1)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rows() -> list[AgentToken]:
    factory = db_module.get_session_factory()
    with factory() as session:
        return list(session.execute(select(AgentToken)).scalars())


def _id_of(label: str) -> str:
    """按名称取令牌 id——列表里可能有多枚,按行序取容易拿错。"""
    matches = [row for row in _rows() if row.label == label]
    assert len(matches) == 1, f"名称 {label!r} 应恰好对应一枚令牌"
    return matches[0].id


def _login(client: TestClient) -> str:
    """登录后返回该会话的 CSRF 令牌。"""
    page = client.get("/login")
    hidden = page.text.split('name="csrf_token" value="')[1].split('"')[0]
    client.post(
        "/login",
        data={
            "username": "admin",
            "password": "test-password",
            "csrf_token": hidden,
            "next": "/admin",
        },
    )
    dashboard = client.get("/admin")
    found = CSRF_RE.search(dashboard.text)
    assert found, "登录后管理页缺少 data-csrf"
    return found.group(1)


# --------------------------------------------------------------------------
# 明文只出现一次,库里只有哈希
# --------------------------------------------------------------------------
def test_plaintext_is_shown_once_and_only_hash_is_stored(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    plaintext = _issue(admin_client, api_headers["X-CSRF-Token"])

    assert plaintext.startswith("sisi_")
    rows = _rows()
    assert len(rows) == 1
    expected = hashlib.sha256(plaintext.encode()).hexdigest()
    assert rows[0].token_hash == expected
    # 明文绝不能以任何形式留在库里
    assert plaintext not in rows[0].token_hash
    assert plaintext != rows[0].token_hash

    listing = admin_client.get("/admin/partials/tokens")
    assert listing.status_code == 200
    assert plaintext not in listing.text, "列表不得回显明文"
    assert expected[:16] in listing.text, "指纹应展示,便于与审计日志对账"


def test_settings_page_carries_the_token_card(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    _issue(admin_client, api_headers["X-CSRF-Token"], label="客厅 iPad")

    page = admin_client.get("/admin/settings")
    assert page.status_code == 200
    assert "远程接入令牌" in page.text
    assert "客厅 iPad" in page.text
    assert "已配置 1 枚" in page.text
    # 一次性提示只在创建响应里出现,刷新页面不应再显示
    assert "令牌明文只显示这一次" not in page.text


# --------------------------------------------------------------------------
# DB 令牌鉴权
# --------------------------------------------------------------------------
def test_db_token_authorizes_agent_requests(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    # 没有任何令牌时,整个接口等同不存在
    assert admin_client.post("/api/agent/events", json=AGENT_PAYLOAD).status_code == 404

    plaintext = _issue(admin_client, api_headers["X-CSRF-Token"])

    response = admin_client.post(
        "/api/agent/events", json=AGENT_PAYLOAD, headers=_bearer(plaintext)
    )
    assert response.status_code == 201, response.text
    assert response.json()["source"] == EVENT_SOURCE_AGENT


def test_db_token_and_env_token_coexist(agent_client: TestClient) -> None:
    """env 里的令牌仍作为兜底:两套令牌同时有效。"""
    csrf = _login(agent_client)
    env_headers = {"Authorization": "Bearer test-agent-token-abcdefghijklmnop"}

    def create(title: str, headers: dict[str, str]) -> int:
        return agent_client.post(
            "/api/agent/events",
            json={**AGENT_PAYLOAD, "title": title},
            headers=headers,
        ).status_code

    assert create("env 建的", env_headers) == 201

    plaintext = _issue(agent_client, csrf, label="平台建的")
    assert create("平台建的", _bearer(plaintext)) == 201
    # env 令牌不受平台内新增令牌影响
    assert create("env 又建的", env_headers) == 201


# --------------------------------------------------------------------------
# 撤销与停用
# --------------------------------------------------------------------------
def test_revoked_token_stops_working_immediately(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    csrf = api_headers["X-CSRF-Token"]
    keep = _issue(admin_client, csrf, label="保留")
    doomed = _issue(admin_client, csrf, label="待撤销")
    doomed_id = _id_of("待撤销")

    assert (
        admin_client.post(
            "/api/agent/events", json=AGENT_PAYLOAD, headers=_bearer(doomed)
        ).status_code
        == 201
    )

    revoked = admin_client.post(
        f"/admin/partials/tokens/{doomed_id}/delete", data={"csrf_token": csrf}
    )
    assert revoked.status_code == 200
    assert "已撤销" in revoked.text

    # 令牌已撤销 → 401(接口本身仍因另一枚令牌而开放)
    assert (
        admin_client.post(
            "/api/agent/events", json={**AGENT_PAYLOAD, "title": "撤销后"}, headers=_bearer(doomed)
        ).status_code
        == 401
    )
    assert keep  # 另一枚仍然有效
    assert (
        admin_client.post(
            "/api/agent/events", json={**AGENT_PAYLOAD, "title": "保留的"}, headers=_bearer(keep)
        ).status_code
        == 201
    )
    assert len(_rows()) == 1


def test_revoking_every_token_hides_the_endpoint_again(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    csrf = api_headers["X-CSRF-Token"]
    plaintext = _issue(admin_client, csrf)
    token_id = _id_of("iPhone 快捷指令")
    admin_client.post(f"/admin/partials/tokens/{token_id}/delete", data={"csrf_token": csrf})

    # 没有任何来源的令牌时回到 404,而不是 401
    assert admin_client.post("/api/agent/events", json=AGENT_PAYLOAD).status_code == 404
    assert (
        admin_client.post(
            "/api/agent/events", json=AGENT_PAYLOAD, headers=_bearer(plaintext)
        ).status_code
        == 404
    )


def test_disabled_token_is_rejected_and_can_be_reenabled(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    csrf = api_headers["X-CSRF-Token"]
    _issue(admin_client, csrf, label="占位")
    plaintext = _issue(admin_client, csrf, label="要停用的")
    token_id = _id_of("要停用的")

    stopped = admin_client.post(
        f"/admin/partials/tokens/{token_id}/toggle", data={"csrf_token": csrf}
    )
    assert stopped.status_code == 200
    assert "已停用" in stopped.text
    assert (
        admin_client.post(
            "/api/agent/events", json=AGENT_PAYLOAD, headers=_bearer(plaintext)
        ).status_code
        == 401
    )

    resumed = admin_client.post(
        f"/admin/partials/tokens/{token_id}/toggle", data={"csrf_token": csrf}
    )
    assert resumed.status_code == 200
    assert "已启用" in resumed.text
    assert (
        admin_client.post(
            "/api/agent/events", json=AGENT_PAYLOAD, headers=_bearer(plaintext)
        ).status_code
        == 201
    )


def test_token_can_be_renamed(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    csrf = api_headers["X-CSRF-Token"]
    _issue(admin_client, csrf, label="旧名字")
    token_id = _id_of("旧名字")

    renamed = admin_client.post(
        f"/admin/partials/tokens/{token_id}", data={"csrf_token": csrf, "label": "新名字"}
    )
    assert renamed.status_code == 200
    assert "新名字" in renamed.text
    assert _rows()[0].label == "新名字"

    blank = admin_client.post(
        f"/admin/partials/tokens/{token_id}", data={"csrf_token": csrf, "label": "   "}
    )
    assert blank.status_code == 422
    assert "不能为空" in blank.text
    assert _rows()[0].label == "新名字", "校验失败不得写入"


# --------------------------------------------------------------------------
# 管理接口的守卫
# --------------------------------------------------------------------------
def test_token_management_requires_login(client: TestClient) -> None:
    anonymous = client.post(
        "/admin/partials/tokens", data={"label": "x", "acknowledge": "1"}
    )
    assert anonymous.status_code == 303
    assert "/login" in anonymous.headers["location"]


def test_token_management_requires_csrf(
    admin_client: TestClient,
) -> None:
    no_csrf = admin_client.post(
        "/admin/partials/tokens", data={"label": "x", "acknowledge": "1"}
    )
    assert no_csrf.status_code == 403
    assert _rows() == []


def test_creation_requires_acknowledgement_and_label(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    csrf = api_headers["X-CSRF-Token"]

    no_ack = admin_client.post(
        "/admin/partials/tokens", data={"csrf_token": csrf, "label": "没有确认"}
    )
    assert no_ack.status_code == 422
    assert "请先勾选确认" in no_ack.text

    no_label = admin_client.post(
        "/admin/partials/tokens",
        data={"csrf_token": csrf, "label": "   ", "acknowledge": "1"},
    )
    assert no_label.status_code == 422
    assert "不能为空" in no_label.text

    assert _rows() == []


def test_unknown_token_id_returns_404(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    csrf = api_headers["X-CSRF-Token"]
    for path in (
        "/admin/partials/tokens/nope/toggle",
        "/admin/partials/tokens/nope/delete",
    ):
        response = admin_client.post(path, data={"csrf_token": csrf})
        assert response.status_code == 404, path
        assert "令牌不存在" in response.text

    renamed = admin_client.post(
        "/admin/partials/tokens/nope", data={"csrf_token": csrf, "label": "x"}
    )
    assert renamed.status_code == 404


# --------------------------------------------------------------------------
# 明文绝不进入日志
# --------------------------------------------------------------------------
def test_plaintext_never_reaches_logs(
    admin_client: TestClient,
    api_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    csrf = api_headers["X-CSRF-Token"]
    with caplog.at_level(logging.INFO):
        plaintext = _issue(admin_client, csrf, label="日志检查")
        token_id = _id_of("日志检查")
        admin_client.post(
            "/api/agent/events", json=AGENT_PAYLOAD, headers=_bearer(plaintext)
        )
        admin_client.post(
            f"/admin/partials/tokens/{token_id}/delete", data={"csrf_token": csrf}
        )

    assert plaintext not in caplog.text, "令牌明文绝不能出现在日志里"
    # 指纹仍应出现在审计日志中,便于追查是哪枚令牌在用
    assert hashlib.sha256(plaintext.encode()).hexdigest()[:16] in caplog.text