"""Core user API. Staff cookies never authenticate an end user."""
# ruff: noqa: B008 -- FastAPI resolves dependency defaults, not Python callers.

import re
import time
from types import SimpleNamespace
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .credits import balance, change, settle
from .db import get_db
from .models import ModelCall, ModelRoute
from .security import ApiError, digest, limiter, require_admin, token
from .user_models import (
    AccountAudit,
    CreditAccount,
    CreditLedger,
    ModelReservation,
    RegistrationPolicy,
    UserAccount,
    UserSession,
)

public = APIRouter(prefix="/api/core/v1")
admin = APIRouter(prefix="/api/admin/v1", dependencies=[Depends(require_admin)])
hasher = PasswordHasher()
DUMMY_HASH = hasher.hash("synthetic-unused-account-password")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Strict):
    email: str = Field(max_length=254)
    password: str = Field(min_length=10, max_length=256)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("invalid email")
        return value


def policy(db):
    row = db.get(RegistrationPolicy, 1)
    return row or SimpleNamespace(enabled=False, initial_tokens=0, allowed_models=[], revision=0)


def lock_user(db, user_id, *, active_only=False):
    """Serialize session issuance/rotation and staff revocation on the same user row.

    A no-op UPDATE also obtains a write lock on SQLite (SELECT FOR UPDATE doesn't).
    Keep the lock until the caller commits the session/security mutation.
    """
    stmt = update(UserAccount).where(UserAccount.id == user_id)
    if active_only:
        stmt = stmt.where(UserAccount.disabled.is_(False))
    row = db.execute(stmt.values(disabled=UserAccount.disabled).returning(UserAccount.disabled)).first()
    if row is None:
        if active_only:
            raise ApiError(401, "USER_SESSION_EXPIRED", "账号不可用，请重新登录")
        raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
    return row[0]


def issue_session(db, user):
    lock_user(db, user.id, active_only=True)
    access, refresh = "ouc_" + token(), "our_" + token()
    now = time.time()
    db.add(
        UserSession(
            user_id=user.id,
            access_hash=digest(access),
            refresh_hash=digest(refresh),
            access_expires=now + 900,
            refresh_expires=now + 30 * 86400,
        )
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 900,
        "user": {"id": user.id, "email": user.email},
    }


def auth_limit(request):
    limiter.check("core-auth:" + (request.client.host if request.client else "unknown"), 10, 300)


@public.get("/registration-policy")
def public_policy(db=Depends(get_db)):
    p = policy(db)
    return {"enabled": p.enabled, "initial_tokens": p.initial_tokens, "unit": "tokens"}


@public.post("/register", status_code=201)
def register(body: Credentials, request: Request, db=Depends(get_db)):
    auth_limit(request)
    p = policy(db)
    if not p.enabled:
        raise ApiError(403, "REGISTRATION_DISABLED", "暂未开放注册，请联系 OpsArk")
    user = UserAccount(email=body.email, password_hash=hasher.hash(body.password), created_at=time.time())
    try:
        db.add(user)
        db.flush()
        db.add(CreditAccount(user_id=user.id, available=0, reserved=0))
        db.flush()
        change(
            db,
            user.id,
            p.initial_tokens,
            0,
            "initial_grant",
            "registration",
            "registration",
            f"注册赠额，策略版本 {p.revision}",
        )
        response = issue_session(db, user)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "REGISTRATION_CONFLICT", "无法注册，请尝试登录或联系支持")
    return {**response, "balance": balance(db, user.id)}


@public.post("/login")
def login(body: Credentials, request: Request, db=Depends(get_db)):
    auth_limit(request)
    user = db.scalar(select(UserAccount).where(UserAccount.email == body.email))
    try:
        valid = hasher.verify(user.password_hash if user else DUMMY_HASH, body.password)
    except VerificationError:
        valid = False
    if not user or not valid or user.disabled:
        raise ApiError(401, "INVALID_CREDENTIALS", "邮箱或密码不正确，或账号不可用")
    response = issue_session(db, user)
    db.commit()
    return {**response, "balance": balance(db, user.id)}


def require_user(request: Request, db=Depends(get_db)):
    header = request.headers.get("authorization", "")
    session = (
        db.scalar(select(UserSession).where(UserSession.access_hash == digest(header[7:])))
        if header.startswith("Bearer ouc_")
        else None
    )
    if not session or session.revoked or session.access_expires <= time.time():
        raise ApiError(401, "USER_SESSION_EXPIRED", "请登录 OpsArk 账号")
    user = db.get(UserAccount, session.user_id)
    if not user or user.disabled:
        raise ApiError(401, "USER_SESSION_EXPIRED", "账号不可用")
    return session


