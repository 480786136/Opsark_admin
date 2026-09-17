import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import select
from test_gateway import mock
from test_gateway import setup as source_setup
from test_platform import auth
from test_platform import env as source_env

from app.accounts import require_user
from app.credits import reserve
from app.db import get_db
from app.main import app
from app.security import ApiError
from app.user_models import CreditAccount, CreditLedger, ModelReservation, UserAccount, UserSession

env = source_env
setup = source_setup
CREDS = {"email": "person@example.test", "password": "synthetic-password-123"}


def configure(c, headers, *, tokens=100000, models=None):
    previous = c.get("/api/admin/v1/registration-policy").json()
    r = c.put(
        "/api/admin/v1/registration-policy",
        headers=headers,
        json={
            "enabled": True,
            "initial_tokens": tokens,
            "allowed_models": models or [],
            "revision": previous["revision"],
        },
    )
    assert r.status_code == 200, r.text


def signup(c):
    r = c.post("/api/core/v1/register", json=CREDS)
    assert r.status_code == 201, r.text
    return r.json(), {"Authorization": "Bearer " + r.json()["access_token"]}


def test_registration_policy_and_one_time_grant(env):
    assert env.post("/api/core/v1/register", json=CREDS).status_code == 403
    staff = auth(env)
    configure(env, staff, tokens=1200)
    u, headers = signup(env)
    assert u["balance"]["available"] == 1200
    assert env.post("/api/core/v1/register", json=CREDS).status_code == 409
    configure(env, staff, tokens=9000)
    assert env.post("/api/core/v1/login", json=CREDS).json()["balance"]["available"] == 1200
    assert len(env.get("/api/core/v1/usage", headers=headers).json()) == 1
    env.cookies.clear()
    assert env.get("/api/admin/v1/users", headers=headers).status_code == 401
    assert env.get("/api/core/v1/me").status_code == 401


def test_refresh_rotation_logout_and_no_token_storage(env):
    configure(env, auth(env))
    u, headers = signup(env)
    r = env.post("/api/core/v1/session/refresh", json={"refresh_token": u["refresh_token"]})
    assert r.status_code == 200
    assert env.get("/api/core/v1/me", headers=headers).status_code == 401
    assert env.post("/api/core/v1/session/refresh", json={"refresh_token": u["refresh_token"]}).status_code == 401
    new = r.json()
    new_headers = {"Authorization": "Bearer " + new["access_token"]}
    for db in app.dependency_overrides[get_db]():
        stored = db.scalar(select(UserSession))
        assert stored.access_hash != new["access_token"] and stored.refresh_hash != new["refresh_token"]
    assert env.delete("/api/core/v1/session", headers=new_headers).status_code == 200
    assert env.get("/api/core/v1/me", headers=new_headers).status_code == 401


def test_adjustment_idempotency_and_prevent_negative(env):
    staff = auth(env)
    configure(env, staff, tokens=100)
    u, headers = signup(env)
    url = "/api/admin/v1/users/" + u["user"]["id"] + "/adjustments"
    adjustment = {"delta": 40, "reason": "试点补偿", "idempotency_key": "adjustment-test-0001"}
    assert env.post(url, headers=staff, json=adjustment).json()["available"] == 140
    assert env.post(url, headers=staff, json=adjustment).json()["available"] == 140
    assert env.post(url, headers=staff, json={**adjustment, "delta": 41}).status_code == 409
    assert (
        env.post(
            url, headers=staff, json={**adjustment, "delta": -141, "idempotency_key": "adjustment-test-0002"}
        ).status_code
        == 402
    )
    assert env.get("/api/core/v1/me", headers=headers).json()["balance"]["available"] == 140
    assert len(env.get(url.replace("adjustments", "ledger"), headers=staff).json()) == 2


def test_concurrent_reservation_cannot_overspend(env):
    configure(env, auth(env), tokens=100)
    u, _ = signup(env)

    def take(i):
        for db in app.dependency_overrides[get_db]():
            try:
                reserve(db, u["user"]["id"], f"call-{i}", f"request-{i}", "h" * 64, 70)
                db.commit()
                return True
            except ApiError:
                db.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(take, [1, 2])) == [False, True]
    for db in app.dependency_overrides[get_db]():
        row = db.get(CreditAccount, u["user"]["id"])
        assert (row.available, row.reserved) == (30, 70)


