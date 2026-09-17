"""Staff-only request monitoring, grouped by authenticated principal and client task ID."""

# ruff: noqa: B008
import re
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, or_, select

from .db import get_db
from .models import ModelCall, Provider
from .security import ApiError, require_admin
from .user_models import UserAccount

router = APIRouter(prefix="/api/admin/v1/call-monitor", dependencies=[Depends(require_admin)])
OPERATIONS = {"plan", "stage_decision", "requirement", "model_check", "summary", "review", "other"}


def trace_metadata(request):
    """Headers are diagnostic labels, never authorization or upstream request fields."""
    data = {}
    for field in ("task_id", "round_id", "step_id", "client_request_id"):
        value = request.headers.get("x-opsark-" + field.replace("_", "-"))
        if value is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
                raise ApiError(422, "INVALID_CALL_TRACE", "调用关联标识格式不正确")
            data[field] = value
    operation = request.headers.get("x-opsark-operation")
    if operation is not None:
        if operation not in OPERATIONS:
            raise ApiError(422, "INVALID_CALL_TRACE", "调用类型不正确")
        data["operation"] = operation
    phase = request.headers.get("x-opsark-phase-index")
    if phase is not None:
        if not re.fullmatch(r"[0-9]{1,6}", phase):
            raise ApiError(422, "INVALID_CALL_TRACE", "任务阶段格式不正确")
        data["phase_index"] = int(phase)
    if not data.get("task_id") and any(key in data for key in ("round_id", "step_id", "phase_index")):
        raise ApiError(422, "INVALID_CALL_TRACE", "步骤或阶段关联必须包含任务标识")
    return data


def principal(call):
    return f"user:{call.user_id}" if call.user_id else f"key:{call.key_id}"


def call_data(call, email=None, provider_name=None):
    return {
        **{column.name: getattr(call, column.name) for column in ModelCall.__table__.columns},
        "user_email": email,
        "provider_name": provider_name,
        "principal": principal(call),
        "identity_kind": "user" if call.user_id else "model_key",
    }


class CallFilters:
    def __init__(
        self,
        q: str = Query(default="", max_length=254),
        user_id: str = Query(default="", max_length=64),
        task_id: str = Query(default="", max_length=128),
        principal_id: str = Query(default="", max_length=70),
        model: str = Query(default="", max_length=128),
        provider_id: str = Query(default="", max_length=64),
        status: Literal["all", "running", "succeeded", "failed"] = "all",
        association: Literal["all", "linked", "unlinked"] = "all",
        started_after: float | None = Query(default=None, ge=0, le=32503680000),
        started_before: float | None = Query(default=None, ge=0, le=32503680000),
    ):
        if started_after is not None and started_before is not None and started_after > started_before:
            raise ApiError(422, "INVALID_TIME_RANGE", "开始时间不能晚于结束时间")
        self.conditions = []
        if q.strip():
            term = q.strip().lower()
            self.conditions.append(
                or_(
                    *[
                        func.lower(field).contains(term, autoescape=True)
                        for field in (
                            ModelCall.id,
                            ModelCall.client_request_id,
                            ModelCall.task_id,
                            UserAccount.email,
                            ModelCall.owner,
                        )
                    ]
                )
            )
        for field, value in [
            (ModelCall.user_id, user_id),
            (ModelCall.task_id, task_id),
            (ModelCall.model, model),
            (ModelCall.provider_id, provider_id),
        ]:
            if value:
                self.conditions.append(field == value)
        if principal_id:
            kind, _, value = principal_id.partition(":")
            if kind not in {"user", "key"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                raise ApiError(422, "INVALID_PRINCIPAL")
            self.conditions.append(
                ModelCall.user_id == value
                if kind == "user"
                else (ModelCall.user_id.is_(None) & (ModelCall.key_id == value))
            )
        if status != "all":
            self.conditions.append(ModelCall.status == status)
        if association != "all":
            self.conditions.append(
                ModelCall.task_id.is_not(None) if association == "linked" else ModelCall.task_id.is_(None)
            )
        if started_after is not None:
            self.conditions.append(ModelCall.started_at >= started_after)
        if started_before is not None:
            self.conditions.append(ModelCall.started_at <= started_before)

    def apply(self, stmt):
        return (
            stmt.select_from(ModelCall)
            .outerjoin(UserAccount, ModelCall.user_id == UserAccount.id)
            .where(*self.conditions)
        )


def aggregates():
    return [
        func.count().label("total"),
        func.sum(case((ModelCall.status == "succeeded", 1), else_=0)).label("succeeded"),
        func.sum(case((ModelCall.status == "failed", 1), else_=0)).label("failed"),
        func.sum(case((ModelCall.status == "running", 1), else_=0)).label("running"),
        func.sum(ModelCall.input_tokens).label("input_tokens"),
        func.sum(ModelCall.output_tokens).label("output_tokens"),
        func.sum(case(((ModelCall.input_tokens.is_(None) | ModelCall.output_tokens.is_(None)), 1), else_=0)).label(
            "unknown_usage"
        ),
        func.avg(ModelCall.duration_ms).label("average_duration_ms"),
    ]


def stats_data(row):
    return {
        key: (None if key == "average_duration_ms" else 0) if value is None else value for key, value in row.items()
    }


@router.get("/requests")
def requests(
    filters: CallFilters = Depends(),
    page: int = Query(default=1, ge=1, le=1000000),
    page_size: int = Query(default=20, ge=1, le=100),
    order: Literal["asc", "desc"] = "desc",
    db=Depends(get_db),
):
    summary = stats_data(db.execute(filters.apply(select(*aggregates()))).mappings().one())
    stmt = filters.apply(select(ModelCall, UserAccount.email, Provider.name)).outerjoin(
        Provider, Provider.id == ModelCall.provider_id
    )
    ordering = [getattr(field, order)() for field in (ModelCall.started_at, ModelCall.id)]
    rows = db.execute(stmt.order_by(*ordering).offset((page - 1) * page_size).limit(page_size))
    return {
        "items": [call_data(*row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": summary["total"],
        "summary": summary,
    }


@router.get("/tasks")
def tasks(
    filters: CallFilters = Depends(),
    page: int = Query(default=1, ge=1, le=1000000),
    page_size: int = Query(default=20, ge=1, le=100),
    db=Depends(get_db),
):
    # Sessions of one user share a task; two users reusing a task ID never share a group.
    owner = case((ModelCall.user_id.is_not(None), "user:" + ModelCall.user_id), else_="key:" + ModelCall.key_id)
    stmt = filters.apply(
        select(
            owner.label("principal"),
            ModelCall.task_id,
            ModelCall.user_id,
            func.max(UserAccount.email).label("user_email"),
            func.max(ModelCall.owner).label("owner"),
            func.min(ModelCall.started_at).label("first_call_at"),
            func.max(ModelCall.started_at).label("last_call_at"),
            *aggregates(),
        )
    )
    stmt = stmt.where(ModelCall.task_id.is_not(None)).group_by(owner, ModelCall.task_id, ModelCall.user_id)
    count = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(func.max(ModelCall.started_at).desc(), owner, ModelCall.task_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).mappings()
    return {"items": [dict(row) for row in rows], "total": count, "page": page, "page_size": page_size}
