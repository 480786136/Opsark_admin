"""Explicit, owner-scoped synchronization. A user document never becomes a system Skill."""

# ruff: noqa: B008
import json
import re
import time
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .accounts import Strict, require_user
from .cloud_common import check_cloud_version, reject_credentials, seal, unseal
from .cloud_models import CloudMutation, UserSkill
from .db import get_db
from .security import ApiError, digest, limiter

router = APIRouter(prefix="/api/core/v1/skills")


class UserSkillContent(Strict):
    name: str = Field(min_length=1, max_length=80)
    category: Literal[
        "connectivity", "source-control", "environment", "build", "data", "deployment", "transfer", "other"
    ]
    description: str = Field(min_length=1, max_length=1000)
    instructions: str = Field(min_length=1, max_length=8000)
    matchRules: list[str] = Field(default_factory=list, max_length=50)
    allowedToolIds: list[str] = Field(default_factory=list, max_length=100)
    allowShell: bool = True
    enabled: bool = True
    version: int = Field(default=1, strict=True, ge=1, le=10**6)

    @field_validator("name", "description", "instructions")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("empty content")
        return value.strip()

    @field_validator("matchRules", "allowedToolIds")
    @classmethod
    def bounded_strings(cls, values):
        if any(len(value) > 256 for value in values):
            raise ValueError("item too long")
        return list(dict.fromkeys(values))


class SkillMutation(Strict):
    base_revision: int = Field(strict=True, ge=0)
    mutation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    content: UserSkillContent | None = None


def representation(row):
    return {
        "id": row.id,
        "revision": row.revision,
        "deleted": row.deleted,
        "updated_at": row.updated_at,
        "content": unseal(row.encrypted_payload),
    }


@router.get("")
def list_skills(request: Request, after: str = "", session=Depends(require_user), db=Depends(get_db)):
    check_cloud_version(request, db)
    if after and not re.fullmatch(r"skill-[a-zA-Z0-9-]{1,110}", after):
        raise ApiError(422, "INVALID_CURSOR")
    rows = list(
        db.scalars(
            select(UserSkill)
            .where(UserSkill.user_id == session.user_id, UserSkill.id > after)
            .order_by(UserSkill.id)
            .limit(21)
        )
    )
    return {"items": [representation(r) for r in rows[:20]], "next_cursor": rows[19].id if len(rows) > 20 else None}


@router.put("/{skill_id}")
def mutate(skill_id: str, body: SkillMutation, request: Request, session=Depends(require_user), db=Depends(get_db)):
    check_cloud_version(request, db)
    limiter.check("skill-write:" + session.user_id, 60)
    if not re.fullmatch(r"skill-[a-zA-Z0-9-]{1,110}", skill_id):
        raise ApiError(422, "INVALID_USER_SKILL_ID", "系统 Skill 不能通过用户同步接口写入")
    payload = body.content.model_dump() if body.content else None
    reject_credentials(payload)
    request_hash = digest(
        json.dumps({"skill": skill_id, "revision": body.base_revision, "content": payload}, sort_keys=True)
    )
    previous = db.get(CloudMutation, (session.user_id, body.mutation_id))
    if previous:
        if previous.request_hash != request_hash:
            raise ApiError(409, "MUTATION_CONFLICT")
        return previous.response
    row = db.get(UserSkill, (session.user_id, skill_id))
    if (row.revision if row else 0) != body.base_revision:
        raise ApiError(409, "SKILL_REVISION_CONFLICT", "云版本已改变，请刷新并下载冲突副本，不要覆盖")
    if row is None and payload is None:
        raise ApiError(404, "SKILL_NOT_FOUND")
    if (
        row is None
        and db.scalar(select(func.count()).select_from(UserSkill).where(UserSkill.user_id == session.user_id)) >= 200
    ):
        raise ApiError(409, "SKILL_LIMIT", "当前账号最多保留 200 个云 Skill 标识（含删除记录）")
    revision, timestamp = body.base_revision + 1, time.time()
    values = {
        "revision": revision,
        "updated_at": timestamp,
        "deleted": payload is None,
        "encrypted_payload": seal(payload) if payload is not None else None,
    }
    result = {"id": skill_id, "revision": revision, "deleted": payload is None, "updated_at": timestamp}
    try:
        if row is None:
            db.add(UserSkill(id=skill_id, user_id=session.user_id, **values))
        elif (
            db.execute(
                update(UserSkill)
                .where(
                    UserSkill.user_id == session.user_id,
                    UserSkill.id == skill_id,
                    UserSkill.revision == body.base_revision,
                )
                .values(**values)
            ).rowcount
            != 1
        ):
            raise ApiError(409, "SKILL_REVISION_CONFLICT", "云版本已改变，请刷新")
        db.add(
            CloudMutation(
                user_id=session.user_id,
                id=body.mutation_id,
                request_hash=request_hash,
                response=result,
                created_at=timestamp,
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "SKILL_REVISION_CONFLICT", "并发同步冲突，请刷新；未覆盖云内容")
    return result
