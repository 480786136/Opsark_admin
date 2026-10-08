import json
from types import SimpleNamespace

import httpx
import pytest
from test_gateway import mock, request
from test_gateway import setup as source_setup
from test_platform import env as source_env

from app.model_parameters import ModelParameters
from app.model_presets import catalog, resolve_parameters, route_capabilities, validate_request_capabilities
from app.security import ApiError

env = source_env
setup = source_setup


@pytest.mark.parametrize(
    "preset,model,expected",
    [
        ("deepseek-chat-v1", "deepseek-flash", {"thinking": {"type": "disabled"}, "max_tokens": 1234}),
        ("qwen-chat-v1", "qwen-plus", {"enable_thinking": False, "max_tokens": 1234}),
        ("openai-gpt41-v1", "gpt-4.1", {"max_completion_tokens": 1234}),
    ],
)
def test_token_mapping_does_not_increase_budget(preset, model, expected):
    values, _ = resolve_parameters(preset, model, request={"max_completion_tokens": 1234})
    assert values == expected
    values, _ = resolve_parameters(preset, model, request={"max_tokens": 1234})
    assert values == expected


def test_aliases_obey_layer_precedence_and_do_not_mutate_inputs():
    defaults = {"max_completion_tokens": 9000, "enable_thinking": True}
    request_values = {"max_tokens": 1234, "thinking": {"type": "disabled"}}
    result, _ = resolve_parameters("qwen-chat-v1", "qwen-flash", defaults, request_values)
    assert result == {"max_tokens": 1234, "enable_thinking": False}
    result, _ = resolve_parameters(
        "qwen-chat-v1", "qwen-flash", defaults, request_values, {"enable_thinking": True, "max_tokens": 300}
    )
    assert result == {"max_tokens": 300, "enable_thinking": True}
    assert defaults == {"max_completion_tokens": 9000, "enable_thinking": True}
    assert request_values == {"max_tokens": 1234, "thinking": {"type": "disabled"}}


def test_deepseek_explicit_effort_overrides_default_disabled():
    values, _ = resolve_parameters("deepseek-chat-v1", "deepseek-v4-pro", request={"reasoning_effort": "high"})
    assert values == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    with pytest.raises(ApiError) as error:
        resolve_parameters("deepseek-chat-v1", "deepseek-v4-pro", request={"reasoning_effort": "medium"})
    assert error.value.code == "PRESET_PARAMETER_UNSUPPORTED"


@pytest.mark.parametrize(
    "values",
    [
        {"enable_thinking": "false"},
        {"enable_thinking": False, "thinking": {"type": "enabled"}},
        {"enable_thinking": False, "reasoning_effort": "high"},
        {"max_tokens": 1, "max_completion_tokens": 2},
    ],
)
def test_invalid_aliases_and_native_types(values):
    with pytest.raises(ValueError):
        ModelParameters.model_validate(values)


@pytest.mark.parametrize(
    "preset,model,values,code",
    [
        ("typo", "gpt-4.1", {}, "UNKNOWN_MODEL_PRESET"),
        ("openai-gpt41-v1", "gpt-5.4", {}, "PRESET_MODEL_MISMATCH"),
        ("openai-gpt41-v1", "gpt-4.1", {"thinking": {"type": "enabled"}}, "PRESET_PARAMETER_UNSUPPORTED"),
        ("openai-gpt41-v1", "gpt-4.1", {"max_tokens": 32769}, "PRESET_OUTPUT_LIMIT"),
        ("qwen-chat-v1", "qwen-plus", {"temperature": 2}, "PRESET_PARAMETER_UNSUPPORTED"),
        (
            "qwen-chat-v1",
            "qwen-plus",
            {"enable_thinking": True, "max_completion_tokens": 300},
            "PRESET_PARAMETER_UNSUPPORTED",
        ),
        ("qwen-chat-v1", "qwen-plus", {"reasoning_effort": "high"}, "PRESET_PARAMETER_UNSUPPORTED"),
        ("qwen-chat-v1", "qwen-plus", {"frequency_penalty": 1}, "PRESET_PARAMETER_UNSUPPORTED"),
    ],
)
def test_profile_constraints(preset, model, values, code):
    with pytest.raises(ApiError) as error:
        resolve_parameters(preset, model, request=values)
    assert error.value.code == code


def test_custom_is_not_guessed_from_model_name():
    values = {"thinking": {"type": "disabled"}, "max_tokens": 101}
    assert resolve_parameters("custom", "gpt-4.1", request=values) == (values, [])
    validate_request_capabilities("custom", "deepseek-flash", {"response_format": {"type": "json_schema"}})


