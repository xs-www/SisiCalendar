"""对外基地址(origin 覆盖)与 Tailscale 辅助的回归测试(2026-10-14 新增)。

要点:界面里**只能改 origin**(域名/端口),路径前缀仍然是 `PUBLIC_BASE_URL` 环境变量
说了算 —— 路由是启动时按前缀挂载的(`app/main.py`),运行时改路径会让链接与路由错位。
本文件同时守住「改了 origin 立刻生效、不需要重启」与「非法输入一律 422」。
"""

from __future__ import annotations

import pytest
from app import urls
from app.host_port import HOST_GATEWAY_NAME, probe_host_port
from app.routers import pages
from app.settings_store import (
    SettingValidationError,
    effective_settings,
    load_overrides,
    save_overrides,
    validate_overrides,
)


# --------------------------------------------------------------------------
# 校验:只收 origin,路径必须与挂载前缀一致
# --------------------------------------------------------------------------
def test_accepts_origin_only_and_strips_the_matching_path(settings) -> None:
    cleaned = validate_overrides({"public_base_url": "sisi-laptop.tail921448.ts.net"}, settings)
    assert cleaned["public_base_url"] == "https://sisi-laptop.tail921448.ts.net"

    cleaned = validate_overrides(
        {"public_base_url": "https://sisi-laptop.tail921448.ts.net/calendar"}, settings
    )
    assert cleaned["public_base_url"] == "https://sisi-laptop.tail921448.ts.net"

    cleaned = validate_overrides({"public_base_url": "http://nas.local:18443"}, settings)
    assert cleaned["public_base_url"] == "http://nas.local:18443"


def test_empty_value_means_revert_to_environment(settings) -> None:
    assert validate_overrides({"public_base_url": "   "}, settings)["public_base_url"] == ""


@pytest.mark.parametrize(
    "value",
    [
        "https://node.ts.net/other",  # 路径前缀不是挂载前缀
        "https://node.ts.net/calendar?x=1",  # 带 query
        "https://node.ts.net/calendar#top",  # 带 fragment
        "https://user:pw@node.ts.net",  # 带用户名口令
        "ftp://node.ts.net",  # 非 http(s)
        "https://node.ts.net:not-a-port",  # 端口不是数字
        "https://",  # 没有主机名
    ],
)
def test_rejects_anything_that_is_not_a_usable_origin(value: str, settings) -> None:
    with pytest.raises(SettingValidationError):
        validate_overrides({"public_base_url": value}, settings)


def test_path_rule_error_explains_why(settings) -> None:
    with pytest.raises(SettingValidationError) as excinfo:
        validate_overrides({"public_base_url": "https://node.ts.net/team"}, settings)
    message = excinfo.value.fields["public_base_url"]
    assert "/calendar" in message
    assert "环境变量" in message


# --------------------------------------------------------------------------
# 生效值:origin 来自库,路径永远来自环境变量
# --------------------------------------------------------------------------
def test_override_changes_only_the_origin(session, settings) -> None:
    save_overrides(session, {"public_base_url": "https://other-node.ts.net"}, settings)
    session.commit()

    effective = effective_settings(session, settings)
    assert urls.public_base_url(effective) == "https://other-node.ts.net/calendar"
    # 挂载前缀没变(它只在启动时用于挂路由)
    assert urls.public_base_path(effective) == "/calendar"
    assert effective.public_base_path == "/calendar"


def test_clearing_the_override_falls_back_to_the_environment(session, settings) -> None:
    save_overrides(session, {"public_base_url": "https://other-node.ts.net"}, settings)
    session.commit()
    assert load_overrides(session)["public_base_url"] == "https://other-node.ts.net"

    save_overrides(session, {"public_base_url": ""}, settings)
    session.commit()
    assert "public_base_url" not in load_overrides(session)
    assert urls.public_base_url(effective_settings(session, settings)) == (
        "https://example.test/calendar"
    )


def test_environment_path_wins_even_with_a_stale_override(settings) -> None:
    """路径从环境变量现算:改 .env 并重建容器后,库里的 origin 覆盖不会带错路径。"""
    from app.settings_store import compose_public_base_url

    moved = settings.model_copy(update={"public_base_url": "https://node.ts.net/team"})
    assert compose_public_base_url("https://other.ts.net", moved) == (
        "https://other.ts.net/team"
    )


def test_garbage_override_falls_back_instead_of_breaking_links(session, settings) -> None:
    """库里被手工改脏(不是 origin)时,链接退回环境配置而不是渲染半截地址。"""
    from app.models import AppSetting

    session.add(AppSetting(key="public_base_url", value="not a url"))
    session.commit()
    assert urls.public_base_url(effective_settings(session, settings)) == (
        "https://example.test/calendar"
    )


