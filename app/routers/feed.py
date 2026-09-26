"""只读订阅入口 `/calendar` 与 `/calendar.ics`。

对应开发文档 6.3 节:固定 MIME 类型、`ETag`、`Last-Modified`、
`Cache-Control: private, max-age=300, must-revalidate`,支持条件请求返回 304,
并且**不做任何 HTML 重定向**,避免 iPhone 保存成错误地址。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.exporter import ExportError, export_calendar, get_state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["calendar"])

CONTENT_DISPOSITION = 'inline; filename="calendar.ics"'
FEED_MEDIA_TYPE = "text/calendar; charset=utf-8"


def _ensure_current(session: Session, settings: Settings) -> None:
    """dirty 时先尝试重新生成;失败则记录告警并继续返回最近的有效文件。"""
    try:
        state = get_state(session)
        if not state.dirty and settings.calendar_path.exists():
            return
        export_calendar(session, settings=settings)
        session.commit()
    except ExportError as exc:
        session.rollback()
        logger.warning("订阅请求触发的 ICS 重新生成失败,返回最近的有效版本:%s", exc)
    except Exception as exc:  # noqa: BLE001 - 订阅入口不得因数据库问题中断
        session.rollback()
        logger.warning("订阅入口检查导出状态失败:%s", type(exc).__name__)


def _is_not_modified(request: Request, etag: str, modified: datetime) -> bool:
    if_none_match = request.headers.get("if-none-match")
    if if_none_match:
        candidates = [item.strip() for item in if_none_match.split(",")]
        # If-None-Match 存在时不再看 If-Modified-Since(RFC 9110)
        return "*" in candidates or etag in candidates

    if_modified_since = request.headers.get("if-modified-since")
    if if_modified_since:
        try:
            since = parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError):
            return False
        if since is None:
            return False
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        return modified.replace(microsecond=0) <= since.astimezone(UTC).replace(microsecond=0)
    return False


@router.api_route("/calendar.ics", methods=["GET", "HEAD"], summary="订阅日历(兼容别名)")
@router.api_route("/calendar", methods=["GET", "HEAD"], summary="订阅日历")
def calendar_feed(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    _ensure_current(session, settings)

    path = settings.calendar_path
    if not path.exists():
        logger.error("发布文件不存在且无法生成:%s", path)
        return Response(
            content="日历尚未生成,请在本机管理界面确认导出状态。\n",
            status_code=503,
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    data = path.read_bytes()
    etag = f'"{hashlib.sha256(data).hexdigest()}"'
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    headers = {
        "ETag": etag,
        "Last-Modified": format_datetime(modified, usegmt=True),
        "Cache-Control": (
            f"private, max-age={max(0, settings.feed_cache_seconds)}, must-revalidate"
        ),
        "Content-Disposition": CONTENT_DISPOSITION,
        "X-Content-Type-Options": "nosniff",
    }

    if _is_not_modified(request, etag, modified):
        return Response(status_code=304, headers=headers)

    is_head = request.method.upper() == "HEAD"
    headers["Content-Length"] = str(len(data))
    return Response(
        content=b"" if is_head else data,
        media_type=FEED_MEDIA_TYPE,
        headers=headers,
    )
