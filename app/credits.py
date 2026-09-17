"""Every balance change and its audit entry share the caller's DB transaction."""

import time

from sqlalchemy import select, update

from .security import ApiError
from .user_models import CreditAccount, CreditLedger, ModelReservation


def balance(db, user_id):
    row = db.get(CreditAccount, user_id, populate_existing=True)
    if row is None:
        raise ApiError(404, "USER_NOT_FOUND")
    return {"available": row.available, "reserved": row.reserved, "revision": row.revision, "unit": "tokens"}


def change(db, user_id, delta, reserved_delta, kind, operation_key, actor_id, reason):
    row = db.execute(
        update(CreditAccount)
        .where(
            CreditAccount.user_id == user_id,
            CreditAccount.available + delta >= 0,
            CreditAccount.reserved + reserved_delta >= 0,
        )
        .values(
            available=CreditAccount.available + delta,
            reserved=CreditAccount.reserved + reserved_delta,
            revision=CreditAccount.revision + 1,
        )
        .returning(CreditAccount.available, CreditAccount.reserved)
    ).first()
    if row is None:
        raise ApiError(402, "INSUFFICIENT_CREDITS", "可用额度不足，可补充额度或使用自己的模型")
    db.add(
        CreditLedger(
            user_id=user_id,
            operation_key=operation_key,
            kind=kind,
            delta=delta,
            reserved_delta=reserved_delta,
            available_after=row[0],
            reserved_after=row[1],
            actor_id=actor_id,
            reason=reason,
            created_at=time.time(),
        )
    )
    db.flush()


def lock_account(db, user_id):
    # A no-op UPDATE also locks on SQLite, where SELECT FOR UPDATE is ignored.
    # Reservation admission and transitions to needs_reconciliation use this
    # same lock, so a newly unresolved call cannot race another admission.
    row = db.execute(update(CreditAccount).where(CreditAccount.user_id == user_id)
                     .values(revision=CreditAccount.revision).returning(CreditAccount.user_id)).first()
    if row is None:
        raise ApiError(404, "USER_NOT_FOUND")


def reserve(db, user_id, call_id, key, request_hash, amount, *, estimate_details=None):
    if type(amount) is not int or amount <= 0:
        raise ApiError(422, "INVALID_RESERVATION_AMOUNT")
    lock_account(db, user_id)
    previous = db.scalar(
        select(ModelReservation).where(ModelReservation.user_id == user_id, ModelReservation.idempotency_key == key)
    )
    if previous:
        raise ApiError(409, "REQUEST_ALREADY_ACCEPTED", f"请求已受理，请核对调用记录：{previous.id}")
    current = balance(db, user_id)
    details = {**(estimate_details or {}), "available_tokens": current["available"],
               "reserved_tokens": current["reserved"], "required_tokens": amount, "retryable": False}
    unresolved = db.scalar(select(ModelReservation.id).where(
        ModelReservation.user_id == user_id, ModelReservation.state == "needs_reconciliation").limit(1))
    if unresolved:
        raise ApiError(409, "CREDITS_RECONCILIATION_REQUIRED",
                       "已有官方模型请求用量待核对，请先核对额度后继续；重复请求不会解除该阻断",
                       details=details, retryable=False)
    db.add(
        ModelReservation(
            id=call_id,
            user_id=user_id,
            idempotency_key=key,
            request_hash=request_hash,
            amount=amount,
            state="reserved",
            created_at=time.time(),
        )
    )
    db.flush()
    try:
        change(db, user_id, -amount, amount, "reserve", "reserve:" + call_id, user_id, "官方模型调用预留")
    except ApiError as error:
        if error.code != "INSUFFICIENT_CREDITS":
            raise
        raise ApiError(402, "INSUFFICIENT_CREDITS",
                       "单次请求的保守预留额度不足；可缩小上下文或输出上限，或补充额度后重试",
                       details=details, retryable=False) from error


