"""首位管理员「初始口令」救援通道的回归测试(2026-10-14 新增)。

真机事故:首位管理员的口令若由系统随机生成,明文**只在启动日志里出现一次**,
`docker logs` 一卡就永久丢失(库里只有 Argon2id 哈希,算不回来)。本文件守住三件事:

1. 初始口令状态会落成**文件**(不依赖日志);
2. 口令一旦设置,引导码文件立刻删除、`/bootstrap` 立即 404(不是常驻后门);
3. CLI 兜底能查看引导码、也能直接重置口令。
"""

from __future__ import annotations

import json
import re

import pytest
from app import bootstrap_access
from app.accounts import owner_user
from app.config import Settings


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "页面缺少 csrf_token"
    return match.group(1)


def _post_payload(code: str, csrf: str, *, password: str = "family-pass-123") -> dict[str, str]:
    return {
        "code": code,
        "password": password,
        "password_confirm": password,
        "csrf_token": csrf,
    }


@pytest.fixture()
def pending_admin(client, app_session, settings):
    """把运行中应用的首位管理员打回「初始口令」状态,并准备好引导码文件。"""
    owner = owner_user(app_session)
    assert owner is not None
    owner.must_change_password = True
    app_session.commit()
    return bootstrap_access.ensure_code(settings)


# --------------------------------------------------------------------------
# 领域层:文件是唯一载体,状态由 must_change_password 决定
# --------------------------------------------------------------------------
def test_code_file_follows_the_initial_password_state(session, family, settings) -> None:
    path = bootstrap_access.code_path(settings)
    # 测试环境用 ADMIN_PASSWORD 注入,不是初始口令 → 没有引导码文件
    assert bootstrap_access.sync_code_state(session, settings) is None
    assert not path.exists()

    family.owner.must_change_password = True
    session.commit()
    code = bootstrap_access.sync_code_state(session, settings)
    assert code is not None
    assert code.startswith(bootstrap_access.BOOTSTRAP_CODE_PREFIX)
    assert path.read_text(encoding="utf-8").strip() == code
    # 幂等:再同步一次不会换码
    assert bootstrap_access.sync_code_state(session, settings) == code

    family.owner.must_change_password = False
    session.commit()
    assert bootstrap_access.sync_code_state(session, settings) is None
    assert not path.exists()


def test_code_is_random_and_rotation_invalidates_the_old_one(settings) -> None:
    first = bootstrap_access.ensure_code(settings)
    assert bootstrap_access.verify_code(settings, first)
    assert not bootstrap_access.verify_code(settings, f"{first}x")
    assert not bootstrap_access.verify_code(settings, "")
    assert not bootstrap_access.verify_code(settings, None)
    # 不轮换时返回同一枚
    assert bootstrap_access.ensure_code(settings) == first

    rotated = bootstrap_access.ensure_code(settings, rotate=True)
    assert rotated != first
    assert not bootstrap_access.verify_code(settings, first)
    assert bootstrap_access.verify_code(settings, rotated)


def test_bootstrap_url_is_built_from_the_public_base(settings) -> None:
    url = bootstrap_access.bootstrap_url(settings, "sisi_boot_demo")
    assert url == "https://example.test/calendar/bootstrap?code=sisi_boot_demo"


