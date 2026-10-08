"""Both identities use one gateway policy; tests never contact an upstream."""
import json

import httpx
import pytest
from test_accounts import configure, signup
from test_gateway import mock
from test_gateway import setup as source_setup
from test_platform import env as source_env

from app.main import app
from app.db import get_db
from app.models import ModelCall
from sqlalchemy import select

env = source_env
setup = source_setup


def configure_responses(setup, **extra):
    client, staff, provider, route, issued = setup
    body = {"alias": "public-model", "provider_id": provider, "upstream_model": "private-model",
            "api_protocol": "responses", "structured_output": "json_schema", "budget_semantics": "total_output",
            **extra}
    response = client.put(f"/api/admin/v1/routes/{route}", headers=staff, json=body)
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + issued["api_key"]}


def request(client, headers, **extra):
    return client.post("/v1/responses", headers=headers, json={"model": "public-model", "input": [
        {"role": "system", "content": "Return JSON."}, {"role": "user", "content": "synthetic prompt"}],
        "stream": False, **extra})


def envelope(status="completed", usage=True, **extra):
    value = {"id": "resp_synthetic", "object": "response", "model": "private-model", "status": status,
             "error": None, "output": [{"type": "reasoning", "summary": []},
             {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
             **extra}
    if usage:
        value["usage"] = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                          "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2}}
    return value


def test_responses_routes_same_protocol_and_preserves_schema_usage_and_headers(setup):
    client, staff, _, _, _ = setup
    headers = configure_responses(setup)
    model = client.get("/v1/models", headers=headers).json()["data"][0]
    assert model["capabilities"] is None
    caps = model["capabilitiesV2"]
    assert caps["version"] == "model-capabilities@2"
    assert caps["preferredProtocol"] == "responses"
    assert caps["tokenField"] == "max_output_tokens"
    assert caps["outputModes"]["json_schema"] == "supported"
    assert caps["evidence"]["source"] == "user_declared"
    captured = []
    mock(lambda req: (captured.append(req), httpx.Response(200, json=envelope()))[1])
    schema = {"type": "json_schema", "name": "test", "strict": True,
              "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                         "required": ["ok"], "additionalProperties": False}}
    result = request(client, {**headers, "X-Opsark-Expected-Capability-Revision": caps["revision"]},
                     text={"format": schema}, max_output_tokens=50, store=False)
    assert result.status_code == 200, result.text
    sent = json.loads(captured[0].content)
    assert str(captured[0].url) == "https://one.example/v1/responses"
    assert sent["text"]["format"] == schema
    assert sent["max_output_tokens"] == 50
    assert sent["store"] is False
    assert "messages" not in sent and "max_tokens" not in sent and "response_format" not in sent
    assert "x-opsark-expected-capability-revision" not in captured[0].headers
    assert result.json()["output"][0]["type"] == "reasoning"
    assert result.json()["model"] == "public-model"
    calls = client.get("/api/admin/v1/calls", headers=staff).json()
    call = calls[0]
    assert (call["input_tokens"], call["output_tokens"], call["cached_tokens"], call["reasoning_tokens"]) == (10, 5, 3, 2)
    assert call["api_protocol"] == "responses" and call["raw_status"] == "completed"
    assert call["usage_raw"]["total_tokens"] == 15


@pytest.mark.parametrize("fields", [
    {"tools": []}, {"previous_response_id": "resp_old"}, {"background": False}, {"stream": True},
    {"store": True}, {"metadata": {}}, {"messages": []}, {"max_tokens": 12},
    {"response_format": {"type": "json_object"}}, {"text": {"format": {"type": "json_schema", "json_schema": {}}}},
    {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://example.test"}]}]},
    {"input": [{"type": "function_call_output", "call_id": "c1", "output": "ok"}]},
    {"reasoning": {"effort": "low", "summary": "auto"}}, {"max_output_tokens": True},
])
def test_responses_does_not_expand_authority(setup, fields):
    client = setup[0]
    headers = configure_responses(setup)
    mock(lambda _: pytest.fail("unsupported request must not dispatch"))
    result = request(client, headers, **fields)
    assert result.status_code == 422, result.text


def test_revision_and_protocol_are_checked_before_dispatch_for_both_identities(setup):
    client, staff, _, _, _ = setup
    headers = configure_responses(setup)
    configure(client, staff, tokens=100, models=["public-model"])
    _, account = signup(client)
    mock(lambda _: pytest.fail("preflight failure must not dispatch"))
    for identity in [headers, {**account, "Idempotency-Key": "responses-preflight-0001"}]:
        mismatch = request(client, {**identity, "X-Opsark-Expected-Capability-Revision": "v2:stale"})
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "CAPABILITY_REVISION_MISMATCH"
        assert mismatch.json()["error"]["details"]["dispatchCertainty"] == "not_dispatched"
        wrong = client.post("/v1/chat/completions", headers=identity,
                            json={"model": "public-model", "messages": [{"role": "user", "content": "hi"}]})
        assert wrong.status_code == 409
        assert wrong.json()["error"]["code"] == "API_PROTOCOL_MISMATCH"
    assert client.get("/api/core/v1/me", headers=account).json()["balance"]["available"] == 100
    assert client.get("/api/admin/v1/calls", headers=staff).json() == []


def test_official_responses_usage_bills_once_and_missing_usage_remains_unknown(setup):
    client, staff, _, _, _ = setup
    configure_responses(setup)
    configure(client, staff, tokens=100, models=["public-model"])
    _, headers = signup(client)
    calls = []
    mock(lambda req: (calls.append(req), httpx.Response(200, json=envelope()))[1])
    identity = {**headers, "Idempotency-Key": "responses-billing-key-0001"}
    result = request(client, identity)
    assert result.status_code == 200, result.text
    assert client.get("/api/core/v1/me", headers=headers).json()["balance"]["available"] == 85
    again = request(client, identity)
    assert again.status_code == 409 and len(calls) == 1
    changed = request(client, identity, max_output_tokens=30)
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    mock(lambda _: httpx.Response(200, json=envelope(usage=False)))
    second = request(client, {**headers, "Idempotency-Key": "responses-billing-key-0002"})
    assert second.status_code == 200
    assert client.get("/api/core/v1/me", headers=headers).json()["balance"]["available"] == 85
    for db in app.dependency_overrides[get_db]():
        unknown = db.scalar(select(ModelCall).where(ModelCall.id == second.headers["x-request-id"]))
        assert unknown.input_tokens is None and unknown.output_tokens is None
        assert unknown.cached_tokens is None and unknown.reasoning_tokens is None
    status = client.get("/api/core/v1/model-requests/responses-billing-key-0002", headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()["actual"] is None


@pytest.mark.parametrize("status,extra", [
    ("incomplete", {"incomplete_details": {"reason": "max_output_tokens"}}),
    ("failed", {"error": {"code": "server_error", "message": "failed"}}),
    ("queued", {}), ("in_progress", {}), ("cancelled", {}),
    ("completed", {"output": [{"type": "message", "role": "assistant", "content": [
        {"type": "refusal", "refusal": "Cannot comply"}]}]}),
])
def test_provider_status_refusal_and_output_items_are_not_rewritten(setup, status, extra):
    client = setup[0]
    headers = configure_responses(setup)
    data = envelope(status, **extra)
    mock(lambda _: httpx.Response(200, json=data))
    response = request(client, headers)
    assert response.status_code == 200, response.text
    assert response.json() == {**data, "model": "public-model"}


def test_capabilities_revision_changes_with_config_but_not_public_upstream_details(setup):
    client, staff, provider, _, _ = setup
    headers = configure_responses(setup)
    first = client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"]
    configure_responses(setup, parameter_overrides={"max_output_tokens": 500})
    second = client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"]
    assert first["revision"] != second["revision"]
    assert second["defaultOutputTokens"] == 500
    response = client.put(f"/api/admin/v1/providers/{provider}", headers=staff, json={
        "name": "first", "base_url": "https://one.example/v1", "api_key": "rotated-private-key"})
    assert response.status_code == 200
    third = client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"]
    assert third["revision"] != second["revision"]
    public = json.dumps(third)
    assert "rotated-private-key" not in public and "private-model" not in public and "one.example" not in public


def test_responses_configuration_rejects_cross_protocol_and_semantically_unknown_budget(setup):
    client, staff, provider, route, _ = setup
    headers = configure_responses(setup)
    mock(lambda _: pytest.fail("invalid config must not dispatch"))
    for change, code in [
        ({"preset_id": "deepseek-chat-v1", "upstream_model": "deepseek-flash"}, "PRESET_PROTOCOL_UNSUPPORTED"),
        ({"parameter_overrides": {"frequency_penalty": 1}}, "PROTOCOL_PARAMETER_UNSUPPORTED"),
        ({"budget_semantics": "unknown", "parameter_overrides": {"max_completion_tokens": 50}}, "PARAMETER_SEMANTICS_CONFLICT"),
    ]:
        result = client.put(f"/api/admin/v1/routes/{route}", headers=staff, json={
            "alias": "public-model", "provider_id": provider, "upstream_model": "private-model",
            "api_protocol": "responses", **change})
        assert result.status_code == 422 and result.json()["error"]["code"] == code, result.text
    configure_responses(setup, structured_output="unknown")
    unknown = request(client, headers, text={"format": {"type": "json_object"}})
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "OUTPUT_CAPABILITY_UNKNOWN"


def test_responses_full_endpoint_is_not_appended_twice(setup):
    client, staff, provider, _, _ = setup
    headers = configure_responses(setup)
    result = client.put(f"/api/admin/v1/providers/{provider}", headers=staff, json={
        "name": "first", "base_url": "https://one.example/v1/responses", "api_key": "new-key"})
    assert result.status_code == 200, result.text
    captured = []
    mock(lambda req: (captured.append(req), httpx.Response(200, json=envelope()))[1])
    assert request(client, headers).status_code == 200
    assert str(captured[0].url) == "https://one.example/v1/responses"


def test_revision_covers_precedence_not_only_merged_default(setup):
    client, staff, provider, _, _ = setup
    headers = configure_responses(setup)
    assert client.put(f"/api/admin/v1/providers/{provider}", headers=staff, json={
        "name": "first", "base_url": "https://one.example/v1", "parameter_defaults": {"temperature": 1.0}
    }).status_code == 200
    first = client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"]["revision"]
    configure_responses(setup, parameter_overrides={"temperature": 1.0})
    second = client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"]["revision"]
    assert first != second  # forced value now wins over caller, despite equal default preview


def test_shared_responses_envelopes_preserve_core_classification_input():
    from pathlib import Path
    from app.model_protocols import response_status
    cases = json.loads((Path(__file__).resolve().parents[2] /
                        "Opsark_core/contracts/model-api/protocol-cases.json").read_text())
    covered = 0
    for case in cases:
        if case["apiProtocol"] != "responses":
            continue
        data = case["payload"]
        before = json.loads(json.dumps(data))
        if data.get("status") in {"completed", "incomplete", "failed", "cancelled", "queued", "in_progress"}:
            assert response_status(data) == data["status"], case["id"]
            assert data == before, case["id"]
            covered += 1
        else:
            with pytest.raises(ValueError):
                response_status(data)
    assert covered >= 10


def test_openai_preset_both_protocols_accept_explicit_no_store_and_v2_rules(setup):
    client, staff, provider, route, issued = setup
    headers = {"Authorization": "Bearer " + issued["api_key"]}
    for protocol in ("chat_completions", "responses"):
        saved = client.put(f"/api/admin/v1/routes/{route}", headers=staff, json={
            "alias": "public-model", "provider_id": provider, "upstream_model": "gpt-4.1",
            "preset_id": "openai-gpt41-v1", "api_protocol": protocol})
        assert saved.status_code == 200, saved.text
        caps = client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"]
        assert caps["parameterRules"]["temperature"] == "supported"
        assert caps["parameterRules"]["presencePenalty"] == (
            "supported" if protocol == "chat_completions" else "unsupported")
        assert caps["strictFlag"] == "required"
        sent = []
        payload = {"choices": [{"message": {"content": "ok"}}]} if protocol == "chat_completions" else envelope()
        mock(lambda req: (sent.append(req), httpx.Response(200, json=payload))[1])
        data = {"model": "public-model", "store": False, "temperature": 0.5}
        data.update({"messages": [{"role": "user", "content": "hello"}]} if protocol == "chat_completions" else {"input": "hello"})
        path = "/v1/chat/completions" if protocol == "chat_completions" else "/v1/responses"
        result = client.post(path, headers={**headers, "X-Opsark-Expected-Capability-Revision": caps["revision"]}, json=data)
        assert result.status_code == 200, result.text
        assert json.loads(sent[0].content)["store"] is False
        assert client.post(path, headers=headers, json={**data, "store": True}).status_code == 422


def test_legacy_custom_route_stays_on_legacy_until_capability_declared(setup):
    from app.models import ModelRoute
    client, staff, provider, route, issued = setup
    headers = {"Authorization": "Bearer " + issued["api_key"]}
    # New custom routes expose unknown V2 rather than assuming schema support.
    fresh = client.get("/v1/models", headers=headers).json()["data"][0]
    assert fresh["capabilitiesV2"]["outputModes"] == {"json_object": "unknown", "json_schema": "unknown"}
    for db in app.dependency_overrides[get_db]():
        db.get(ModelRoute, route).capabilities_v2_enabled = False
        db.commit()
    legacy = client.get("/v1/models", headers=headers).json()["data"][0]
    assert legacy["capabilitiesV2"] is None
    assert legacy["capabilities"]["protocol"] == "chat_completions"
    body = {"alias": "public-model", "provider_id": provider, "upstream_model": "private-model",
            "display_name": "renamed", "api_protocol": "chat_completions", "structured_output": "unknown"}
    assert client.put(f"/api/admin/v1/routes/{route}", headers=staff, json=body).status_code == 200
    assert client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"] is None
    mock(lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}))
    assert client.post("/v1/chat/completions", headers=headers, json={"model": "public-model",
        "messages": [{"role": "user", "content": "legacy JSON"}], "response_format": {"type": "json_object"}}).status_code == 200
    assert client.put(f"/api/admin/v1/routes/{route}", headers=staff, json={
        **body, "structured_output": "json_object"}).status_code == 200
    enabled = client.get("/v1/models", headers=headers).json()["data"][0]
    assert enabled["capabilitiesV2"]["outputModes"]["json_object"] == "supported"
    assert enabled["capabilitiesV2"]["outputModes"]["json_schema"] == "unknown"
    assert enabled["capabilitiesV2"]["evidence"]["source"] == "user_declared"


def test_v2_chat_request_cannot_exceed_advertised_output_capability(setup):
    client, staff, provider, route, issued = setup
    headers = {"Authorization": "Bearer " + issued["api_key"]}
    assert client.put(f"/api/admin/v1/routes/{route}", headers=staff, json={
        "alias": "public-model", "provider_id": provider, "upstream_model": "private-model",
        "structured_output": "json_object"}).status_code == 200
    caps = client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"]
    mock(lambda _: pytest.fail("output capability mismatch must not dispatch"))
    response = client.post("/v1/chat/completions", headers={**headers,
        "X-Opsark-Expected-Capability-Revision": caps["revision"]}, json={"model": "public-model",
        "messages": [{"role": "user", "content": "JSON"}], "response_format": {"type": "json_schema",
        "json_schema": {"name": "test", "schema": {"type": "object"}}}})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "OUTPUT_CAPABILITY_UNKNOWN"
    assert response.json()["error"]["dispatchCertainty"] == "not_dispatched"


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("value", [0, 1, -0.001, 1.001, True, "0"])
def test_sampling_probability_inclusive_bounds_and_native_json_types(setup, protocol, value):
    client, staff, provider, route, issued = setup
    assert client.put(f"/api/admin/v1/routes/{route}", headers=staff, json={
        "alias": "public-model", "provider_id": provider, "upstream_model": "gpt-4.1",
        "preset_id": "openai-gpt41-v1", "api_protocol": protocol}).status_code == 200
    headers = {"Authorization": "Bearer " + issued["api_key"]}
    caps = client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"]
    assert caps["parameterRules"]["topP"] == "supported"
    sent = []
    payload = {"choices": [{"message": {"content": "ok"}}]} if protocol == "chat_completions" else envelope()
    mock(lambda req: (sent.append(req), httpx.Response(200, json=payload))[1])
    data = {"model": "public-model", "top_p": value}
    data.update({"messages": [{"role": "user", "content": "hello"}]} if protocol == "chat_completions" else {"input": "hello"})
    path = "/v1/chat/completions" if protocol == "chat_completions" else "/v1/responses"
    result = client.post(path, headers={**headers, "X-Opsark-Expected-Capability-Revision": caps["revision"]}, json=data)
    if type(value) in (int, float) and 0 <= value <= 1:
        assert result.status_code == 200, result.text
        assert len(sent) == 1 and json.loads(sent[0].content)["top_p"] == value
    else:
        assert result.status_code == 422, result.text
        assert result.json()["error"]["code"] == "INVALID_MODEL_PARAMETERS"
        assert not sent


@pytest.mark.parametrize("field", ["temperature", "top_p", "presence_penalty", "frequency_penalty"])
@pytest.mark.parametrize("value", [True, False, "0", "0.5"])
def test_numeric_model_parameters_do_not_coerce_non_numbers(field, value):
    from app.model_parameters import ModelParameters
    with pytest.raises(ValueError):
        ModelParameters.model_validate({field: value})


def test_missing_budget_uses_published_official_limit(setup):
    client = setup[0]
    headers = configure_responses(setup)
    caps = client.get("/v1/models", headers=headers).json()["data"][0]["capabilitiesV2"]
    captured = []
    mock(lambda req: (captured.append(req), httpx.Response(200, json=envelope()))[1])
    result = request(client, headers)
    assert result.status_code == 200, result.text
    assert caps["defaultOutputTokens"] == caps["maxOutputTokens"]
    assert json.loads(captured[0].content)["max_output_tokens"] == caps["maxOutputTokens"]