def begin_direct_call(db, user_id, call_id, key, request_hash):
    """Register an idempotent call without freezing or estimating any credits.

    amount=0 distinguishes direct billing from historical positive reservations.
    The existing table is an accounting journal, not a balance hold in this mode.
    """
    lock_account(db, user_id)
    previous = db.scalar(select(ModelReservation).where(
        ModelReservation.user_id == user_id, ModelReservation.idempotency_key == key))
    if previous:
        raise ApiError(409, "REQUEST_ALREADY_ACCEPTED", f"请求已受理，请核对调用记录：{previous.id}")
    current = balance(db, user_id)
    details = {"billing_mode": "direct", "available_tokens": current["available"],
               "reserved_tokens": current["reserved"], "retryable": False}
    unresolved = db.scalar(select(ModelReservation.id).where(
        ModelReservation.user_id == user_id,
        (ModelReservation.state == "needs_reconciliation")
        | ((ModelReservation.state == "pending_usage") & (ModelReservation.created_at <= time.time() - 1200)),
    ).limit(1))
    if unresolved:
        raise ApiError(409, "CREDITS_RECONCILIATION_REQUIRED", "已有调用用量待核对或余额不足结算，请先处理待扣用量",
                       details=details, retryable=False)
    if current["available"] <= 0:
        raise ApiError(402, "INSUFFICIENT_CREDITS", "可用余额已用完，请补充额度或使用自己的模型",
                       details=details, retryable=False)
    db.add(ModelReservation(id=call_id, user_id=user_id, idempotency_key=key,
                            request_hash=request_hash, amount=0, state="pending_usage", created_at=time.time()))
    db.flush()


def settle_direct_call(db, row, actual, *, no_dispatch, actor_id, reason, manual):
    if no_dispatch:
        actual = 0
    if type(actual) is not int or actual < 0:
        row.state = "needs_reconciliation"
        return
    if row.actual is not None and row.actual != actual:
        raise ApiError(409, "ALREADY_RECONCILED", "已记录真实用量不一致，请核对调用记录")
    current = balance(db, row.user_id)
    if actual > current["available"]:
        if manual:
            raise ApiError(402, "INSUFFICIENT_CREDITS", "余额不足结算已发生的真实用量，请补充额度后核对",
                           details={"billing_mode": "direct", "available_tokens": current["available"],
                                    "required_tokens": actual, "reserved_tokens": current["reserved"],
                                    "retryable": False}, retryable=False)
        row.actual, row.state = actual, "needs_reconciliation"
        return
    if actual:
        change(db, row.user_id, -actual, 0, "usage_debit", "settle:" + row.id, actor_id, reason)
    row.actual, row.state = actual, "released" if no_dispatch else "settled"


def settle(db, call_id, actual=None, *, no_dispatch=False, actor_id="gateway", reason="模型用量结算",
           allow_overage=False):
    reservation = db.get(ModelReservation, call_id, populate_existing=True)
    if reservation is None:
        return
    lock_account(db, reservation.user_id)
    reservation = db.get(ModelReservation, call_id, populate_existing=True)
    if reservation.state in {"settled", "released"}:
        if allow_overage and reservation.actual != actual:
            raise ApiError(409, "ALREADY_RECONCILED", "已结算且数额不一致，请核对流水")
        return
    if reservation.amount == 0:
        settle_direct_call(db, reservation, actual, no_dispatch=no_dispatch, actor_id=actor_id,
                           reason=reason, manual=allow_overage)
        db.flush()
        return
    if no_dispatch:
        actual = 0
    if allow_overage and actual is not None and actual > reservation.amount:
        extra = actual - reservation.amount
        current = balance(db, reservation.user_id)
        try:
            change(db, reservation.user_id, -extra, extra, "reserve_adjustment", "reserve_adjustment:" + call_id,
                   actor_id, reason)
        except ApiError as error:
            if error.code != "INSUFFICIENT_CREDITS":
                raise
            raise ApiError(402, "INSUFFICIENT_CREDITS", "实际用量超过原预留且余额不足补足差额；请补充额度后核对",
                           details={"available_tokens": current["available"], "reserved_tokens": current["reserved"],
                                    "required_tokens": extra, "actual_tokens": actual,
                                    "original_reservation_tokens": reservation.amount, "retryable": False},
                           retryable=False) from error
        reservation.amount = actual
        db.flush()
    if actual is None or actual < 0 or actual > reservation.amount:
        db.execute(
            update(ModelReservation)
            .where(ModelReservation.id == call_id, ModelReservation.state == "reserved")
            .values(state="needs_reconciliation")
        )
        return
    won = db.execute(
        update(ModelReservation)
        .where(ModelReservation.id == call_id, ModelReservation.state.in_(["reserved", "needs_reconciliation"]))
        .values(state="released" if no_dispatch else "settled", actual=actual)
    )
    if won.rowcount != 1:
        return
    change(
        db,
        reservation.user_id,
        reservation.amount - actual,
        -reservation.amount,
        "release" if no_dispatch else "settle",
        "settle:" + call_id,
        actor_id,
        reason,
    )
