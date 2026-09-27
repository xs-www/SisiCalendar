"""远程 Agent 接入接口。

用途:让本机之外的自动化 Agent(脚本、LLM Agent、iOS 快捷指令等)通过 HTTP 新建日程。
设计原则:

- **默认关闭**:未配置 `AGENT_API_TOKEN` 时整个 `/api/agent` 返回 404,等同不存在。
- **只认 Bearer 令牌**:不接受 Cookie 会话,因此不存在 CSRF 面;
  浏览器即使已登录管理界面,没有令牌也无法调用。
- **请求体宽容**:同时接受 `application/json` 与 `application/x-www-form-urlencoded`。
  表单形式是为 iOS 快捷指令准备的——「获取 URL 内容」选 Form 就不用手写 JSON 转义。
- **幂等**:可选的 `client_ref` 让重试安全——同一个值重复提交返回同一个事件,
  既不会重复建事件,也不会重复触发邮件。
- 走与界面完全相同的链路:落库 → 标记待导出 → 生成 ICS → 为启用人员创建发信任务。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AgentDep
from app.errors import error_payload, field_errors_from
from app.events import (
    EventValidationError,
    create_event,
    get_event,
    serialize_event,
)
from app.models import CHANGE_CREATED, EVENT_SOURCE_AGENT, Event
from app.schemas import EventWrite
from app.security import AuditLog, rate_limiter
from app.settings_store import effective_settings
from app.sync_service import mark_export_dirty, publish

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

MAX_CLIENT_REF = 200
FORM_CONTENT_TYPES = {"application/x-www-form-urlencoded", "multipart/form-data"}


def _serialize_result(event: Event, *, duplicate: bool, sync: dict[str, object] | None) -> dict:
    payload = serialize_event(event)
    payload["duplicate"] = duplicate
    payload["sync"] = sync
    return payload


async def read_event_payload(request: Request) -> EventWrite:
    """解析请求体:JSON 或表单。

    表单里空字符串一律视为「未提供」,这样快捷指令中留空的字段不会被当成有效值。
    """
    raw_content_type = request.headers.get("content-type", "")
    content_type = raw_content_type.split(";")[0].strip().lower()

    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            data = await request.json()
        except Exception as exc:  # noqa: BLE001 - 任何解析失败都是客户端问题
            raise HTTPException(
                422, error_payload("validation_error", "请求体不是合法的 JSON")
            ) from exc
        if not isinstance(data, dict):
            raise HTTPException(
                422, error_payload("validation_error", "请求体必须是 JSON 对象")
            )
    elif content_type in FORM_CONTENT_TYPES:
        form = await request.form()
        data = {
            key: (value if isinstance(value, str) and value.strip() else None)
            for key, value in form.items()
        }
    else:
        raise HTTPException(
            415,
            error_payload(
                "unsupported_media_type",
                "请使用 application/json 或 application/x-www-form-urlencoded",
            ),
        )

    try:
        return EventWrite.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            422,
            error_payload("validation_error", "请求参数校验失败", field_errors_from(exc)),
        ) from exc


@router.post(
    "/events",
    summary="远程 Agent 新建日程",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": EventWrite.model_json_schema()},
                "application/x-www-form-urlencoded": {
                    "schema": {"type": "object", "additionalProperties": {"type": "string"}}
                },
            },
        }
    },
)
async def agent_create_event(
    request: Request,
    principal: AgentDep,
    session: Session = Depends(get_db),
) -> JSONResponse:
    settings = effective_settings(session)

    allowed, retry_after = rate_limiter.check(
        principal.label,
        limit=max(1, settings.agent_rate_limit_per_minute),
        window_seconds=60,
    )
    if not allowed:
        AuditLog.record("agent_create", "rate_limited", actor=principal.label)
        raise HTTPException(
            status_code=429,
            detail=error_payload(
                "rate_limited", f"请求过于频繁,请在 {retry_after} 秒后重试"
            ),
            headers={"Retry-After": str(retry_after)},
        )

    payload = await read_event_payload(request)

    client_ref = (payload.client_ref or "").strip() or None
    if client_ref and len(client_ref) > MAX_CLIENT_REF:
        raise HTTPException(
            status_code=422,
            detail=error_payload(
                "validation_error",
                "幂等键过长",
                {"client_ref": f"最多 {MAX_CLIENT_REF} 个字符"},
            ),
        )

    # 幂等重放:同一 client_ref 已存在时直接返回既有事件,不新建、不重新导出、不发信
    if client_ref:
        existing = session.execute(
            select(Event).where(Event.client_ref == client_ref)
        ).scalar_one_or_none()
        if existing is not None:
            AuditLog.record(
                "agent_create", "duplicate", actor=principal.label, detail=existing.id
            )
            return JSONResponse(
                _serialize_result(existing, duplicate=True, sync=None), status_code=200
            )

    try:
        event = create_event(
            session,
            payload,
            settings=settings,
            source=EVENT_SOURCE_AGENT,
            client_ref=client_ref,
        )
    except EventValidationError as exc:
        session.rollback()
        AuditLog.record("agent_create", "invalid", actor=principal.label)
        raise HTTPException(
            status_code=422,
            detail=error_payload("validation_error", "事件校验失败", exc.fields),
        ) from exc

    mark_export_dirty(session)
    session.commit()

    outcome = publish(
        session, settings=settings, change_type=CHANGE_CREATED, event=event
    )
    AuditLog.record(
        "agent_create",
        "ok" if outcome.ok else "export_failed",
        actor=principal.label,
        detail=f"{event.id} version={outcome.version}",
    )
    logger.info("Agent 新建日程 id=%s sync_ok=%s", event.id, outcome.ok)

    return JSONResponse(
        _serialize_result(event, duplicate=False, sync=outcome.as_dict()),
        status_code=201,
    )


@router.get("/events/{event_id}", summary="远程 Agent 查询自己创建的事件")
def agent_read_event(
    event_id: str,
    principal: AgentDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """只读回查,便于 Agent 在超时后确认结果。仅返回 Agent 创建的事件。"""
    event = get_event(session, event_id)
    if event is None or event.source != EVENT_SOURCE_AGENT:
        raise HTTPException(404, error_payload("not_found", "事件不存在"))
    return _serialize_result(event, duplicate=False, sync=None)
