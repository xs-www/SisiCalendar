"""只读订阅入口。

**只有带令牌的地址存在**:`<公开基地址>/<令牌>.ics`,内容是该账号可见范围
(个人 + 共享)的合并日历。升级前的 `/calendar` 与 `/calendar.ics`(无令牌、绑定
首位管理员)已**永久取消**——它们会把首位管理员的私人日历暴露给任何拿到地址的人,
现在每人都用自己那条固定地址(账号创建时生成,永不改变)。

路由路径是**相对的** `/<令牌>.ics`:应用把它同时挂在裸路径与本机配置的公开前缀下
(见 `app/main.py`),因此公开地址永远是 `<基地址>/<令牌>.ics`。后缀限定 `.ics`,
这样 `/calendar/login`、`/calendar/admin/...` 不会被订阅路由抢走。

令牌无效或停用一律 404:订阅入口不做 HTML 重定向,避免 iPhone 把错误页存成日历地址。

响应固定 MIME 类型,带 `ETag`、`Last-Modified`、`Cache-Control: private, max-age=...`,
支持条件请求返回 304。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app import feed_tokens
from app.config import Settings, get_settings
from app.db import get_db
from app.exporter import ExportError, export_calendar, feed_path_for, get_state

logger = logging.getLogger(__name__)

router = APIRouter(tags=["calendar"])

CONTENT_DISPOSITION = 'inline; filename="calendar.ics"'
FEED_MEDIA_TYPE = "text/calendar; charset=utf-8"


def _ensure_current(session: Session, settings: Settings, *, path: Path) -> None:
    """dirty 或订阅文件缺失时先尝试重新生成;失败则记录告警并继续返回最近的有效文件。

    `path` 是本次要返回的文件:**新账号或新令牌刚创建时**它还不存在,即使全局状态
    不 dirty 也必须补生成,否则订阅端会拿到 503。
    """
    try:
        state = get_state(session)
        if not state.dirty and settings.calendar_path.exists() and path.exists():
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


def _serve_feed(
    request: Request,
    *,
    path: Path,
    settings: Settings,
    missing_message: str,
) -> Response:
    if not path.exists():
        logger.error("订阅文件不存在且无法生成:%s", path)
        return Response(
            content=missing_message + "\n",
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


def _token_or_404(raw_token: str) -> str:
    """路由参数可能带 `.ics` 后缀,统一剥掉。"""
    token = (raw_token or "").strip()
    if token.endswith(".ics"):
        token = token[: -len(".ics")]
    return token


@router.api_route(
    "/{token}.ics", methods=["GET", "HEAD"], summary="按成员订阅日历"
)
def member_calendar_feed(
    token: str,
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    plaintext = _token_or_404(token)
    row = feed_tokens.resolve_feed_token(session, plaintext)
    if row is None:
        logger.info("订阅令牌无效或已停用")
        return Response(
            content="订阅地址无效或已被停用,请在日历应用的设置里重新获取。\n",
            status_code=404,
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    _ensure_current(session, settings, path=feed_path_for(settings, user_id=row.user_id))
    try:
        feed_tokens.touch(session, row)
        session.commit()
    except Exception:  # noqa: BLE001 - 「最近拉取时间」只是统计,不能拖垮订阅
        session.rollback()
        logger.exception("记录订阅令牌最近使用时间失败,本次订阅照常返回")

    return _serve_feed(
        request,
        path=feed_path_for(settings, user_id=row.user_id),
        settings=settings,
        missing_message="日历尚未生成,请在本机管理界面确认导出状态。",
    )