def test_openai_off_parameters_are_noops_not_thinking_claims():
    values, changes = resolve_parameters(
        "openai-gpt41-v1", "gpt-4.1-mini", request={"thinking": {"type": "disabled"}, "reasoning_effort": "none"}
    )
    assert values == {}
    assert len(changes) == 2


@pytest.mark.parametrize(
    "body,code",
    [
        ({"enable_thinking": True}, "PRESET_THINKING_INCOMPATIBLE"),
        (
            {"enable_thinking": True, "stream": True, "response_format": {"type": "json_object"}},
            "PRESET_THINKING_INCOMPATIBLE",
        ),
        ({"response_format": {"type": "json_schema"}}, "UPSTREAM_SCHEMA_UNSUPPORTED"),
        ({"response_format": "json_object"}, "INVALID_RESPONSE_FORMAT"),
        ({"response_format": {"type": []}}, "INVALID_RESPONSE_FORMAT"),
        ({"response_format": []}, "INVALID_RESPONSE_FORMAT"),
        ({"response_format": {}}, "INVALID_RESPONSE_FORMAT"),
    ],
)
def test_qwen_request_constraints(body, code):
    with pytest.raises(ApiError) as error:
        validate_request_capabilities("qwen-chat-v1", "qwen-plus", body)
    assert error.value.code == code


def save_preset(setup, preset="deepseek-chat-v1", model="deepseek-flash", **extra):
    client, headers, pid, rid, _ = setup
    return client.put(
        f"/api/admin/v1/routes/{rid}",
        headers=headers,
        json={
            "alias": "public-model",
            "provider_id": pid,
            "upstream_model": model,
            "preset_id": preset,
            **extra,
        },
    )


def test_catalog_and_preview_require_admin(env):
    assert env.get("/api/admin/v1/model-presets").status_code == 401
    assert env.post("/api/admin/v1/model-presets/preview", json={}).status_code == 401


def test_catalog_is_detached():
    data = catalog()
    data[0]["models"].append("invented")
    assert "invented" not in catalog()[0]["models"]


def test_authenticated_capabilities_are_secret_free_and_revisioned(setup):
    client, _, _, _, issued = setup
    assert save_preset(setup, "openai-gpt41-v1", "gpt-4.1").status_code == 200
    response = client.get("/v1/models", headers={"Authorization": "Bearer " + issued["api_key"]})
    assert response.status_code == 200
    model = response.json()["data"][0]
    assert model["id"] == "public-model"
    caps = model["capabilities"]
    assert caps["parameterAdapter"] == "gateway"
    assert caps["structuredOutput"] == "json_schema"
    assert caps["tokenField"] == "max_completion_tokens"
    assert caps["maxOutputTokens"] == 16384
    for private in ("gpt-4.1", "one.example", "synthetic-upstream-private", issued["api_key"]):
        assert private not in response.text
    assert save_preset(setup).status_code == 200
    changed = client.get("/v1/models", headers={"Authorization": "Bearer " + issued["api_key"]}).json()["data"][0]["capabilities"]
    assert changed["structuredOutput"] == "json_object"
    assert changed["version"] != caps["version"]


def test_capability_revision_tracks_endpoint_and_invalid_legacy_settings_are_isolated():
    route = SimpleNamespace(preset_id="deepseek-chat-v1", upstream_model="deepseek-flash", parameter_overrides={})
    provider = SimpleNamespace(base_url="https://first.invalid", parameter_defaults={})
    first = route_capabilities(route, provider)
    assert first["parameterRules"] == {
        "reasoningEfforts": ["low", "high"], "thinkingEnabled": True, "frequencyPenalty": True,
    }
    provider.base_url = "https://second.invalid"
    assert route_capabilities(route, provider)["version"] != first["version"]
    provider.parameter_defaults = {"reasoning_effort": "medium"}
    assert route_capabilities(route, provider) is None


def test_call_detail_records_requested_and_effective_parameters(setup, monkeypatch):
    from app.config import settings

    client, _, _, _, issued = setup
    monkeypatch.setattr(settings(), "model_call_capture_enabled", True)
    assert save_preset(setup, "openai-gpt41-v1", "gpt-4.1").status_code == 200
    mock(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}))
    response = request(client, issued, max_tokens=777, response_format={"type": "json_object"})
    assert response.status_code == 200
    detail = client.get(f"/api/admin/v1/calls/{response.headers['x-request-id']}").json()["detail"]
    assert detail["requested_parameters"]["max_tokens"] == 777
    assert detail["effective_parameters"]["max_completion_tokens"] == 777
    assert "max_tokens" not in detail["effective_parameters"]
    assert detail["effective_output_mode"] == "json_object"
    assert detail["parameter_changes"]


