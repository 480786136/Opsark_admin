import base64
import json
import math
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import select
from test_accounts import configure, signup
from test_gateway import mock
from test_gateway import setup as source_setup
from test_platform import auth
from test_platform import env as source_env

from app.credits import reserve, settle
from app.db import get_db
from app.main import app
from app.models import ModelCall
from app.security import ApiError
from app.token_estimation import estimate_input_structure, estimate_reservation, estimate_text_tokens
from app.user_models import CreditAccount, CreditLedger, ModelReservation

setup = source_setup
env = source_env


@pytest.mark.parametrize("text,expected", [
    ("hello", 2), ("12345", 3), ("中文", 2), ("，。", 2), ("😀", 4), ("𠮷", 4),
    ("abcdefghijklmnopqrstuvwxy", 25), (" \n\t ", 3), ("\\\"", 2), ("", 0),
])
def test_estimate_character_classes_are_explicitly_heuristic(text, expected):
    assert estimate_text_tokens(text) == expected


def test_long_opaque_base64_does_not_receive_a_prose_discount():
    content = base64.b64encode(bytes(range(256)) * 20).decode()
    assert estimate_text_tokens(content) == len(content)


def test_uncommon_ascii_whitespace_is_not_ignored():
    assert estimate_text_tokens("\v\f" * 5000) == 10000
    assert estimate_text_tokens(" \t\v\f \n") == 5


def test_request_estimate_counts_structure_framing_and_tool_schema_not_http_escaping():
    body = {"model": "not-a-tokenizer-name", "messages": [{"role": "user", "content": "a\n\"中\""}]}
    inputs = {"messages": body["messages"]}
    expected = math.ceil((estimate_input_structure(inputs) + 16 + 8) * 1.30) + 1024
    result = estimate_reservation(body, 300)
    assert result.estimated_input_tokens == expected
    assert result.required_tokens == expected + 300
    assert result.exact is False
    assert result.estimator.startswith("unicode_heuristic_")
    assert estimate_input_structure("\"") == 3  # one real quote, plus bounded structure, not a JSON escape
    richer = {**body, "tools": [{"type": "function", "function": {
        "name": "inspect", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": {"type": "object"}}}}
    assert estimate_reservation(richer, 300).estimated_input_tokens > expected
    assert estimate_reservation({**body, "temperature": 0.1}, 300) == result


def test_unicode_context_can_fit_real_credit_budget_without_reserving_utf8_bytes(setup):
    client, staff, _, _, _ = setup
    configure(client, staff, tokens=12000, models=["public-model"])
    user, headers = signup(client)
    payload = {"model": "public-model", "messages": [{"role": "user", "content": "查询当前运行状态，" * 700}],
               "max_tokens": 128}
    old_requirement = len(json.dumps(payload, ensure_ascii=False).encode()) + 64 + 1024 + 128
    assert old_requirement > 12000
    estimate = estimate_reservation(payload, 128)
    assert estimate.required_tokens < 12000
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                        "usage": {"prompt_tokens": 5000, "completion_tokens": 10}})

    mock(upstream)
    result = client.post("/v1/chat/completions", headers={**headers, "Idempotency-Key": "unicode-context-request-01"},
                         json=payload)
    assert result.status_code == 200, result.text
    assert len(requests) == 1
    current = client.get("/api/core/v1/me", headers=headers).json()["balance"]
    assert (current["available"], current["reserved"]) == (6990, 0)
    for db in app.dependency_overrides[get_db]():
        reservation = db.scalar(select(ModelReservation).where(ModelReservation.user_id == user["user"]["id"]))
        assert (reservation.amount, reservation.actual) == (0, 5010)


def test_zero_balance_has_explanation_and_zero_billing(setup):
    client, staff, _, _, _ = setup
    configure(client, staff, tokens=0, models=["public-model"])
    user, headers = signup(client)
    before = client.get("/api/core/v1/me", headers=headers).json()["balance"]
    payload = {"model": "public-model", "messages": [{"role": "user", "content": "状态检查" * 300}], "max_tokens": 1000}
    mock(lambda _: pytest.fail("insufficient reservation must never dispatch"))
    response = client.post("/v1/chat/completions", headers={**headers, "Idempotency-Key": "positive-balance-reject-01"},
                           json=payload)
    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "INSUFFICIENT_CREDITS" and error["retryable"] is False
    assert error["details"] == {"billing_mode": "direct", "retryable": False, "available_tokens": 0, "reserved_tokens": 0,
                                "request_id": error["details"]["request_id"]}
    assert client.get("/api/core/v1/me", headers=headers).json()["balance"] == before
    for db in app.dependency_overrides[get_db]():
        assert list(db.scalars(select(ModelReservation))) == []
        assert len(list(db.scalars(select(CreditLedger)))) == 1  # initial grant only
        rejected = db.scalar(select(ModelCall))
        assert rejected.user_id == user["user"]["id"]
        assert (rejected.status, rejected.http_status, rejected.error_code) == ("failed", 402, "INSUFFICIENT_CREDITS")
        assert rejected.input_tokens is None and rejected.output_tokens is None


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": -1, "completion_tokens": 2},
                                  {"prompt_tokens": 150000, "completion_tokens": 2}])
