import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select
from test_accounts import configure, signup
from test_gateway import mock
from test_gateway import setup as source_setup
from test_platform import auth
from test_platform import env as source_env

from app.credits import begin_direct_call, change, refresh_reconciliations, reserve, settle
from app.db import get_db
from app.main import app
from app.models import ModelCall
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


def add_call(db, call_id, uid, *, status="running", started_at=None, input_tokens=None, output_tokens=None):
    db.add(ModelCall(
        id=call_id,
        key_id="synthetic-session",
        owner=uid,
        user_id=uid,
        provider_id="synthetic-provider",
        model="public-model",
        started_at=time.time() if started_at is None else started_at,
        status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    ))


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


def test_orphaned_direct_call_is_logged_without_blocking_next_call(env):
    staff, uid = direct_call(env)
    for db in app.dependency_overrides[get_db]():
        db.get(ModelReservation, "direct-call").created_at = time.time() - 1300
        db.commit()
        begin_direct_call(db, uid, "next-call", "next-call-request-01", "x" * 64)
        db.commit()
        account = db.get(CreditAccount, uid)
        assert (account.available, account.reserved) == (100, 0)
        assert sorted(r.id for r in db.scalars(select(ModelReservation))) == ["direct-call", "next-call"]

    items = env.get("/api/admin/v1/credit-reservations", headers=staff).json()
    assert [item["id"] for item in items] == ["direct-call"]
    assert items[0]["anomaly_code"] == "CALL_TIMEOUT"


def test_known_unpaid_usage_is_audit_only_for_later_admission(env):
    staff, uid = direct_call(env, tokens=50)
    for db in app.dependency_overrides[get_db]():
        settle(db, "direct-call", 80)
        db.commit()
        assert db.get(ModelReservation, "direct-call").state == "needs_reconciliation"
        begin_direct_call(db, uid, "next-call", "next-call-request-01", "x" * 64)
        db.commit()
        assert db.get(ModelReservation, "next-call").state == "pending_usage"
        assert db.get(CreditAccount, uid).available == 50

    items = env.get("/api/admin/v1/credit-reservations", headers=staff).json()
    assert [item["id"] for item in items] == ["direct-call"]
    assert items[0]["anomaly_code"] == "USAGE_EXCEEDS_BALANCE"
    assert items[0]["actual"] == 80


def test_fresh_in_flight_call_is_not_an_anomaly(env):
    staff, _ = direct_call(env)
    assert env.get("/api/admin/v1/credit-reservations", headers=staff).json() == []


def test_refresh_promotes_timed_out_call_and_closes_running_monitor_state(env):
    staff, uid = direct_call(env)
    old = time.time() - 1300
    for db in app.dependency_overrides[get_db]():
        db.get(ModelReservation, "direct-call").created_at = old
        add_call(db, "direct-call", uid, started_at=old)
        db.commit()

    before = env.get("/api/admin/v1/credit-reservations").json()[0]
    assert before["state"] == "pending_usage"
    assert before["effective_state"] == "needs_reconciliation"
    assert before["reconcile_allowed"] is True
    assert before["reconcile_after"] is None
    assert before["call_status"] == "running"

    refreshed = env.post("/api/admin/v1/credit-reservations/refresh", headers=staff)
    assert refreshed.status_code == 200
    assert {key: refreshed.json()[key] for key in ("promoted", "settled", "calls_closed")} == {
        "promoted": 1, "settled": 0, "calls_closed": 1,
    }
    assert refreshed.json()["pending"][0]["state"] == "needs_reconciliation"
    assert refreshed.json()["pending"][0]["call_status"] == "failed"
    again = env.post("/api/admin/v1/credit-reservations/refresh", headers=staff).json()
    assert (again["promoted"], again["settled"], again["calls_closed"]) == (0, 0, 0)
    for db in app.dependency_overrides[get_db]():
        assert db.get(ModelReservation, "direct-call").state == "needs_reconciliation"
        call = db.get(ModelCall, "direct-call")
        assert (call.status, call.error_code) == ("failed", "CALL_OUTCOME_UNKNOWN")


def test_concurrent_refresh_promotes_and_closes_each_call_once(env):
    _, uid = direct_call(env)
    old = time.time() - 1300
    for db in app.dependency_overrides[get_db]():
        db.get(ModelReservation, "direct-call").created_at = old
        add_call(db, "direct-call", uid, started_at=old)
        db.commit()

    def refresh(index):
        for db in app.dependency_overrides[get_db]():
            result = refresh_reconciliations(db, actor_id=f"admin-{index}")
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(refresh, [1, 2]))
    assert sum(result["promoted"] for result in results) == 1
    assert sum(result["calls_closed"] for result in results) == 1