def test_first_boot_with_a_random_password_writes_the_code_file(
    engine, session, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真机场景:没给 ADMIN_PASSWORD,口令是随机生成的 → 引导码必须落到文件上。"""
    from app.bootstrap import ensure_family_setup

    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    resolved = Settings()
    resolved.ensure_directories()

    ensure_family_setup(session, resolved)

    owner = owner_user(session)
    assert owner is not None
    assert owner.must_change_password is True
    code = bootstrap_access.read_code(resolved)
    assert code is not None
    assert bootstrap_access.code_path(resolved).exists()
    # 生成的口令哈希仍在老位置,但**引导码是另一条路**:不需要读日志
    assert (resolved.secret_dir / "admin.hash").exists()


# --------------------------------------------------------------------------
# HTTP 层:引导页与设口令
# --------------------------------------------------------------------------
def test_bootstrap_page_is_closed_when_the_password_is_not_initial(client) -> None:
    assert client.get("/bootstrap").status_code == 404
    assert client.get("/calendar/bootstrap").status_code == 404


def test_bootstrap_page_serves_the_form_with_csp(client, pending_admin) -> None:
    response = client.get(f"/bootstrap?code={pending_admin}")
    assert response.status_code == 200
    assert "Content-Security-Policy" in response.headers
    # 码被预填进表单,方便从地址栏直接进来
    assert pending_admin in response.text
    # 带前缀的入口同样可用(路由注册两遍)
    assert client.get("/calendar/bootstrap").status_code == 200


def test_setting_the_password_closes_the_channel(
    client, app_session, settings, pending_admin
) -> None:
    page = client.get("/bootstrap")
    response = client.post("/bootstrap", data=_post_payload(pending_admin, _csrf(page.text)))
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")

    # 通道关闭:文件被删、页面 404、状态位清零
    assert bootstrap_access.read_code(settings) is None
    assert client.get("/bootstrap").status_code == 404
    app_session.expire_all()
    owner = owner_user(app_session)
    assert owner is not None and owner.must_change_password is False

    # 新口令可用,而且不再被引导去改密
    username = owner.username
    login_page = client.get("/login")
    login = client.post(
        "/login",
        data={
            "username": username,
            "password": "family-pass-123",
            "csrf_token": _csrf(login_page.text),
            "next": "/admin",
        },
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/admin"


def test_wrong_code_and_bad_password_keep_the_channel_open(client, settings, pending_admin) -> None:
    page = client.get("/bootstrap")
    csrf = _csrf(page.text)

    wrong = client.post("/bootstrap", data=_post_payload("sisi_boot_not-the-code", csrf))
    assert wrong.status_code == 401
    assert bootstrap_access.read_code(settings) == pending_admin

    mismatch = client.post(
        "/bootstrap",
        data={
            "code": pending_admin,
            "password": "family-pass-123",
            "password_confirm": "family-pass-124",
            "csrf_token": csrf,
        },
    )
    assert mismatch.status_code == 422

    too_short = client.post(
        "/bootstrap", data=_post_payload(pending_admin, csrf, password="123")
    )
    assert too_short.status_code == 422
    assert bootstrap_access.read_code(settings) == pending_admin


def test_bootstrap_rejects_a_missing_csrf_token(client, settings, pending_admin) -> None:
    response = client.post("/bootstrap", data=_post_payload(pending_admin, "wrong-csrf"))
    assert response.status_code == 400
    assert bootstrap_access.read_code(settings) == pending_admin


def test_bootstrap_is_rate_limited_per_client(client, pending_admin) -> None:
    page = client.get("/bootstrap")
    csrf = _csrf(page.text)
    for _ in range(5):
        assert client.post("/bootstrap", data=_post_payload("bad", csrf)).status_code == 401
    limited = client.post("/bootstrap", data=_post_payload(pending_admin, csrf))
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_closed_channel_rejects_the_post_too(client, app_session, pending_admin) -> None:
    page = client.get("/bootstrap")
    csrf = _csrf(page.text)
    owner = owner_user(app_session)
    owner.must_change_password = False
    app_session.commit()

    response = client.post("/bootstrap", data=_post_payload(pending_admin, csrf))
    assert response.status_code == 404


# --------------------------------------------------------------------------
# CLI 兜底(容器内一条命令,不需要口令、不需要看日志)
# --------------------------------------------------------------------------
def test_cli_bootstrap_reports_not_pending(client, capsys: pytest.CaptureFixture[str]) -> None:
    from app import cli

    assert cli.main(["bootstrap"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "not_pending"
    assert "reset-admin-password" in payload["hint"]


def test_cli_bootstrap_prints_the_code_and_url(
    client, app_session, settings, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import cli

    owner = owner_user(app_session)
    owner.must_change_password = True
    app_session.commit()

    assert cli.main(["bootstrap"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "pending"
    assert payload["code"] == bootstrap_access.read_code(settings)
    assert payload["url"].endswith(f"/bootstrap?code={payload['code']}")

    rotated = bootstrap_access.read_code(settings)
    assert cli.main(["bootstrap", "--rotate"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["code"] != rotated


def test_cli_reset_admin_password_generates_a_working_one_time_password(
    client, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import cli

    assert cli.main(["reset-admin-password", "--generate"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["must_change"] is True
    # 提示必须说清「只显示这一次」
    assert "只显示这一次" in captured.err

    page = client.get("/login")
    login = client.post(
        "/login",
        data={
            "username": payload["username"],
            "password": payload["generated_password"],
            "csrf_token": _csrf(page.text),
            "next": "/admin",
        },
    )
    assert login.status_code == 303
    # 一次性口令 → 登录后先被送去改密
    assert "/admin/me" in login.headers["location"]


def test_cli_reset_admin_password_accepts_an_explicit_password(
    client, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import cli

    assert cli.main(["reset-admin-password", "--password", "chosen-pass-123"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["must_change"] is False

    page = client.get("/login")
    login = client.post(
        "/login",
        data={
            "username": payload["username"],
            "password": "chosen-pass-123",
            "csrf_token": _csrf(page.text),
            "next": "/admin",
        },
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/admin"