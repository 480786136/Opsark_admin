"""Opt-in support diagnostics, encrypted at rest and never fed into Knowledge."""

# ruff: noqa: B008
import base64
import binascii
import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from .accounts import Strict, require_user
from .cloud_common import reject_credentials, seal, unseal
from .cloud_models import CloudMutation, SupportTicket
from .db import get_db
from .security import ApiError, digest, limiter, require_admin
from .user_models import AccountAudit

user = APIRouter(prefix="/api/core/v1/feedback")
admin = APIRouter(prefix="/api/admin/v1/feedback", dependencies=[Depends(require_admin)])


class TaskDiagnostic(Strict):
    taskId: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    status: str = Field(pattern=r"^[a-z_]{1,40}$")
    stepCount: int = Field(strict=True, ge=0, le=100000)
    completedSteps: int = Field(strict=True, ge=0, le=100000)
    failedSteps: int = Field(strict=True, ge=0, le=100000)


class FeedbackImage(Strict):
    name: str = Field(min_length=1, max_length=255)
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    data: str = Field(min_length=1, max_length=4 * ((5 * 1024 * 1024 + 2) // 3))

    @field_validator("data")
    @classmethod
    def validate_image(cls, value, info):
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("图片编码无效")
        if not raw or len(raw) > 5 * 1024 * 1024:
            raise ValueError("每张图片不能超过 5 MB")
        signatures = {
            "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
            "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
        }
        if not signatures.get(info.data.get("mime_type")):
            raise ValueError("图片内容与格式不符，仅支持 PNG、JPG、WebP")
        return value


class FeedbackInput(Strict):
    mutation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    title: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=4000)
    category: Literal["general", "task"]
    core_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    diagnostic: TaskDiagnostic | None = None
    log_excerpt: str = Field(default="", max_length=16000)
    task_logs: str = Field(default="", max_length=64 * 1024 * 1024)
    contact: str = Field(default="", max_length=254)
    images: list[FeedbackImage] = Field(default_factory=list, max_length=3)
    consent: Literal[True]

    @field_validator("title", "message")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("请填写标题和问题描述")
        return value.strip()

    @field_validator("task_logs")
    @classmethod
    def log_size(cls, value):
        if len(value.encode("utf-8")) > 64 * 1024 * 1024:
            raise ValueError("任务日志超过 64 MB，请通过联系方式提交；日志不会被截断")
        return value


def optional_user(request: Request, db=Depends(get_db)):
    # Invalid credentials are never silently converted into an anonymous submission.
    return require_user(request, db) if request.headers.get("authorization") else None


def purge_expired(db):
    db.execute(
        update(SupportTicket)
        .where(SupportTicket.expires_at <= time.time(), SupportTicket.encrypted_payload.is_not(None))
        .values(encrypted_payload=None, status="expired", revision=SupportTicket.revision + 1)
    )


def summary(row):
    return {
        "id": row.id,
        "category": row.category,
        "status": row.status,
        "revision": row.revision,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
    }


def detail(row):
    return {**summary(row), "content": unseal(row.encrypted_payload) if row.expires_at > time.time() else None}


@user.post("", status_code=201)
def submit(body: FeedbackInput, request: Request, session=Depends(optional_user), db=Depends(get_db)):
    user_id = session.user_id if session else None
    source = user_id or "guest:" + digest(request.client.host if request.client else "unknown")
    limiter.check("feedback:" + source, 10, 3600)
    if (body.category == "task") != (body.diagnostic is not None):
        raise ApiError(422, "TASK_SELECTION_REQUIRED", "任务反馈必须选择任务；普通反馈不能附带任务日志")
    if body.category == "general" and (body.log_excerpt or body.task_logs):
        raise ApiError(422, "UNEXPECTED_LOGS")
    content = body.model_dump(exclude={"mutation_id", "consent"})
    reject_credentials({key: value for key, value in content.items() if key != "images"})
    fingerprint = digest("feedback:" + json.dumps(content, sort_keys=True))
    guest_key = digest("guest-feedback:" + body.mutation_id) if user_id is None else None
    old = (db.get(CloudMutation, (user_id, body.mutation_id)) if user_id else
           db.scalar(select(SupportTicket).where(SupportTicket.guest_submission_key == guest_key)))
    if old:
        if old.request_hash != fingerprint:
            raise ApiError(409, "MUTATION_CONFLICT")
        return old.response if user_id else {"id": old.id, "revision": 1}
    purge_expired(db)
    row = SupportTicket(
        user_id=user_id,
        guest_submission_key=guest_key,
        request_hash=fingerprint,
        category=body.category,
        status="open",
        revision=1,
        encrypted_payload=seal({**content, "replies": []}),
        created_at=time.time(),
        expires_at=time.time() + 30 * 86400,
    )
    try:
        db.add(row)
        db.flush()
        response = {"id": row.id, "revision": row.revision}
        if user_id:
            db.add(CloudMutation(
                user_id=user_id,
                id=body.mutation_id,
                request_hash=fingerprint,
                response=response,
                created_at=time.time(),
            ))
        db.commit()
    except IntegrityError:
        db.rollback()
        if guest_key:
            old = db.scalar(select(SupportTicket).where(SupportTicket.guest_submission_key == guest_key))
            if old and old.request_hash == fingerprint:
                return {"id": old.id, "revision": 1}
        raise ApiError(409, "MUTATION_CONFLICT", "提交可能已受理，请使用原内容重试")
    return response


@user.get("")
def mine(session=Depends(require_user), db=Depends(get_db)):
    purge_expired(db)
    db.commit()
    rows = db.scalars(
        select(SupportTicket)
        .options(defer(SupportTicket.encrypted_payload))
        .where(SupportTicket.user_id == session.user_id)
        .order_by(SupportTicket.created_at.desc())
        .limit(100)
    )
    return [summary(row) for row in rows]


def owned(db, ticket_id, user_id=None):
    row = db.get(SupportTicket, ticket_id)
    if not row or (user_id is not None and row.user_id != user_id):
        raise ApiError(404, "FEEDBACK_NOT_FOUND")
    return row


@user.get("/{ticket_id}")
def read_mine(ticket_id: str, session=Depends(require_user), db=Depends(get_db)):
    row = owned(db, ticket_id, session.user_id)
    purge_expired(db)
    db.commit()
    db.refresh(row)
    return detail(row)


@user.delete("/{ticket_id}")
def delete_mine(ticket_id: str, session=Depends(require_user), db=Depends(get_db)):
    row = owned(db, ticket_id, session.user_id)
    row.encrypted_payload = None
    row.status = "deleted"
    row.revision += 1
    db.commit()
    return {"ok": True}


@admin.get("")
def inbox(staff=Depends(require_admin), db=Depends(get_db)):
    purge_expired(db)
    rows = list(db.scalars(
        select(SupportTicket).options(defer(SupportTicket.encrypted_payload))
        .order_by(SupportTicket.created_at.desc()).limit(200)
    ))
    db.add(
        AccountAudit(
            actor_id=staff.admin_id, action="feedback_list", detail={"count": len(rows)}, created_at=time.time()
        )
    )
    db.commit()
    return [
        {
            "id": row.id,
            "user_id": row.user_id,
            "category": row.category,
            "status": row.status,
            "revision": row.revision,
            "created_at": row.created_at,
            "expires_at": row.expires_at,
        }
        for row in rows
    ]


@admin.get("/{ticket_id}")
def read_ticket(ticket_id: str, staff=Depends(require_admin), db=Depends(get_db)):
    row = owned(db, ticket_id)
    purge_expired(db)
    db.add(AccountAudit(actor_id=staff.admin_id, action="feedback_read", detail={"id": row.id}, created_at=time.time()))
    db.commit()
    db.refresh(row)
    return detail(row)


class ReplyInput(Strict):
    revision: int = Field(strict=True, ge=1)
    status: Literal["open", "in_progress", "resolved"]
    message: str = Field(default="", max_length=2000)


@admin.put("/{ticket_id}")
def reply(ticket_id: str, body: ReplyInput, staff=Depends(require_admin), db=Depends(get_db)):
    row = owned(db, ticket_id)
    if row.encrypted_payload is None or row.expires_at <= time.time():
        raise ApiError(410, "FEEDBACK_EXPIRED")
    payload = unseal(row.encrypted_payload)
    if len(payload["replies"]) >= 50:
        raise ApiError(409, "REPLY_LIMIT")
    reject_credentials(body.message)
    if body.message.strip():
        payload["replies"].append({"message": body.message.strip(), "created_at": time.time()})
    if (
        db.execute(
            update(SupportTicket)
            .where(SupportTicket.id == row.id, SupportTicket.revision == body.revision)
            .values(encrypted_payload=seal(payload), status=body.status, revision=body.revision + 1)
        ).rowcount
        != 1
    ):
        raise ApiError(409, "FEEDBACK_CONFLICT", "反馈已变化，请刷新后回复")
    db.add(
        AccountAudit(
            actor_id=staff.admin_id,
            action="feedback_reply",
            detail={"id": row.id, "status": body.status},
            created_at=time.time(),
        )
    )
    db.commit()
    db.refresh(row)
    return detail(row)
