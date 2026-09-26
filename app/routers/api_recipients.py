"""同步人员(邮件收件人)配置 API。

安全提示:文档要求新增或启用人员时必须提示“该人员将收到完整日历”,
因此新增与启用都需要显式的 `acknowledge_full_calendar=true`。
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import AdminDep, CsrfHeaderDep
from app.errors import error_payload
from app.models import SyncRecipient, normalize_email
from app.schemas import RecipientWrite
from app.timeutil import iso_utc, utcnow
from app.util import mask_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["recipients"])

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 320


def serialize_recipient(recipient: SyncRecipient) -> dict[str, object]:
    return {
        "id": recipient.id,
        "email": recipient.email,
        "display_name": recipient.display_name,
        "enabled": bool(recipient.enabled),
        "created_at": iso_utc(recipient.created_at),
        "updated_at": iso_utc(recipient.updated_at),
        "last_sent_at": iso_utc(recipient.last_sent_at),
        "masked_email": mask_email(recipient.email),
    }


def _validate_email(raw: str | None) -> str:
    email = normalize_email(raw or "")
    if not email:
        raise HTTPException(
            422,
            error_payload("validation_error", "邮箱不能为空", {"email": "邮箱不能为空"}),
        )
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.match(email):
        raise HTTPException(
            422,
            error_payload(
                "validation_error", "邮箱格式不正确", {"email": "请输入有效的邮箱地址"}
            ),
        )
    return email


def _require_acknowledgement(payload: RecipientWrite, *, enabling: bool) -> None:
    if payload.acknowledge_full_calendar:
        return
    if enabling:
        raise HTTPException(
            422,
            error_payload(
                "confirmation_required",
                "该人员将收到完整日历(可能包含全部私人日程),请先确认。",
                {"acknowledge_full_calendar": "必须确认隐私提示"},
            ),
        )


@router.get("/sync-recipients", summary="查询同步人员")
def list_recipients(admin: AdminDep, session: Session = Depends(get_db)) -> dict[str, object]:
    recipients = list(
        session.execute(select(SyncRecipient).order_by(SyncRecipient.created_at.asc())).scalars()
    )
    return {
        "items": [serialize_recipient(recipient) for recipient in recipients],
        "total": len(recipients),
        "enabled": sum(1 for recipient in recipients if recipient.enabled),
    }


@router.post("/sync-recipients", status_code=201, summary="新增同步人员")
def create_recipient(
    payload: RecipientWrite,
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    email = _validate_email(payload.email)
    enabled = True if payload.enabled is None else bool(payload.enabled)
    _require_acknowledgement(payload, enabling=enabled)

    existing = session.execute(
        select(SyncRecipient).where(SyncRecipient.email == email)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409,
            error_payload("conflict", "该邮箱已在同步人员列表中", {"email": "邮箱已存在"}),
        )

    recipient = SyncRecipient(
        email=email,
        display_name=(payload.display_name or "").strip() or None,
        enabled=enabled,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(recipient)
    session.commit()
    logger.info("已新增同步人员 %s", mask_email(email))
    return serialize_recipient(recipient)


@router.patch("/sync-recipients/{recipient_id}", summary="修改名称或启用状态")
def update_recipient(
    recipient_id: str,
    payload: RecipientWrite,
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    recipient = session.get(SyncRecipient, recipient_id)
    if recipient is None:
        raise HTTPException(404, error_payload("not_found", "同步人员不存在"))

    if payload.email is not None:
        email = _validate_email(payload.email)
        if email != recipient.email:
            clash = session.execute(
                select(SyncRecipient).where(
                    SyncRecipient.email == email, SyncRecipient.id != recipient.id
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(
                    409,
                    error_payload(
                        "conflict", "该邮箱已存在", {"email": "邮箱已存在"}
                    ),
                )
            recipient.email = email

    if payload.display_name is not None:
        recipient.display_name = payload.display_name.strip() or None

    if payload.enabled is not None:
        enabling = bool(payload.enabled) and not recipient.enabled
        if enabling:
            _require_acknowledgement(payload, enabling=True)
        recipient.enabled = bool(payload.enabled)

    recipient.updated_at = utcnow()
    session.commit()
    return serialize_recipient(recipient)


@router.delete("/sync-recipients/{recipient_id}", summary="移除同步人员")
def delete_recipient(
    recipient_id: str,
    admin: CsrfHeaderDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    recipient = session.get(SyncRecipient, recipient_id)
    if recipient is None:
        raise HTTPException(404, error_payload("not_found", "同步人员不存在"))

    masked = mask_email(recipient.email)
    session.delete(recipient)
    session.commit()
    logger.info("已移除同步人员 %s", masked)
    return {"ok": True, "id": recipient_id, "masked_email": masked}
