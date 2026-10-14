"""事件 REST API。

权限模型:可见性由日历决定,读不到的事件返回 404(不暴露存在性),
写不动的事件返回 403(明确告诉调用方是权限问题)。日历清单见 `GET /api/v1/calendars`。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.accounts import calendar_access
from app.db import get_db
from app.deps import CsrfHeaderDep, UserDep
from app.errors import error_payload
from app.events import (
    EventConflictError,
    EventValidationError,
    build_range,
    create_event,
    expand_occurrences,
    get_event,
    move_event_to_calendar,
    query_events,
    serialize_event,
    serialize_occurrence,
    soft_delete_event,
    update_event,
)
from app.models import CHANGE_CREATED, CHANGE_DELETED, CHANGE_UPDATED
from app.schemas import EventWrite
from app.settings_store import effective_settings
from app.sync_service import SyncOutcome, input_time_text, mark_export_dirty, publish

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["events"])

MAX_LIMIT = 2000


def _validation_error(exc: EventValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=error_payload("validation_error", "事件校验失败", exc.fields),
    )


def _resolve_write_calendar(
    payload_calendar_id: str | None,
    access,
    *,
    existing_event_calendar_id: str | None = None,
) -> str:
    """决定这次写入落到哪个日历。

    - 显式给了 `calendar_id` → 必须可写,否则 403;
    - 修改既有事件且没给日历 → 沿用原日历(必须可写,否则调用方本来也读不到);
    - 新建且没给日历 → 用个人日历,其次第一个可写的共享日历。
    """
    if payload_calendar_id:
        if not access.can_view(payload_calendar_id):
            raise HTTPException(
                404, error_payload("not_found", "日历不存在或你没有访问权限")
            )
        if not access.can_write(payload_calendar_id):
            raise HTTPException(
                403,
                error_payload(
                    "forbidden",
                    "你没有修改该日历的权限(家庭共享日历默认只有管理员能改)",
                ),
            )
        return payload_calendar_id

    if existing_event_calendar_id:
        if not access.can_write(existing_event_calendar_id):
            raise HTTPException(
                403, error_payload("forbidden", "你没有修改该事件的权限")
            )
        return existing_event_calendar_id

    default_id = access.default_write_calendar_id()
    if not default_id:
        raise HTTPException(
            409,
            error_payload("no_writable_calendar", "你没有任何可写的日历,请联系家庭管理员"),
        )
    return default_id


@router.get("/calendars", summary="查询我有权访问的日历")
def list_calendars(current: UserDep, session: Session = Depends(get_db)) -> dict[str, object]:
    access = calendar_access(session, current.user)
    return {
        "items": [
            {
                "id": calendar.id,
                "name": calendar.name,
                "kind": calendar.kind,
                "color": calendar.color,
                "role": access.role_for(calendar.id),
                "can_write": access.can_write(calendar.id),
                "owner_user_id": calendar.owner_user_id,
            }
            for calendar in access.calendars
        ],
        "default_calendar_id": access.default_write_calendar_id(),
    }


@router.get("/events", summary="按日期范围和条件查询事件")
def list_events(
    request: Request,
    current: UserDep,
    session: Session = Depends(get_db),
    start: str | None = Query(default=None, description="范围起点,ISO 日期或时间"),
    end: str | None = Query(default=None, description="范围终点(不含),ISO 日期或时间"),
    q: str | None = Query(default=None, description="标题/说明/地点关键词"),
    status: str | None = Query(default=None),
    source: str | None = Query(default=None, description="来源:web 或 agent"),
    all_day: bool | None = Query(default=None),
    recurring: bool | None = Query(default=None),
    calendar_id: list[str] | None = Query(
        default=None, description="只看这些日历;可重复传,缺省为全部可见日历"
    ),
    expand: bool = Query(default=True, description="是否把重复事件展开为实例"),
    limit: int = Query(default=MAX_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    settings = effective_settings(session)
    access = calendar_access(session, current.user)

    allowed = access.viewable_ids()
    if calendar_id:
        # 只保留调用方真正有权看的日历,越权 id 直接忽略(而不是报错泄露存在性)
        allowed = [item for item in allowed if item in set(calendar_id)]

    # 只有显式给了 start/end 才做日期过滤,否则关键词检索覆盖全部事件
    use_range = bool(start or end)
    date_range = build_range(start, end, settings=settings) if use_range else None

    events, total = query_events(
        session,
        date_range=date_range,
        keyword=q,
        status=status,
        source=source,
        all_day=all_day,
        recurring=recurring,
        limit=limit,
        offset=offset,
        calendar_ids=allowed,
    )

    if date_range is None:
        items: list[dict[str, object]] = [
            serialize_event(event, access=access) for event in events
        ]
    elif expand:
        items = []
        for event in events:
            for occurrence in expand_occurrences(event, date_range):
                items.append(serialize_occurrence(occurrence, access=access))
        items.sort(
            key=lambda item: (str(item.get("start") or ""), str(item.get("title") or ""))
        )
    else:
        items = [serialize_event(event, access=access) for event in events]

    return {
        "items": items,
        "total": total,
        "calendars": [calendar.id for calendar in access.calendars],
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
    current: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    settings = effective_settings(session)
    access = calendar_access(session, current.user)
    target_calendar_id = _resolve_write_calendar(payload.calendar_id, access)
    try:
        event = create_event(
            session,
            payload,
            settings=settings,
            calendar_id=target_calendar_id,
            user_id=current.user_id,
        )
    except EventValidationError as exc:
        raise _validation_error(exc) from exc

    mark_export_dirty(session)
    session.commit()

    outcome = publish(
        session,
        settings=settings,
        change_type=CHANGE_CREATED,
        event=event,
        time_text=input_time_text(event, payload.start_at, payload.end_at),
        actor_user_id=current.user_id,
        actor_label=current.label,
        changed_calendar_ids=[target_calendar_id],
    )
    logger.info(
        "事件已创建 id=%s calendar=%s user=%s sync_ok=%s",
        event.id,
        target_calendar_id,
        current.username,
        outcome.ok,
    )
    return {**serialize_event(event, access=access), "sync": outcome.as_dict()}


@router.get("/events/{event_id}", summary="读取事件")
def read_event(
    event_id: str,
    current: UserDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    access = calendar_access(session, current.user)
    event = get_event(session, event_id, calendar_ids=access.viewable_ids())
    if event is None:
        raise HTTPException(404, error_payload("not_found", "事件不存在或已删除"))
    payload = serialize_event(event, access=access)
    settings = effective_settings(session)
    payload["occurrences"] = [
        serialize_occurrence(occurrence, access=access)
        for occurrence in expand_occurrences(
            event, build_range(None, None, settings=settings, default_days=365)
        )
    ]
    return payload


@router.patch("/events/{event_id}", summary="修改事件")
def update_event_endpoint(
    event_id: str,
    payload: EventWrite,
    current: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    settings = effective_settings(session)
    access = calendar_access(session, current.user)
    event = get_event(session, event_id, calendar_ids=access.viewable_ids())
    if event is None:
        raise HTTPException(404, error_payload("not_found", "事件不存在或已删除"))
    if not access.can_write(event.calendar_id):
        raise HTTPException(
            403,
            error_payload(
                "forbidden", "你没有修改该事件的权限(家庭共享日历默认只有管理员能改)"
            ),
        )

    previous_calendar_id = event.calendar_id
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

    # 换日历必须放在乐观锁校验**之后**:`move_event_to_calendar` 会刷新 `updated_at`,
    # 先搬家会让「客户端看到的那一版」被判成冲突(带 expected_updated_at 的搬家永远 409)。
    moved = False
    if payload.calendar_id and payload.calendar_id != event.calendar_id:
        target_calendar_id = _resolve_write_calendar(
            payload.calendar_id, access, existing_event_calendar_id=event.calendar_id
        )
        moved = move_event_to_calendar(session, event, target_calendar_id)

    if not changed and not moved:
        session.commit()
        outcome = SyncOutcome(
            ok=True,
            changed=False,
            version=None,
            generated_at=None,
            mail_created=0,
        )
        return {**serialize_event(event, access=access), "sync": outcome.as_dict()}

    mark_export_dirty(session)
    session.commit()
    outcome = publish(
        session,
        settings=settings,
        change_type=CHANGE_UPDATED,
        event=event,
        time_text=input_time_text(event, payload.start_at, payload.end_at),
        actor_user_id=current.user_id,
        actor_label=current.label,
        # 事件可能刚从别的日历移过来:新旧两边的可见者都要被告知
        changed_calendar_ids=[previous_calendar_id, event.calendar_id],
    )
    return {**serialize_event(event, access=access), "sync": outcome.as_dict()}


@router.delete("/events/{event_id}", summary="软删除事件")
def delete_event_endpoint(
    event_id: str,
    current: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    settings = effective_settings(session)
    access = calendar_access(session, current.user)
    event = get_event(session, event_id, calendar_ids=access.viewable_ids())
    if event is None:
        raise HTTPException(404, error_payload("not_found", "事件不存在或已删除"))
    if not access.can_write(event.calendar_id):
        raise HTTPException(
            403,
            error_payload(
                "forbidden", "你没有删除该事件的权限(家庭共享日历默认只有管理员能改)"
            ),
        )

    title = event.title
    soft_delete_event(session, event)
    mark_export_dirty(session)
    session.commit()

    outcome = publish(
        session,
        settings=settings,
        change_type=CHANGE_DELETED,
        event=event,
        event_title=title,
        actor_user_id=current.user_id,
        actor_label=current.label,
        changed_calendar_ids=[event.calendar_id],
    )
    logger.info("事件已软删除 id=%s user=%s sync_ok=%s", event_id, current.username, outcome.ok)
    return {"ok": True, "id": event_id, "sync": outcome.as_dict()}