# --------------------------------------------------------------------------
# HTTP:API 与设置页卡片
# --------------------------------------------------------------------------
def test_api_accepts_the_origin_and_returns_the_composed_url(admin_client, api_headers) -> None:
    response = admin_client.patch(
        "/api/v1/settings",
        json={"public_base_url": "https://other-node.ts.net"},
        headers=api_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["public_base_url"] == "https://other-node.ts.net/calendar"
    assert body["overrides"]["public_base_url"] == "https://other-node.ts.net"


def test_api_rejects_a_different_path(admin_client, api_headers) -> None:
    response = admin_client.patch(
        "/api/v1/settings",
        json={"public_base_url": "https://other-node.ts.net/team"},
        headers=api_headers,
    )
    assert response.status_code == 422
    assert "public_base_url" in response.text


def test_settings_card_shows_source_prefix_and_serve_command(admin_client) -> None:
    page = admin_client.get("/admin/partials/settings")
    assert page.status_code == 200
    text = page.text
    assert "对外地址(Tailscale)" in text
    assert "https://example.test/calendar" in text  # 当前生效值
    assert "来自环境变量" in text
    assert "tailscale serve status" in text  # 先快照
    # 命令里的端口与路径都来自配置,不是照抄文档里的 443
    assert pages._serve_command(admin_client.app.state.settings) in text


def test_serve_command_follows_host_port_and_prefix(settings) -> None:
    nas = settings.model_copy(update={"host_port": 18443})
    command = pages._serve_command(nas)
    assert command == (
        "tailscale serve --bg --yes --https=443 --set-path=/calendar "
        "http://127.0.0.1:18443/calendar"
    )
    bare = settings.model_copy(
        update={"host_port": 443, "public_base_url": "https://example.test"}
    )
    assert pages._serve_command(bare) == (
        "tailscale serve --bg --yes --https=443 http://127.0.0.1:443"
    )


def test_tailnet_visit_is_auto_filled_but_localhost_is_not(admin_client) -> None:
    tailnet = admin_client.get(
        "/admin/partials/settings", headers={"host": "sisi-laptop.tail921448.ts.net"}
    )
    assert "已按你当前的访问地址自动填入" in tailnet.text
    assert "https://sisi-laptop.tail921448.ts.net" in tailnet.text

    local = admin_client.get("/admin/partials/settings", headers={"host": "localhost:443"})
    assert "已按你当前的访问地址自动填入" not in local.text
    assert "请手动填写" in local.text


def test_tailnet_hint_scheme_falls_back_to_the_configured_base(settings) -> None:
    """没有 `X-Forwarded-Proto` 时按 `PUBLIC_BASE_URL` 的协议兜底,而不是硬猜 https。"""

    class _Request:
        def __init__(self, host: str, proto: str | None = None) -> None:
            self.headers = {"host": host}
            if proto is not None:
                self.headers["x-forwarded-proto"] = proto

    https_settings = settings.model_copy(
        update={"public_base_url": "https://example.test/calendar"}
    )
    http_settings = settings.model_copy(
        update={"public_base_url": "http://127.0.0.1:18099/calendar"}
    )
    node = "node.tail921448.ts.net"

    assert pages._tailnet_origin_hint(_Request(node), https_settings) == f"https://{node}"
    assert pages._tailnet_origin_hint(_Request(node), http_settings) == f"http://{node}"
    # 转发头优先
    assert pages._tailnet_origin_hint(_Request(node, "http"), https_settings) == f"http://{node}"
    # 非 tailnet 域名一律不猜
    assert pages._tailnet_origin_hint(_Request("localhost:443"), https_settings) == ""
    assert pages._tailnet_origin_hint(_Request("[::1]:443"), https_settings) == ""


def test_saving_the_card_field_updates_the_effective_url(admin_client, api_headers) -> None:
    page = admin_client.get("/admin/partials/settings")
    import re

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    response = admin_client.post(
        "/admin/actions/settings",
        data={
            "csrf_token": csrf,
            "calendar_name": "Sisi 测试日历",
            "default_timezone": "Australia/Brisbane",
            "default_event_duration_minutes": "60",
            "mail_max_attempts": "5",
            "mail_backoff_minutes": "1,5,15",
            "public_base_url": "https://card-node.ts.net",
        },
    )
    assert response.status_code == 200, response.text
    assert "https://card-node.ts.net/calendar" in response.text

    shown = admin_client.get("/api/v1/settings", headers=api_headers).json()
    assert shown["public_base_url"] == "https://card-node.ts.net/calendar"


# --------------------------------------------------------------------------
# 宿主端口探测
# --------------------------------------------------------------------------
def test_probe_rejects_an_out_of_range_port(settings) -> None:
    broken = settings.model_copy(update={"host_port": 0})
    reachable, detail = probe_host_port(broken)
    assert reachable is False
    assert "不合法" in detail


def test_probe_reports_a_reachable_port(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """真的探测一次「本机某个端口」:用假的主机名把请求指回自己起的服务。"""
    import http.server
    import threading

    from app import host_port as host_port_module

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == host_port_module.PROBE_PATH:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # noqa: ANN002, D102
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(host_port_module, "HOST_GATEWAY_NAME", "127.0.0.1")
    try:
        reachable, detail = probe_host_port(settings.model_copy(update={"host_port": port}))
    finally:
        server.shutdown()
        server.server_close()
    assert reachable is True, detail
    assert "可达" in detail


def test_probe_explains_how_to_fix_an_unreachable_port(settings) -> None:
    # 用一个几乎不会有人监听的端口,连不上时必须给出下一步而不是抛异常
    reachable, detail = probe_host_port(settings.model_copy(update={"host_port": 9}))
    assert reachable is False
    assert "extra_hosts" in detail
    assert HOST_GATEWAY_NAME in detail


def test_host_port_check_endpoint_returns_a_partial(
    admin_client, api_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import host_port as host_port_module

    monkeypatch.setattr(
        host_port_module, "probe_host_port", lambda *a, **k: (True, "可达:测试替身")
    )
    from app.routers import pages as pages_module

    monkeypatch.setattr(pages_module.host_port, "probe_host_port", host_port_module.probe_host_port)

    response = admin_client.post("/admin/actions/host-port-check", headers=api_headers)
    assert response.status_code == 200
    assert "可达:测试替身" in response.text
    assert "host.docker.internal" in response.text