class Refresh(Strict):
    refresh_token: str = Field(min_length=20, max_length=256)


@public.post("/session/refresh")
def refresh_session(body: Refresh, request: Request, db=Depends(get_db)):
    auth_limit(request)
    owner = db.scalar(select(UserSession.user_id).where(UserSession.refresh_hash == digest(body.refresh_token)))
    if owner is None:
        raise ApiError(401, "USER_SESSION_EXPIRED", "请重新登录")
    lock_user(db, owner, active_only=True)
    access, refresh = "ouc_" + token(), "our_" + token()
    now = time.time()
    session = db.execute(
        update(UserSession)
        .where(
            UserSession.refresh_hash == digest(body.refresh_token),
            UserSession.revoked.is_(False),
            UserSession.refresh_expires > now,
            UserSession.user_id.in_(select(UserAccount.id).where(UserAccount.disabled.is_(False))),
        )
        .values(access_hash=digest(access), refresh_hash=digest(refresh), access_expires=now + 900)
        .returning(UserSession.user_id)
    ).first()
    if not session:
        raise ApiError(401, "USER_SESSION_EXPIRED", "请重新登录")
    user = db.get(UserAccount, session[0])
    db.commit()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 900,
        "user": {"id": user.id, "email": user.email},
        "balance": balance(db, user.id),
    }


@public.delete("/session")
def logout(session=Depends(require_user), db=Depends(get_db)):
    session.revoked = True
    db.commit()
    return {"ok": True}


@public.get("/me")
def me(session=Depends(require_user), db=Depends(get_db)):
    user = db.get(UserAccount, session.user_id)
    return {
        "user": {"id": user.id, "email": user.email},
        "balance": balance(db, user.id),
        "allowed_models": policy(db).allowed_models,
        "billing_mode": "direct",
    }


@public.get("/usage")
def user_usage(session=Depends(require_user), db=Depends(get_db)):
    rows = db.scalars(
        select(CreditLedger)
        .where(CreditLedger.user_id == session.user_id)
        .order_by(CreditLedger.created_at.desc())
        .limit(100)
    )
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "delta": r.delta,
            "reserved_delta": r.reserved_delta,
            "reason": r.reason,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@public.get("/model-requests/{request_key}")
def model_request_status(request_key: str, session=Depends(require_user), db=Depends(get_db)):
    row = db.execute(
        select(ModelReservation, ModelCall)
        .join(ModelCall, ModelCall.id == ModelReservation.id)
        .where(ModelReservation.user_id == session.user_id, ModelReservation.idempotency_key == request_key)
    ).first()
    if row is None:
        raise ApiError(404, "REQUEST_NOT_FOUND")
    reservation, call = row
    return {
        "call_id": call.id,
        "status": call.status,
        "error_code": call.error_code,
        "credit_state": reservation.state,
        "reserved": reservation.amount,
        "actual": reservation.actual,
    }


def model_identity(request, db):
    from .cloud_common import check_cloud_version

    check_cloud_version(request, db)
    session = require_user(request, db)
    limiter.check("user-model:" + session.user_id, 30)
    return SimpleNamespace(
        id=session.id, owner=session.user_id, user_id=session.user_id, allowed_models=policy(db).allowed_models
    )


class PolicyInput(Strict):
    enabled: bool
    initial_tokens: int = Field(strict=True, ge=0, le=10**9)
    allowed_models: list[str] = Field(max_length=100)
    revision: int = Field(strict=True, ge=0)


@admin.get("/registration-policy")
def get_policy(db=Depends(get_db)):
    p = policy(db)
    return {
        "enabled": p.enabled,
        "initial_tokens": p.initial_tokens,
        "allowed_models": p.allowed_models,
        "revision": p.revision,
    }


@admin.put("/registration-policy")
def save_policy(body: PolicyInput, session=Depends(require_admin), db=Depends(get_db)):
    aliases = set(db.scalars(select(ModelRoute.alias)))
    if not set(body.allowed_models).issubset(aliases):
        raise ApiError(422, "UNKNOWN_MODEL")
    values = {**body.model_dump(exclude={"revision"}), "revision": body.revision + 1, "updated_by": session.admin_id}
    try:
        if db.get(RegistrationPolicy, 1) is None and body.revision == 0:
            db.add(RegistrationPolicy(id=1, **values))
        elif (
            db.execute(
                update(RegistrationPolicy)
                .where(RegistrationPolicy.id == 1, RegistrationPolicy.revision == body.revision)
                .values(**values)
            ).rowcount
            != 1
        ):
            raise ApiError(409, "POLICY_CONFLICT", "配置已更新，请刷新后重试")
        db.add(
            AccountAudit(actor_id=session.admin_id, action="registration_policy", detail=values, created_at=time.time())
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "POLICY_CONFLICT", "配置已更新，请刷新后重试")
    return get_policy(db)


