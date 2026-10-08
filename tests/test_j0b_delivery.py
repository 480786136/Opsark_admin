"""Offline gateway/account regression cases for J0-B delivery boundaries."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_accounts import configure, signup
from test_gateway import mock, request
from test_gateway import setup as source_setup
from test_platform import env as source_env

from app.credits import begin_direct_call
from app.db import get_db
from app.main import app
from app.model_delivery import request_status
from app.model_presets import resolve_parameters
from app.security import ApiError
from app.user_models import ModelReservation

env = source_env
setup = source_setup
BODY = {"model": "public-model", "messages": [{"role": "user", "content": "synthetic-content"}]}
KEY = "j0b-official-request-00001"
SCHEMA = {"type": "json_schema", "json_schema": {
    "name": "test", "strict": True,
    "schema": {"type": "object", "properties": {}, "additionalProperties": False},
}}


def official(setup):
    client, staff, _, _, _ = setup
    configure(client, staff, tokens=100, models=["public-model"])
    user, headers = signup(client)
    return client, user["user"]["id"], {**headers, "Idempotency-Key": KEY}


def success(content="{}", status=200):
    return httpx.Response(status, json={
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    })


@pytest.mark.parametrize("message,code,expected", [
    ("response_format json_schema unsupported keyword allOf", None, "UPSTREAM_SCHEMA_INVALID"),
    ("Invalid schema for response_format: additionalProperties must be false", None, "UPSTREAM_SCHEMA_INVALID"),
    ("json_schema is unsupported", "invalid_json_schema", "UPSTREAM_SCHEMA_INVALID"),
    ("json_schema unsupported", None, "UPSTREAM_SCHEMA_UNSUPPORTED"),
    ("response_format service unavailable", None, "UPSTREAM_HTTP_ERROR"),
])
def test_schema_error_keeps_two_hop_status_and_never_exposes_provider_text(setup, message, code, expected):
    client, _, _, _, issued = setup
    mock(lambda _: httpx.Response(400, json={"error": {
        "code": code, "message": message + " https://private.example/internal synthetic-secret",
    }}))
    response = request(client, issued, response_format=SCHEMA)
    error = response.json()["error"]
    assert error["code"] == expected
    assert error["providerHttpStatus"] == error["gatewayHttpStatus"] == 400
    assert error["dispatchCertainty"] == "response_received"
    assert error["callId"] == response.headers["x-request-id"]
    assert error["retryable"] is False
    assert "private.example" not in response.text and "synthetic-secret" not in response.text
    if code:
        assert error["providerCode"] == code


def test_gateway_preserves_provider_auth_status_without_forwarding_raw_code(setup):
    client, _, _, _, issued = setup
    mock(lambda _: httpx.Response(401, json={"error": {
        "code": "synthetic-private-provider-code", "message": "synthetic-secret",
    }}))
    response = request(client, issued)
    assert response.status_code == 502
    error = response.json()["error"]
    assert (error["providerHttpStatus"], error["gatewayHttpStatus"]) == (401, 502)
    assert "providerCode" not in error
    assert "synthetic-secret" not in response.text


@pytest.mark.parametrize("failure,code,certainty", [
    (httpx.ConnectError, "UPSTREAM_CONNECT_FAILED", "not_dispatched"),
    (httpx.ConnectTimeout, "UPSTREAM_CONNECT_TIMEOUT", "not_dispatched"),
    (httpx.WriteError, "UPSTREAM_WRITE_FAILED", "may_have_dispatched"),
    (httpx.WriteTimeout, "UPSTREAM_WRITE_FAILED", "may_have_dispatched"),
    (httpx.ReadTimeout, "UPSTREAM_TIMEOUT", "may_have_dispatched"),
    (httpx.ReadError, "UPSTREAM_READ_FAILED", "may_have_dispatched"),
])
def test_transport_failures_preserve_delivery_certainty_and_journal_state(setup, failure, code, certainty):
    client, _, headers = official(setup)

    def fail(_):
        raise failure("https://private.example synthetic-secret")

    mock(fail)
    response = client.post("/v1/chat/completions", headers=headers, json=BODY)
    error = response.json()["error"]
    assert error["code"] == code
    assert error["dispatchCertainty"] == certainty
    assert "providerHttpStatus" not in error
    assert error["requestKey"] == KEY
    assert "synthetic-secret" not in response.text
    state = client.get(f"/api/core/v1/model-requests/{KEY}", headers=headers).json()
    assert state["requestKey"] == KEY and state["callId"] == error["callId"]
    assert state["dispatchCertainty"] == certainty
    assert state["credit_state"] == ("released" if certainty == "not_dispatched" else "needs_reconciliation")
    assert state["responseAvailable"] is False
    # Unknown usage belongs to this call only; it does not freeze the account.
    mock(lambda _: success())
    assert client.post("/v1/chat/completions", headers={**headers, "Idempotency-Key": KEY + "-next"},
                       json=BODY).status_code == 200


@pytest.mark.parametrize("payload", [b"not-json", b'{"choices":[]}'])
def test_outer_envelope_failure_retains_real_provider_200(setup, payload):
    client, _, _, _, issued = setup
    mock(lambda _: httpx.Response(200, content=payload))
    response = request(client, issued)
    error = response.json()["error"]
    assert response.status_code == 502
    assert error["code"] == "UPSTREAM_RESPONSE_INVALID"
    assert error["stage"] == "response_envelope"
    assert error["providerHttpStatus"] == 200


def test_same_key_conflict_and_status_are_account_scoped_without_response_replay(setup):
    client, _, headers = official(setup)
    sent = []
    mock(lambda request: (sent.append(request), success("synthetic-private-result", status=201))[1])
    first = client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert first.status_code == 201
    assert first.headers["x-opsark-provider-http-status"] == "201"
    assert first.headers["x-opsark-call-id"] == first.headers["x-request-id"]
    repeated = client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "REQUEST_ALREADY_ACCEPTED"
    assert repeated.json()["error"]["details"]["callId"] == first.headers["x-request-id"]
    changed = client.post("/v1/chat/completions", headers=headers, json={**BODY, "max_tokens": 10})
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert len(sent) == 1
    status = client.get(f"/api/core/v1/model-requests/{KEY}", headers=headers)
    assert status.json()["responseAvailable"] is False
    assert status.json()["providerHttpStatus"] == 201
    assert status.json()["gatewayHttpStatus"] == 201
    assert "synthetic-private-result" not in status.text
    other = client.post("/api/core/v1/register", json={
        "email": "other@example.test", "password": "synthetic-password-123",
    }).json()
    assert client.get(f"/api/core/v1/model-requests/{KEY}", headers={
        "Authorization": "Bearer " + other["access_token"],
    }).status_code == 404


@pytest.mark.parametrize("same_hash", [True, False])
def test_unique_conflict_reloads_winning_request_hash(setup, monkeypatch, same_hash):
    client, _, headers = official(setup)
    mock(lambda _: success())
    assert client.post("/v1/chat/completions", headers=headers, json=BODY).status_code == 200

    def race(*args):
        raise IntegrityError("synthetic concurrent insert", {}, Exception())

    monkeypatch.setattr("app.gateway.begin_direct_call", race)
    changed = BODY if same_hash else {**BODY, "max_tokens": 10}
    response = client.post("/v1/chat/completions", headers=headers, json=changed)
    assert response.json()["error"]["code"] == (
        "REQUEST_ALREADY_ACCEPTED" if same_hash else "IDEMPOTENCY_KEY_CONFLICT")


def test_simultaneous_admission_checks_same_key_hash_under_account_lock(setup):
    _, uid, _ = official(setup)

    def accept(index):
        for db in app.dependency_overrides[get_db]():
            try:
                begin_direct_call(db, uid, f"concurrent-{index}", KEY, str(index) * 64)
                db.commit()
                return "accepted"
            except ApiError as error:
                db.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(accept, [1, 2]))
    assert sorted(outcomes) == ["IDEMPOTENCY_KEY_CONFLICT", "accepted"]
    for db in app.dependency_overrides[get_db]():
        assert len(list(db.scalars(select(ModelReservation)))) == 1


@pytest.mark.parametrize("outer_invalid", [False, True])
def test_confirmed_usage_is_billable_even_when_model_content_or_envelope_is_invalid(setup, outer_invalid):
    client, _, headers = official(setup)
    payload = {"choices": [] if outer_invalid else [{"message": {"content": "{broken business JSON"}}],
               "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    mock(lambda _: httpx.Response(200, json=payload))
    response = client.post("/v1/chat/completions", headers=headers, json=BODY)
    assert response.status_code == (502 if outer_invalid else 200)
    balance = client.get("/api/core/v1/me", headers=headers).json()["balance"]
    assert (balance["available"], balance["reserved"]) == (90, 0)
    state = client.get(f"/api/core/v1/model-requests/{KEY}", headers=headers).json()
    assert state["actual"] == 10 and state["credit_state"] == "settled"


@pytest.mark.parametrize("preset,model,field", [
    ("custom", "any", "max_completion_tokens"),
    ("deepseek-chat-v1", "deepseek-flash", "max_tokens"),
    ("qwen-chat-v1", "qwen-plus", "max_tokens"),
    ("openai-gpt41-v1", "gpt-4.1", "max_completion_tokens"),
])
def test_route_override_cannot_raise_explicit_output_cap(preset, model, field):
    result, changes = resolve_parameters(preset, model, {"max_tokens": 12000},
                                         {"max_tokens": 100}, {"max_completion_tokens": 16000})
    assert result[field] == 100
    assert any("capped by explicit caller" in item for item in changes)
    lowered, _ = resolve_parameters(preset, model, request={"max_tokens": 100}, overrides={"max_tokens": 50})
    assert next(lowered[name] for name in ("max_tokens", "max_completion_tokens") if name in lowered) == 50


def test_gateway_uses_caller_cap_after_route_token_alias_override(setup):
    client, staff, pid, rid, issued = setup
    assert client.put(f"/api/admin/v1/routes/{rid}", headers=staff, json={
        "alias": "public-model", "provider_id": pid, "upstream_model": "private-model",
        "parameter_overrides": {"max_completion_tokens": 10000},
    }).status_code == 200
    sent = []
    mock(lambda req: (sent.append(json.loads(req.content)), success())[1])
    assert request(client, issued, max_tokens=123).status_code == 200
    assert sent[0]["max_completion_tokens"] == 123
    assert "max_tokens" not in sent[0]


def test_qwen_thinking_override_cannot_disguise_incompatible_total_budget():
    with pytest.raises(ApiError) as failure:
        resolve_parameters("qwen-chat-v1", "qwen-plus", request={"max_completion_tokens": 100},
                           overrides={"enable_thinking": True, "max_tokens": 50})
    assert failure.value.code == "PRESET_PARAMETER_UNSUPPORTED"


def test_busy_gateway_records_no_dispatch_and_does_not_call_provider(setup, monkeypatch):
    client, _, headers = official(setup)

    class Busy:
        async def acquire(self):
            raise TimeoutError()

    monkeypatch.setattr(app.state, "model_slots", Busy())
    sent = []
    mock(lambda request: (sent.append(request), success())[1])
    response = client.post("/v1/chat/completions", headers=headers, json=BODY)
    error = response.json()["error"]
    assert error["code"] == "GATEWAY_BUSY" and error["dispatchCertainty"] == "not_dispatched"
    assert error["gatewayHttpStatus"] == 502 and "providerHttpStatus" not in error
    assert sent == []
    state = client.get(f"/api/core/v1/model-requests/{KEY}", headers=headers).json()
    assert state["credit_state"] == "released"


def test_read_failure_after_headers_keeps_provider_status(setup):
    client, _, headers = official(setup)

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"choices":'
            raise httpx.ReadError("synthetic-private-transport-error")

    mock(lambda _: httpx.Response(200, stream=Broken()))
    response = client.post("/v1/chat/completions", headers=headers, json=BODY)
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_READ_FAILED"
    assert error["providerHttpStatus"] == 200 and error["gatewayHttpStatus"] == 502
    assert error["dispatchCertainty"] == "response_received"
    assert "synthetic-private-transport-error" not in response.text


@pytest.mark.parametrize("status,code", [("running", None), ("cancelled", "CLIENT_DISCONNECTED"),
                                         ("failed", "CALL_OUTCOME_UNKNOWN")])
def test_unobserved_delivery_status_never_fabricates_http_response(status, code):
    reservation = SimpleNamespace(state="needs_reconciliation", idempotency_key=KEY, amount=0, actual=None)
    call = SimpleNamespace(id="synthetic-call", status=status, error_code=code, http_status=None)
    state = request_status(reservation, call)
    assert state["dispatchCertainty"] == "may_have_dispatched"
    assert "providerHttpStatus" not in state and "gatewayHttpStatus" not in state