def test_preview_save_and_legacy_edit_preserve_preset(setup):
    client, headers, pid, rid, _ = setup
    mock(lambda r: pytest.fail("Preview/configuration must not call upstream"))
    assert len(client.get("/api/admin/v1/model-presets").json()) == 3
    assert save_preset(setup).status_code == 200
    response = client.post(
        "/api/admin/v1/model-presets/preview",
        headers=headers,
        json={
            "preset_id": "qwen-chat-v1",
            "upstream_model": "qwen-plus",
            "provider_id": pid,
            "parameter_overrides": {"max_completion_tokens": 777},
        },
    )
    assert response.status_code == 200
    assert response.json()["parameters"] == {"enable_thinking": False, "max_tokens": 777}
    assert client.get("/api/admin/v1/calls").json() == []
    response = client.put(
        f"/api/admin/v1/routes/{rid}",
        headers=headers,
        json={
            "alias": "public-model",
            "provider_id": pid,
            "upstream_model": "deepseek-v4-pro",
        },
    )
    assert response.status_code == 200
    assert client.get("/api/admin/v1/routes").json()[0]["preset_id"] == "deepseek-chat-v1"
    assert save_preset(setup, model="invented").status_code == 422
    assert save_preset(setup, preset="custom", model="invented").status_code == 200


def test_known_schema_capability_rejects_locally_then_core_can_fallback(setup):
    client, _, _, _, issued = setup
    assert save_preset(setup).status_code == 200
    captured = []

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"steps": [1]}'}}]})

    mock(handler)
    result = request(client, issued, response_format={"type": "json_schema"})
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "UPSTREAM_SCHEMA_UNSUPPORTED"
    assert result.headers["x-request-id"]
    assert not captured
    calls = client.get("/api/admin/v1/calls").json()
    assert calls[0]["error_code"] == "UPSTREAM_SCHEMA_UNSUPPORTED"
    assert request(client, issued, response_format={"type": "json_object"}, max_tokens=321).status_code == 200
    assert captured[0]["max_tokens"] == 321
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert captured[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "preset,model,expected",
    [
        ("qwen-chat-v1", "qwen-plus", {"max_tokens": 123, "enable_thinking": False}),
        ("openai-gpt41-v1", "gpt-4.1-mini", {"max_completion_tokens": 123}),
    ],
)
def test_gateway_applies_native_parameters(setup, preset, model, expected):
    client, _, _, _, issued = setup
    assert save_preset(setup, preset, model).status_code == 200
    captured = []

    def handler(r):
        captured.append(json.loads(r.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    mock(handler)
    assert request(client, issued, max_tokens=123).status_code == 200
    assert {k: v for k, v in captured[0].items() if k not in ("model", "messages")} == expected


def test_openai_schema_is_unchanged(setup):
    client, _, _, _, issued = setup
    assert save_preset(setup, "openai-gpt41-v1", "gpt-4.1").status_code == 200
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "plan",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
                "required": ["steps"],
                "additionalProperties": False,
            },
        },
    }

    def handler(r):
        assert json.loads(r.content)["response_format"] == schema
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"steps": ["clone"]}'}}]})

    mock(handler)
    assert request(client, issued, response_format=schema).status_code == 200


def test_provider_update_cannot_break_preset_routes(setup):
    client, headers, pid, _, _ = setup
    assert save_preset(setup, "openai-gpt41-v1", "gpt-4.1").status_code == 200
    result = client.put(
        f"/api/admin/v1/providers/{pid}",
        headers=headers,
        json={
            "name": "first",
            "base_url": "https://one.example/v1",
            "parameter_defaults": {"thinking": {"type": "enabled"}},
        },
    )
    assert result.status_code == 422
    assert client.get("/api/admin/v1/providers").json()[0]["parameter_defaults"] == {}


def test_official_schema_preflight_has_no_charge_or_unresolved_usage(setup):
    from sqlalchemy import select
    from test_accounts import configure, signup

    from app.db import get_db
    from app.main import app
    from app.user_models import ModelReservation

    client, headers, _, _, _ = setup
    assert save_preset(setup).status_code == 200
    configure(client, headers, tokens=1000, models=["public-model"])
    _, user_headers = signup(client)
    mock(lambda r: pytest.fail("Capability rejection must not dispatch"))
    response = client.post(
        "/v1/chat/completions",
        headers={
            **user_headers,
            "Idempotency-Key": "preset-preflight-0001",
        },
        json={
            "model": "public-model",
            "messages": [{"role": "user", "content": "JSON plan"}],
            "response_format": {"type": "json_schema"},
        },
    )
    assert response.status_code == 400
    balance = client.get("/api/core/v1/me", headers=user_headers).json()["balance"]
    assert (balance["available"], balance["reserved"]) == (1000, 0)
    for db in app.dependency_overrides[get_db]():
        reservation = db.scalar(select(ModelReservation))
        assert reservation.state == "released"
        assert reservation.actual == 0


