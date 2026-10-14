"""邮件收件人配置 API(仅家庭管理员)。

家庭版里收件人分两类,收信范围完全不同:

- **账号收件人**(`user_id` 非空):账号邮箱,在「我的设置」里维护,只会收到
  **该账号可见日历**里尚未结束的日程;
- **外部收件人**(`user_id` 为空):只收到**家庭共享日历**范围,不含任何人的私人日程。

账号收件人是**账号资料的派生结果**(启用 + 邮箱已验证,见
`app/accounts.py::sync_account_recipients`),所以本接口只负责**外部邮箱**:
新增与启用都需要显式 `acknowledge_full_calendar=true`(表示管理员已确认对方的收信范围),
账号收件人不能在这里改名、启停或删除,否则会和账号邮箱脱节;那类改动要走账号资料
(要停收信就停用账号)。账号收件人仍然**只读地**出现在列表里(`scope == "account"`),
方便管理员看清谁在收信。
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import get_user, recipient_scope_user_id
from app.db import get_db
from app.deps import AdminDep, CsrfHeaderAdminDep
from app.errors import error_payload
from app.models import SyncRecipient, normalize_email
from app.schemas import RecipientWrite
from app.timeutil import iso_utc, utcnow
from app.util import mask_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["recipients"])

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL_LENGTH = 320


def serialize_recipient(recipient: SyncRecipient, *, session: Session | None = None) -> dict:
    """序列化收件人:带上收信范围,前端才能区分「账号邮箱」与「外部邮箱」。"""
    username = None
    if session is not None and recipient.user_id:
        user = get_user(session, recipient.user_id)
        username = user.username if user is not None else None
    scope_user_id = recipient_scope_user_id(recipient)
    scope_user = get_user(session, scope_user_id) if session is not None and scope_user_id else None
    if recipient.user_id:
        scope, scope_label = "account", "本人可见日历(个人 + 家庭共享)"
    elif scope_user_id:
        scope = "member"
        scope_label = f"{scope_user.label if scope_user else '某成员'}可见日历(历史绑定)"
    else:
        scope, scope_label = "family", "家庭共享日历"
    return {
        "id": recipient.id,
        "email": recipient.email,
        "display_name": recipient.display_name,
        "enabled": bool(recipient.enabled),
        "created_at": iso_utc(recipient.created_at),
        "updated_at": iso_utc(recipient.updated_at),
        "last_sent_at": iso_utc(recipient.last_sent_at),
        "masked_email": mask_email(recipient.email),
        "user_id": recipient.user_id,
        "scope_user_id": scope_user_id,
        "username": username,
        "scope_user_name": scope_user.username if scope_user is not None else None,
        "scope": scope,
        "scope_label": scope_label,
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
                "该邮箱只会收到家庭共享日历(不含任何人的私人日程),请先确认。",
                {"acknowledge_full_calendar": "必须确认收信范围"},
            ),
        )


def _reject_account_recipient(recipient: SyncRecipient, *, action: str) -> None:
    """账号收件人由账号邮箱派生,不通过本接口维护(改名、启停、删除都不行)。"""
    if recipient.user_id:
        raise HTTPException(
            409,
            error_payload(
                "conflict",
                f"「{recipient.email}」是账号邮箱,{action}请在成员页或该成员的「我的设置」里操作;"
                "要让该账号停止收信,请停用这个账号。",
                {"user_id": "账号邮箱不通过本接口维护"},
            ),
        )


@router.get("/sync-recipients", summary="查询邮件收件人")
def list_recipients(admin: AdminDep, session: Session = Depends(get_db)) -> dict[str, object]:
    recipients = list(
        session.execute(select(SyncRecipient).order_by(SyncRecipient.created_at.asc())).scalars()
    )
    return {
        "items": [serialize_recipient(recipient, session=session) for recipient in recipients],
        "total": len(recipients),
        "enabled": sum(1 for recipient in recipients if recipient.enabled),
    }


@router.post("/sync-recipients", status_code=201, summary="新增外部收件人")
def create_recipient(
    payload: RecipientWrite,
    admin: CsrfHeaderAdminDep,
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
            error_payload("conflict", "该邮箱已在收件人列表中", {"email": "邮箱已存在"}),
        )

    recipient = SyncRecipient(
        email=email,
        display_name=(payload.display_name or "").strip() or None,
        enabled=enabled,
        user_id=None,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(recipient)
    session.commit()
    logger.info("已新增外部收件人 %s(家庭共享范围)", mask_email(email))
    return serialize_recipient(recipient, session=session)


@router.patch("/sync-recipients/{recipient_id}", summary="修改外部收件人")
def update_recipient(
    recipient_id: str,
    payload: RecipientWrite,
    admin: CsrfHeaderAdminDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    recipient = session.get(SyncRecipient, recipient_id)
    if recipient is None:
        raise HTTPException(404, error_payload("not_found", "收件人不存在"))

    if payload.email is not None:
        _reject_account_recipient(recipient, action="修改邮箱")
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
                    error_payload("conflict", "该邮箱已存在", {"email": "邮箱已存在"}),
                )
            recipient.email = email

    if payload.display_name is not None:
        recipient.display_name = payload.display_name.strip() or None

    if payload.enabled is not None:
        # 账号邮箱只在账号资料里维护:启用/停用同样不通过本接口(停用账号 = 停止收信)
        _reject_account_recipient(recipient, action="停用或启用")
        enabling = bool(payload.enabled) and not recipient.enabled
        if enabling:
            _require_acknowledgement(payload, enabling=True)
        recipient.enabled = bool(payload.enabled)

    recipient.updated_at = utcnow()
    session.commit()
    return serialize_recipient(recipient, session=session)


@router.delete("/sync-recipients/{recipient_id}", summary="移除外部收件人")
def delete_recipient(
    recipient_id: str,
    admin: CsrfHeaderAdminDep,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    recipient = session.get(SyncRecipient, recipient_id)
    if recipient is None:
        raise HTTPException(404, error_payload("not_found", "收件人不存在"))
    _reject_account_recipient(recipient, action="删除收件人")

    masked = mask_email(recipient.email)
    session.delete(recipient)
    session.commit()
    logger.info("已移除外部收件人 %s", masked)
    return {"ok": True, "id": recipient_id, "masked_email": masked}