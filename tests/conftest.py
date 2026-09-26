"""pytest 夹具:每个测试使用独立的数据目录与数据库。"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from app import db as db_module
from app.config import Settings, reset_settings_cache
from app.models import Base
from fastapi.testclient import TestClient

TEST_TIMEZONE = "Australia/Brisbane"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "test-password"


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """把数据目录指向临时路径,并清空所有可能干扰测试的环境变量。"""
    for key in list(os.environ):
        if key.startswith(
            (
                "DATA_DIR",
                "ADMIN_",
                "SESSION_",
                "SMTP_",
                "CALENDAR_",
                "DEFAULT_",
                "MAIL_",
                "SUBSCRIPTION_",
                "SCHEDULER_",
                "FEED_",
                "BACKUP_",
            )
        ):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("DEFAULT_TIMEZONE", TEST_TIMEZONE)
    monkeypatch.setenv("CALENDAR_NAME", "Sisi 测试日历")
    monkeypatch.setenv("SUBSCRIPTION_URL", "https://example.test/calendar")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    reset_settings_cache()

    resolved = Settings()
    resolved.ensure_directories()
    yield resolved
    reset_settings_cache()


@pytest.fixture()
def engine(settings: Settings) -> Iterator[object]:
    created = db_module.init_engine(settings, use_null_pool=True)
    Base.metadata.create_all(created)
    yield created
    db_module.dispose_engine()


@pytest.fixture()
def session(engine: object) -> Iterator[object]:
    factory = db_module.get_session_factory()
    db_session = factory()
    try:
        yield db_session
    finally:
        db_session.rollback()
        db_session.close()


# --------------------------------------------------------------------------
# HTTP 层夹具:走完整 lifespan(含 Alembic 迁移)
# --------------------------------------------------------------------------
@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    from app.main import create_app

    application = create_app(settings)
    with TestClient(application, follow_redirects=False) as test_client:
        yield test_client
    db_module.dispose_engine()


def _hidden_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "登录页缺少 csrf_token"
    return match.group(1)


@pytest.fixture()
def admin_client(client: TestClient) -> TestClient:
    page = client.get("/login")
    assert page.status_code == 200
    response = client.post(
        "/login",
        data={
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
            "csrf_token": _hidden_csrf(page.text),
            "next": "/admin",
        },
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture()
def session_csrf(admin_client: TestClient) -> str:
    page = admin_client.get("/admin")
    assert page.status_code == 200, page.text
    match = re.search(r'data-csrf="([^"]+)"', page.text)
    assert match, "日历页缺少 data-csrf"
    return match.group(1)


@pytest.fixture()
def api_headers(session_csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": session_csrf}
