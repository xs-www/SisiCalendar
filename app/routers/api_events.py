"""事件 REST API。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AdminDep, CsrfHeaderDep
from app.errors import error_payload
from app.events import (
    EventConflictError,
    EventValidationError,
    build_range,
    create_event,
    expand_occurrences,
    get_event,
    query_events,
    serialize_event,
    serialize_occurrence,
    soft_delete_event,
    update_event,
)
from app.models import CHANGE_CREATED, CHANGE_DELETED, CHANGE_UPDATED
from app.schemas import EventWrite
from app.settings_store import effective_settings
from app.sync_service import SyncOutcome, mark_export_dirty, publish

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["events"])

MAX_LIMIT = 2000


def _validation_error(exc: EventValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=error_payload("validation_error", "事件校验失败", exc.fields),
    )


@router.get("/events", summary="按日期范围和条件查询事件")
def list_events(
    request: Request,
    admin: AdminDep,
    session: Session = Depends(get_db),
    start: str | None = Query(default=None, description="范围起点,ISO 日期或时间"),
    end: str | None = Query(default=None, description="范围终点(不含),ISO 日期或时间"),
    q: str | None = Query(default=None, description="标题/说明/地点关键词"),
    status: str | None = Query(default=None),
    all_day: bool | None = Query(default=None),
    recurring: bool | None = Query(default=None),
    expand: bool = Query(default=True, description="是否把重复事件展开为实例"),
    limit: int = Query(default=MAX_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    settings = effective_settings(session)
    # 只有显式给了 start/end 才做日期过滤,否则关键词检索覆盖全部事件
    use_range = bool(start or end)
    date_range = build_range(start, end, settings=settings) if use_range else None

    events, total = query_events(
        session,
        date_range=date_range,
        keyword=q,
        status=status,
        all_day=all_day,
        recurring=recurring,
        limit=limit,
        offset=offset,
    )

    if date_range is None:
        items: list[dict[str, object]] = [serialize_event(event) for event in events]
    elif expand:
        items = []
        for event in events:
            for occurrence in expand_occurrences(event, date_range):
                items.append(serialize_occurrence(occurrence))
        items.sort(
            key=lambda item: (str(item.get("start") or ""), str(item.get("title") or ""))
        )
    else:
        items = [serialize_event(event) for event in events]

    return {
        "items": items,
        "total": total,
        "range": (
            {
                "start": date_range.start_utc.isoformat() + "Z",
                "end": date_range.end_utc.isoformat() + "Z",
                "start_date": date_range.start_date.isoformat(),
                "end_date": date_range.end_date.isoformat(),
            }
            if date_range
            else None
        ),
    }


@router.post("/events", status_code=201, summary="新建事件")
def create_event_endpoint(
    payload: EventWrite,
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    settings = effective_settings(session)
    try:
        event = create_event(session, payload, settings=settings)
    except EventValidationError as exc:
        raise _validation_error(exc) from exc

    mark_export_dirty(session)
    session.commit()

    outcome = publish(
        session, settings=settings, change_type=CHANGE_CREATED, event=event
    )
    logger.info("事件已创建 id=%s sync_ok=%s", event.id, outcome.ok)
    return {**serialize_event(event), "sync": outcome.as_dict()}


@router.get("/events/{event_id}", summary="读取事件")
def read_event(
    event_id: str,
    admin: AdminDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    event = get_event(session, event_id)
    if event is None:
        raise HTTPException(404, error_payload("not_found", "事件不存在或已删除"))
    payload = serialize_event(event)
    settings = effective_settings(session)
    payload["occurrences"] = [
        serialize_occurrence(occurrence)
        for occurrence in expand_occurrences(
            event, build_range(None, None, settings=settings, default_days=365)
        )
    ]
    return payload


@router.patch("/events/{event_id}", summary="修改事件")
def update_event_endpoint(
    event_id: str,
    payload: EventWrite,
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    settings = effective_settings(session)
    event = get_event(session, event_id)
    if event is None:
        raise HTTPException(404, error_payload("not_found", "事件不存在或已删除"))

    try:
        changed = update_event(session, event, payload, settings=settings)
    except EventConflictError as exc:
        session.rollback()
        raise HTTPException(
            409,
            error_payload(
                "conflict",
                str(exc),
                {"expected_updated_at": "该事件已被其他页面修改,请刷新后重试"},
            ),
        ) from exc
    except EventValidationError as exc:
        session.rollback()
        raise _validation_error(exc) from exc

    if not changed:
        session.commit()
        outcome = SyncOutcome(
            ok=True,
            changed=False,
            version=None,
            generated_at=None,
            mail_created=0,
        )
        return {**serialize_event(event), "sync": outcome.as_dict()}

    mark_export_dirty(session)
    session.commit()
    outcome = publish(
        session, settings=settings, change_type=CHANGE_UPDATED, event=event
    )
    return {**serialize_event(event), "sync": outcome.as_dict()}


@router.delete("/events/{event_id}", summary="软删除事件")
def delete_event_endpoint(
    event_id: str,
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    settings = effective_settings(session)
    event = get_event(session, event_id)
    if event is None:
        raise HTTPException(404, error_payload("not_found", "事件不存在或已删除"))

    title = event.title
    soft_delete_event(session, event)
    mark_export_dirty(session)
    session.commit()

    outcome = publish(
        session, settings=settings, change_type=CHANGE_DELETED, event=event, event_title=title
    )
    logger.info("事件已软删除 id=%s sync_ok=%s", event_id, outcome.ok)
    return {"ok": True, "id": event_id, "sync": outcome.as_dict()}
