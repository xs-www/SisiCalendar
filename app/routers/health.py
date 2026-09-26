"""健康检查:进程存活与就绪状态。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.config import Settings, get_settings
from app.db import get_db
from app.exporter import get_state

router = APIRouter(tags=["ops"])


@router.get("/healthz", summary="进程健康检查")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


def _check_database(session: Session) -> tuple[bool, str | None]:
    try:
        session.execute(text("SELECT 1")).scalar_one()
    except Exception as exc:  # noqa: BLE001
        return False, f"数据库不可用:{type(exc).__name__}"
    return True, None


def _check_data_dir(settings: Settings) -> tuple[bool, str | None]:
    try:
        settings.ensure_directories()
        handle, name = tempfile.mkstemp(dir=str(settings.data_dir), prefix=".readyz-")
        os.close(handle)
        Path(name).unlink(missing_ok=True)
    except OSError as exc:
        return False, f"数据目录不可写:{exc.strerror or exc}"
    return True, None


def _check_export(session: Session, settings: Settings) -> tuple[bool, str | None]:
    try:
        state = get_state(session)
    except Exception as exc:  # noqa: BLE001
        return False, f"导出状态不可读:{type(exc).__name__}"
    path = settings.calendar_path
    if not path.exists():
        return False, "发布文件不存在"
    if state.dirty:
        return False, f"导出待修复:{state.last_error or '未生成最新版本'}"
    if state.last_error:
        return False, f"最近一次导出错误:{state.last_error}"
    return True, f"version={state.version}"


@router.get("/readyz", summary="数据库、数据目录与导出状态检查")
def readyz(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    checks: list[dict[str, object]] = []
    for name, (ok, detail) in (
        ("database", _check_database(session)),
        ("data_dir", _check_data_dir(settings)),
        ("export", _check_export(session, settings)),
    ):
        checks.append({"name": name, "ok": ok, "detail": detail})

    all_ok = all(bool(check["ok"]) for check in checks)
    payload = {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
        "mail_configured": settings.mail_configured,
    }
    return JSONResponse(payload, status_code=200 if all_ok else 503)
