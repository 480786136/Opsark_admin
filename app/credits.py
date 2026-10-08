"""Every balance change and its audit entry share the caller's DB transaction."""

import time

from sqlalchemy import or_, select, update

from .model_delivery import request_status
from .models import ModelCall
from .security import ApiError
from .user_models import CreditAccount, CreditLedger, ModelReservation

RECONCILIATION_GRACE_SECONDS = 20 * 60
OPEN_RESERVATION_STATES = ("reserved", "pending_usage")
UNRESOLVED_RESERVATION_STATES = (*OPEN_RESERVATION_STATES, "needs_reconciliation")


def reconciliation_metadata(row, call_status, *, now=None):
    """Return the server-authoritative review state for an unsettled journal row."""
    now = time.time() if now is None else now
    deadline = row.created_at + RECONCILIATION_GRACE_SECONDS
    call_finished = call_status is not None and call_status != "running"
    timed_out = now >= deadline
    allowed = row.state == "needs_reconciliation" or (
        row.state in OPEN_RESERVATION_STATES and (call_finished or timed_out)
    )
    return {
        "effective_state": (
            "needs_reconciliation" if allowed and row.state in OPEN_RESERVATION_STATES else row.state
        ),
        "reconcile_allowed": allowed,
        "reconcile_after": deadline if row.state in OPEN_RESERVATION_STATES and not allowed else None,
        "call_status": call_status,
    }


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


def reject_duplicate_request(db, user_id, key, request_hash):
    """Shared by admission and the unique-constraint race recovery path."""
    previous = db.scalar(select(ModelReservation).where(
        ModelReservation.user_id == user_id, ModelReservation.idempotency_key == key))
    if previous is None:
        return
    call = db.get(ModelCall, previous.id)
    details = request_status(previous, call) if call else {
        "callId": previous.id, "requestKey": key, "dispatchCertainty": "may_have_dispatched",
        "responseAvailable": False,
    }
    details = {**details, "origin": "gateway", "stage": "request_deduplication", "gatewayHttpStatus": 409}
    if previous.request_hash != request_hash:
        raise ApiError(409, "IDEMPOTENCY_KEY_CONFLICT", "同一请求标识已绑定不同内容，不能重用",
                       details=details, retryable=False)
    raise ApiError(409, "REQUEST_ALREADY_ACCEPTED", "请求已受理，请查询原请求状态；不会重复发送或恢复响应正文",
                   details=details, retryable=False)


def reserve(db, user_id, call_id, key, request_hash, amount, *, estimate_details=None):
    if type(amount) is not int or amount <= 0:
        raise ApiError(422, "INVALID_RESERVATION_AMOUNT")
    lock_account(db, user_id)
    reject_duplicate_request(db, user_id, key, request_hash)
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
    reject_duplicate_request(db, user_id, key, request_hash)
    current = balance(db, user_id)
    details = {"billing_mode": "direct", "available_tokens": current["available"],
               "reserved_tokens": current["reserved"], "retryable": False}
    # Usage anomalies are audit-only in direct billing mode. They must not
    # block later calls; staff can inspect them by call ID in Admin.
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
        # Keep the provider-confirmed amount even when an administrator must fund
        # the account before retrying.  The API commits this review state before
        # returning the manual 402 response.
        row.actual, row.state = actual, "needs_reconciliation"
        if manual:
            raise ApiError(402, "INSUFFICIENT_CREDITS", "余额不足结算已发生的真实用量，请补充额度后核对",
                           details={"billing_mode": "direct", "available_tokens": current["available"],
                                    "required_tokens": actual, "reserved_tokens": current["reserved"],
                                    "retryable": False}, retryable=False)
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


def _trusted_terminal_usage(call):
    if call is None or call.status != "succeeded":
        return None
    if type(call.input_tokens) is not int or type(call.output_tokens) is not int:
        return None
    if call.input_tokens < 0 or call.output_tokens < 0:
        return None
    return call.input_tokens + call.output_tokens


def close_stale_call(db, call, *, now=None):
    """Close a monitor row whose maximum gateway lifetime has elapsed."""
    from .models import ModelCall

    if call is None or call.status != "running":
        return 0
    now = time.time() if now is None else now
    cutoff = now - RECONCILIATION_GRACE_SECONDS
    if call.started_at > cutoff:
        return 0
    won = db.execute(
        update(ModelCall)
        .where(ModelCall.id == call.id, ModelCall.status == "running", ModelCall.started_at <= cutoff)
        .values(
            status="failed",
            error_code="CALL_OUTCOME_UNKNOWN",
            # ModelCall.duration_ms is a 32-bit SQL Integer on PostgreSQL.
            duration_ms=min(2_147_483_647, max(0, int((now - call.started_at) * 1000))),
        )
    )
    return won.rowcount


def refresh_reconciliations(db, *, actor_id, now=None):
    """Repair unfinished reservation/call pairs without guessing provider usage.

    A terminal successful call with complete, validated usage can finish the
    accounting transition that should have committed with it.  Every other
    terminal or timed-out open row is promoted for explicit reconciliation.
    Existing ``needs_reconciliation`` rows are never silently charged merely
    because an account was funded later.
    """
    from .models import ModelCall

    now = time.time() if now is None else now
    cutoff = now - RECONCILIATION_GRACE_SECONDS
    candidates = db.execute(
        select(ModelReservation.id, ModelReservation.user_id)
        .outerjoin(ModelCall, ModelCall.id == ModelReservation.id)
        .where(
            ModelReservation.state.in_(UNRESOLVED_RESERVATION_STATES),
            or_(ModelCall.status != "running", ModelReservation.created_at <= cutoff),
        )
        .order_by(ModelReservation.user_id, ModelReservation.created_at, ModelReservation.id)
    ).all()
    promoted = settled = calls_closed = 0
    for call_id, user_id in candidates:
        # All settlement/admission paths use this same per-account lock.  The
        # reload plus conditional updates make concurrent refreshes idempotent.
        lock_account(db, user_id)
        row = db.get(ModelReservation, call_id, populate_existing=True)
        call = db.get(ModelCall, call_id, populate_existing=True)
        if row is None or row.state not in UNRESOLVED_RESERVATION_STATES:
            continue

        call_finished = call is not None and call.status != "running"
        timed_out = row.created_at <= cutoff
        if row.state in OPEN_RESERVATION_STATES and (call_finished or timed_out):
            actual = _trusted_terminal_usage(call)
            if actual is not None:
                settle(
                    db,
                    row.id,
                    actual,
                    actor_id=actor_id,
                    reason="刷新修复终态调用结算",
                )
                repaired = db.get(ModelReservation, row.id, populate_existing=True)
                if repaired.state in {"settled", "released"}:
                    settled += 1
                elif repaired.state == "needs_reconciliation":
                    promoted += 1
            else:
                won = db.execute(
                    update(ModelReservation)
                    .where(
                        ModelReservation.id == row.id,
                        ModelReservation.state.in_(OPEN_RESERVATION_STATES),
                    )
                    .values(state="needs_reconciliation")
                )
                promoted += won.rowcount

        if timed_out:
            calls_closed += close_stale_call(db, call, now=now)

    db.flush()
    return {"promoted": promoted, "settled": settled, "calls_closed": calls_closed}
