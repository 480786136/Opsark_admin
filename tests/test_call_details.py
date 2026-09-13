import json
import time

import httpx

from app.config import settings
from app.db import get_db
from app.gateway import cipher
from app.main import app
from app.models import ModelCallDetail
from app.call_details import snapshot
from test_gateway import setup, env, mock, request  # noqa: F401


def test_details_encrypted_redacted_and_expiring(setup):
    client, _, _, _, issued = setup
    mock(
        lambda r: httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "answer password=secret-value sk-secret-value"}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 2081, "completion_tokens": 5000},
            },
        )
    )
    result = request(client, issued)
    ident = result.headers["x-request-id"]
    details = client.get("/api/admin/v1/calls/" + ident).json()
    assert details["detail"]["finish_reasons"] == ["length"]
    assert details["call"]["output_tokens"] == 5000
    assert "synthetic-private-prompt" in details["detail"]["input"]["text"]
    assert "secret-value" not in json.dumps(details)
    assert "synthetic-private-prompt" not in client.get("/api/admin/v1/calls").text
    for db in app.dependency_overrides[get_db]():
        row = db.get(ModelCallDetail, ident)
        assert "synthetic-private-prompt" not in row.encrypted_content
        assert "secret-value" not in cipher().decrypt(row.encrypted_content.encode()).decode()
        row.expires_at = time.time() - 1
        db.commit()
    assert client.get("/api/admin/v1/calls/" + ident).json()["detail"] is None
    client.cookies.clear()
    assert client.get("/api/admin/v1/calls/" + ident).status_code == 401


def test_disabled_capture_keeps_usage(setup, monkeypatch):
    client, _, _, _, issued = setup
    monkeypatch.setattr(settings(), "model_call_capture_enabled", False)
    mock(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]}))
    result = request(client, issued)
    detail = client.get("/api/admin/v1/calls/" + result.headers["x-request-id"]).json()
    assert detail["detail"] is None
    assert detail["call"]["input_tokens"] is None


def test_snapshot_bounds_and_secrets():
    value = snapshot(
        {
            "api_key": "hidden",
            "content": "Bearer abc password='has spaces' https://user:pass@example.com sk-private omk_private",
        }
    )
    for secret in ("hidden", "abc", "has spaces", "user:pass", "sk-private", "omk_private"):
        assert secret not in value["text"]
    assert snapshot("中" * 100000)["truncated"]
    assert len(snapshot("中" * 100000)["text"].encode()) <= 64 * 1024


def test_stream_detail_joins_deltas_and_usage(setup):
    client, _, _, _, issued = setup
    events = [
        {"choices": [{"index": 0, "delta": {"content": "hello "}}]},
        {"choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    body = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
    mock(lambda r: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}))
    result = request(client, issued, stream=True)
    detail = client.get("/api/admin/v1/calls/" + result.headers["x-request-id"]).json()
    assert "hello world" in detail["detail"]["output"]["text"]
    assert detail["call"]["output_tokens"] == 2


def test_snapshot_failure_does_not_break_call(setup, monkeypatch):
    client, _, _, _, issued = setup

    def fail(*args):
        raise RuntimeError("do not log content")

    monkeypatch.setattr("app.gateway.snapshot", fail)
    mock(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]}))
    result = request(client, issued)
    assert result.status_code == 200
    assert client.get("/api/admin/v1/calls").json()[0]["status"] == "succeeded"


def test_http_failure_snapshot_filters_upstream_key(setup):
    client, _, _, _, issued = setup
    mock(lambda r: httpx.Response(429, json={"error": {"message": "rejected synthetic-upstream-private", "api_key": "secret"}}))
    result = request(client, issued)
    assert result.status_code == 429
    detail = client.get("/api/admin/v1/calls/" + result.headers["x-request-id"]).json()
    assert detail["call"]["status"] == "failed"
    assert "synthetic-upstream-private" not in json.dumps(detail)
    assert "rejected" in detail["detail"]["output"]["text"]