def user_data(u, c):
    return {
        "id": u.id,
        "email": u.email,
        "disabled": u.disabled,
        "created_at": u.created_at,
        "available": c.available,
        "reserved": c.reserved,
        "revision": c.revision,
    }


@admin.get("/users")
def users(
    q: str = Query(default="", max_length=254),
    status: Literal["all", "active", "disabled"] = "all",
    page: int = Query(default=1, ge=1, le=1000000),
    page_size: int = Query(default=20, ge=1, le=100),
    db=Depends(get_db),
):
    filters = []
    if q.strip():
        term = q.strip().lower()
        filters.append(
            or_(
                func.lower(UserAccount.email).contains(term, autoescape=True),
                func.lower(UserAccount.id).contains(term, autoescape=True),
            )
        )
    if status != "all":
        filters.append(UserAccount.disabled.is_(status == "disabled"))
    base = select(UserAccount, CreditAccount).join(CreditAccount).where(*filters)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = db.execute(
        base.order_by(UserAccount.created_at.desc(), UserAccount.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [user_data(u, c) for u, c in rows], "total": total, "page": page, "page_size": page_size}


@admin.get("/users/{user_id}")
def user_detail(user_id: str, db=Depends(get_db)):
    row = db.execute(select(UserAccount, CreditAccount).join(CreditAccount).where(UserAccount.id == user_id)).first()
    if row is None:
        raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
    return user_data(*row)


class SecurityAction(Strict):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("reason required")
        return value.strip()


class UserStatusInput(SecurityAction):
    disabled: bool = Field(strict=True)
    expected_disabled: bool = Field(strict=True)


def audit_security(db, staff, action, user_id, reason, **details):
    db.add(
        AccountAudit(
            actor_id=staff.admin_id,
            action=action,
            detail={"user_id": user_id, "reason": reason, **details},
            created_at=time.time(),
        )
    )


@admin.put("/users/{user_id}/status")
def set_user_status(user_id: str, body: UserStatusInput, session=Depends(require_admin), db=Depends(get_db)):
    current = lock_user(db, user_id)
    if current != body.expected_disabled:
        raise ApiError(409, "USER_STATUS_CONFLICT", "用户状态已变化，请刷新用户详情后重试")
    count = 0
    if current != body.disabled:
        db.execute(update(UserAccount).where(UserAccount.id == user_id).values(disabled=body.disabled))
        if body.disabled:
            count = db.execute(
                update(UserSession)
                .where(UserSession.user_id == user_id, UserSession.revoked.is_(False))
                .values(revoked=True)
            ).rowcount
        audit_security(
            db,
            session,
            "user_disabled" if body.disabled else "user_enabled",
            user_id,
            body.reason,
            previous_disabled=current,
            disabled=body.disabled,
            revoked_count=count,
        )
    db.commit()
    return {"user": user_detail(user_id, db), "revoked_count": count}


@admin.get("/users/{user_id}/sessions")
def user_sessions(
    user_id: str,
    status: Literal["all", "active", "revoked", "expired"] = "all",
    page: int = Query(default=1, ge=1, le=1000000),
    page_size: int = Query(default=20, ge=1, le=100),
    db=Depends(get_db),
):
    user = db.get(UserAccount, user_id)
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
    now = time.time()
    filters = [UserSession.user_id == user_id]
    if status == "active":
        filters.extend([UserSession.revoked.is_(False), UserSession.refresh_expires > now, not user.disabled])
    elif status == "revoked":
        filters.append(UserSession.revoked.is_(True))
    elif status == "expired":
        filters.extend([UserSession.revoked.is_(False), UserSession.refresh_expires <= now])
    total = db.scalar(select(func.count()).select_from(UserSession).where(*filters))
    rows = db.scalars(
        select(UserSession)
        .where(*filters)
        .order_by(UserSession.refresh_expires.desc(), UserSession.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    for row in rows:
        if row.revoked:
            state = "revoked"
        elif row.refresh_expires <= now:
            state = "expired"
        elif user.disabled:
            state = "blocked"
        elif row.access_expires <= now:
            state = "refresh_required"
        else:
            state = "active"
        # Explicit allowlist: never serialize token hashes or access/refresh credentials.
        items.append(
            {
                "id": row.id,
                "status": state,
                "access_expires": row.access_expires,
                "refresh_expires": row.refresh_expires,
                "can_revoke": state in {"active", "refresh_required", "blocked"},
            }
        )
    revocable = db.scalar(
        select(func.count())
        .select_from(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked.is_(False), UserSession.refresh_expires > now)
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size, "revocable_count": revocable}


@admin.post("/users/{user_id}/sessions/revoke-all")
def revoke_all_sessions(user_id: str, body: SecurityAction, session=Depends(require_admin), db=Depends(get_db)):
    lock_user(db, user_id)
    count = db.execute(
        update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked.is_(False)).values(revoked=True)
    ).rowcount
    if count:
        audit_security(db, session, "user_sessions_revoked", user_id, body.reason, revoked_count=count)
    db.commit()
    return {"revoked_count": count}


@admin.post("/users/{user_id}/sessions/{session_id}/revoke")
def revoke_user_session(
    user_id: str, session_id: str, body: SecurityAction, session=Depends(require_admin), db=Depends(get_db)
):
    lock_user(db, user_id)
    row = db.scalar(select(UserSession).where(UserSession.id == session_id, UserSession.user_id == user_id))
    if row is None:
        raise ApiError(404, "USER_SESSION_NOT_FOUND", "该用户的会话不存在")
    count = db.execute(
        update(UserSession).where(UserSession.id == session_id, UserSession.revoked.is_(False)).values(revoked=True)
    ).rowcount
    if count:
        audit_security(
            db, session, "user_session_revoked", user_id, body.reason, session_id=session_id, revoked_count=count
        )
    db.commit()
    return {"revoked_count": count}


class Adjustment(Strict):
    delta: int = Field(strict=True, ge=-(10**9), le=10**9)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,100}$")

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("reason required")
        return value.strip()


@admin.post("/users/{user_id}/adjustments")
def adjust(user_id: str, body: Adjustment, session=Depends(require_admin), db=Depends(get_db)):
    balance(db, user_id)
    key = "adjust:" + body.idempotency_key
    previous = db.scalar(select(CreditLedger).where(CreditLedger.user_id == user_id, CreditLedger.operation_key == key))
    if previous:
        if (previous.delta, previous.reason, previous.actor_id) != (body.delta, body.reason, session.admin_id):
            raise ApiError(409, "IDEMPOTENCY_CONFLICT")
        return balance(db, user_id)
    try:
        change(db, user_id, body.delta, 0, "admin_adjustment", key, session.admin_id, body.reason)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "ADJUSTMENT_CONFLICT", "请求可能已生效，请刷新后核对")
    return balance(db, user_id)


