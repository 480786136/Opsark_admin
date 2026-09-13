import json
import time
import httpx
import pytest
from cryptography.fernet import Fernet
from app.config import settings
from app.main import app
from app.models import ModelKey, Provider
from app.db import get_db
from test_platform import env as source_env, auth

env = source_env


@pytest.fixture
def setup(env, monkeypatch):
    monkeypatch.setattr(settings(), "model_key_encryption_key", Fernet.generate_key().decode())
    headers = auth(env)
    provider = {"name": "first", "base_url": "https://one.example/v1", "api_key": "synthetic-upstream-private"}
    p = env.post("/api/admin/v1/providers", headers=headers, json=provider)
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    route = {"alias": "public-model", "provider_id": pid, "upstream_model": "private-model"}
    r = env.post("/api/admin/v1/routes", headers=headers, json=route)
    assert r.status_code == 201, r.text
    issued = env.post(
        "/api/admin/v1/model-keys", headers=headers, json={"owner": "customer-a", "allowed_models": ["public-model"]}
    )
    assert issued.status_code == 201, issued.text
    return env, headers, pid, r.json()["id"], issued.json()


def mock(handler):
    # Reuse the lifespan-owned client so test teardown closes it.
    app.state.model_client._transport = httpx.MockTransport(handler)


def request(client, issued, **extra):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + issued["api_key"]},
        json={"model": "public-model", "messages": [{"role": "user", "content": "synthetic-private-prompt"}], **extra},
    )


def test_json_routing_keys_and_usage(setup):
    c, admin, pid, rid, issued = setup
    captured = []

    def handler(r):
        captured.append(r)
        return httpx.Response(
            200,
            json={
                "id": "response",
                "model": "private-model",
                "choices": [{"message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    mock(handler)
    response = request(c, issued)
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "public-model"
    assert str(captured[0].url) == "https://one.example/v1/chat/completions"
    assert json.loads(captured[0].content)["model"] == "private-model"
    assert captured[0].headers["authorization"] == "Bearer synthetic-upstream-private"
    assert "cookie" not in captured[0].headers
    assert issued["api_key"] not in str(captured[0].headers)
    calls = c.get("/api/admin/v1/calls").json()
    assert calls[0]["status"] == "succeeded" and calls[0]["input_tokens"] == 4
    assert calls[0]["id"] == response.headers["x-request-id"]
    assert "synthetic-private-prompt" not in json.dumps(calls)
    assert "synthetic-upstream-private" not in c.get("/api/admin/v1/providers").text
    assert issued["api_key"] not in c.get("/api/admin/v1/model-keys").text
    for db in app.dependency_overrides[get_db]():
        assert "synthetic-upstream-private" not in db.get(Provider, pid).encrypted_key
        assert db.get(ModelKey, issued["id"]).token_hash != issued["api_key"]


def test_client_deadline_caps_gateway_upstream_timeout(setup):
    c, _admin, _pid, _rid, issued = setup
    captured = []

    def handler(r):
        captured.append(r.extensions["timeout"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]},
        )

    mock(handler)
    response = c.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer " + issued["api_key"],
            "X-Opsark-Timeout-Seconds": "7",
        },
        json={"model": "public-model", "messages": [{"role": "user", "content": "deadline"}]},
    )
    assert response.status_code == 200
    assert captured == [{"connect": 7, "read": 7, "write": 7, "pool": 7}]


def test_model_deadline_can_exceed_provider_fallback_timeout(setup):
    c, _admin, _pid, _rid, issued = setup
    captured = []

    def handler(r):
        captured.append(r.extensions["timeout"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]},
        )

    mock(handler)
    response = c.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer " + issued["api_key"],
            "X-Opsark-Timeout-Seconds": "358",
        },
        json={"model": "public-model", "messages": [{"role": "user", "content": "slow model"}]},
    )
    assert response.status_code == 200
    assert captured == [{"connect": 358, "read": 358, "write": 358, "pool": 358}]


def test_two_suppliers_can_be_switched(setup):
    c, admin, pid, rid, issued = setup
    p2 = c.post(
        "/api/admin/v1/providers",
        headers=admin,
        json={"name": "second", "base_url": "https://two.example/v1", "api_key": "second-private"},
    ).json()
    r = c.put(
        "/api/admin/v1/routes/" + rid,
        headers=admin,
        json={"alias": "public-model", "provider_id": p2["id"], "upstream_model": "second-model"},
    )
    assert r.status_code == 200

    def handler(r):
        assert r.url.host == "two.example"
        assert json.loads(r.content)["model"] == "second-model"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "second reply"}, "finish_reason": "stop"}]},
        )

    mock(handler)
    assert request(c, issued).status_code == 200
    assert c.get("/api/admin/v1/calls").json()[0]["input_tokens"] is None