def test_refresh_repairs_terminal_call_with_trusted_usage_exactly_once(env):
    staff, uid = direct_call(env)
    for db in app.dependency_overrides[get_db]():
        add_call(db, "direct-call", uid, status="succeeded", input_tokens=30, output_tokens=20)
        db.commit()

    item = env.get("/api/admin/v1/credit-reservations").json()[0]
    assert (item["call_status"], item["reconcile_allowed"]) == ("succeeded", True)
    assert item["anomaly_code"] == "SETTLEMENT_INCOMPLETE"
    assert (item["input_tokens"], item["output_tokens"]) == (30, 20)
    result = env.post("/api/admin/v1/credit-reservations/refresh", headers=staff).json()
    assert (result["promoted"], result["settled"], result["pending"]) == (0, 1, [])
    assert env.post("/api/admin/v1/credit-reservations/refresh", headers=staff).json()["settled"] == 0
    for db in app.dependency_overrides[get_db]():
        assert (db.get(CreditAccount, uid).available, db.get(ModelReservation, "direct-call").state) == (50, "settled")
        assert len(list(db.scalars(select(CreditLedger).where(CreditLedger.kind == "usage_debit")))) == 1


def test_terminal_call_allows_immediate_manual_reconciliation(env):
    staff, uid = direct_call(env)
    for db in app.dependency_overrides[get_db]():
        add_call(db, "direct-call", uid, status="failed")
        db.commit()
    response = env.post(
        "/api/admin/v1/credit-reservations/direct-call/reconcile",
        headers=staff,
        json={"actual": 20, "reason": "供应商后台已核对用量"},
    )
    assert response.status_code == 200
    assert response.json()["available"] == 80


def test_timed_out_call_manual_reconciliation_closes_running_monitor_state(env):
    staff, uid = direct_call(env)
    old = time.time() - 1300
    for db in app.dependency_overrides[get_db]():
        db.get(ModelReservation, "direct-call").created_at = old
        add_call(db, "direct-call", uid, started_at=old)
        db.commit()

    response = env.post(
        "/api/admin/v1/credit-reservations/direct-call/reconcile",
        headers=staff,
        json={"actual": 20, "reason": "供应商后台已核对用量"},
    )
    assert response.status_code == 200
    for db in app.dependency_overrides[get_db]():
        call = db.get(ModelCall, "direct-call")
        assert (call.status, call.error_code) == ("failed", "CALL_OUTCOME_UNKNOWN")


def test_insufficient_manual_settlement_keeps_confirmed_direct_usage(env):
    staff, uid = direct_call(env, tokens=50)
    for db in app.dependency_overrides[get_db]():
        db.get(ModelReservation, "direct-call").created_at = time.time() - 1300
        db.commit()
    url = "/api/admin/v1/credit-reservations/direct-call/reconcile"
    body = {"actual": 80, "reason": "供应商后台已核对用量"}
    response = env.post(url, headers=staff, json=body)
    assert response.status_code == 402
    for db in app.dependency_overrides[get_db]():
        row = db.get(ModelReservation, "direct-call")
        assert (row.state, row.actual) == ("needs_reconciliation", 80)
        change(db, uid, 50, 0, "admin_adjustment", "fund-after-review", "admin", "补充额度")
        db.commit()
    # Refresh is status maintenance, not implicit consent to debit after funding.
    refreshed = env.post("/api/admin/v1/credit-reservations/refresh", headers=staff).json()
    assert refreshed["settled"] == 0
    assert refreshed["pending"][0]["actual"] == 80
    assert env.post(url, headers=staff, json=body).json()["available"] == 20


def test_refresh_promotes_legacy_reservation_without_releasing_its_hold(env):
    staff = auth(env)
    configure(env, staff, tokens=100)
    user, _ = signup(env)
    uid = user["user"]["id"]
    for db in app.dependency_overrides[get_db]():
        reserve(db, uid, "legacy-call", "legacy-request-0001", "h" * 64, 50)
        add_call(db, "legacy-call", uid, status="failed")
        db.commit()

    result = env.post("/api/admin/v1/credit-reservations/refresh", headers=staff).json()
    assert (result["promoted"], result["settled"]) == (1, 0)
    for db in app.dependency_overrides[get_db]():
        account = db.get(CreditAccount, uid)
        row = db.get(ModelReservation, "legacy-call")
        assert (account.available, account.reserved) == (50, 50)
        assert (row.amount, row.actual, row.state) == (50, None, "needs_reconciliation")

    response = env.post(
        "/api/admin/v1/credit-reservations/legacy-call/reconcile",
        headers=staff,
        json={"actual": 20, "reason": "供应商后台已核对用量"},
    )
    assert (response.json()["available"], response.json()["reserved"]) == (80, 0)