@pytest.mark.parametrize("with_usage", [True, False])
def test_official_model_billing_with_capture_and_reconciliation(setup, with_usage):
    c, staff, _, _, _ = setup
    configure(c, staff, models=["public-model"])
    _, headers = signup(c)
    requests = []

    def handler(r):
        requests.append(r)
        body = {"choices": [{"message": {"content": "ok"}}]}
        if with_usage:
            body["usage"] = {"prompt_tokens": 40, "completion_tokens": 10}
        return httpx.Response(200, json=body)

    mock(handler)
    payload = {"model": "public-model", "messages": [{"role": "user", "content": "hello"}]}
    headers["Idempotency-Key"] = "test-official-request-0001"
    r = c.post("/v1/chat/completions", headers=headers, json=payload)
    assert r.status_code == 200, r.text
    assert requests[0].headers["authorization"] != headers["Authorization"]
    assert json.loads(requests[0].content)["max_tokens"] == 4096
    b = c.get("/api/core/v1/me", headers=headers).json()["balance"]
    if with_usage:
        assert (b["available"], b["reserved"]) == (99950, 0)
    else:
        assert (b["available"], b["reserved"]) == (100000, 0)
        pending = c.get("/api/admin/v1/credit-reservations").json()
        assert pending[0]["state"] == "needs_reconciliation"
        assert (
            c.post(
                f"/api/admin/v1/credit-reservations/{pending[0]['id']}/reconcile",
                headers=staff,
                json={"actual": 50, "reason": "供应商后台确认用量"},
            ).status_code
            == 200
        )
        assert c.get("/api/core/v1/me", headers=headers).json()["balance"]["available"] == 99950
    detail = c.get("/api/admin/v1/calls/" + r.headers["x-request-id"]).json()
    assert detail["detail_policy"] == "redacted_snapshot"
    assert json.loads(detail["detail"]["input"]["text"])["messages"][0]["content"] == "hello"
    assert json.loads(detail["detail"]["output"]["text"])["choices"][0]["message"]["content"] == "ok"
    assert c.post("/v1/chat/completions", headers=headers, json=payload).status_code == 409
    assert len(requests) == 1


