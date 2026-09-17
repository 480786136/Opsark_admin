import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import select
from test_accounts import configure, signup
from test_gateway import mock
from test_gateway import setup as source_setup
from test_platform import auth
from test_platform import env as source_env

from app.credits import begin_direct_call, change, settle
from app.db import get_db
from app.main import app
from app.security import ApiError
from app.user_models import CreditAccount, CreditLedger, ModelReservation

setup = source_setup
env = source_env


def test_positive_balance_dispatches_without_reserving_or_estimating(setup):
    client, staff, _, _, _ = setup
    configure(client, staff, tokens=1, models=["public-model"])
    user, headers = signup(client)
    uid = user["user"]["id"]

    def upstream(request):
        for db in app.dependency_overrides[get_db]():
            account = db.get(CreditAccount, uid)
            assert (account.available, account.reserved, account.revision) == (1, 0, 1)
            entry = db.scalar(select(ModelReservation))
            assert (entry.amount, entry.state) == (0, "pending_usage")
            assert len(list(db.scalars(select(CreditLedger)))) == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                        "usage": {"prompt_tokens": 1, "completion_tokens": 0}})

    mock(upstream)
    payload = {"model": "public-model", "messages": [{"role": "user", "content": "检查服务器状态" * 1000}]}
    response = client.post("/v1/chat/completions", headers={**headers, "Idempotency-Key": "direct-positive-balance-01"},
                           json=payload)
    assert response.status_code == 200
    me = client.get("/api/core/v1/me", headers=headers).json()
    assert me["billing_mode"] == "direct"
    assert (me["balance"]["available"], me["balance"]["reserved"]) == (0, 0)
    for db in app.dependency_overrides[get_db]():
        assert [(r.kind, r.delta, r.reserved_delta) for r in db.scalars(
            select(CreditLedger).order_by(CreditLedger.created_at))] == [("initial_grant", 1, 0), ("usage_debit", -1, 0)]


def direct_call(env, tokens=100):
    staff = auth(env)
    configure(env, staff, tokens=tokens)
    user, _ = signup(env)
    uid = user["user"]["id"]
    for db in app.dependency_overrides[get_db]():
        begin_direct_call(db, uid, "direct-call", "direct-request-00001", "h" * 64)
        db.commit()
    return staff, uid


def test_direct_usage_is_debited_once_under_concurrency(env):
    _, uid = direct_call(env)

    def finish(_):
        for db in app.dependency_overrides[get_db]():
            settle(db, "direct-call", 80)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, [0, 1]))
    for db in app.dependency_overrides[get_db]():
        row = db.get(CreditAccount, uid)
        assert (row.available, row.reserved) == (20, 0)
        assert len(list(db.scalars(select(CreditLedger).where(CreditLedger.kind == "usage_debit")))) == 1


def test_unpaid_real_usage_survives_and_is_settled_after_funding(env):
    staff, uid = direct_call(env, tokens=50)
    url = "/api/admin/v1/credit-reservations/direct-call/reconcile"
    data = {"actual": 80, "reason": "核对供应商返回的真实用量"}
    assert env.post(url, headers=staff, json=data).status_code == 409  # still running
    for db in app.dependency_overrides[get_db]():
        settle(db, "direct-call", 80)
        db.commit()
        row = db.get(ModelReservation, "direct-call")
        assert (row.state, row.actual, row.amount) == ("needs_reconciliation", 80, 0)
        account = db.get(CreditAccount, uid)
        assert (account.available, account.reserved) == (50, 0)
    assert env.post(url, headers=staff, json=data).status_code == 402
    assert env.post(url, headers=staff, json={**data, "actual": 0}).status_code == 409
    for db in app.dependency_overrides[get_db]():
        change(db, uid, 50, 0, "admin_adjustment", "test-funding", "admin", "补充额度")
        db.commit()
    response = env.post(url, headers=staff, json=data)
    assert response.status_code == 200
    assert (response.json()["available"], response.json()["reserved"]) == (20, 0)
    assert env.post(url, headers=staff, json=data).json() == response.json()
    for db in app.dependency_overrides[get_db]():
        assert db.get(ModelReservation, "direct-call").amount == 0


def test_concurrent_distinct_calls_do_not_overdraw_balance(env):
    _, uid = direct_call(env)
    for db in app.dependency_overrides[get_db]():
        begin_direct_call(db, uid, "second-call", "direct-request-00002", "j" * 64)
        db.commit()

    def finish(call_id):
        for db in app.dependency_overrides[get_db]():
            settle(db, call_id, 80)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, ["direct-call", "second-call"]))
    for db in app.dependency_overrides[get_db]():
        account = db.get(CreditAccount, uid)
        assert (account.available, account.reserved) == (20, 0)
        rows = list(db.scalars(select(ModelReservation)))
        assert sorted(r.state for r in rows) == ["needs_reconciliation", "settled"]
        assert [r.actual for r in rows] == [80, 80]


def test_orphaned_direct_call_requires_review_instead_of_disappearing(env):
    _, uid = direct_call(env)
    for db in app.dependency_overrides[get_db]():
        db.get(ModelReservation, "direct-call").created_at = time.time() - 1300
        db.commit()
        with pytest.raises(ApiError) as error:
            begin_direct_call(db, uid, "next-call", "next-call-request-01", "x" * 64)
        assert error.value.code == "CREDITS_RECONCILIATION_REQUIRED"
        db.rollback()
        account = db.get(CreditAccount, uid)
        assert (account.available, account.reserved) == (100, 0)