def test_permissions_revocation_and_expiration(setup):
    c, admin, pid, rid, issued = setup
    assert c.get("/v1/models").status_code == 401
    assert request(c, issued, model="other-model").status_code == 403
    assert c.post("/api/admin/v1/providers", json={}).status_code == 403
    assert request(c, issued, base_url="https://evil.example").status_code == 422
    headers = {"Authorization": "Bearer " + issued["api_key"]}
    assert c.get("/v1/models", headers=headers).json()["data"][0]["id"] == "public-model"
    for db in app.dependency_overrides[get_db]():
        db.get(ModelKey, issued["id"]).expires = time.time() - 1
        db.commit()
    assert c.get("/v1/models", headers=headers).status_code == 401
    assert c.delete("/api/admin/v1/model-keys/" + issued["id"], headers=admin).status_code == 200
    assert request(c, issued).status_code == 401


def test_rejects_unknown_destination_and_new_url_without_key(setup):
    c, admin, pid, rid, issued = setup
    for url in [
        "ftp://one.example/v1",
        "http://one.example:0/v1",
        "http://one.example:99999/v1",
        "https://user:key@one.example/v1",
        "https://one.example/v1?key=x",
        "https://127.0.0.1/v1",
    ]:
        assert (
            c.post(
                "/api/admin/v1/providers", headers=admin, json={"name": "bad", "base_url": url, "api_key": "key"}
            ).status_code
            == 422
        )
    assert (
        c.put(
            "/api/admin/v1/providers/" + pid,
            headers=admin,
            json={"name": "changed", "base_url": "https://two.example/v1"},
        ).status_code
        == 422
    )


def test_new_provider_host_needs_no_allowlist(setup):
    c, admin, *_ = setup
    response = c.post("/api/admin/v1/providers", headers=admin, json={
        "name": "new-provider", "base_url": "https://new-provider.example/v1",
        "api_key": "synthetic-new-key",
    })
    assert response.status_code == 201
    assert "allowed_hosts" not in c.get("/api/admin/v1/config").json()


def test_private_http_upstream_preserves_custom_base_path(setup):
    c, admin, pid, rid, issued = setup
    response = c.put("/api/admin/v1/providers/" + pid, headers=admin, json={
        "name": "internal", "base_url": "http://10.213.80.243:18080/v1-openai/",
        "api_key": "synthetic-private-key",
    })
    assert response.status_code == 200
    seen = []

    def handle(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "data": [],
        })

    mock(handle)
    assert c.post("/api/admin/v1/providers/" + pid + "/test", headers=admin).status_code == 200
    assert request(c, issued).status_code == 200
    assert seen == [
        "http://10.213.80.243:18080/v1-openai/models",
        "http://10.213.80.243:18080/v1-openai/chat/completions",
    ]


def test_upstream_errors_are_sanitized_and_no_redirects(setup):
    c, admin, pid, rid, issued = setup
    for status in [401, 429, 500, 302]:
        mock(
            lambda r: httpx.Response(
                status,
                json={"error": {"message": "sensitive-upstream-details"}},
                headers={"Location": "https://evil.example"},
            )
        )
        response = request(c, issued)
        assert response.status_code == (429 if status == 429 else 502)
        assert "sensitive-upstream-details" not in response.text
        assert c.get("/api/admin/v1/calls").json()[0]["status"] == "failed"


