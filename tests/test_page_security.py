"""页面安全响应头测试。

管理页启用了严格 CSP。历史上这里踩过一个坑:FullCalendar 的图标字体是内嵌的
base64 data URI,当时没有声明 `font-src`,它回退到 `default-src 'self'` 被拦掉,
日历上/下月按钮就退化成了方块。这个文件把策略固定下来,避免回归。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

CSP_PAGES = ["/admin", "/admin/settings", "/admin/sync", "/admin/events/new"]


def _directives(header: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for chunk in header.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, value = chunk.partition(" ")
        parsed[name.strip().lower()] = value.strip()
    return parsed


def test_csp_present_on_admin_pages(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    for path in CSP_PAGES:
        response = admin_client.get(path)
        assert response.status_code == 200, path
        assert "content-security-policy" in response.headers, f"{path} 缺少 CSP"


def test_csp_allows_the_embedded_icon_font(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    """FullCalendar 的图标字体是 data: URI,font-src 必须放行 data:。"""
    response = admin_client.get("/admin")
    directives = _directives(response.headers["content-security-policy"])

    assert "font-src" in directives, "缺少 font-src 会回退到 default-src,内嵌字体被拦"
    sources = directives["font-src"].split()
    assert "'self'" in sources
    assert "data:" in sources, "图标字体是 data: URI,不放行就会渲染成方块"


def test_csp_blocks_external_scripts_and_plugins(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    directives = _directives(
        admin_client.get("/admin").headers["content-security-policy"]
    )
    assert directives["default-src"] == "'self'"
    assert directives["script-src"] == "'self'", "禁止内联脚本,前端资源必须同源"
    assert "unsafe-eval" not in directives["script-src"]
    assert directives["frame-ancestors"] == "'none'"
    assert directives["base-uri"] == "'self'"
    assert directives["form-action"] == "'self'"
    assert "object-src" in directives or directives["default-src"] == "'self'"


def test_login_page_also_gets_csp(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "content-security-policy" in response.headers


def test_common_security_headers(
    admin_client: TestClient, api_headers: dict[str, str]
) -> None:
    response = admin_client.get("/admin")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"


def test_feed_is_not_served_with_admin_csp(client: TestClient) -> None:
    """订阅接口是给日历客户端消费的,不该套管理页的 CSP。"""
    response = client.get("/calendar")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"


def test_vendored_frontend_assets_are_self_hosted(client: TestClient) -> None:
    """前端依赖必须同源托管,否则会被 script-src 'self' 拦掉。"""
    for path in (
        "/static/vendor/fullcalendar.global.min.js",
        "/static/vendor/fullcalendar.locales.min.js",
        "/static/vendor/htmx.min.js",
        "/static/css/app.css",
        "/static/js/calendar.js",
        "/static/js/event_form.js",
    ):
        response = client.get(path)
        assert response.status_code == 200, path

    page = client.get("/login")
    assert "http" not in page.text.split("static/css/app.css")[0].split("<head>")[-1]