def test_unknown_or_unpaid_usage_is_recorded_and_blocks_further_official_calls(setup, usage):
    client, staff, _, _, _ = setup
    configure(client, staff, tokens=100000, models=["public-model"])
    _, headers = signup(client)
    dispatched = []

    def upstream(request):
        dispatched.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": usage})

    mock(upstream)
    payload = {"model": "public-model", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}
    first = client.post("/v1/chat/completions", headers={**headers, "Idempotency-Key": "usage-reconciliation-first"},
                        json=payload)
    assert first.status_code == 200
    balance = client.get("/api/core/v1/me", headers=headers).json()["balance"]
    assert balance["available"] + balance["reserved"] == 100000
    assert balance["reserved"] == 0
    second = client.post("/v1/chat/completions", headers={**headers, "Idempotency-Key": "usage-reconciliation-next"},
                         json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CREDITS_RECONCILIATION_REQUIRED"
    assert second.json()["error"]["details"]["retryable"] is False
    assert len(dispatched) == 1
    assert client.get("/api/core/v1/me", headers=headers).json()["balance"] == balance
    for db in app.dependency_overrides[get_db]():
        pending = list(db.scalars(select(ModelReservation)))
        assert len(pending) == 1 and pending[0].state == "needs_reconciliation"


def pending_overage(env, tokens=100):
    staff = auth(env)
    configure(env, staff, tokens=tokens)
    user, _ = signup(env)
    uid = user["user"]["id"]
    for db in app.dependency_overrides[get_db]():
        reserve(db, uid, "overage-call", "overage-request-0001", "h" * 64, 50)
        settle(db, "overage-call", 80)
        db.commit()
    return staff, uid


def test_admin_can_reconcile_real_overage_atomically_and_idempotently(env):
    staff, uid = pending_overage(env)
    url = "/api/admin/v1/credit-reservations/overage-call/reconcile"
    data = {"actual": 80, "reason": "供应商后台确认真实用量"}
    response = env.post(url, headers=staff, json=data)
    assert response.status_code == 200
    assert (response.json()["available"], response.json()["reserved"]) == (20, 0)
    assert env.post(url, headers=staff, json=data).json() == response.json()
    assert env.post(url, headers=staff, json={**data, "actual": 81}).status_code == 409
    for db in app.dependency_overrides[get_db]():
        row = db.get(ModelReservation, "overage-call")
        assert (row.amount, row.actual, row.state) == (80, 80, "settled")
        ledger = list(db.scalars(select(CreditLedger).where(CreditLedger.user_id == uid)))
        assert sum(entry.delta for entry in ledger) == 20
        assert sum(entry.reserved_delta for entry in ledger) == 0
        assert [entry.kind for entry in ledger].count("reserve_adjustment") == 1
        assert [entry.kind for entry in ledger].count("settle") == 1


def test_overage_without_funds_keeps_original_reservation_and_ledger(env):
    staff, uid = pending_overage(env, tokens=60)
    for db in app.dependency_overrides[get_db]():
        account = db.get(CreditAccount, uid)
        before = (account.available, account.reserved, account.revision)
        ledger_count = len(list(db.scalars(select(CreditLedger))))
    response = env.post("/api/admin/v1/credit-reservations/overage-call/reconcile", headers=staff,
                        json={"actual": 80, "reason": "供应商后台确认真实用量"})
    assert response.status_code == 402
    assert response.json()["error"]["details"]["required_tokens"] == 30
    for db in app.dependency_overrides[get_db]():
        account = db.get(CreditAccount, uid)
        assert (account.available, account.reserved, account.revision) == before
        assert len(list(db.scalars(select(CreditLedger)))) == ledger_count
        row = db.get(ModelReservation, "overage-call")
        assert (row.amount, row.actual, row.state) == (50, None, "needs_reconciliation")


def test_concurrent_overage_reconciliation_charges_exactly_once(env):
    _, uid = pending_overage(env)

    def reconcile(_):
        for db in app.dependency_overrides[get_db]():
            settle(db, "overage-call", 80, allow_overage=True, actor_id="test-admin", reason="确认真实用量")
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(reconcile, [0, 1]))
    for db in app.dependency_overrides[get_db]():
        account = db.get(CreditAccount, uid)
        assert (account.available, account.reserved) == (20, 0)
        assert len(list(db.scalars(select(CreditLedger).where(CreditLedger.kind == "reserve_adjustment")))) == 1
        assert len(list(db.scalars(select(CreditLedger).where(CreditLedger.kind == "settle")))) == 1


def test_admissions_cannot_skip_an_existing_reconciliation_under_concurrency(env):
    _, uid = pending_overage(env)

    def take(index):
        for db in app.dependency_overrides[get_db]():
            try:
                reserve(db, uid, f"blocked-{index}", f"blocked-request-{index}", "h" * 64, 1)
                db.commit()
                return "accepted"
            except ApiError as error:
                db.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(take, [0, 1])) == ["CREDITS_RECONCILIATION_REQUIRED"] * 2
    for db in app.dependency_overrides[get_db]():
        assert len(list(db.scalars(select(ModelReservation)))) == 1