@admin.get("/users/{user_id}/ledger")
def ledger(user_id: str, db=Depends(get_db)):
    return [
        {c.name: getattr(r, c.name) for c in CreditLedger.__table__.columns}
        for r in db.scalars(
            select(CreditLedger)
            .where(CreditLedger.user_id == user_id)
            .order_by(CreditLedger.created_at.desc())
            .limit(200)
        )
    ]


@admin.get("/credit-reservations")
def reservations(db=Depends(get_db)):
    return [
        {c.name: getattr(r, c.name) for c in ModelReservation.__table__.columns}
        for r in db.scalars(
            select(ModelReservation)
            .where(ModelReservation.state.in_(["reserved", "pending_usage", "needs_reconciliation"]))
            .order_by(ModelReservation.created_at)
            .limit(200)
        )
    ]


class Reconciliation(Strict):
    actual: int = Field(strict=True, ge=0, le=10**9)
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if len(value.strip()) < 5:
            raise ValueError("reason must explain the reconciliation")
        return value.strip()


@admin.post("/credit-reservations/{call_id}/reconcile")
def reconcile(call_id: str, body: Reconciliation, session=Depends(require_admin), db=Depends(get_db)):
    row = db.get(ModelReservation, call_id)
    if not row:
        raise ApiError(404, "NOT_FOUND")
    if row.state in {"settled", "released"}:
        if row.actual != body.actual:
            raise ApiError(409, "ALREADY_RECONCILED", "已结算且数额不一致，请核对流水")
        return balance(db, row.user_id)
    if row.state in {"reserved", "pending_usage"} and time.time() - row.created_at < 1200:
        raise ApiError(409, "REQUEST_STILL_RUNNING")
    settle(db, call_id, body.actual, actor_id=session.admin_id, reason=body.reason, allow_overage=True)
    db.commit()
    return balance(db, row.user_id)
