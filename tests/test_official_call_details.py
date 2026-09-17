"""Official account diagnostics use encrypted snapshots, not public usage records."""

import json
import time

import httpx
import pytest
from sqlalchemy import select
from test_accounts import configure, signup
from test_gateway import mock
from test_gateway import setup as source_setup
from test_platform import env as source_env

from app.config import settings
from app.db import get_db
from app.gateway import cipher
from app.main import app
from app.models import ModelCallDetail
from app.user_models import AccountAudit

env = source_env
setup = source_setup
REQUEST_KEY = "official-capture-request-0001"


@pytest.fixture
def official(setup, monkeypatch):
    client, staff, *_ = setup
    monkeypatch.setattr(settings(), "model_call_capture_enabled", True)
    monkeypatch.setattr(settings(), "model_call_retention_days", 7)
    configure(client, staff, tokens=1000000, models=["public-model"])
    account, bearer = signup(client)
    return client, account, bearer


def call(client, bearer, content="synthetic official prompt"):
    return client.post(
        "/v1/chat/completions",
        headers={**bearer, "Idempotency-Key": REQUEST_KEY, "X-Opsark-Task-Id": "task-capture"},
        json={"model": "public-model", "messages": [{"role": "user", "content": content}]},
    )


def answer(content="synthetic official answer"):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10},
        },
    )


def test_official_snapshots_encrypted_redacted_audited_and_admin_only(official):
    client, account, bearer = official
    secrets = [account["access_token"], account["refresh_token"], "synthetic-upstream-private", "hidden-password"]
    secret_text = " ".join(secrets[:3]) + " password=hidden-password"
    prompt = "synthetic official prompt " + secret_text
    output = "synthetic official answer " + secret_text
    mock(lambda _: answer(output))
    result = call(client, bearer, prompt)
    assert result.status_code == 200
    # Redaction is for the diagnostic copy, not the live model request/response.
    assert result.json()["choices"][0]["message"]["content"] == output
    ident = result.headers["x-request-id"]
    url = "/api/admin/v1/calls/" + ident
    detail = client.get(url).json()
    assert detail["detail_policy"] == "redacted_snapshot"
    assert detail["call"]["user_id"] == account["user"]["id"]
    assert detail["call"]["task_id"] == "task-capture"
    assert "synthetic official prompt" in detail["detail"]["input"]["text"]
    assert "synthetic official answer" in detail["detail"]["output"]["text"]
    assert detail["detail"]["finish_reasons"] == ["stop"]
    assert time.time() + 6 * 86400 < detail["expires_at"] <= time.time() + 7 * 86400
    assert all(secret not in json.dumps(detail) for secret in secrets)
    for path in ("/api/admin/v1/calls", "/api/admin/v1/call-monitor/requests", "/api/admin/v1/call-monitor/tasks"):
        response = client.get(path)
        assert response.status_code == 200
        assert "synthetic official prompt" not in response.text
        assert "synthetic official answer" not in response.text
    for db in app.dependency_overrides[get_db]():
        row = db.get(ModelCallDetail, ident)
        assert "synthetic official" not in row.encrypted_content
        plaintext = cipher().decrypt(row.encrypted_content.encode()).decode()
        assert all(secret not in plaintext for secret in secrets)
        audit = db.scalar(select(AccountAudit).where(AccountAudit.action == "model_call_viewed"))
        assert audit.actor_id and audit.detail["snapshot_available"] is True
        assert audit.detail["call_id"] == ident and audit.detail["task_id"] == "task-capture"
        assert "synthetic official" not in str(audit.detail)
        row.expires_at = time.time() - 1
        db.commit()
    assert client.get(url).json()["detail"] is None
    assert client.get(url).json()["call"]["input_tokens"] == 40
    client.cookies.clear()
    assert client.get(url).status_code == 401
    assert client.get(url, headers=bearer).status_code == 401
    status = client.get("/api/core/v1/model-requests/" + REQUEST_KEY, headers=bearer)
    assert status.status_code == 200 and status.json()["actual"] == 50
    assert "synthetic official" not in status.text


def test_official_capture_switch_does_not_change_billing(official, monkeypatch):
    client, _, bearer = official
    monkeypatch.setattr(settings(), "model_call_capture_enabled", False)
    mock(lambda _: answer())
    result = call(client, bearer)
    assert result.status_code == 200
    detail = client.get("/api/admin/v1/calls/" + result.headers["x-request-id"]).json()
    assert detail["detail"] is None
    assert (detail["call"]["input_tokens"], detail["call"]["output_tokens"]) == (40, 10)
    balance = client.get("/api/core/v1/me", headers=bearer).json()["balance"]
    assert (balance["available"], balance["reserved"]) == (999950, 0)


@pytest.mark.parametrize("upstream_status", [429, 500])
def test_official_upstream_error_is_captured_without_credentials(official, upstream_status):
    client, account, bearer = official
    mock(
        lambda _: httpx.Response(
            upstream_status,
            json={
                "error": {
                    "message": "synthetic upstream rejection synthetic-upstream-private " + account["access_token"],
                    "refresh_token": account["refresh_token"],
                }
            },
        )
    )
    result = call(client, bearer)
    assert result.status_code == (429 if upstream_status == 429 else 502)
    detail = client.get("/api/admin/v1/calls/" + result.headers["x-request-id"]).json()
    assert detail["call"]["status"] == "failed"
    assert "synthetic official prompt" in detail["detail"]["input"]["text"]
    assert "synthetic upstream rejection" in detail["detail"]["output"]["text"]
    assert "synthetic-upstream-private" not in json.dumps(detail)
    assert account["access_token"] not in json.dumps(detail)
    assert account["refresh_token"] not in json.dumps(detail)
    assert "synthetic upstream rejection" not in result.text


def test_official_snapshot_failure_preserves_response_billing_and_idempotency(official, monkeypatch, caplog):
    client, _, bearer = official

    def fail(*args):
        raise RuntimeError("synthetic sensitive exception")

    monkeypatch.setattr("app.gateway.snapshot", fail)
    mock(lambda _: answer())
    result = call(client, bearer)
    assert result.status_code == 200
    detail = client.get("/api/admin/v1/calls/" + result.headers["x-request-id"]).json()
    assert detail["call"]["status"] == "succeeded" and detail["detail"] is None
    assert "synthetic sensitive exception" not in caplog.text
    assert "call_detail_save_failed" in caplog.text
    assert "synthetic official prompt" not in caplog.text
    assert client.get("/api/core/v1/me", headers=bearer).json()["balance"]["available"] == 999950
    mock(lambda _: pytest.fail("duplicate must not dispatch"))
    assert call(client, bearer).status_code == 409


def test_official_snapshots_are_bounded_without_truncating_live_response(official):
    client, _, bearer = official
    content = "中" * 30000
    mock(lambda _: answer(content))
    result = call(client, bearer, content)
    assert result.status_code == 200
    assert result.json()["choices"][0]["message"]["content"] == content
    detail = client.get("/api/admin/v1/calls/" + result.headers["x-request-id"]).json()
    for field in ("input", "output"):
        assert detail["detail"][field]["truncated"] is True
        assert len(detail["detail"][field]["text"].encode()) <= 64 * 1024