def test_streaming_and_incomplete_stream(setup):
    c, admin, pid, rid, issued = setup
    event = {"model": "private-model", "choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}
    end = {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
    wire = "data: " + json.dumps(event) + "\n\ndata: " + json.dumps(end) + "\n\ndata: [DONE]\n\n"
    mock(lambda r: httpx.Response(200, content=wire, headers={"Content-Type": "text/event-stream"}))
    response = request(c, issued, stream=True)
    assert response.status_code == 200 and "[DONE]" in response.text
    assert "public-model" in response.text and "private-model" not in response.text
    assert c.get("/api/admin/v1/calls").json()[0]["output_tokens"] == 1
    mock(
        lambda r: httpx.Response(
            200, content="data: " + json.dumps(event) + "\n\n", headers={"Content-Type": "text/event-stream"}
        )
    )
    response = request(c, issued, stream=True)
    assert "STREAM_INTERRUPTED" in response.text
    assert c.get("/api/admin/v1/calls").json()[0]["status"] == "failed"


def test_rate_limit_and_disabled_provider(setup):
    c, admin, pid, rid, issued = setup
    limited = c.post(
        "/api/admin/v1/model-keys",
        headers=admin,
        json={"owner": "limited", "allowed_models": ["public-model"], "rpm": 1},
    ).json()
    h = {"Authorization": "Bearer " + limited["api_key"]}
    assert c.get("/v1/models", headers=h).status_code == 200
    assert c.get("/v1/models", headers=h).status_code == 429
    c.put(
        "/api/admin/v1/providers/" + pid,
        headers=admin,
        json={"name": "off", "base_url": "https://one.example/v1", "enabled": False},
    )
    assert request(c, issued).status_code == 503


def test_missing_encryption_key_fails_closed(env, monkeypatch):
    headers = auth(env)
    monkeypatch.setattr(settings(), "model_key_encryption_key", "")
    response = env.post(
        "/api/admin/v1/providers",
        headers=headers,
        json={"name": "first", "base_url": "https://one.example/v1", "api_key": "never-store-plaintext"},
    )
    assert response.status_code == 503
    assert env.get("/api/admin/v1/providers").json() == []


def test_invalid_or_timed_out_upstream_never_counts_as_success(setup):
    c, admin, pid, rid, issued = setup
    mock(lambda r: httpx.Response(200, json={"choices": []}))
    assert request(c, issued).status_code == 502
    assert c.get("/api/admin/v1/calls").json()[0]["status"] == "failed"

    def timeout(r):
        raise httpx.ReadTimeout("sensitive-upstream-details", request=r)

    mock(timeout)
    result = request(c, issued)
    assert result.status_code == 502 and "sensitive-upstream-details" not in result.text
    assert app.state.model_slots._value == 8


def test_changed_encryption_key_cannot_send_old_secrets(setup, monkeypatch):
    c, admin, pid, rid, issued = setup
    monkeypatch.setattr(settings(), "model_key_encryption_key", Fernet.generate_key().decode())

    def forbidden(r):
        raise AssertionError("must not call upstream when decryption fails")

    mock(forbidden)
    result = request(c, issued)
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "KEY_DECRYPT_FAILED"


def test_validated_parameter_defaults_and_route_overrides(setup):
    client, admin, provider_id, route_id, issued = setup
    provider = client.put(
        "/api/admin/v1/providers/" + provider_id,
        headers=admin,
        json={
            "name": "first",
            "base_url": "https://one.example/v1",
            "parameter_defaults": {
                "thinking": {"type": "disabled"},
                "temperature": 0.2,
                "max_tokens": 4000,
            },
        },
    )
    assert provider.status_code == 200, provider.text
    route = client.put(
        "/api/admin/v1/routes/" + route_id,
        headers=admin,
        json={
            "alias": "public-model",
            "provider_id": provider_id,
            "upstream_model": "private-model",
            "parameter_overrides": {"temperature": 0.1, "max_tokens": 3000},
        },
    )
    assert route.status_code == 200, route.text
    captured = []

    def handler(upstream):
        captured.append(json.loads(upstream.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]},
        )

    mock(handler)
    result = request(client, issued, temperature=0.9, max_tokens=5000)
    assert result.status_code == 200, result.text
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert captured[0]["temperature"] == 0.1
    assert captured[0]["max_tokens"] == 3000
    assert client.get("/api/admin/v1/providers").json()[0]["parameter_defaults"]["thinking"]["type"] == "disabled"
    assert client.get("/api/admin/v1/routes").json()[0]["parameter_overrides"]["max_tokens"] == 3000


@pytest.mark.parametrize(
    "parameters",
    [
        {"thinking": {"type": "sometimes"}},
        {"temperature": 3},
        {"top_p": 0},
        {"max_tokens": 0},
        {"unknown_vendor_option": True},
        {"thinking": {"type": "disabled"}, "reasoning_effort": "high"},
        {"max_tokens": 10, "max_completion_tokens": 10},
    ],
)
def test_provider_rejects_unsafe_advanced_parameters(setup, parameters):
    client, admin, provider_id, _, _ = setup
    result = client.put(
        "/api/admin/v1/providers/" + provider_id,
        headers=admin,
        json={
            "name": "first",
            "base_url": "https://one.example/v1",
            "parameter_defaults": parameters,
        },
    )
    assert result.status_code == 422


def test_invalid_caller_model_parameters_are_not_forwarded(setup):
    client, _, _, _, issued = setup
    called = []
    mock(lambda upstream: called.append(upstream))
    result = request(client, issued, thinking={"type": "unexpected"})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "INVALID_MODEL_PARAMETERS"
    assert called == []