def test_insufficient_credit_never_dispatches(setup):
    c, staff, _, _, _ = setup
    configure(c, staff, tokens=0, models=["public-model"])
    _, headers = signup(c)
    mock(lambda r: pytest.fail("must not dispatch"))
    r = c.post(
        "/v1/chat/completions",
        headers={**headers, "Idempotency-Key": "no-credit-request-0001"},
        json={"model": "public-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 402


def test_user_scope_is_enforced(env):
    staff = auth(env)
    configure(env, staff)
    u, headers = signup(env)
    assert env.get("/api/core/v1/me", headers=headers).json()["user"]["id"] == u["user"]["id"]
    assert env.post("/api/core/v1/register", json={**CREDS, "owner_id": "someone-else"}).status_code == 422
    assert require_user not in app.dependency_overrides
    for db in app.dependency_overrides[get_db]():
        assert len(list(db.scalars(select(CreditLedger)))) == 1


def test_policy_revision_and_user_api_never_accept_staff_cookies(env):
    staff = auth(env)
    configure(env, staff)
    assert env.get("/api/core/v1/me").status_code == 401
    old = env.get("/api/admin/v1/registration-policy").json()
    assert env.put("/api/admin/v1/registration-policy", headers=staff, json=old).status_code == 200
    assert env.put("/api/admin/v1/registration-policy", headers=staff, json=old).status_code == 409


def test_disabled_accounts_cannot_refresh_or_call(env):
    configure(env, auth(env))
    u, headers = signup(env)
    for db in app.dependency_overrides[get_db]():
        db.get(UserAccount, u["user"]["id"]).disabled = True
        db.commit()
    assert env.get("/api/core/v1/me", headers=headers).status_code == 401
    assert env.get("/v1/models", headers=headers).status_code == 401
    assert env.post("/api/core/v1/session/refresh", json={"refresh_token": u["refresh_token"]}).status_code == 401


@pytest.mark.parametrize("failure", ["transport", "status", "invalid_body", "negative_usage"])
def test_dispatched_failures_never_become_free_requests(setup, failure):
    c, staff, _, _, _ = setup
    configure(c, staff, models=["public-model"])
    _, headers = signup(c)
    headers["Idempotency-Key"] = "failure-request-000001"
    attempts = []

    def handler(r):
        attempts.append(r)
        if failure == "transport":
            raise httpx.ReadTimeout("synthetic timeout", request=r)
        if failure == "status":
            return httpx.Response(503, json={"error": "synthetic unavailable"})
        if failure == "invalid_body":
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": -1, "completion_tokens": 1}},
        )

    mock(handler)
    body = {"model": "public-model", "messages": [{"role": "user", "content": "hello"}]}
    c.post("/v1/chat/completions", headers=headers, json=body)
    balance = c.get("/api/core/v1/me", headers=headers).json()["balance"]
    assert balance["reserved"] == 0
    assert balance["available"] + balance["reserved"] == 100000
    status = c.get("/api/core/v1/model-requests/failure-request-000001", headers=headers)
    assert status.json()["credit_state"] == "needs_reconciliation"
    assert c.post("/v1/chat/completions", headers=headers, json=body).status_code == 409
    assert len(attempts) == 1
    second = c.post("/api/core/v1/register", json={**CREDS, "email": "second@example.test"}).json()
    assert (
        c.get(
            "/api/core/v1/model-requests/failure-request-000001",
            headers={"Authorization": "Bearer " + second["access_token"]},
        ).status_code
        == 404
    )


def test_no_dispatch_releases_reservation(setup, monkeypatch):
    c, staff, _, _, _ = setup
    configure(c, staff, models=["public-model"])
    _, headers = signup(c)

    class UnavailableSlot:
        async def acquire(self):
            raise TimeoutError("synthetic saturated capacity")

    monkeypatch.setattr(app.state, "model_slots", UnavailableSlot())
    mock(lambda r: pytest.fail("no upstream request allowed"))
    response = c.post(
        "/v1/chat/completions",
        headers={**headers, "Idempotency-Key": "not-dispatched-request-01"},
        json={"model": "public-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 502
    assert c.get("/api/core/v1/me", headers=headers).json()["balance"]["available"] == 100000
    status = c.get("/api/core/v1/model-requests/not-dispatched-request-01", headers=headers).json()
    assert (status["credit_state"], status["actual"]) == ("released", 0)


def test_reconciliation_is_idempotent_and_cannot_release_live_calls(env):
    staff = auth(env)
    configure(env, staff, tokens=100)
    u, _ = signup(env)
    for db in app.dependency_overrides[get_db]():
        reserve(db, u["user"]["id"], "pending-call", "reserved-request-0001", "x" * 64, 50)
        db.commit()
    url = "/api/admin/v1/credit-reservations/pending-call/reconcile"
    data = {"actual": 20, "reason": "供应商记录核对完成"}
    assert env.post(url, headers=staff, json=data).status_code == 409
    for db in app.dependency_overrides[get_db]():
        db.get(ModelReservation, "pending-call").created_at = time.time() - 1300
        db.commit()
    assert env.post(url, headers=staff, json=data).json()["available"] == 80
    assert env.post(url, headers=staff, json=data).json()["available"] == 80
    assert env.post(url, headers=staff, json={**data, "actual": 21}).status_code == 409


def test_completion_token_limit_is_preserved(setup):
    c, staff, _, _, _ = setup
    configure(c, staff, models=["public-model"])
    _, headers = signup(c)

    def handler(r):
        body = json.loads(r.content)
        assert body["max_completion_tokens"] == 300
        assert "max_tokens" not in body
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    mock(handler)
    response = c.post(
        "/v1/chat/completions",
        headers={**headers, "Idempotency-Key": "output-parameter-request-1"},
        json={
            "model": "public-model",
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 300,
        },
    )
    assert response.status_code == 200