def test_official_openai_default_budget_uses_native_field(setup):
    from test_accounts import configure, signup

    client, headers, _, _, _ = setup
    assert save_preset(setup, "openai-gpt41-v1", "gpt-4.1").status_code == 200
    configure(client, headers, tokens=1000, models=["public-model"])
    _, user_headers = signup(client)

    def handler(r):
        body = json.loads(r.content)
        assert "max_tokens" not in body
        assert body["max_completion_tokens"] == 16384
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    mock(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={
            **user_headers,
            "Idempotency-Key": "preset-openai-default-01",
        },
        json={"model": "public-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("parameters", [
    {"reasoning_effort": "high", "temperature": 0.5},
    {"reasoning_effort": "high", "frequency_penalty": 0},
    {"reasoning_effort": "high", "presence_penalty": 1},
    {"reasoning_effort": "high", "top_p": 0.94},
    {"top_p": 0.99},
])
def test_deepseek_final_mode_rejects_explicit_ignored_or_clamped_parameters(parameters):
    with pytest.raises(ApiError) as error:
        resolve_parameters("deepseek-chat-v1", "deepseek-flash", request=parameters)
    assert error.value.code == "PRESET_PARAMETER_UNSUPPORTED"


def test_deepseek_final_mode_omits_invalid_defaults_and_preserves_supported_probability():
    values, changes = resolve_parameters("deepseek-chat-v1", "deepseek-flash",
        defaults={"temperature": 0.3, "frequency_penalty": 1, "top_p": 0.5}, request={"reasoning_effort": "high"})
    assert values == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert len(changes) == 3
    for top_p in (0.95, 1):
        values, changes = resolve_parameters("deepseek-chat-v1", "deepseek-flash",
            request={"reasoning_effort": "high", "top_p": top_p})
        assert values["top_p"] == top_p and not changes
    values, changes = resolve_parameters("deepseek-chat-v1", "deepseek-flash",
        defaults={"top_p": 0.99}, request={"thinking": {"type": "disabled"}, "temperature": 0})
    assert "top_p" not in values and values["temperature"] == 0 and len(changes) == 1


def test_deepseek_gateway_enforces_actual_request_mode_before_dispatch(setup):
    client, staff, provider, route, issued = setup
    assert client.put(f"/api/admin/v1/routes/{route}", headers=staff, json={
        "alias": "public-model", "provider_id": provider, "upstream_model": "deepseek-flash",
        "preset_id": "deepseek-chat-v1"}).status_code == 200
    sent = []
    mock(lambda req: (sent.append(json.loads(req.content)), httpx.Response(200, json={
        "choices": [{"message": {"content": "{}"}}]}))[1])
    for parameters in ({"temperature": 0.3}, {"top_p": 0.5}, {"frequency_penalty": 1}):
        response = request(client, issued, reasoning_effort="high", **parameters)
        assert response.status_code == 422 and response.json()["error"]["code"] == "PRESET_PARAMETER_UNSUPPORTED"
    assert not sent
    assert request(client, issued, reasoning_effort="high", top_p=0.95).status_code == 200
    assert len(sent) == 1 and sent[0]["thinking"] == {"type": "enabled"} and sent[0]["top_p"] == 0.95


def test_deepseek_v2_advertises_max_without_changing_legacy_or_mapping_effort():
    from app.model_presets import route_capabilities_v2
    route = SimpleNamespace(preset_id="deepseek-chat-v1", upstream_model="deepseek-flash",
                            parameter_overrides={}, api_protocol="chat_completions")
    provider = SimpleNamespace(parameter_defaults={}, base_url="https://api.deepseek.com")
    assert route_capabilities(route, provider)["parameterRules"]["reasoningEfforts"] == ["low", "high"]
    caps = route_capabilities_v2(route, provider)
    assert caps["parameterRules"]["reasoningEfforts"] == ["low", "high", "max"]
    assert caps["parameterRules"]["topP"] == "conditional"
    values, changes = resolve_parameters("deepseek-chat-v1", "deepseek-flash", request={"reasoning_effort": "max"})
    assert values == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    assert not changes
